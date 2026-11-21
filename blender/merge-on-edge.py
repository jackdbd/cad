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

# Blender does not add the directory of a script to the import path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import base_geometry as geometry  # noqa: E402
import cad_common as common  # noqa: E402

# The second model is pushed this far into the first one before the union. Walls that
# are coplanar only up to rounding errors make the union produce slivers, and an overlap
# also joins models that touch along an edge. It is below the resolution of a 3D printer.
OVERLAP = 0.01  # mm
# Vertices closer than this are welded in the merged model
WELD_DISTANCE = 0.001  # mm
# The bounding box of the merged model can differ this much from the one of the two models
BOUNDING_BOX_TOLERANCE = 0.1  # mm
# The volume of the merged model can differ this much from the sum of the volumes of the
# two models, as a fraction of that sum
VOLUME_TOLERANCE = 0.02

LOG_LEVEL_ENV_VAR = "MERGE_LOG_LEVEL"

logger = logging.getLogger(Path(__file__).stem)
timed = partial(common.timed, logger)


def fail(message, *args):
    """Log an error and exit."""
    logger.error(message, *args)
    sys.exit(1)


def overlap_transform(obj, edge, axis_idx):
    """Return the translation that pushes a model on the other side of edge into obj."""
    coords = geometry.vertex_coordinates(obj)
    center = mathutils.Vector((coords.min(axis=0) + coords.max(axis=0)) / 2)
    axis = mathutils.Vector([1.0 if i == axis_idx else 0.0 for i in range(3)])
    normal = axis.cross(edge.p1 - edge.p0).normalized()
    if normal.dot(center - (edge.p0 + edge.p1) / 2) < 0:
        normal = -normal
    return mathutils.Matrix.Translation(normal * OVERLAP)


def merge_models(obj_a, obj_b):
    """Merge obj_b into obj_a with a union (EXACT solver).

    The union takes care of walls that are back to back, with a different triangulation
    on each side, which would be left inside the model by a simple weld. Vertices closer
    than WELD_DISTANCE are then welded. Return the number of welded vertices.
    """
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = obj_a
    mod_bool = obj_a.modifiers.new(name="Merge", type="BOOLEAN")
    mod_bool.operation = "UNION"
    mod_bool.solver = "EXACT"
    mod_bool.object = obj_b
    bpy.ops.object.modifier_apply(modifier=mod_bool.name)
    bpy.data.objects.remove(obj_b, do_unlink=True)

    bm = bmesh.new()
    bm.from_mesh(obj_a.data)
    vertices_before = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=WELD_DISTANCE)
    welded = vertices_before - len(bm.verts)
    bm.to_mesh(obj_a.data)
    bm.free()
    return welded


def check_result(obj, expected_min, expected_max, expected_volume):
    """Look for signs of a broken merge in obj.

    A Boolean solver can fail without any error, for example by returning only one of
    the two models. A union does not change where the two models are, nor their total
    volume (except for the small overlap): the merged model must have the bounding box of
    the two models and the sum of their volumes. Return the problems found, the number of
    non-manifold edges and the volume of the merged model.
    """
    coords = geometry.vertex_coordinates(obj)
    merged_min, merged_max = coords.min(axis=0), coords.max(axis=0)
    non_manifold, volume = common.analyze_mesh(obj.data)

    problems = []
    if (
        np.abs(merged_min - expected_min).max() > BOUNDING_BOX_TOLERANCE
        or np.abs(merged_max - expected_max).max() > BOUNDING_BOX_TOLERANCE
    ):
        problems.append(
            "the bounding box of the merged model (%s to %s) is not the one of the two "
            "models (%s to %s): a model may be missing"
            % tuple(np.array2string(v, precision=1) for v in (merged_min, merged_max, expected_min, expected_max))
        )
    if abs(volume - expected_volume) > VOLUME_TOLERANCE * expected_volume:
        problems.append(
            f"the volume of the merged model is {volume:.1f} mm3, but the two models "
            f"together have {expected_volume:.1f} mm3 (+/-{VOLUME_TOLERANCE:.0%}): "
            "geometry may be missing"
        )
    return problems, non_manifold, volume


def process_models(input_a, input_b, output):
    common.reset_scene()
    with timed("STL import"):
        obj_a = geometry.import_model(input_a)
        obj_b = geometry.import_model(input_b)

    # Step 1: put the bottoms of the two models on the same plane
    height_idx = geometry.height_axis(obj_a)
    if geometry.height_axis(obj_b) != height_idx:
        fail(
            "The models are not oriented the same way: the thinnest dimension is %s "
            "in %s and %s in %s",
            "XYZ"[height_idx],
            input_a,
            "XYZ"[geometry.height_axis(obj_b)],
            input_b,
        )
    shift = geometry.bottom_height(obj_a, height_idx) - geometry.bottom_height(obj_b, height_idx)
    if abs(shift) > geometry.BOTTOM_PLANE_TOLERANCE:
        offset = mathutils.Vector([shift if i == height_idx else 0.0 for i in range(3)])
        obj_b.data.transform(mathutils.Matrix.Translation(offset))
    logger.info(
        "Bottoms put on the same plane (%s axis): second model moved by %.3f mm",
        "XYZ"[height_idx],
        shift,
    )

    # Step 2: find the straight edges of the bottom outlines
    straight_edges = []
    for filepath, obj in ((input_a, obj_a), (input_b, obj_b)):
        straight_edges.append(geometry.find_straight_edges(obj, height_idx))
        logger.info("%s: %d straight edge(s) on the bottom outline", filepath, len(straight_edges[-1]))
        if not straight_edges[-1]:
            fail(
                "%s has no straight edge on its bottom outline (a round or oval model?): "
                "there is nothing to merge on",
                filepath,
            )

    # Step 3: look for the first pair of edges that match
    centers = []
    for obj in (obj_a, obj_b):
        coords = geometry.vertex_coordinates(obj)
        centers.append(mathutils.Vector((coords.min(axis=0) + coords.max(axis=0)) / 2))
    match, rejected = geometry.match_edges(
        straight_edges[0], straight_edges[1], centers[0], centers[1], height_idx
    )
    if match is None:
        fail(
            "No edge of %s matches an edge of %s. Pairs of edges rejected because the "
            "lengths differ by more than %.1f mm: %d; because they are not parallel "
            "(within %.1f deg): %d; because the models would overlap: %d",
            input_a,
            input_b,
            geometry.COARSE_TOLERANCE,
            rejected["length"],
            math.degrees(geometry.MAX_ROTATION),
            rejected["direction"],
            rejected["side"],
        )
    edge_a, edge_b, flipped, rotation, length_difference = match
    logger.info(
        "Edges that match: %.3f mm long in %s, %.3f mm long in %s",
        (edge_a.p1 - edge_a.p0).length,
        input_a,
        (edge_b.p1 - edge_b.p0).length,
        input_b,
    )

    # Step 4: move the second model onto the first one, without deforming it
    transform, _ = geometry.alignment_transform(edge_a, edge_b, flipped, height_idx)
    transform = overlap_transform(obj_a, edge_a, height_idx) @ transform
    if length_difference <= geometry.PERFECT_TOLERANCE and abs(rotation) <= geometry.PERFECT_ANGLE:
        logger.info(
            "The edges match perfectly (length difference %.4f mm, rotation %.4f deg)",
            length_difference,
            math.degrees(rotation),
        )
    else:
        logger.warning(
            "The edges do not match perfectly: the lengths differ by %.3f mm and the "
            "second model is rotated by %.3f deg to fit the first one",
            length_difference,
            math.degrees(rotation),
        )
    obj_b.data.transform(transform)

    # Step 5: merge the two models
    before = [common.analyze_mesh(obj.data) for obj in (obj_a, obj_b)]
    non_manifold_before = sum(non_manifold for non_manifold, _ in before)
    volume_before = sum(volume for _, volume in before)
    coords = np.vstack([geometry.vertex_coordinates(obj_a), geometry.vertex_coordinates(obj_b)])
    expected_min, expected_max = coords.min(axis=0), coords.max(axis=0)
    with timed("Merge"):
        welded = merge_models(obj_a, obj_b)
    logger.info("%d vertex(es) welded after the union", welded)

    with timed("Result check"):
        problems, non_manifold_after, volume = check_result(
            obj_a, expected_min, expected_max, volume_before
        )
    for problem in problems:
        logger.error("Result check failed: %s", problem)
    if problems:
        fail("The merged model looks broken: it is not exported")
    logger.info(
        "Result check passed: the bounding box covers both models and the volume is "
        "%.1f mm3 (the two models have %.1f mm3)",
        volume,
        volume_before,
    )
    if non_manifold_after > non_manifold_before:
        logger.warning(
            "The merged model has %d more non-manifold edge(s) than the two models "
            "(%d before, %d after): they may touch only along an edge, or have walls "
            "that overlap",
            non_manifold_after - non_manifold_before,
            non_manifold_before,
            non_manifold_after,
        )

    with timed("STL export"):
        common.export_stl(output)
    logger.info("Exported %s", output)


if __name__ == "__main__":
    # Get parameters passed after Blender's '--' separator
    argv = common.script_arguments()

    parser = argparse.ArgumentParser(
        description="Merge two models of miniature bases on the edge that matches. "
        "The bottoms are put on the same plane and the second model is moved onto the first one."
    )
    parser.add_argument(
        "-a", "--in-a", required=True, help="Path to the first STL file (it does not move)"
    )
    parser.add_argument(
        "-b",
        "--in-b",
        required=True,
        help="Path to the second STL file (it is moved to fit the first one)",
    )
    parser.add_argument(
        "-o", "--out", required=True, help="Path to the merged STL file"
    )

    args = parser.parse_args(argv)

    common.setup_logging(logger, LOG_LEVEL_ENV_VAR)
    logger.info("Command line arguments: %s", shlex.join(argv))
    logger.info("Input file A: %s", args.in_a)
    logger.info("Input file B: %s", args.in_b)
    logger.info("Output file: %s", args.out)

    try:
        with timed("Total"):
            process_models(args.in_a, args.in_b, args.out)
    except Exception:
        # Blender only prints the traceback to stderr: keep it in the log file too
        logger.exception("Failed to merge %s and %s", args.in_a, args.in_b)
        raise
