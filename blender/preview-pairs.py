import argparse
import html
import logging
import math
import shlex
import sys
from functools import partial
from pathlib import Path

import bpy
import mathutils
import numpy as np

# Blender does not add the directory of a script to the import path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import base_geometry as geometry  # noqa: E402
import cad_common as common  # noqa: E402

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "assets" / "output" / "previews"
IMAGE_WIDTH = 1600  # px, of the longest side of a preview
MARGIN = 1.04  # space around the two models in a preview, as a fraction of their size
# Camera rotation to look at a model from above, for each axis that can be its height
CAMERA_ROTATIONS = {
    0: (0.0, math.radians(90), 0.0),
    1: (math.radians(-90), 0.0, 0.0),
    2: (0.0, 0.0, 0.0),
}

LOG_LEVEL_ENV_VAR = "PREVIEW_LOG_LEVEL"

logger = logging.getLogger(Path(__file__).stem)
timed = partial(common.timed, logger)


def list_models(directory):
    """Return the STL files of a directory, sorted by name."""
    directory = Path(directory)
    if not directory.is_dir():
        logger.error("Not a directory: %s", directory)
        sys.exit(1)
    files = sorted(p for p in directory.iterdir() if p.suffix.lower() == ".stl")
    if not files:
        logger.error("No STL files found in %s", directory)
        sys.exit(1)
    return files


def describe_model(path):
    """Load a model and return what is needed to match it with another one."""
    common.reset_scene()
    obj = geometry.import_model(str(path))
    height_idx = geometry.height_axis(obj)
    coords = geometry.vertex_coordinates(obj)
    model = {
        "path": path,
        "height_idx": height_idx,
        "bottom": float(coords[:, height_idx].min()),
        "thickness": float(np.ptp(coords[:, height_idx])),
        "center": mathutils.Vector((coords.min(axis=0) + coords.max(axis=0)) / 2),
        "edges": geometry.find_straight_edges(obj, height_idx),
    }
    logger.info(
        "%s: thinnest dimension %s (%.1f mm), %d straight edge(s) on the bottom outline",
        path,
        "XYZ"[height_idx],
        model["thickness"],
        len(model["edges"]),
    )
    return model


def match_models(a, b):
    """Return how the models a and b can be merged, or why they cannot.

    The result is a dict: "match" is the first pair of edges that match (as in
    merge-on-edge.py), or None, and "reason" says why there is none.
    """
    if a["height_idx"] != b["height_idx"]:
        return {"match": None, "reason": "the thinnest dimension is not the same axis"}
    if not a["edges"] or not b["edges"]:
        return {"match": None, "reason": "a model has no straight edge on its bottom outline"}
    # Put the bottoms of the two models on the same plane, like merge-on-edge.py does
    shift = mathutils.Vector(
        [a["bottom"] - b["bottom"] if i == a["height_idx"] else 0.0 for i in range(3)]
    )
    edges_b = [geometry.Edge(e.p0 + shift, e.p1 + shift) for e in b["edges"]]
    match, rejected = geometry.match_edges(
        a["edges"], edges_b, a["center"], b["center"] + shift, a["height_idx"]
    )
    if match is None:
        reason = (
            "no pair of edges matches (rejected because of the length: %d, the direction: "
            "%d, the models would overlap: %d)"
            % (rejected["length"], rejected["direction"], rejected["side"])
        )
        return {"match": None, "reason": reason}
    return {"match": match, "reason": None}


def render_preview(obj_a, obj_b, height_idx, filepath):
    """Render a picture of the two models, seen from above."""
    scene = bpy.context.scene
    coords = np.vstack(
        [geometry.vertex_coordinates(obj_a), geometry.vertex_coordinates(obj_b)]
    )
    low, high = coords.min(axis=0), coords.max(axis=0)

    rotation = mathutils.Euler(CAMERA_ROTATIONS[height_idx]).to_matrix()
    right = np.array(rotation @ mathutils.Vector((1, 0, 0)))
    up = np.array(rotation @ mathutils.Vector((0, 1, 0)))
    width_mm, height_mm = np.ptp(coords @ right), np.ptp(coords @ up)

    camera_data = bpy.data.cameras.new("preview")
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = max(width_mm, height_mm) * MARGIN
    camera = bpy.data.objects.new("preview", camera_data)
    scene.collection.objects.link(camera)
    location = (low + high) / 2
    location[height_idx] = high[height_idx] + 100
    camera.location = location.tolist()
    camera.rotation_euler = CAMERA_ROTATIONS[height_idx]
    scene.camera = camera

    scene.render.resolution_x = round(IMAGE_WIDTH * min(1.0, width_mm / height_mm))
    scene.render.resolution_y = round(IMAGE_WIDTH * min(1.0, height_mm / width_mm))
    scene.render.engine = "BLENDER_WORKBENCH"
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = "SINGLE"
    shading.single_color = (0.75, 0.72, 0.65)
    shading.show_cavity = True
    scene.render.filepath = str(filepath)
    bpy.ops.render.render(write_still=True)

    bpy.data.objects.remove(camera, do_unlink=True)
    bpy.data.cameras.remove(camera_data)


def preview_pair(a, b, match, filepath):
    """Place b next to a on the matching edge, without merging them, and render them."""
    common.reset_scene()
    obj_a = geometry.import_model(str(a["path"]))
    obj_b = geometry.import_model(str(b["path"]))
    height_idx = a["height_idx"]
    shift = mathutils.Vector(
        [a["bottom"] - b["bottom"] if i == height_idx else 0.0 for i in range(3)]
    )
    obj_b.data.transform(mathutils.Matrix.Translation(shift))
    edge_a, edge_b, flipped, _, _ = match
    transform, _ = geometry.alignment_transform(
        edge_a, geometry.Edge(edge_b.p0 + shift, edge_b.p1 + shift), flipped, height_idx
    )
    obj_b.data.transform(transform)
    render_preview(obj_a, obj_b, height_idx, filepath)


def describe_match(result):
    """Return a short description of the match of two models, with its quality."""
    match = result["match"]
    if match is None:
        return result["reason"]
    edge_a, edge_b, _, rotation, length_difference = match
    perfect = (
        length_difference <= geometry.PERFECT_TOLERANCE
        and abs(rotation) <= geometry.PERFECT_ANGLE
    )
    return "%s match: edges of %.1f mm and %.1f mm, length difference %.2f mm, rotation %.2f deg" % (
        "perfect" if perfect else "approximate",
        (edge_a.p1 - edge_a.p0).length,
        (edge_b.p1 - edge_b.p0).length,
        length_difference,
        math.degrees(rotation),
    )


def write_index(out_dir, previews, rejected):
    """Write an HTML page with the previews, to look at them in a browser."""
    rows = []
    for preview in previews:
        rows.append(
            "<section><h2>%s + %s</h2><p>%s</p><p>Thickness: %.1f mm and %.1f mm (a step of "
            "%.1f mm at the seam).</p><img src=\"%s\" alt=\"\"></section>"
            % (
                html.escape(str(preview["a"]["path"].parent.name + "/" + preview["a"]["path"].name)),
                html.escape(str(preview["b"]["path"].parent.name + "/" + preview["b"]["path"].name)),
                html.escape(preview["description"]),
                preview["a"]["thickness"],
                preview["b"]["thickness"],
                abs(preview["a"]["thickness"] - preview["b"]["thickness"]),
                html.escape(preview["image"].name),
            )
        )
    skipped = "".join(
        "<li>%s + %s: %s</li>"
        % (
            html.escape(str(r["a"]["path"].parent.name + "/" + r["a"]["path"].name)),
            html.escape(str(r["b"]["path"].parent.name + "/" + r["b"]["path"].name)),
            html.escape(r["description"]),
        )
        for r in rejected
    )
    page = (
        "<!doctype html><meta charset=\"utf-8\"><title>Previews of the pairs of bases</title>"
        "<style>body{font-family:sans-serif;max-width:1100px;margin:2em auto;padding:0 1em}"
        "img{max-width:100%;background:#000}section{margin-bottom:3em}</style>"
        "<h1>Previews of the pairs of bases</h1>"
        "<p>Each picture shows the two models next to each other on the edge that matches, "
        "seen from above. They are not merged.</p>"
        + "".join(rows)
        + "<h2>Pairs that cannot be merged</h2><ul>"
        + skipped
        + "</ul>"
    )
    (out_dir / "index.html").write_text(page, encoding="utf-8")


def preview_pairs(dir_a, dir_b, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    same_directory = Path(dir_a).resolve() == Path(dir_b).resolve()

    with timed("Loading the models"):
        models_a = [describe_model(p) for p in list_models(dir_a)]
        models_b = models_a if same_directory else [describe_model(p) for p in list_models(dir_b)]

    pairs = []
    for i, a in enumerate(models_a):
        for j, b in enumerate(models_b):
            if same_directory and j <= i:
                continue
            result = match_models(a, b)
            pairs.append({"a": a, "b": b, "result": result, "description": describe_match(result)})
    matching = [p for p in pairs if p["result"]["match"] is not None]
    rejected = [p for p in pairs if p["result"]["match"] is None]
    for pair in rejected:
        logger.info("%s + %s: %s", pair["a"]["path"], pair["b"]["path"], pair["description"])
    logger.info("%d of %d pair(s) can be merged", len(matching), len(pairs))

    for pair in matching:
        a, b = pair["a"]["path"], pair["b"]["path"]
        name = f"{a.parent.name}-{a.stem}__{b.parent.name}-{b.stem}.png".replace(" ", "-")
        pair["image"] = out_dir / name
        with timed(f"Preview of {a.name} + {b.name}"):
            preview_pair(pair["a"], pair["b"], pair["result"]["match"], pair["image"])
        logger.info("%s + %s: %s: %s", a, b, pair["description"], pair["image"])
    write_index(out_dir, matching, rejected)
    logger.info("Index written: %s", out_dir / "index.html")
    return matching, rejected


if __name__ == "__main__":
    # Get parameters passed after Blender's '--' separator
    argv = common.script_arguments()

    parser = argparse.ArgumentParser(
        description="Render a picture of each pair of bases that can be merged on an edge, "
        "to see how they go together before merging them with merge-on-edge.py."
    )
    parser.add_argument(
        "-a", "--dir-a", required=True, help="Directory with the first STL files (they do not move)"
    )
    parser.add_argument(
        "-b",
        "--dir-b",
        help="Directory with the second STL files (default: the same as --dir-a, to look for "
        "pairs inside it)",
    )
    parser.add_argument(
        "-o",
        "--out",
        default=str(DEFAULT_OUT),
        help=f"Directory for the pictures and the index.html page (default: {DEFAULT_OUT})",
    )

    args = parser.parse_args(argv)

    common.setup_logging(logger, LOG_LEVEL_ENV_VAR)
    logger.info("Command line arguments: %s", shlex.join(argv))

    try:
        with timed("Total"):
            preview_pairs(args.dir_a, args.dir_b or args.dir_a, args.out)
    except Exception:
        # Blender only prints the traceback to stderr: keep it in the log file too
        logger.exception("Failed to preview the pairs in %s and %s", args.dir_a, args.dir_b)
        raise
