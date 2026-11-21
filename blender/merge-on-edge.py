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

# The outline of the bottom stays within this distance of a straight edge. The outline of
# a real model is made of many tiny edges, and not exactly straight.
STRAIGHT_TOLERANCE = 0.1  # mm
# Two straight edges that touch each other and have directions within this angle make up
# a single straight edge
STRAIGHT_ANGLE = math.radians(1)
# How far from the bottom plane an edge can be and still lie on it
BOTTOM_PLANE_TOLERANCE = 0.05  # mm
# Two straight edges on the same line make up a single edge if they are within this
# distance of the line through both, and the gap (a notch in the outline) between them is
# at most this fraction of the length of the joined edge
JOIN_TOLERANCE = 0.5  # mm
MAX_GAP_RATIO = 0.1
# Shortest straight edge to merge on, as a fraction of the longest dimension of its
# model. It rules out the many short edges that approximate a circle or an oval.
MIN_EDGE_RATIO = 0.2
# Two edges match perfectly if their lengths differ by less than this, and their
# directions by less than PERFECT_ANGLE
PERFECT_TOLERANCE = 0.01  # mm
PERFECT_ANGLE = math.radians(0.1)
# Two edges do not match at all if their lengths differ by more than this (the outline of
# a real model is rounded at the corners), or if their directions differ by more than
# MAX_ROTATION
COARSE_TOLERANCE = 2.0  # mm
MAX_ROTATION = math.radians(5)
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


def simplify_polyline(points, tolerance):
    """Return the indices of the points that keep a polyline within tolerance of itself.

    This is the Douglas-Peucker algorithm: the points between two consecutive indices
    are within tolerance of the straight line that joins them.
    """
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        line = points[j] - points[i]
        length = np.linalg.norm(line)
        between = points[i + 1 : j] - points[i]
        if length == 0:
            distances = np.linalg.norm(between, axis=1)
        else:
            distances = np.linalg.norm(np.cross(between, line / length), axis=1)
        k = int(np.argmax(distances))
        if distances[k] > tolerance:
            keep.add(i + 1 + k)
            stack.extend([(i, i + 1 + k), (i + 1 + k, j)])
    return sorted(keep)


def straight_runs(points, closed):
    """Split a polyline into straight runs: a list of (start point, end point)."""
    if closed:
        # Split the loop in two, at the point farthest from where it starts
        k = int(np.argmax(np.linalg.norm(points - points[0], axis=1)))
        parts = [points[: k + 1], np.vstack([points[k:], points[:1]])]
    else:
        parts = [points]
    runs = []
    for part in parts:
        kept = simplify_polyline(part, STRAIGHT_TOLERANCE)
        runs.extend((part[i], part[j]) for i, j in zip(kept, kept[1:]))

    # Join the runs that go in the same direction (also across the start of a loop)
    def collinear(a, b):
        da, db = a[1] - a[0], b[1] - b[0]
        norm = np.linalg.norm(da) * np.linalg.norm(db)
        return norm > 0 and abs(da @ db) / norm >= math.cos(STRAIGHT_ANGLE)

    joined = []
    for run in runs:
        if joined and collinear(joined[-1], run):
            joined[-1] = (joined[-1][0], run[1])
        else:
            joined.append(run)
    if closed and len(joined) > 1 and collinear(joined[-1], joined[0]):
        joined[0] = (joined[-1][0], joined[0][1])
        joined.pop()
    return joined


def join_edges(a, b):
    """Return the edge that joins two edges on the same line, or None.

    The two edges can have a gap between them, like the two sides of a notch.
    """
    da, db = a.p1 - a.p0, b.p1 - b.p0
    if abs(da.dot(db)) / (da.length * db.length) < math.cos(STRAIGHT_ANGLE):
        return None
    direction = da.normalized()
    points = [a.p0, a.p1, b.p0, b.p1]
    along = [(p - a.p0).dot(direction) for p in points]
    start, end = points[along.index(min(along))], points[along.index(max(along))]
    line = (end - start).normalized()
    if any((p - start).cross(line).length > JOIN_TOLERANCE for p in points):
        return None
    runs = sorted([sorted(along[:2]), sorted(along[2:])])
    gap = max(0.0, runs[1][0] - runs[0][1])
    if gap > MAX_GAP_RATIO * (end - start).length:
        return None
    return Edge(start.copy(), end.copy())


def join_collinear_edges(edges):
    """Join the edges that are on the same line, until no more can be joined."""
    edges = list(edges)
    joined = True
    while joined:
        joined = False
        for i in range(len(edges)):
            for j in range(i + 1, len(edges)):
                edge = join_edges(edges[i], edges[j])
                if edge is not None:
                    edges[i] = edge
                    del edges[j]
                    joined = True
                    break
            if joined:
                break
    return edges


def find_straight_edges(obj, height_idx):
    """Return the straight edges of the outline of the bottom of a model.

    The bottom is the flat region of the model on the bottom plane, and its outline is
    split into straight edges. Short edges are left out: a round model has many of them
    and no straight edge to merge on.
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

    # The faces of the bottom, and the edges that have a single face of the bottom
    bottom_faces = {
        f.index
        for v in on_bottom
        for f in v.link_faces
        if all(other in on_bottom for other in f.verts)
    }
    bm.faces.ensure_lookup_table()
    outline = {}
    for index in bottom_faces:
        for edge in bm.faces[index].edges:
            if sum(f.index in bottom_faces for f in edge.link_faces) == 1:
                outline[edge.index] = edge

    # Follow the outline from vertex to vertex, to get its polylines
    neighbors = {}
    for edge in outline.values():
        for vert in edge.verts:
            neighbors.setdefault(vert.index, []).append(edge)
    visited = set()

    def extend(chain, last_edge):
        """Add vertices to the end of chain while the outline goes on without a fork."""
        while len(neighbors[chain[-1].index]) == 2:
            following = [e for e in neighbors[chain[-1].index] if e is not last_edge]
            if not following or following[0].index in visited:
                break
            last_edge = following[0]
            visited.add(last_edge.index)
            chain.append(last_edge.other_vert(chain[-1]))
        return chain

    straight_edges = []
    for edge in outline.values():
        if edge.index in visited:
            continue
        visited.add(edge.index)
        chain = extend([edge.verts[0], edge.verts[1]], edge)
        closed = chain[0] is chain[-1]
        if not closed:
            chain = extend(chain[::-1], edge)[::-1]
        points = np.array([v.co[:] for v in (chain[:-1] if closed else chain)])
        for start, end in straight_runs(points, closed):
            if np.linalg.norm(end - start) >= min_length:
                straight_edges.append(
                    Edge(mathutils.Vector(start.tolist()), mathutils.Vector(end.tolist()))
                )
    bm.free()
    return join_collinear_edges(straight_edges)


def match_edges(edges_a, edges_b, center_a, center_b, axis_idx):
    """Return the first pair of edges that match, with how they differ.

    Take the pairs of an edge of a and an edge of b, the closest pair first (the one that
    asks for the smallest move of b), and keep the first one where the two edges:
    - have about the same length (within COARSE_TOLERANCE);
    - are about parallel (within MAX_ROTATION), so b only needs a small rotation;
    - have the models on the opposite sides, once the two edges are on each other (the
      models touch instead of overlapping). A model lies on the side of its edge where
      its center is.

    Where the models are in the files only decides the order: it does not stop edges that
    are far apart from matching. Return (edge a, edge b, whether edge b has to be
    flipped, rotation, length difference), or None if no pair matches. Also return how
    many pairs were rejected for each reason.
    """
    axis = mathutils.Vector([1.0 if i == axis_idx else 0.0 for i in range(3)])
    rejected = {"length": 0, "direction": 0, "side": 0}

    def distance(pair):
        a, b = pair
        return ((a.p0 + a.p1) / 2 - (b.p0 + b.p1) / 2).length

    pairs = sorted(((a, b) for a in edges_a for b in edges_b), key=distance)
    for edge_a, edge_b in pairs:
        da, db = edge_a.p1 - edge_a.p0, edge_b.p1 - edge_b.p0
        length_difference = abs(da.length - db.length)
        if length_difference > COARSE_TOLERANCE:
            rejected["length"] += 1
            continue
        flipped = da.dot(db) < 0
        if flipped:
            db = -db
        rotation = math.atan2(db.cross(da).dot(axis), db.dot(da))
        if abs(rotation) > MAX_ROTATION:
            rejected["direction"] += 1
            continue
        # The side of its edge where each model lies, along the same normal
        normal = axis.cross(da)
        side_a = normal.dot(center_a - (edge_a.p0 + edge_a.p1) / 2)
        side_b = normal.dot(center_b - (edge_b.p0 + edge_b.p1) / 2)
        if side_a * side_b > 0:
            rejected["side"] += 1
            continue
        return (edge_a, edge_b, flipped, rotation, length_difference), rejected
    return None, rejected


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

    # Step 3: look for the first pair of edges that match
    centers = []
    for obj in (obj_a, obj_b):
        coords = vertex_coordinates(obj)
        centers.append(mathutils.Vector((coords.min(axis=0) + coords.max(axis=0)) / 2))
    match, rejected = match_edges(
        straight_edges[0], straight_edges[1], centers[0], centers[1], height_idx
    )
    if match is None:
        fail(
            "No edge of %s matches an edge of %s. Pairs of edges rejected because the "
            "lengths differ by more than %.1f mm: %d; because they are not parallel "
            "(within %.1f deg): %d; because the models would overlap: %d",
            input_a,
            input_b,
            COARSE_TOLERANCE,
            rejected["length"],
            math.degrees(MAX_ROTATION),
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
    transform, _ = alignment_transform(edge_a, edge_b, flipped, height_idx)
    transform = overlap_transform(obj_a, edge_a, height_idx) @ transform
    if length_difference <= PERFECT_TOLERANCE and abs(rotation) <= PERFECT_ANGLE:
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
