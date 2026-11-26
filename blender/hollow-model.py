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


def cut_drain_holes(shell, positions, bottom_z, floor_z, hole_radius):
    """Cut a hole through the bottom wall of the shell, at each position, into the cavity."""
    low = bottom_z - HOLE_OVERSHOOT
    high = floor_z + HOLE_DEPTH_INTO_CAVITY
    cutters = []
    for x, y in positions:
        bpy.ops.mesh.primitive_cylinder_add(
            radius=hole_radius,
            depth=high - low,
            vertices=CUTTER_VERTICES,
            location=(x, y, (low + high) / 2),
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


def count_pieces(mesh):
    """Return the number of separate pieces of a mesh (sets of connected faces)."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    seen = set()
    pieces = 0
    for vert in bm.verts:
        if vert.index in seen:
            continue
        pieces += 1
        seen.add(vert.index)
        stack = [vert]
        while stack:
            current = stack.pop()
            for edge in current.link_edges:
                other = edge.other_vert(current)
                if other.index not in seen:
                    seen.add(other.index)
                    stack.append(other)
    bm.free()
    return pieces


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


def process_model(
    input_stl,
    output_stl,
    wall_thickness=DEFAULT_WALL_THICKNESS,
    voxel_size=DEFAULT_VOXEL_SIZE,
    drain_holes=True,
    hole_radius=DEFAULT_HOLE_RADIUS,
):
    if wall_thickness <= 0 or voxel_size <= 0 or hole_radius <= 0:
        fail("The wall thickness, the voxel size and the hole radius must be positive")
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
    outer_coords = geometry.vertex_coordinates(outer)
    bottom_z = float(outer_coords[:, 2].min())
    outer_min, outer_max = outer_coords.min(axis=0), outer_coords.max(axis=0)
    with timed("Input mesh analysis"):
        non_manifold_before, volume_before = common.analyze_mesh(outer.data)
        pieces = count_pieces(outer.data)
    logger.info(
        "Input mesh: %d triangle(s), %d non-manifold edge(s), %d piece(s), size %s mm, "
        "volume %.0f mm3",
        len(outer.data.polygons),
        non_manifold_before,
        pieces,
        " x ".join("%.1f" % v for v in outer_max - outer_min),
        volume_before,
    )
    if pieces > 1:
        logger.warning(
            "The model is made of %d separate pieces: if they overlap, the cavity cannot be "
            "computed reliably",
            pieces,
        )

    # The cavity: the surface of the model moved inwards by the thickness of the wall
    with timed("Cavity"):
        cavity = offset_surface(outer, voxel_size, -wall_thickness)
    if not cavity.data.polygons:
        fail(
            "The model has no cavity: it is nowhere more than %.2f mm from its surface",
            wall_thickness,
        )
    cavity_non_manifold, cavity_volume = common.analyze_mesh(cavity.data)
    cavity_coords = geometry.vertex_coordinates(cavity)
    floor_z = float(cavity_coords[:, 2].min())
    logger.info(
        "Cavity: %d triangle(s), %.0f mm3 (%.0f%% of the model)",
        len(cavity.data.polygons),
        cavity_volume,
        100 * cavity_volume / volume_before,
    )

    with timed("Wall thickness check"):
        thin_share = check_wall_thickness(mesh_tree(outer), cavity_coords, wall_thickness)
    if thin_share > MAX_THIN_SHARE:
        fail(
            "%.1f%% of the points of the cavity are less than %.2f mm from the outer surface "
            "(the limit is %.0f%%): the cavity breaks through the wall. The model is probably "
            "not a single closed solid (it has %d piece(s) and %d non-manifold edge(s)): "
            "merge its pieces first",
            100 * thin_share,
            MIN_WALL_RATIO * wall_thickness,
            100 * MAX_THIN_SHARE,
            pieces,
            non_manifold_before,
        )

    positions = []
    if drain_holes:
        cavity_tree = mesh_tree(cavity)
        positions = drain_hole_positions(
            cavity_coords, cavity_tree, floor_z, hole_radius, voxel_size
        )
        for number, (x, y) in enumerate(positions, start=1):
            if not point_in_mesh(cavity_tree, (x, y, floor_z + 1.0)):
                fail(
                    "Drain hole %d, at (%.1f, %.1f), does not reach the cavity: use "
                    "--no-drain-holes, or a smaller --hole-radius",
                    number,
                    x,
                    y,
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
            cut_drain_holes(shell, positions, bottom_z, floor_z, hole_radius)
        # Each hole removes a cylinder as long as the bottom wall is thick
        expected_volume -= len(positions) * math.pi * hole_radius**2 * (floor_z - bottom_z)

    with timed("Result check"):
        problems = check_result(
            shell,
            outer_min,
            outer_max,
            expected_volume,
            non_manifold_before + cavity_non_manifold,
            positions,
            bottom_z,
            floor_z,
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
    positions,
    bottom_z,
    floor_z,
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
    for number, (x, y) in enumerate(positions, start=1):
        # The middle of the bottom wall must be air: the hole goes through it
        if point_in_mesh(tree, (x, y, (bottom_z + floor_z) / 2)):
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
            )
    except Exception:
        # Blender only prints the traceback to stderr: keep it in the log file too
        logger.exception("Failed to hollow %s", args.input)
        raise
