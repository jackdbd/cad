# Project conventions

## Logs

Never write log files in `logs/`: the user generates them.

`blender/carve-magnet-holes.py` writes a log file to `logs/` every time it runs, and `carve.sh` (the `carve` command) and the `cad:carve-magnet-holes:run` devenv task run it, so do not run them.
