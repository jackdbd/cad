# CAD

Some Blender and OpenSCAD scripts.

## Table of Contents

- [Development environment](#development-environment)
  - [Environment variables](#environment-variables)
- [Blender scripts](#blender-scripts)
  - [carve-magnet-holes.py](#carve-magnet-holespy)
  - [carve](#carve)
  - [Logging](#logging)
- [OpenSCAD scripts](#openscad-scripts)

## Development environment

This project defines a development environment in `devenv.nix`. You can obtain details about the development environment by running:

```sh
devenv info
```

When you want to work on this project, activate the devenv shell:

```sh
devenv shell
```

This project can be managed with a combination of [devenv tasks](https://devenv.sh/tasks/) and [scripts](https://devenv.sh/scripts/). From within the devenv shell, you can see the list of tasks with this command:

```sh
task-list
```

The devenv shell also provides these scripts:

| Script | Description |
| --- | --- |
| `carve` | Runs `carve.sh`: carves magnet sockets into every STL file in a directory (see [carve](#carve)) |
| `task-list` | Lists the devenv tasks |
| `versions` | Prints the versions of Claude Code, git, OpenSCAD and Python |
| `cdoctor` | Runs `claude doctor` |

### Environment variables

| Variable | Default | Defined in | Used by | Description |
| --- | --- | --- | --- | --- |
| `CARVE_LOG_LEVEL` | `INFO` (when unset) | you | `carve-magnet-holes.py`, `carve` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL` (case-insensitive). An invalid value logs a warning and falls back to `INFO`. See [Logging](#logging). |
| `NO_COLOR` | unset | you | `carve-magnet-holes.py` | When set (to any value), log output on stdout is never colored. Colors are only used when stdout is a terminal. |
| `MAGNET_DIAMETER` | `5.0` | `devenv.nix` | `cad:carve-magnet-holes:run` task | Magnet diameter in mm, passed to the script as `--diameter`. |
| `MAGNET_HEIGHT` | `2.0` | `devenv.nix` | `cad:carve-magnet-holes:run` task | Magnet height in mm, passed to the script as `--height`. |
| `CLAUDE_CONFIG_DIR` | `/home/jack/.claude-zartis/` | `devenv.nix` | Claude Code | Directory where Claude Code keeps its configuration. |

## Blender scripts

These scripts need [Blender](https://www.blender.org/) (tested with 5.2.2) and the `blender` command on your `PATH`. The devenv shell does not provide it.

### carve-magnet-holes.py

Carves cylindrical magnet sockets into an STL file. Run it with Blender in background mode: everything after the `--` is passed to the script.

| Option | Required | Default | Description |
| --- | --- | --- | --- |
| `-i`, `--in` | yes | | Path to the input STL file. |
| `-o`, `--out` | yes | | Path to the output STL file. Missing parent directories are created. |
| `-d`, `--diameter` | no | `5.0` | Magnet diameter in mm. |
| `-H`, `--height` | no | `2.0` | Magnet height in mm. |
| `--center` | at least one of `--center` and `--corners` | | Carve a hole in the center of the model. |
| `--corners` | | | Carve a hole in each of the 4 corners of a rectangular model. |
| `-h`, `--help` | no | | Show the help and exit. |

How it works:

- The hole is 0.2 mm larger than the magnet, both in diameter and in depth (`TOLERANCE` in the script): with the defaults you get a 5.2 mm wide and 2.2 mm deep hole.
- The smallest dimension of the model is taken as its height, and the holes are cut into its bottom face.
- `--center` carves a hole in the center of the model.
- `--corners` carves a hole in each of the 4 corners, with each hole centered one magnet diameter from the two nearest edges (`CORNER_EDGE_DISTANCE_FACTOR` in the script). It only applies to rectangular models: if a corner of the bounding box is not filled by the model (a probe is cast 0.5 mm inside each corner, so slightly rounded corners are tolerated), the script prints a warning and ignores `--corners`.
- Pass both flags to carve 5 holes. All the holes are cut in a single Boolean operation.

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | The output file was written. |
| `1` | Nothing was carved (for example `--corners` alone on a model that is not rectangular), so no file was written. |
| `2` | Neither `--center` nor `--corners` was passed: the script prints its help. |

Blender exits with `0` even when the script fails with an unexpected error (for example a missing input file), unless you pass `--python-exit-code 1`. Pass it whenever you check the exit code.

Show the help:

```sh
blender -b -P blender/carve-magnet-holes.py -- --help
```

Carve 5 holes into a model:

```sh
blender -b --python-exit-code 1 -P blender/carve-magnet-holes.py -- \
    --in "assets/input/wood-elf-bases/50x75/base 1.stl" \
    --out "assets/output/base 1_with_holes.stl" \
    --diameter 5 --height 2 \
    --center --corners
```

The devenv task `cad:carve-magnet-holes:help` shows the help. The task `cad:carve-magnet-holes:run` carves a center hole into `assets/input/high-elf-bases/20x20/base-2.stl`, using `MAGNET_DIAMETER` and `MAGNET_HEIGHT`, and writes `assets/output/high-elf-bases/20x20-with-holes/base-2_with_5x2_magnet.stl` (the output filename is fixed, whatever the magnet size):

```sh
devenv tasks run cad:carve-magnet-holes:help
devenv tasks run cad:carve-magnet-holes:run
```

### carve

`carve.sh` runs `carve-magnet-holes.py` on every STL file in a directory. Inside the devenv shell it is available as the `carve` command, from any directory. Without devenv, run `./carve.sh` from the repo root.

| Option | Required | Default | Description |
| --- | --- | --- | --- |
| `-i`, `--in` | yes | | Directory with the input STL files. Only the `.stl` files directly inside it are processed (not subdirectories). Its path must contain a component named `input`. |
| `-d`, `--diameter` | no | `5` | Magnet diameter in mm (a positive number). |
| `-H`, `--height` | no | `2` | Magnet height in mm (a positive number). |
| `--center` | no | used if you pass neither flag | Carve a hole in the center of each model. |
| `--corners` | no | | Carve a hole in each corner of each rectangular model. Pass both flags for 5 holes. |
| `-h`, `--help` | no | | Show the help and exit. |

How it works:

- Output goes to the same path with the `input` component replaced by `output`, for example `assets/input/wood-elf-bases/25x25` becomes `assets/output/wood-elf-bases/25x25`. The directory is created if it does not exist.
- Each output file keeps the name of the input file, with a `_with_<diameter>x<height>_magnet` suffix: `base 1.stl` becomes `base 1_with_5x2_magnet.stl`. Numbers are written without trailing zeros (`--diameter 6.0` gives `6`). The suffix does not say which holes were carved, so running `carve` again with different flags overwrites the previous files.
- Files where no hole could be carved (for example `--corners` alone on a model that is not rectangular) are skipped. They are listed at the end and `carve` exits with `1`. It also exits with `1` when the directory does not exist or has no STL files, and with `2` when the arguments are invalid.
- Each file is processed by its own Blender process, so each one gets its own [log file](#logging).

```sh
carve -i assets/input/wood-elf-bases/25x25 -d 5 -H 2 --center --corners
# -> assets/output/wood-elf-bases/25x25/base 1_with_5x2_magnet.stl, ...
```

### Logging

`carve-magnet-holes.py` logs to stdout (colored when writing to a terminal, disable with `NO_COLOR=1`) and to a file in the `logs/` directory of the repo, named like `carve-magnet-holes_2026-11-21_23-58-59.log` (UTC timestamp of the start of the run). Log files are gitignored. Each run writes its own file, and timestamps inside the log are in UTC too. Runs that only print the help or fail on invalid arguments do not create a log file.

Set the level with the `CARVE_LOG_LEVEL` environment variable (`DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, default `INFO`):

```sh
CARVE_LOG_LEVEL=DEBUG carve -i assets/input/wood-elf-bases/25x25 --center --corners
```

What is logged:

- `INFO`: the log file path, the command line arguments, the input and output filenames, how long each phase took (`STL import`, `Rectangularity check` (only with `--corners`), `Carving N hole(s)`, `STL export` and `Total`), and the result.
- `DEBUG`: also the bounding box of the model, the detected height axis and the position of each hole.
- `WARNING`: for example when `--corners` is ignored because the model is not rectangular.
- `ERROR`: when there is nothing to carve, or when the script fails (with the traceback).

Messages printed by Blender itself (for example `Removed N degenerate triangles during import`) only go to stdout, not to the log file.

## OpenSCAD scripts

First, center the model using `trimesh` (WIP):

```sh
python3 -c "import trimesh, sys; m=trimesh.load(sys.argv[1]); m.apply_translation(-m.bounding_box.centroid); m.export(sys.argv[2])" base.stl base_centered.stl
```

Then run the openscad program against the centered model:

```sh
openscad -o base_with_hole.stl openscad/hole-for-magnet.scad
```
