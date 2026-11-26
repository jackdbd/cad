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

DEFAULT_WALL_THICKNESS = 2.2  # mm (2.0 to 2.5 mm is recommended)
# Size of the voxels used to compute the cavity: the smaller, the more precise (and slower)
DEFAULT_VOXEL_SIZE = 0.4  # mm
DEFAULT_HOLE_RADIUS = 2.5  # mm (a radius of 2.5 mm is a diameter of 5 mm)
# Size of the voxels used to merge the pieces of a model (--merge-pieces). The merged surface
# is on the original one, within one voxel.
DEFAULT_MERGE_VOXEL_SIZE = 0.2  # mm
# A piece of the merged model, or a pocket of the cavity, that is smaller than this number of
# voxels in every direction is a speck left by a voxel grid: it is removed
SPECK_SIZE_VOXELS = 5
# Width of the band of voxels around the surface, in the grid used to compute the cavity.
# A larger band gives the same cavity, but takes longer.
SDF_BAND_WIDTH = 3  # voxels
# The two drain holes are on each side of the center of the floor of the cavity, with their
# centers at this multiple of the radius from it
HOLE_SPACING_FACTOR = 2.2
# The holes start this far below the bottom of the model, and end this far above the floor
# of the cavity
HOLE_OVERSHOOT = 1.0  # mm
HOLE_DEPTH_INTO_CAVITY = 2.0  # mm
CUTTER_VERTICES = 64  # Vertices of each cylinder subtracted from the model
# A wall thinner than this fraction of the wanted thickness is reported as a warning. The
# voxels cannot resolve the narrowest crevices of a model, so a few spots are thinner than
# wanted.
MIN_WALL_RATIO = 0.5
# If more than this share of the points of the cavity are thinner than that, the cavity
# breaks through the wall: it is an error, not a warning. It happens with a model that is not
# a single closed solid (several overlapping pieces, for example).
MAX_THIN_SHARE = 0.02
# Allowed difference between the volume of the hollowed model and the expected one, as a
# fraction of the expected one
VOLUME_TOLERANCE = 0.01
# Allowed difference between the bounding box of the hollowed model and the original one
BOUNDING_BOX_TOLERANCE = 0.01  # mm

LOG_LEVEL_ENV_VAR = "HOLLOW_LOG_LEVEL"

logger = logging.getLogger(Path(__file__).stem)
timed = partial(common.timed, logger)


def fail(message, *args):
    """Log an error and exit."""
    logger.error(message, *args)
    sys.exit(1)


def default_output_path(input_path):
    """Return the path of the output file: the same one in `output`, with a suffix.

    The first directory named `input` becomes `output`, and `-hollowed` is added to the
    name of the file: assets/input/a/b.stl becomes assets/output/a/b-hollowed.stl.
    """
    parts = list(Path(input_path).parts)
    if "input" not in parts:
        fail("There is no 'input' directory in %s: pass the output file with --out", input_path)
    parts[parts.index("input")] = "output"
    output = Path(*parts)
    return output.with_name(f"{output.stem}-hollowed{output.suffix}")


def offset_surface(obj, voxel_size, distance):
    """Return a copy of obj, with its surface moved by distance (negative: inwards).

    The surface is moved in a grid of signed distances (SDF), so that the parts of the model
    that are thinner than twice the distance disappear instead of turning inside out.
    """
    tree = bpy.data.node_groups.new("offset", "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    nodes = tree.nodes
    group_input = nodes.new("NodeGroupInput")
    group_output = nodes.new("NodeGroupOutput")
    to_sdf = nodes.new("GeometryNodeMeshToSDFGrid")
    offset = nodes.new("GeometryNodeSDFGridOffset")
    to_mesh = nodes.new("GeometryNodeGridToMesh")
    to_sdf.inputs["Voxel Size"].default_value = voxel_size
    to_sdf.inputs["Band Width"].default_value = SDF_BAND_WIDTH
    offset.inputs["Distance"].default_value = distance
    to_mesh.inputs["Threshold"].default_value = 0.0
    to_mesh.inputs["Adaptivity"].default_value = 0.0
    links = tree.links
    links.new(group_input.outputs[0], to_sdf.inputs["Mesh"])
    links.new(to_sdf.outputs[0], offset.inputs["Grid"])
    links.new(offset.outputs[0], to_mesh.inputs["Grid"])
    links.new(to_mesh.outputs[0], group_output.inputs[0])

    copy = obj.copy()
    copy.data = obj.data.copy()
    bpy.context.collection.objects.link(copy)
    bpy.context.view_layer.objects.active = copy
    modifier = copy.modifiers.new("offset", "NODES")
    modifier.node_group = tree
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.node_groups.remove(tree)
    return copy


def point_in_mesh(tree, point):
    """Return True if point is inside the closed mesh of tree.

    The point is inside if most of three rays, almost along the axes, cross an odd number
    of faces on their way out.
    """
    votes = 0
    for direction in (
        mathutils.Vector((1.0, 0.0137, 0.0071)),
        mathutils.Vector((0.0119, 1.0, 0.0053)),
        mathutils.Vector((0.0087, 0.0101, 1.0)),
    ):
        crossings, start = 0, mathutils.Vector(point)
        while True:
            hit = tree.ray_cast(start, direction)
            if hit[0] is None:
                break
            crossings += 1
            start = hit[0] + direction.normalized() * 1e-4
        votes += crossings % 2
    return votes >= 2


def mesh_tree(obj):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    tree = BVHTree.FromBMesh(bm)
    bm.free()
    return tree


def drain_hole_positions(cavity_coords, cavity_tree, floor_z, hole_radius, voxel_size):
    """Return the (x, y) of the two drain holes.

    The floor is the lowest part of the cavity: the holes are on each side of its center,
    along the X axis. If the cavity is not above one of those places (the floor is a ring,
    for example), the hole is moved to the closest point of the floor.
    """
    floor = cavity_coords[cavity_coords[:, 2] <= floor_z + voxel_size]
    center = (floor.min(axis=0) + floor.max(axis=0)) / 2
    spacing = HOLE_SPACING_FACTOR * hole_radius
    positions = []
    for side in (-1, 1):
        x, y = float(center[0] + side * spacing), float(center[1])
        if not point_in_mesh(cavity_tree, (x, y, floor_z + 1.0)):
            closest = floor[np.argmin(np.hypot(floor[:, 0] - x, floor[:, 1] - y))]
            x, y = float(closest[0]), float(closest[1])
        positions.append((x, y))
    return positions


def plan_drain_holes(cavity, outer_tree, hole_radius, voxel_size):
    """Return the drain holes: two for each pocket of the cavity.

    A hole is a dict: its position x and y, the height of the floor of the cavity above it, and
    the height of the outer surface under it (the bottom of the wall that the hole goes through).
    """
    cavity_tree = mesh_tree(cavity)
    coords = geometry.vertex_coordinates(cavity)
    holes = []
    for pocket in piece_vertices(cavity.data):
        pocket_coords = coords[pocket]
        floor_z = float(pocket_coords[:, 2].min())
        for x, y in drain_hole_positions(
            pocket_coords, cavity_tree, floor_z, hole_radius, voxel_size
        ):
            number = len(holes) + 1
            if not point_in_mesh(cavity_tree, (x, y, floor_z + 1.0)):
                fail(
                    "Drain hole %d, at (%.1f, %.1f), does not reach the cavity: use "
                    "--no-drain-holes, or a smaller --hole-radius",
                    number,
                    x,
                    y,
                )
            below = mathutils.Vector((x, y, floor_z + 0.5))
            hit = outer_tree.ray_cast(below, mathutils.Vector((0.0, 0.0, -1.0)))
            if hit[0] is None:
                fail("Drain hole %d, at (%.1f, %.1f), has no wall under it", number, x, y)
            holes.append({"x": x, "y": y, "floor_z": floor_z, "wall_bottom": float(hit[0].z)})
    return holes


def cut_drain_holes(shell, holes, hole_radius):
    """Cut each hole through the bottom wall of the shell, into the cavity."""
    cutters = []
    for hole in holes:
        low = hole["wall_bottom"] - HOLE_OVERSHOOT
        high = hole["floor_z"] + HOLE_DEPTH_INTO_CAVITY
        bpy.ops.mesh.primitive_cylinder_add(
            radius=hole_radius,
            depth=high - low,
            vertices=CUTTER_VERTICES,
            location=(hole["x"], hole["y"], (low + high) / 2),
        )
        cutters.append(bpy.context.active_object)
    if len(cutters) > 1:
        bpy.ops.object.select_all(action="DESELECT")
        for cutter in cutters:
            cutter.select_set(True)
        bpy.context.view_layer.objects.active = cutters[0]
        bpy.ops.object.join()
    cutter = cutters[0]

    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = shell
    modifier = shell.modifiers.new(name="Drain holes", type="BOOLEAN")
    modifier.operation = "DIFFERENCE"
    modifier.solver = "EXACT"
    modifier.object = cutter
    bpy.ops.object.modifier_apply(modifier=modifier.name)
    bpy.data.objects.remove(cutter, do_unlink=True)


def piece_vertices(mesh):
    """Return the separate pieces of a mesh: for each one, the indices of its vertices."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    seen = set()
    pieces = []
    for vert in bm.verts:
        if vert.index in seen:
            continue
        seen.add(vert.index)
        stack = [vert]
        indices = []
        while stack:
            current = stack.pop()
            indices.append(current.index)
            for edge in current.link_edges:
                other = edge.other_vert(current)
                if other.index not in seen:
                    seen.add(other.index)
                    stack.append(other)
        pieces.append(np.array(indices))
    bm.free()
    return pieces


def delete_small_pieces(obj, is_small):
    """Delete the pieces of obj whose size (along X, Y and Z) satisfies is_small.

    Return the number of pieces deleted.
    """
    coords = geometry.vertex_coordinates(obj)
    small = [p for p in piece_vertices(obj.data) if is_small(np.ptp(coords[p], axis=0))]
    if small:
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(
            bm, geom=[bm.verts[i] for piece in small for i in piece], context="VERTS"
        )
        bm.to_mesh(obj.data)
        bm.free()
    return len(small)


def merge_pieces(obj, voxel_size):
    """Return a copy of obj where the pieces are merged into a single outer surface.

    A model made of overlapping pieces has surfaces inside the material: the cavity cannot be
    computed from it. A voxel remesh keeps only the outer surface of the union of the pieces.
    """
    merged = obj.copy()
    merged.data = obj.data.copy()
    bpy.context.collection.objects.link(merged)
    merged.data.remesh_voxel_size = voxel_size
    merged.data.remesh_voxel_adaptivity = 0.0
    bpy.context.view_layer.objects.active = merged
    bpy.ops.object.voxel_remesh()
    return merged


def log_distance_to_surface(tree, coords, label):
    """Log how far a sample of the points is from the surface of tree."""
    rng = np.random.default_rng(0)
    sample = coords[rng.choice(len(coords), min(len(coords), 50000), replace=False)]
    distances = np.array([tree.find_nearest(mathutils.Vector(p))[3] for p in sample])
    logger.info(
        "%s: median %.3f mm, 99th percentile %.3f mm, farthest %.3f mm",
        label,
        np.median(distances),
        np.percentile(distances, 99),
        distances.max(),
    )


def check_wall_thickness(outer_tree, cavity_coords, wall_thickness):
    """Log how thick the wall is: the distance of the cavity surface to the outer surface.

    Return the share of the points of the cavity that are thinner than MIN_WALL_RATIO times
    the wanted thickness.
    """
    distances = np.array(
        [outer_tree.find_nearest(mathutils.Vector(p))[3] for p in cavity_coords]
    )
    thin = distances < MIN_WALL_RATIO * wall_thickness
    logger.info(
        "Wall thickness (wanted %.2f mm): median %.2f mm, 1st percentile %.2f mm, thinnest %.2f mm",
        wall_thickness,
        np.median(distances),
        np.percentile(distances, 1),
        distances.min(),
    )
    if thin.any():
        spot = cavity_coords[int(np.argmin(distances))]
        logger.warning(
            "%d of %d points of the cavity (%.2f%%) are less than %.2f mm from the outer "
            "surface: the narrowest crevices of the model are thinner than a voxel. The "
            "thinnest wall is %.2f mm, near (%.1f, %.1f, %.1f)",
            thin.sum(),
            len(distances),
            100 * thin.mean(),
            MIN_WALL_RATIO * wall_thickness,
            distances.min(),
            *spot,
        )
    return float(thin.mean())


def describe_input(obj, label):
    """Log the size of obj. Return its non-manifold edges, its volume and its pieces."""
    with timed(f"{label} analysis"):
        non_manifold, volume = common.analyze_mesh(obj.data)
        pieces = len(piece_vertices(obj.data))
    coords = geometry.vertex_coordinates(obj)
    logger.info(
        "%s: %d triangle(s), %d non-manifold edge(s), %d piece(s), size %s mm, volume %.0f mm3",
        label,
        len(obj.data.polygons),
        non_manifold,
        pieces,
        " x ".join("%.1f" % v for v in np.ptp(coords, axis=0)),
        volume,
    )
    return non_manifold, volume, pieces


def process_model(
    input_stl,
    output_stl,
    wall_thickness=DEFAULT_WALL_THICKNESS,
    voxel_size=DEFAULT_VOXEL_SIZE,
    drain_holes=True,
    hole_radius=DEFAULT_HOLE_RADIUS,
    merge=False,
    merge_voxel_size=DEFAULT_MERGE_VOXEL_SIZE,
):
    if wall_thickness <= 0 or voxel_size <= 0 or hole_radius <= 0 or merge_voxel_size <= 0:
        fail("The wall thickness, the voxel sizes and the hole radius must be positive")
    if voxel_size > wall_thickness / 2:
        logger.warning(
            "The voxel size (%.2f mm) is more than half of the wall thickness (%.2f mm): "
            "the walls will not be precise",
            voxel_size,
            wall_thickness,
        )

    common.reset_scene()
    with timed("STL import"):
        outer = common.import_stl(input_stl)
    non_manifold_before, volume_before, pieces = describe_input(outer, "Input mesh")
    if pieces > 1 and not merge:
        logger.warning(
            "The model is made of %d separate pieces: if they overlap, the cavity cannot be "
            "computed reliably (see --merge-pieces)",
            pieces,
        )

    if merge:
        # Replace the model by its outer surface: a surface inside the material would end up
        # in the wall, or in the cavity
        original_tree = mesh_tree(outer)
        with timed("Merge of the pieces"):
            merged = merge_pieces(outer, merge_voxel_size)
            specks = delete_small_pieces(
                merged, lambda size: size.max() < SPECK_SIZE_VOXELS * merge_voxel_size
            )
        bpy.data.objects.remove(outer, do_unlink=True)
        outer = merged
        if specks:
            logger.info("Removed %d speck(s) left by the voxel grid", specks)
        log_distance_to_surface(
            original_tree,
            geometry.vertex_coordinates(outer),
            "Distance of the merged surface to the original one",
        )
        non_manifold_before, volume_before, pieces = describe_input(outer, "Merged mesh")
        if pieces > 1:
            logger.warning("The merged model still has %d separate pieces", pieces)

    outer_coords = geometry.vertex_coordinates(outer)
    outer_min, outer_max = outer_coords.min(axis=0), outer_coords.max(axis=0)
    outer_tree = mesh_tree(outer)

    # The cavity: the surface of the model moved inwards by the thickness of the wall
    with timed("Cavity"):
        cavity = offset_surface(outer, voxel_size, -wall_thickness)
        # A pocket that is too small for a drain hole is filled, and so is a speck
        specks = delete_small_pieces(
            cavity,
            lambda size: size.max() < SPECK_SIZE_VOXELS * voxel_size
            or (drain_holes and min(size[0], size[1]) < 2 * hole_radius),
        )
    if specks:
        logger.info("Filled %d small pocket(s) of the cavity", specks)
    if not cavity.data.polygons:
        fail(
            "The model has no cavity: it is nowhere more than %.2f mm from its surface",
            wall_thickness,
        )
    cavity_non_manifold, cavity_volume = common.analyze_mesh(cavity.data)
    cavity_coords = geometry.vertex_coordinates(cavity)
    logger.info(
        "Cavity: %d triangle(s), %.0f mm3 (%.0f%% of the model)",
        len(cavity.data.polygons),
        cavity_volume,
        100 * cavity_volume / volume_before,
    )

    with timed("Wall thickness check"):
        thin_share = check_wall_thickness(outer_tree, cavity_coords, wall_thickness)
    if thin_share > MAX_THIN_SHARE:
        fail(
            "%.1f%% of the points of the cavity are less than %.2f mm from the outer surface "
            "(the limit is %.0f%%): the cavity breaks through the wall. The model is probably "
            "not a single closed solid (it has %d piece(s) and %d non-manifold edge(s)): %s",
            100 * thin_share,
            MIN_WALL_RATIO * wall_thickness,
            100 * MAX_THIN_SHARE,
            pieces,
            non_manifold_before,
            "it cannot be hollowed" if merge else "merge its pieces first, with --merge-pieces",
        )

    holes = []
    if drain_holes:
        holes = plan_drain_holes(cavity, outer_tree, hole_radius, voxel_size)
        logger.info(
            "Drain holes: %d, in %d pocket(s) of the cavity",
            len(holes),
            len(holes) // 2,
        )

    # The shell: the model, and the cavity with its faces turned inside out
    bm = bmesh.new()
    bm.from_mesh(cavity.data)
    bmesh.ops.reverse_faces(bm, faces=bm.faces)
    bm.to_mesh(cavity.data)
    bm.free()
    bpy.ops.object.select_all(action="DESELECT")
    outer.select_set(True)
    cavity.select_set(True)
    bpy.context.view_layer.objects.active = outer
    bpy.ops.object.join()
    shell = outer

    expected_volume = volume_before - cavity_volume
    if drain_holes:
        with timed("Drain holes"):
            cut_drain_holes(shell, holes, hole_radius)
        # Each hole removes a cylinder as long as the bottom wall is thick
        expected_volume -= sum(
            math.pi * hole_radius**2 * (hole["floor_z"] - hole["wall_bottom"]) for hole in holes
        )

    with timed("Result check"):
        problems = check_result(
            shell,
            outer_min,
            outer_max,
            expected_volume,
            non_manifold_before + cavity_non_manifold,
            holes,
        )
    for problem in problems:
        logger.error("Result check failed: %s", problem)
    if problems:
        fail("The hollowed model looks broken: it is not exported")

    with timed("STL export"):
        common.export_stl(str(output_stl))
    logger.info("Exported %s", output_stl)


def check_result(
    shell,
    outer_min,
    outer_max,
    expected_volume,
    non_manifold_before,
    holes,
):
    """Look for signs of a broken result in the hollowed model: return the problems found."""
    coords = geometry.vertex_coordinates(shell)
    non_manifold, volume = common.analyze_mesh(shell.data)
    problems = []

    if (
        np.abs(coords.min(axis=0) - outer_min).max() > BOUNDING_BOX_TOLERANCE
        or np.abs(coords.max(axis=0) - outer_max).max() > BOUNDING_BOX_TOLERANCE
    ):
        problems.append("the bounding box of the hollowed model is not the one of the original")
    if abs(volume - expected_volume) > VOLUME_TOLERANCE * expected_volume:
        problems.append(
            f"the volume is {volume:.0f} mm3, but about {expected_volume:.0f} mm3 was "
            f"expected (+/-{VOLUME_TOLERANCE:.0%})"
        )
    tree = mesh_tree(shell)
    for number, hole in enumerate(holes, start=1):
        # The middle of the bottom wall must be air: the hole goes through it
        middle = (hole["wall_bottom"] + hole["floor_z"]) / 2
        if point_in_mesh(tree, (hole["x"], hole["y"], middle)):
            problems.append(f"drain hole {number} does not go through the bottom wall")

    logger.info(
        "Hollowed model: %d triangle(s), volume %.0f mm3 (expected %.0f mm3), %d non-manifold "
        "edge(s)",
        len(shell.data.polygons),
        volume,
        expected_volume,
        non_manifold,
    )
    if non_manifold > non_manifold_before:
        logger.warning(
            "The hollowed model has %d more non-manifold edge(s) than the original and the "
            "cavity (%d before, %d after)",
            non_manifold - non_manifold_before,
            non_manifold_before,
            non_manifold,
        )
    if not problems:
        logger.info("Result check passed")
    return problems


if __name__ == "__main__":
    # Get parameters passed after Blender's '--' separator
    argv = common.script_arguments()

    parser = argparse.ArgumentParser(
        description="Hollow a model: keep a wall of constant thickness around a cavity, and "
        "cut two drain holes in the bottom (the lowest part along the Z axis)."
    )
    parser.add_argument("-i", "--in", dest="input", required=True, help="Path to input STL file")
    parser.add_argument(
        "-o",
        "--out",
        help="Path to output STL file (default: the same path in the `output` directory, with "
        "-hollowed added to the name of the file)",
    )
    parser.add_argument(
        "-w",
        "--wall-thickness",
        type=float,
        default=DEFAULT_WALL_THICKNESS,
        help=f"Thickness of the wall in mm (default: {DEFAULT_WALL_THICKNESS})",
    )
    parser.add_argument(
        "-v",
        "--voxel-size",
        type=float,
        default=DEFAULT_VOXEL_SIZE,
        help="Size of the voxels used to compute the cavity in mm: the smaller, the more "
        f"precise, and the slower (default: {DEFAULT_VOXEL_SIZE})",
    )
    parser.add_argument(
        "--no-drain-holes",
        action="store_true",
        help="Do not cut the two drain holes in the bottom",
    )
    parser.add_argument(
        "--merge-pieces",
        action="store_true",
        help="Merge the pieces of the model into a single solid before hollowing it, for a "
        "model made of overlapping pieces (the outer surface is replaced by a voxel remesh "
        "of the union of the pieces)",
    )
    parser.add_argument(
        "--merge-voxel-size",
        type=float,
        default=DEFAULT_MERGE_VOXEL_SIZE,
        help="Size of the voxels used to merge the pieces in mm (default: "
        f"{DEFAULT_MERGE_VOXEL_SIZE})",
    )
    parser.add_argument(
        "-r",
        "--hole-radius",
        type=float,
        default=DEFAULT_HOLE_RADIUS,
        help=f"Radius of the drain holes in mm (default: {DEFAULT_HOLE_RADIUS})",
    )

    args = parser.parse_args(argv)

    common.setup_logging(logger, LOG_LEVEL_ENV_VAR)
    logger.info("Command line arguments: %s", shlex.join(argv))
    output = Path(args.out) if args.out else default_output_path(args.input)
    logger.info("Input file: %s", args.input)
    logger.info("Output file: %s", output)

    try:
        with timed("Total"):
            process_model(
                args.input,
                output,
                wall_thickness=args.wall_thickness,
                voxel_size=args.voxel_size,
                drain_holes=not args.no_drain_holes,
                hole_radius=args.hole_radius,
                merge=args.merge_pieces,
                merge_voxel_size=args.merge_voxel_size,
            )
    except Exception:
        # Blender only prints the traceback to stderr: keep it in the log file too
        logger.exception("Failed to hollow %s", args.input)
        raise
