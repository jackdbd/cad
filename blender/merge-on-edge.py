import argparse
import copy
import logging
import math
import os
import shlex
import sys
import time
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import bmesh
import bpy
import mathutils
import numpy as np

# Edges between faces that meet at a sharper angle than this are candidates to merge on
SHARP_ANGLE = math.radians(30)
# Neighboring edges within this angle of each other make up a single straight edge
STRAIGHT_ANGLE = math.radians(1)
# How far from the bottom plane an edge can be and still lie on it
BOTTOM_PLANE_TOLERANCE = 0.05  # mm
# Shortest straight edge to merge on, as a fraction of the longest dimension of its
# model. It rules out the many short edges that approximate a circle or an oval.
MIN_EDGE_RATIO = 0.2
# Edges whose ends are closer than this to each other match perfectly
PERFECT_TOLERANCE = 0.01  # mm
# Edges whose ends are farther than this from each other do not match at all
COARSE_TOLERANCE = 2.0  # mm
# The second model is pushed this far into the first one before the union. Walls that
# are coplanar only up to rounding errors make the union produce slivers, and an overlap
# also joins models that touch along an edge. It is below the resolution of a 3D printer.
OVERLAP = 0.01  # mm
# Vertices closer than this are welded in the merged model
WELD_DISTANCE = 0.001  # mm

LOG_LEVEL_ENV_VAR = "MERGE_LOG_LEVEL"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "INFO"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

logger = logging.getLogger(Path(__file__).stem)

# A straight edge of a model, from p0 to p1
Edge = namedtuple("Edge", ["p0", "p1"])


class ColorFormatter(logging.Formatter):
    """Formatter that colors the level name with ANSI escape codes."""

    RESET = "\033[0m"
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }

    def format(self, record):
        # Format a copy: the file handler shares the record and must stay uncolored
        record = copy.copy(record)
        color = self.COLORS.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname:<8}{self.RESET}"
        return super().format(record)


def setup_logging():
    """Log to stdout (colored on a terminal) and to a timestamped file in LOGS_DIR."""
    requested = os.environ.get(LOG_LEVEL_ENV_VAR, DEFAULT_LOG_LEVEL).strip().upper()
    invalid_level = requested not in LOG_LEVELS
    level = getattr(logging, DEFAULT_LOG_LEVEL if invalid_level else requested)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    log_path = LOGS_DIR / f"{Path(__file__).stem}_{timestamp}.log"

    plain_format = "%(asctime)s %(levelname)-8s %(message)s"
    file_formatter = logging.Formatter(plain_format, LOG_DATE_FORMAT)
    stdout_formatter = file_formatter
    if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
        stdout_formatter = ColorFormatter(
            "%(asctime)s %(levelname)s %(message)s", LOG_DATE_FORMAT
        )
    # Timestamps in UTC, like the log filename
    file_formatter.converter = stdout_formatter.converter = time.gmtime

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(stdout_formatter)
    # Append, so two runs starting within the same second never overwrite each other
    file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(file_formatter)

    logger.setLevel(level)
    logger.propagate = False
    logger.addHandler(stdout_handler)
    logger.addHandler(file_handler)

    if invalid_level:
        logger.warning(
            "Invalid %s=%r (expected one of %s): using %s",
            LOG_LEVEL_ENV_VAR,
            requested,
            ", ".join(LOG_LEVELS),
            DEFAULT_LOG_LEVEL,
        )


@contextmanager
def timed(label, level=logging.INFO):
    """Log how long the wrapped block took, even if it raises."""
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.log(level, "%s took %.2f ms", label, (time.perf_counter() - start) * 1000)


def fail(message, *args):
    """Log an error and exit."""
    logger.error(message, *args)
    sys.exit(1)


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_stl(filepath):
    """Import an STL file and return its object, with its transform baked into the mesh."""
    before = set(bpy.data.objects)
    # Import the STL file (compatible with Blender 4+ and earlier versions)
    try:
        bpy.ops.wm.stl_import(filepath=filepath)
    except AttributeError:
        bpy.ops.import_mesh.stl(filepath=filepath)
    (obj,) = set(bpy.data.objects) - before

    # From now on the local coordinates of the mesh are its world coordinates
    obj.data.transform(obj.matrix_world)
    obj.matrix_world = mathutils.Matrix.Identity(4)
    return obj


def export_stl(filepath):
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    try:
        bpy.ops.wm.stl_export(filepath=filepath)
    except AttributeError:
        bpy.ops.export_mesh.stl(filepath=filepath)


def vertex_coordinates(obj):
    coords = np.empty(len(obj.data.vertices) * 3)
    obj.data.vertices.foreach_get("co", coords)
    return coords.reshape(-1, 3)


def height_axis(obj):
    """Return the index of the axis along which the model is the thinnest."""
    coords = vertex_coordinates(obj)
    return int(np.argmin(coords.max(axis=0) - coords.min(axis=0)))


def bottom_height(obj, height_idx):
    return float(vertex_coordinates(obj)[:, height_idx].min())


def count_non_manifold_edges(mesh):
    bm = bmesh.new()
    bm.from_mesh(mesh)
    count = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()
    return count


def find_straight_edges(obj, height_idx):
    """Return the straight edges of the bottom outline of a model.

    These are the sharp edges that lie on the bottom plane, with the collinear ones
    that touch each other joined into a single edge. Short edges are left out: a
    round model has many of them and no straight edge to merge on.
    """
    coords = vertex_coordinates(obj)
    bottom = coords[:, height_idx].min()
    plane_idx = [i for i in range(3) if i != height_idx]
    min_length = MIN_EDGE_RATIO * max(np.ptp(coords[:, i]) for i in plane_idx)

    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    on_bottom = np.nonzero(np.abs(coords[:, height_idx] - bottom) <= BOTTOM_PLANE_TOLERANCE)[0]
    on_bottom = {bm.verts[i] for i in on_bottom}

    # Sharp edges on the bottom plane (an edge with a single face is the border of a hole)
    sharp = {}
    for vert in on_bottom:
        for edge in vert.link_edges:
            if edge.other_vert(vert) in on_bottom and edge.index not in sharp:
                if edge.calc_face_angle(math.pi) > SHARP_ANGLE:
                    sharp[edge.index] = edge
    edges = list(sharp.values())
    directions = [(e.verts[1].co - e.verts[0].co).normalized() for e in edges]

    # Join the edges that touch each other and have the same direction
    parent = list(range(len(edges)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    at_vertex = {}
    for i, edge in enumerate(edges):
        for vert in edge.verts:
            at_vertex.setdefault(vert.index, []).append(i)
    for indices in at_vertex.values():
        for i in indices:
            for j in indices:
                if abs(directions[i].dot(directions[j])) >= math.cos(STRAIGHT_ANGLE):
                    parent[find(i)] = find(j)

    groups = {}
    for i in range(len(edges)):
        groups.setdefault(find(i), []).append(i)

    straight_edges = []
    for indices in groups.values():
        direction = directions[indices[0]]
        origin = edges[indices[0]].verts[0].co
        verts = {v for i in indices for v in edges[i].verts}
        ends = sorted(verts, key=lambda v: (v.co - origin).dot(direction))
        edge = Edge(ends[0].co.copy(), ends[-1].co.copy())
        if (edge.p1 - edge.p0).length >= min_length:
            straight_edges.append(edge)
    bm.free()
    return straight_edges


def edge_error(a, b):
    """Return how far the ends of two edges are from each other, and whether b is flipped.

    The error is the largest distance between corresponding ends, pairing them in
    the way that gives the smallest one. It grows with an offset, a rotation or a
    difference in length of the two edges.
    """
    straight = max((a.p0 - b.p0).length, (a.p1 - b.p1).length)
    crossed = max((a.p0 - b.p1).length, (a.p1 - b.p0).length)
    return (crossed, True) if crossed < straight else (straight, False)


def alignment_transform(a, b, flipped, axis_idx):
    """Return the rotation (around the height axis) and translation that put edge b on a.

    The midpoints of the two edges end up on each other, with the same direction.
    Also return the angle of the rotation.
    """
    b0, b1 = (b.p1, b.p0) if flipped else (b.p0, b.p1)
    axis = mathutils.Vector([1.0 if i == axis_idx else 0.0 for i in range(3)])
    da, db = a.p1 - a.p0, b1 - b0
    angle = math.atan2(db.cross(da).dot(axis), db.dot(da))
    mid_a, mid_b = (a.p0 + a.p1) / 2, (b0 + b1) / 2
    transform = (
        mathutils.Matrix.Translation(mid_a)
        @ mathutils.Matrix.Rotation(angle, 4, axis)
        @ mathutils.Matrix.Translation(-mid_b)
    )
    return transform, angle


def overlap_transform(obj, edge, axis_idx):
    """Return the translation that pushes a model on the other side of edge into obj."""
    coords = vertex_coordinates(obj)
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


def process_models(input_a, input_b, output):
    reset_scene()
    with timed("STL import"):
        obj_a = import_stl(input_a)
        obj_b = import_stl(input_b)

    # Step 1: put the bottoms of the two models on the same plane
    height_idx = height_axis(obj_a)
    if height_axis(obj_b) != height_idx:
        fail(
            "The models are not oriented the same way: the thinnest dimension is %s "
            "in %s and %s in %s",
            "XYZ"[height_idx],
            input_a,
            "XYZ"[height_axis(obj_b)],
            input_b,
        )
    shift = bottom_height(obj_a, height_idx) - bottom_height(obj_b, height_idx)
    if abs(shift) > BOTTOM_PLANE_TOLERANCE:
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
        straight_edges.append(find_straight_edges(obj, height_idx))
        logger.info("%s: %d straight edge(s) on the bottom outline", filepath, len(straight_edges[-1]))
        if not straight_edges[-1]:
            fail(
                "%s has no straight edge on its bottom outline (a round or oval model?): "
                "there is nothing to merge on",
                filepath,
            )

    # Step 3: pick the pair of edges that matches best
    pairs = []
    for edge_a in straight_edges[0]:
        for edge_b in straight_edges[1]:
            error, flipped = edge_error(edge_a, edge_b)
            if error <= COARSE_TOLERANCE:
                pairs.append((error, -(edge_a.p1 - edge_a.p0).length, edge_a, edge_b, flipped))
    if not pairs:
        closest = min(
            edge_error(a, b)[0] for a in straight_edges[0] for b in straight_edges[1]
        )
        fail(
            "No edge of %s matches an edge of %s: the closest pair is %.3f mm apart, "
            "more than the %.3f mm tolerance",
            input_a,
            input_b,
            closest,
            COARSE_TOLERANCE,
        )
    pairs.sort(key=lambda pair: pair[:2])
    error, _, edge_a, edge_b, flipped = pairs[0]
    if len(pairs) > 1:
        logger.info("%d other pair(s) of edges also match: merging on the best one", len(pairs) - 1)

    # Step 4: move the second model onto the first one, without deforming it
    transform, angle = alignment_transform(edge_a, edge_b, flipped, height_idx)
    transform = overlap_transform(obj_a, edge_a, height_idx) @ transform
    length_a, length_b = (edge_a.p1 - edge_a.p0).length, (edge_b.p1 - edge_b.p0).length
    if error <= PERFECT_TOLERANCE:
        logger.info(
            "The edges match perfectly (length %.3f mm, ends within %.4f mm)", length_a, error
        )
    else:
        logger.warning(
            "The edges do not match perfectly: ends %.3f mm apart (rotation %.3f deg, "
            "length %.3f mm and %.3f mm). The second model is moved to fit the first",
            error,
            math.degrees(angle),
            length_a,
            length_b,
        )
    obj_b.data.transform(transform)

    # Step 5: merge the two models
    non_manifold_before = count_non_manifold_edges(obj_a.data) + count_non_manifold_edges(obj_b.data)
    with timed("Merge"):
        welded = merge_models(obj_a, obj_b)
    logger.info("%d vertex(es) welded after the union", welded)
    non_manifold_after = count_non_manifold_edges(obj_a.data)
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
        export_stl(output)
    logger.info("Exported %s", output)


if __name__ == "__main__":
    # Get parameters passed after Blender's '--' separator
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []

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

    setup_logging()
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
