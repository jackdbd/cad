import argparse
import copy
import logging
import os
import shlex
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import bpy
import mathutils

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

LOG_LEVEL_ENV_VAR = "CARVE_LOG_LEVEL"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "INFO"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

logger = logging.getLogger(Path(__file__).stem)


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
        stdout_formatter = ColorFormatter("%(asctime)s %(levelname)s %(message)s", LOG_DATE_FORMAT)
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

    logger.info("Logging to %s", log_path)
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
            vertices=64,
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
):
    reset_scene()
    with timed("STL import"):
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
    logger.debug(
        "Bounding box: min=%s max=%s",
        [round(v, 3) for v in mins],
        [round(v, 3) for v in maxs],
    )

    # Auto-detect the height axis (smallest thickness)
    height_idx = min(range(3), key=lambda i: maxs[i] - mins[i])
    plane_idx = [i for i in range(3) if i != height_idx]
    logger.debug("Detected height axis: %s", "XYZ"[height_idx])

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
        with timed("Rectangularity check"):
            rectangular = is_rectangular(base, height_idx, mins, maxs)
        if rectangular:
            inset = magnet_diameter * CORNER_EDGE_DISTANCE_FACTOR
            for a in (mins[plane_idx[0]] + inset, maxs[plane_idx[0]] - inset):
                for b in (mins[plane_idx[1]] + inset, maxs[plane_idx[1]] - inset):
                    positions.append(location(a, b))
        else:
            logger.warning("--corners ignored: the model is not rectangular")

    if not positions:
        logger.error("No holes to carve: nothing was exported")
        sys.exit(1)

    for loc in positions:
        logger.debug("Carving hole at %s", tuple(round(v, 3) for v in loc))
    with timed(f"Carving {len(positions)} hole(s)"):
        cut_holes(base, positions, rot, hole_radius, cylinder_depth, solver)

    with timed("STL export"):
        export_stl(output_stl)

    logger.info(
        "Exported %s (%d hole(s): D=%.2fmm, H=%.2fmm)",
        output_stl,
        len(positions),
        hole_diameter,
        hole_depth,
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
    parser.add_argument(
        "--solver",
        type=str.upper,
        choices=SOLVERS,
        default=DEFAULT_SOLVER,
        help="Use the FAST solver instead of the EXACT one. The FAST solver is much "
        "quicker, but it does not handle meshes that are not clean: non-manifold "
        "edges, self-intersections, coplanar faces.",
    )

    args = parser.parse_args(argv)

    if not (args.center or args.corners):
        parser.print_help()
        sys.exit(2)

    setup_logging()
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
            )
    except Exception:
        # Blender only prints the traceback to stderr: keep it in the log file too
        logger.exception("Failed to process %s", args.input)
        raise
