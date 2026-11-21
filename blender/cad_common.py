"""Code shared by the Blender scripts of this project.

Blender does not add the directory of a script to the import path, so a script that
uses this module starts with:

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cad_common as common
"""

import copy
import logging
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import bmesh
import bpy

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_LOG_LEVEL = "INFO"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


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


def setup_logging(logger, level_env_var):
    """Log to stdout (colored on a terminal) and to a timestamped file in LOGS_DIR.

    The name of the logger is the name of the script: the log file is named after it. The
    level of the logs is in the environment variable level_env_var.
    """
    requested = os.environ.get(level_env_var, DEFAULT_LOG_LEVEL).strip().upper()
    invalid_level = requested not in LOG_LEVELS
    level = getattr(logging, DEFAULT_LOG_LEVEL if invalid_level else requested)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    log_path = LOGS_DIR / f"{logger.name}_{timestamp}.log"

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
            level_env_var,
            requested,
            ", ".join(LOG_LEVELS),
            DEFAULT_LOG_LEVEL,
        )


@contextmanager
def timed(logger, label, level=logging.INFO):
    """Log how long the wrapped block took, even if it raises."""
    start = time.perf_counter()
    try:
        yield
    finally:
        logger.log(level, "%s took %.2f ms", label, (time.perf_counter() - start) * 1000)


def script_arguments():
    """Return the arguments after the '--' that Blender passes to its scripts."""
    return sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def import_stl(filepath):
    """Import an STL file and return its object."""
    before = set(bpy.data.objects)
    # Import the STL file (compatible with Blender 4+ and earlier versions)
    try:
        bpy.ops.wm.stl_import(filepath=filepath)
    except AttributeError:
        bpy.ops.import_mesh.stl(filepath=filepath)
    (obj,) = set(bpy.data.objects) - before
    return obj


def export_stl(filepath):
    os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
    try:
        bpy.ops.wm.stl_export(filepath=filepath)
    except AttributeError:
        bpy.ops.export_mesh.stl(filepath=filepath)


def analyze_mesh(mesh):
    """Return the number of non-manifold edges and the volume of a mesh."""
    bm = bmesh.new()
    bm.from_mesh(mesh)
    non_manifold_edges = sum(1 for e in bm.edges if not e.is_manifold)
    volume = bm.calc_volume()
    bm.free()
    return non_manifold_edges, volume
