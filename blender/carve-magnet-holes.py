import argparse
import logging
import math
import shlex
import sys
from functools import partial
from pathlib import Path

import bmesh
import bpy
import mathutils
import numpy as np
from mathutils.bvhtree import BVHTree

# Blender does not add the directory of a script to the import path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import base_geometry as geometry  # noqa: E402
import cad_common as common  # noqa: E402

TOLERANCE = 0.2  # Clearance tolerance applied to both diameter and depth (in mm)
EXTRA_CUT = 0.5  # Overcut offset to cleanly puncture the outer bottom face
DEFAULT_MAGNET_DIAMETER = 5.0  # mm
DEFAULT_MAGNET_HEIGHT = 2.0  # mm
SOLVERS = ("EXACT", "FAST")  # Boolean modifier solvers
DEFAULT_SOLVER = "EXACT"
# Blender renamed the FAST Boolean solver to FLOAT: try the names in order
BLENDER_SOLVER_NAMES = {"EXACT": ("EXACT",), "FAST": ("FAST", "FLOAT")}
# Distance from each model edge to the center of a corner hole, as a multiple of the magnet diameter
CORNER_EDGE_DISTANCE_FACTOR = 1.0
# How far inside each bounding-box corner the rectangularity probe is cast: small
# corner rounding is tolerated, as the corner holes still fit inside it
CORNER_PROBE_INSET = 0.5  # mm
# The flat bottom of a model is only the footprint of a base if it is at least this
# fraction of the model, along each axis of the base. If it is smaller, the model does not
# rest on a flat base (for example it is tilted) and the whole model is used instead.
MIN_FOOTPRINT_RATIO = 0.4
CUTTER_VERTICES = 64  # Vertices of each cylinder subtracted from the base
# Allowed difference between the removed and the expected volume, as a fraction of the
# expected one. Measured on real bases the difference is about 0.2%.
VOLUME_TOLERANCE = 0.02

LOG_LEVEL_ENV_VAR = "CARVE_LOG_LEVEL"

logger = logging.getLogger(Path(__file__).stem)
timed = partial(common.timed, logger)


def find_footprint(base, height_idx, mins, maxs):
    """Return the bounding box of the footprint of base: its minimum and its maximum.

    The footprint is the flat bottom of the model, not the whole model: a decoration that
    sticks out of the edge of the base makes the bounding box of the whole model larger
    than the base, and the corners of the base are not the corners of that box.
    """
    world = np.array(base.matrix_world)
    coords = geometry.vertex_coordinates(base) @ world[:3, :3].T + world[:3, 3]
    on_bottom = coords[
        np.abs(coords[:, height_idx] - mins[height_idx]) <= geometry.BOTTOM_PLANE_TOLERANCE
    ]
    foot_min, foot_max = on_bottom.min(axis=0), on_bottom.max(axis=0)

    plane_idx = [i for i in range(3) if i != height_idx]
    if any(
        foot_max[i] - foot_min[i] < MIN_FOOTPRINT_RATIO * (maxs[i] - mins[i])
        for i in plane_idx
    ):
        logger.warning(
            "The flat bottom of the model (%s mm) is much smaller than the model (%s mm): "
            "the model may not rest on a flat base, so the whole model is used",
            " x ".join("%.1f" % (foot_max[i] - foot_min[i]) for i in plane_idx),
            " x ".join("%.1f" % (maxs[i] - mins[i]) for i in plane_idx),
        )
        return np.array(mins), np.array(maxs)
    return foot_min, foot_max


def is_rectangular(base, height_idx, mins, maxs):
    """Return True if the footprint fills all 4 corners of its bounding box.

    A ray is cast along the height axis at each corner, slightly inset, so
    rounded, chamfered, cut or L-shaped footprints are reported as non-rectangular.
    """
    plane_idx = [i for i in range(3) if i != height_idx]
    inverse = base.matrix_world.inverted()
    direction = mathutils.Vector(
        [1.0 if i == height_idx else 0.0 for i in range(3)]
    )

    for a in (mins[plane_idx[0]] + CORNER_PROBE_INSET, maxs[plane_idx[0]] - CORNER_PROBE_INSET):
        for b in (mins[plane_idx[1]] + CORNER_PROBE_INSET, maxs[plane_idx[1]] - CORNER_PROBE_INSET):
            origin = mathutils.Vector([0.0, 0.0, 0.0])
            origin[plane_idx[0]] = a
            origin[plane_idx[1]] = b
            origin[height_idx] = mins[height_idx] - 1.0
            hit, *_ = base.ray_cast(inverse @ origin, inverse.to_3x3() @ direction)
            if not hit:
                return False
    return True


def count_self_intersections(mesh):
    """Return the number of pairs of triangles of a mesh that cross each other.

    Triangles that share a vertex are ignored: neighbors always touch each other.
    """
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    tree = BVHTree.FromBMesh(bm)
    bm.faces.ensure_lookup_table()
    count = 0
    for a, b in tree.overlap(tree):
        if a < b and not {v.index for v in bm.faces[a].verts} & {
            v.index for v in bm.faces[b].verts
        }:
            count += 1
    bm.free()
    return count


def check_result(
    base, solver, volume_before, non_manifold_before, hole_count, radius, depth
):
    """Look for signs of broken geometry in base after carving the holes.

    Boolean solvers do not report problems: on meshes that are not clean, they can
    silently produce broken or missing geometry. Log a warning for each sign found and
    return them, so the caller can decide what to do.
    """
    non_manifold, volume = common.analyze_mesh(base.data)
    removed = volume_before - volume
    cutter_area = (
        0.5 * CUTTER_VERTICES * radius**2 * math.sin(2 * math.pi / CUTTER_VERTICES)
    )
    expected = hole_count * cutter_area * depth

    problems = []
    if abs(removed - expected) > VOLUME_TOLERANCE * expected:
        problems.append(
            f"removed {removed:.1f} mm3 but about {expected:.1f} mm3 was expected "
            f"(+/-{VOLUME_TOLERANCE:.0%}): holes may be missing, partial or outside the model"
        )
    if non_manifold > non_manifold_before:
        problems.append(
            f"carving added {non_manifold - non_manifold_before} non-manifold edge(s) "
            f"({non_manifold_before} before, {non_manifold} after)"
        )

    for problem in problems:
        logger.warning("%s solver result check failed: %s", solver, problem)
    if not problems:
        logger.info(
            "%s solver result check passed: removed %.1f mm3 (expected %.1f), "
            "%d non-manifold edge(s)",
            solver,
            removed,
            expected,
            non_manifold,
        )
    return problems


def cut_holes(base, locations, rotation, radius, depth, solver=DEFAULT_SOLVER):
    """Subtract one cylinder per location from base, in a single Boolean pass.

    The EXACT solver re-processes the whole base mesh on every pass, so the cylinders
    are joined into one cutter object to pay that cost once instead of once per hole.
    """
    cutters = []
    for location in locations:
        bpy.ops.mesh.primitive_cylinder_add(
            radius=radius,
            depth=depth,
            vertices=CUTTER_VERTICES,
            location=location,
            rotation=rotation,
        )
        cutters.append(bpy.context.active_object)

    if len(cutters) > 1:
        bpy.ops.object.select_all(action="DESELECT")
        for obj in cutters:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = cutters[0]
        bpy.ops.object.join()
    cutter = cutters[0]

    # Boolean difference
    mod_bool = base.modifiers.new(name="MagnetHole", type="BOOLEAN")
    mod_bool.operation = "DIFFERENCE"
    available = {i.identifier for i in mod_bool.bl_rna.properties["solver"].enum_items}
    blender_solver = next((n for n in BLENDER_SOLVER_NAMES[solver] if n in available), None)
    if blender_solver is None:
        raise ValueError(
            f"This Blender has no {solver} Boolean solver (available: {sorted(available)})"
        )
    mod_bool.solver = blender_solver
    mod_bool.object = cutter

    bpy.context.view_layer.objects.active = base
    bpy.ops.object.modifier_apply(modifier=mod_bool.name)

    # Remove cutting cylinder
    bpy.data.objects.remove(cutter, do_unlink=True)


def process_base(
    input_stl,
    output_stl,
    magnet_diameter,
    magnet_height,
    center=False,
    corners=False,
    solver=DEFAULT_SOLVER,
    fallback=True,
):
    common.reset_scene()
    with timed("STL import"):
        common.import_stl(input_stl)

    base = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = base

    with timed("Input mesh analysis"):
        non_manifold_before, volume_before = common.analyze_mesh(base.data)
    logger.info(
        "Input mesh: %d triangle(s), %d non-manifold edge(s)",
        len(base.data.polygons),
        non_manifold_before,
    )
    if logger.isEnabledFor(logging.DEBUG):
        # Informational only, and slow (about 2 s on a detailed model): count them
        # only when the result will be logged
        with timed("Self-intersection count", logging.DEBUG):
            self_intersections = count_self_intersections(base.data)
        logger.debug(
            "Input mesh: %d self-intersecting triangle pair(s)", self_intersections
        )

    # Calculate actual world-space bounding box
    bbox_corners = [
        base.matrix_world @ mathutils.Vector(corner) for corner in base.bound_box
    ]

    mins = [min(v[i] for v in bbox_corners) for i in range(3)]
    maxs = [max(v[i] for v in bbox_corners) for i in range(3)]
    logger.debug(
        "Bounding box: min=%s max=%s",
        [round(v, 3) for v in mins],
        [round(v, 3) for v in maxs],
    )

    # Auto-detect the height axis (smallest thickness)
    height_idx = min(range(3), key=lambda i: maxs[i] - mins[i])
    plane_idx = [i for i in range(3) if i != height_idx]
    logger.debug("Detected height axis: %s", "XYZ"[height_idx])

    foot_min, foot_max = find_footprint(base, height_idx, mins, maxs)
    centers = (foot_min + foot_max) / 2.0
    logger.debug(
        "Footprint: min=%s max=%s",
        [round(float(v), 3) for v in foot_min],
        [round(float(v), 3) for v in foot_max],
    )

    # Calculate hole dimensions including tolerance
    hole_diameter = magnet_diameter + TOLERANCE
    hole_radius = hole_diameter / 2.0
    hole_depth = magnet_height + TOLERANCE
    cylinder_depth = hole_depth + EXTRA_CUT

    # The cylinder is created along Z: rotate it onto the detected height axis
    rotation = [(0.0, 1.570796, 0.0), (1.570796, 0.0, 0.0), (0.0, 0.0, 0.0)]
    # Hole position along the height axis (cutting in from the bottom face)
    height_pos = mins[height_idx] + (hole_depth / 2.0) - (EXTRA_CUT / 2.0)
    rot = rotation[height_idx]

    def location(a, b):
        loc = [0.0, 0.0, 0.0]
        loc[plane_idx[0]] = float(a)
        loc[plane_idx[1]] = float(b)
        loc[height_idx] = float(height_pos)
        return tuple(loc)

    positions = []
    if center:
        positions.append(location(centers[plane_idx[0]], centers[plane_idx[1]]))

    if corners:
        with timed("Rectangularity check"):
            rectangular = is_rectangular(base, height_idx, foot_min, foot_max)
        if rectangular:
            inset = magnet_diameter * CORNER_EDGE_DISTANCE_FACTOR
            for a in (foot_min[plane_idx[0]] + inset, foot_max[plane_idx[0]] - inset):
                for b in (foot_min[plane_idx[1]] + inset, foot_max[plane_idx[1]] - inset):
                    positions.append(location(a, b))
        else:
            logger.warning("--corners ignored: the model is not rectangular")

    if not positions:
        logger.error("No holes to carve: nothing was exported")
        sys.exit(1)

    for loc in positions:
        logger.debug("Carving hole at %s", tuple(round(v, 3) for v in loc))
    # Keep the original mesh, in case the FAST solver fails and we need to start over
    original_mesh = base.data.copy() if solver == "FAST" and fallback else None

    used_solver = solver
    with timed(f"Carving {len(positions)} hole(s)"):
        cut_holes(base, positions, rot, hole_radius, cylinder_depth, solver)
    with timed("Result check"):
        problems = check_result(
            base, solver, volume_before, non_manifold_before, len(positions), hole_radius, hole_depth
        )

    if problems and original_mesh is not None:
        logger.warning("The FAST solver result looks broken: falling back to the EXACT solver")
        base.data = original_mesh
        used_solver = "EXACT"
        with timed(f"Carving {len(positions)} hole(s) again"):
            cut_holes(base, positions, rot, hole_radius, cylinder_depth, used_solver)
        with timed("Result check"):
            check_result(
                base, used_solver, volume_before, non_manifold_before, len(positions), hole_radius, hole_depth
            )

    with timed("STL export"):
        common.export_stl(output_stl)

    logger.info(
        "Exported %s (%d hole(s): D=%.2fmm, H=%.2fmm, %s solver)",
        output_stl,
        len(positions),
        hole_diameter,
        hole_depth,
        used_solver,
    )


if __name__ == "__main__":
    # Get parameters passed after Blender's '--' separator
    argv = common.script_arguments()

    parser = argparse.ArgumentParser(
        description="Add cylindrical magnet sockets to miniature bases."
    )
    parser.add_argument(
        "-i", "--in", dest="input", required=True, help="Path to input STL file"
    )
    parser.add_argument(
        "-o", "--out", dest="output", required=True, help="Path to output STL file"
    )
    parser.add_argument(
        "-d",
        "--diameter",
        type=float,
        default=DEFAULT_MAGNET_DIAMETER,
        help=f"Magnet diameter in mm (default: {DEFAULT_MAGNET_DIAMETER})",
    )
    parser.add_argument(
        "-H",
        "--height",
        type=float,
        default=DEFAULT_MAGNET_HEIGHT,
        help=f"Magnet height in mm (default: {DEFAULT_MAGNET_HEIGHT})",
    )

    parser.add_argument(
        "--center",
        action="store_true",
        help="Carve a hole in the center of the model",
    )
    parser.add_argument(
        "--corners",
        action="store_true",
        help="Carve a hole in each of the 4 corners of a rectangular model "
        "(ignored with a warning if the model is not rectangular)",
    )
    parser.add_argument(
        "--solver",
        type=str.upper,
        choices=SOLVERS,
        default=DEFAULT_SOLVER,
        help="Use the FAST solver instead of the EXACT one. The FAST solver is much "
        "quicker, but it does not handle meshes that are not clean: non-manifold "
        "edges, self-intersections, coplanar faces.",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="With --solver FAST, do not fall back to the EXACT solver when the result "
        "looks broken (the warnings are still logged).",
    )

    args = parser.parse_args(argv)

    if not (args.center or args.corners):
        parser.print_help()
        sys.exit(2)

    common.setup_logging(logger, LOG_LEVEL_ENV_VAR)
    logger.info("Command line arguments: %s", shlex.join(argv))
    logger.info("Input file: %s", args.input)
    logger.info("Output file: %s", args.output)
    logger.info("Boolean solver: %s", args.solver)

    try:
        with timed("Total"):
            process_base(
                args.input,
                args.output,
                args.diameter,
                args.height,
                center=args.center,
                corners=args.corners,
                solver=args.solver,
                fallback=not args.no_fallback,
            )
    except Exception:
        # Blender only prints the traceback to stderr: keep it in the log file too
        logger.exception("Failed to process %s", args.input)
        raise
