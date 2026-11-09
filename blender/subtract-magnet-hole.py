import argparse
import os
import sys

import bpy
import mathutils

TOLERANCE = 0.2  # Clearance tolerance applied to both diameter and depth (in mm)
EXTRA_CUT = 0.5  # Overcut offset to cleanly puncture the outer bottom face
DEFAULT_MAGNET_DIAMETER = 5.0  # mm
DEFAULT_MAGNET_HEIGHT = 2.0  # mm


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def process_base(input_stl, output_stl, magnet_diameter, magnet_height):
    reset_scene()

    # Import the STL file (compatible with Blender 4+ and earlier versions)
    try:
        bpy.ops.wm.stl_import(filepath=input_stl)
    except AttributeError:
        bpy.ops.import_mesh.stl(filepath=input_stl)

    base = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = base

    # Calculate actual world-space bounding box
    bbox_corners = [
        base.matrix_world @ mathutils.Vector(corner) for corner in base.bound_box
    ]

    xs = [v.x for v in bbox_corners]
    ys = [v.y for v in bbox_corners]
    zs = [v.z for v in bbox_corners]

    dim_x = max(xs) - min(xs)
    dim_y = max(ys) - min(ys)
    dim_z = max(zs) - min(zs)

    center_x = (max(xs) + min(xs)) / 2.0
    center_y = (max(ys) + min(ys)) / 2.0
    center_z = (max(zs) + min(zs)) / 2.0

    # Auto-detect the height axis (smallest thickness)
    dimensions = {"X": dim_x, "Y": dim_y, "Z": dim_z}
    height_axis = min(dimensions, key=dimensions.get)

    # Calculate hole dimensions including tolerance
    hole_diameter = magnet_diameter + TOLERANCE
    hole_radius = hole_diameter / 2.0
    hole_depth = magnet_height + TOLERANCE
    cylinder_depth = hole_depth + EXTRA_CUT

    # Position and orient the cutting cylinder based on the detected height axis
    if height_axis == "Y":
        loc = (center_x, min(ys) + (hole_depth / 2.0) - (EXTRA_CUT / 2.0), center_z)
        rot = (1.570796, 0.0, 0.0)  # Rotate 90° along X to align with Y axis
    elif height_axis == "Z":
        loc = (center_x, center_y, min(zs) + (hole_depth / 2.0) - (EXTRA_CUT / 2.0))
        rot = (0.0, 0.0, 0.0)
    else:  # 'X'
        loc = (min(xs) + (hole_depth / 2.0) - (EXTRA_CUT / 2.0), center_y, center_z)
        rot = (0.0, 1.570796, 0.0)

    bpy.ops.mesh.primitive_cylinder_add(
        radius=hole_radius,
        depth=cylinder_depth,
        vertices=64,
        location=loc,
        rotation=rot,
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

    # Export result
    os.makedirs(os.path.dirname(os.path.abspath(output_stl)), exist_ok=True)
    try:
        bpy.ops.wm.stl_export(filepath=output_stl)
    except AttributeError:
        bpy.ops.export_mesh.stl(filepath=output_stl)

    print(
        f"[SUCCESS] Exported: {output_stl} (Hole: D={hole_diameter:.2f}mm, H={hole_depth:.2f}mm)"
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

    args = parser.parse_args(argv)
    process_base(args.input, args.output, args.diameter, args.height)
