"""Geometry of the bases: find the straight edges of the bottom of a model, and match them.

Used by the Blender scripts that merge two bases (merge-on-edge.py) and that preview
the pairs of bases that can be merged (preview-pairs.py).
"""

import math
from collections import namedtuple

import bmesh
import mathutils
import numpy as np

import cad_common as common

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

# A straight edge of a model, from p0 to p1
Edge = namedtuple("Edge", ["p0", "p1"])


def import_model(filepath):
    """Import an STL file and return its object, with its transform baked into the mesh."""
    obj = common.import_stl(filepath)
    # From now on the local coordinates of the mesh are its world coordinates
    obj.data.transform(obj.matrix_world)
    obj.matrix_world = mathutils.Matrix.Identity(4)
    return obj


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
