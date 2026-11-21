import argparse
import os
import sys

import bpy
import mathutils

TOLERANCE = 0.2  # Clearance tolerance applied to both diameter and depth (in mm)
EXTRA_CUT = 0.5  # Overcut offset to cleanly puncture the outer bottom face
DEFAULT_MAGNET_DIAMETER = 5.0  # mm
DEFAULT_MAGNET_HEIGHT = 2.0  # mm
# Distance from each model edge to the center of a corner hole, as a multiple of the magnet diameter
CORNER_EDGE_DISTANCE_FACTOR = 1.0
# How far inside each bounding-box corner the rectangularity probe is cast: small
# corner rounding is tolerated, as the corner holes still fit inside it
CORNER_PROBE_INSET = 0.5  # mm


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_stl(filepath):
    # Import the STL file (compatible with Blender 4+ and earlier versions)
    try:
        bpy.ops.wm.stl_import(filepath=filepath)
    except AttributeError:
        bpy.ops.import_mesh.stl(filepath=filepath)


def export_stl(filepath):
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    try:
        bpy.ops.wm.stl_export(filepath=filepath)
    except AttributeError:
        bpy.ops.export_mesh.stl(filepath=filepath)


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


def cut_hole(base, location, rotation, radius, depth):
    bpy.ops.mesh.primitive_cylinder_add(
        radius=radius,
        depth=depth,
        vertices=64,
        location=location,
        rotation=rotation,
    )
    cutter = bpy.context.active_object

    # Boolean difference
    mod_bool = base.modifiers.new(name="MagnetHole", type="BOOLEAN")
    mod_bool.operation = "DIFFERENCE"
    mod_bool.solver = "EXACT"
    mod_bool.object = cutter

    bpy.context.view_layer.objects.active = base
    bpy.ops.object.modifier_apply(modifier=mod_bool.name)

    # Remove cutting cylinder
    bpy.data.objects.remove(cutter, do_unlink=True)


def process_base(
    input_stl, output_stl, magnet_diameter, magnet_height, center=False, corners=False
):
    reset_scene()
    import_stl(input_stl)

    base = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = base

    # Calculate actual world-space bounding box
    bbox_corners = [
        base.matrix_world @ mathutils.Vector(corner) for corner in base.bound_box
    ]

    mins = [min(v[i] for v in bbox_corners) for i in range(3)]
    maxs = [max(v[i] for v in bbox_corners) for i in range(3)]
    centers = [(lo + hi) / 2.0 for lo, hi in zip(mins, maxs)]

    # Auto-detect the height axis (smallest thickness)
    height_idx = min(range(3), key=lambda i: maxs[i] - mins[i])
    plane_idx = [i for i in range(3) if i != height_idx]

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
        loc[plane_idx[0]] = a
        loc[plane_idx[1]] = b
        loc[height_idx] = height_pos
        return tuple(loc)

    positions = []
    if center:
        positions.append(location(centers[plane_idx[0]], centers[plane_idx[1]]))

    if corners:
        if is_rectangular(base, height_idx, mins, maxs):
            inset = magnet_diameter * CORNER_EDGE_DISTANCE_FACTOR
            for a in (mins[plane_idx[0]] + inset, maxs[plane_idx[0]] - inset):
                for b in (mins[plane_idx[1]] + inset, maxs[plane_idx[1]] - inset):
                    positions.append(location(a, b))
        else:
            print(
                "[WARNING] --corners ignored: the model is not rectangular",
                file=sys.stderr,
            )

    if not positions:
        print("[ERROR] No holes to carve: nothing was exported", file=sys.stderr)
        sys.exit(1)

    for loc in positions:
        cut_hole(base, loc, rot, hole_radius, cylinder_depth)

    export_stl(output_stl)

    print(
        f"[SUCCESS] Exported: {output_stl} "
        f"({len(positions)} hole(s): D={hole_diameter:.2f}mm, H={hole_depth:.2f}mm)"
    )


if __name__ == "__main__":
    # Get parameters passed after Blender's '--' separator
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []

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

    args = parser.parse_args(argv)

    if not (args.center or args.corners):
        parser.print_help()
        sys.exit(2)

    process_base(
        args.input,
        args.output,
        args.diameter,
        args.height,
        center=args.center,
        corners=args.corners,
    )
