# CAD

Some Blender and OpenSCAD scripts.

## Table of Contents

- [Development environment](#development-environment)
  - [Environment variables](#environment-variables)
- [Blender scripts](#blender-scripts)
  - [carve-magnet-holes.py](#carve-magnet-holespy)
  - [carve](#carve)
  - [merge-on-edge.py](#merge-on-edgepy)
  - [preview-pairs.py](#preview-pairspy)
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
| `MERGE_LOG_LEVEL` | `INFO` (when unset) | you | `merge-on-edge.py` | Log level of `merge-on-edge.py`, with the same values as `CARVE_LOG_LEVEL`. |
| `PREVIEW_LOG_LEVEL` | `INFO` (when unset) | you | `preview-pairs.py` | Log level of `preview-pairs.py`, with the same values as `CARVE_LOG_LEVEL`. |
| `NO_COLOR` | unset | you | `carve-magnet-holes.py`, `merge-on-edge.py`, `preview-pairs.py` | When set (to any value), log output on stdout is never colored. Colors are only used when stdout is a terminal. |
| `MAGNET_DIAMETER` | `5.0` | `devenv.nix` | `cad:carve-magnet-holes:run` task | Magnet diameter in mm, passed to the script as `--diameter`. |
| `MAGNET_HEIGHT` | `2.0` | `devenv.nix` | `cad:carve-magnet-holes:run` task | Magnet height in mm, passed to the script as `--height`. |
| `CLAUDE_CONFIG_DIR` | `/home/jack/.claude-zartis/` | `devenv.nix` | Claude Code | Directory where Claude Code keeps its configuration. |

## Blender scripts

These scripts need [Blender](https://www.blender.org/) (tested with 5.2.2) and the `blender` command on your `PATH`. The devenv shell does not provide it.

The scripts share their common code (logging, timers, STL import and export, mesh analysis) in `blender/cad_common.py`, and the detection and matching of the straight edges of the bottom of a base in `blender/base_geometry.py`. Blender does not add the directory of a script to the import path, so each script starts by adding it (`sys.path.insert(0, str(Path(__file__).resolve().parent))`) and then imports the module. A new script in `blender/` can do the same.

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
| `--solver` | no | `EXACT` | Boolean solver: `EXACT` or `FAST` (case-insensitive). The `FAST` solver is much quicker, but it does not handle meshes that are not clean: non-manifold edges, self-intersections, coplanar faces. |
| `--no-fallback` | no | | With `--solver FAST`, do not fall back to `EXACT` when the result looks broken (see [Result checks](#result-checks)). The warnings are still logged. |
| `-h`, `--help` | no | | Show the help and exit. |

How it works:

- The hole is 0.2 mm larger than the magnet, both in diameter and in depth (`TOLERANCE` in the script): with the defaults you get a 5.2 mm wide and 2.2 mm deep hole.
- The smallest dimension of the model is taken as its height, and the holes are cut into its bottom face.
- `--center` carves a hole in the center of the model.
- `--corners` carves a hole in each of the 4 corners, with each hole centered one magnet diameter from the two nearest edges (`CORNER_EDGE_DISTANCE_FACTOR` in the script). It only applies to rectangular models: if a corner of the bounding box is not filled by the model (a probe is cast 0.5 mm inside each corner, so slightly rounded corners are tolerated), the script prints a warning and ignores `--corners`.
- Pass both flags to carve 5 holes. All the holes are cut in a single Boolean operation.
- The Boolean operation uses the `EXACT` solver, which is robust but slow on detailed models. `--solver FAST` uses the faster solver instead. Blender 5 calls it `FLOAT`: the script uses whichever name your Blender has. Check the result of `FAST` before you trust it, especially on models that are not clean. When the result of `FAST` looks broken, the script warns and redoes the carving with `EXACT` (see [Result checks](#result-checks)).

#### Result checks

Boolean solvers do not report problems: on meshes that are not clean (non-manifold edges, self-intersections, coplanar faces) they can silently produce broken or missing geometry. So after carving, the script checks the result and logs a `WARNING` for each problem it finds:

- **Removed volume.** The volume removed from the model must match the volume of the holes (number of holes times the area of the cylinder times the hole depth), within 2% (`VOLUME_TOLERANCE` in the script). On your real bases the difference is below 0.1 mm³ for 5 holes. A bigger difference means that a hole is missing or only partly cut, or that a hole is not fully inside the model.
- **Non-manifold edges.** Carving must not increase the number of non-manifold edges of the model.

Before carving, the script also logs the state of the input mesh at `INFO`: triangles and non-manifold edges. With `CARVE_LOG_LEVEL=DEBUG` it also counts the pairs of self-intersecting triangles, which takes about 2 seconds on a detailed model, so it is skipped otherwise. These numbers are information, not warnings: many models already have a few problems and still carve well.

With `--solver FAST`, a failed check also triggers a **fallback**: the script starts again from the original mesh, carves with the `EXACT` solver and logs a warning. The log line `Exported ...` tells which solver produced the file. Pass `--no-fallback` to keep the `FAST` result anyway, for example to look at what `FAST` does wrong. The checks add about 1.5 seconds to each detailed model.

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
| `--solver` | no | `EXACT` in the script | `EXACT` or `FAST`, passed to the script (see above). |
| `--no-fallback` | no | | Passed to the script: with `--solver FAST`, do not fall back to `EXACT` when the result looks broken. |
| `-h`, `--help` | no | | Show the help and exit. |

How it works:

- Output goes to the same path with the `input` component replaced by `output`, for example `assets/input/wood-elf-bases/25x25` becomes `assets/output/wood-elf-bases/25x25`. The directory is created if it does not exist.
- Each output file keeps the name of the input file, with a `_with_<diameter>x<height>_magnet` suffix: `base 1.stl` becomes `base 1_with_5x2_magnet.stl`. Numbers are written without trailing zeros (`--diameter 6.0` gives `6`). The suffix does not say which holes were carved or which solver was used (the log file does), so running `carve` again with different flags or with the other solver overwrites the previous files.
- Files where no hole could be carved (for example `--corners` alone on a model that is not rectangular) are skipped. They are listed at the end and `carve` exits with `1`. It also exits with `1` when the directory does not exist or has no STL files, and with `2` when the arguments are invalid.
- Each file is processed by its own Blender process, so each one gets its own [log file](#logging).

```sh
carve -i assets/input/wood-elf-bases/25x25 -d 5 -H 2 --center --corners
# -> assets/output/wood-elf-bases/25x25/base 1_with_5x2_magnet.stl, ...
```

Carve faster with the `FAST` solver:

```sh
carve -i assets/input/wood-elf-bases/25x25 --center --corners --solver FAST
```

### merge-on-edge.py

Merges two models of miniature bases into one, on the edge where they touch. The first model (A) stays where it is, and the second one (B) is moved onto it.

| Option | Required | Description |
| --- | --- | --- |
| `-a`, `--in-a` | yes | Path to the first STL file. |
| `-b`, `--in-b` | yes | Path to the second STL file. |
| `-o`, `--out` | yes | Path to the merged STL file. Missing parent directories are created. |
| `-h`, `--help` | no | Show the help and exit. |

How it works:

1. **Same bottom plane.** The thinnest dimension of a model is its height. B is moved along it until the bottoms of the two models are on the same plane, so their edges are not tilted. If the thinnest dimension is not the same axis in the two models, the script logs an error and exits.
2. **Straight edges.** On each model, the candidate edges are the straight edges of the outline of the flat bottom (the edges where the faces on the bottom plane meet other faces). The outline of a real model is made of many tiny edges and is not exactly straight, so it is split into runs that stay within 0.1 mm of a straight line. Edges shorter than 20% of the longest dimension of the model are ignored, so a round or oval model, whose outline is made of many short edges, has no candidate: the script logs an error and exits. Straight edges on the same line are joined, even with a gap between them (a notch in the outline), if the gap is at most 10% of the length of the joined edge.
3. **Matching.** The models can be anywhere in the files. The script takes the pairs of an edge of A and an edge of B, the closest pair first (the one that asks for the smallest move of B), and stops at the first pair where the two edges have about the same length (within 2 mm), are about parallel (within 5°), and have the models on opposite sides (they touch instead of overlapping, and B only needs a small rotation). If no pair matches, the script logs an error with the number of pairs rejected for each reason, and exits.
4. **Alignment.** If the lengths of the two edges differ by less than 0.01 mm and the rotation is below 0.1°, the script logs that the edges match perfectly (`INFO`). If not, it logs a warning with the length difference and the rotation, and goes on. B is rotated around the height axis and moved, without deforming it, so that its edge lies on the edge of A. It is then pushed 0.01 mm into A, because walls that are coplanar only up to rounding errors make the union produce slivers, and the overlap also connects models that touch along an edge only.
5. **Union.** The two models are merged with a Boolean union (`EXACT` solver), which removes the walls that were back to back, and the vertices closer than 0.001 mm are welded.
6. **Result check.** A Boolean solver can fail without any error: for example, Blender's `MANIFOLD` solver returns only one of the two models when they are not clean enough. So the script checks that the merged model has the bounding box of the two models (within 0.1 mm) and the sum of their volumes (within 2%). If not, it logs an error for each problem and exits with `1`: **no file is written**. If the merged model has more non-manifold edges than the two models together, it logs a warning.

The thresholds above are constants at the top of the script (`OVERLAP`, `WELD_DISTANCE`, `BOUNDING_BOX_TOLERANCE`, `VOLUME_TOLERANCE`) and at the top of `blender/base_geometry.py` (`STRAIGHT_TOLERANCE`, `STRAIGHT_ANGLE`, `JOIN_TOLERANCE`, `MAX_GAP_RATIO`, `MIN_EDGE_RATIO`, `PERFECT_TOLERANCE`, `PERFECT_ANGLE`, `COARSE_TOLERANCE`, `MAX_ROTATION`).

Exit codes: `0` when the merged file was written (with or without warnings), `1` when the script logged an error (nothing is written in that case), and `2` when the arguments are invalid. As with `carve-magnet-holes.py`, pass `--python-exit-code 1` to Blender if you check the exit code, otherwise an unexpected error exits with `0`.

```sh
blender -b --python-exit-code 1 -P blender/merge-on-edge.py -- \
    --in-a assets/input/base-a.stl \
    --in-b assets/input/base-b.stl \
    --out assets/output/base-a-and-b.stl
```

Notes:

- The script assumes that the bottoms of the models are flat and that the models lie flat with the same axis as height.
- The merged model can have tiny non-manifold edges along the seam. Two real 25 mm bases ended up with 3 of them (0.1 to 0.4 mm long). Two scenic bases about 30 mm thick (50x100 and 100x100, with sculpted walls) ended up with thousands, because the union of two rough walls creates many tiny slivers: the script warns about them, so check the result in a slicer.
- Like `carve-magnet-holes.py`, the script writes a [log file](#logging).

### preview-pairs.py

Renders a picture of each pair of bases that can be merged on an edge, to see how the two bases go together before you merge them. Nothing is merged: each picture shows the two models next to each other, seen from above, on the edge that `merge-on-edge.py` would use.

Whether two bases belong together (the path, the river or the ground continues across the seam) cannot be told from the geometry: almost any two rectangular bases can be joined on an edge. The pictures are there for you to judge.

| Option | Required | Default | Description |
| --- | --- | --- | --- |
| `-a`, `--dir-a` | yes | | Directory with the first STL files (they do not move). |
| `-b`, `--dir-b` | no | the same as `--dir-a` | Directory with the second STL files. If it is the same directory, the script looks for pairs inside it, each pair once. |
| `-o`, `--out` | no | `assets/output/previews` | Directory for the pictures and for the `index.html` page. |
| `-h`, `--help` | no | | Show the help and exit. |

How it works:

1. Each model is loaded once, to find the axis of its height and the straight edges of its bottom outline.
2. Every pair of a model of `--dir-a` and a model of `--dir-b` is matched like `merge-on-edge.py` does (same bottom plane, then the first pair of edges that match).
3. For each pair that matches, the script places the second model next to the first one on those edges, without merging them, and renders them from above (Blender's Workbench engine) into `<directory>-<model>__<directory>-<model>.png`.
4. `index.html` shows the pictures one after the other, with the quality of the match (`perfect` or `approximate`), the thickness of the two models (the step they would have at the seam), and the pairs that cannot be merged, with the reason. Open it in a browser.

On the 7 bases of `50x100` and `100x100`, loading took about 25 seconds and each picture about 3 seconds.

```sh
blender -b --python-exit-code 1 -P blender/preview-pairs.py -- \
    --dir-a assets/input/wood-elf-bases/50x100 \
    --dir-b assets/input/wood-elf-bases/100x100 \
    --out assets/output/previews/50x100-with-100x100
```

Notes:

- A picture shows the arrangement `merge-on-edge.py` would choose, the one on the closest matching edges. Two portrait bases of the same size could also be joined along their other sides: those arrangements are not shown.
- Exit codes: `0` when the pictures are written, `1` when a directory does not exist or has no STL files, and `2` when the arguments are invalid.
- Like the other scripts, it writes a [log file](#logging).

### Logging

`carve-magnet-holes.py`, `merge-on-edge.py` and `preview-pairs.py` log to stdout (colored when writing to a terminal, disable with `NO_COLOR=1`) and to a file in the `logs/` directory of the repo, named like `carve-magnet-holes_2026-11-21_23-58-59.log` or `merge-on-edge_2026-11-21_23-58-59.log` (the script name and the UTC timestamp of the start of the run). Log files are gitignored. Each run writes its own file, and timestamps inside the log are in UTC too. Runs that only print the help or fail on invalid arguments do not create a log file.

Set the level with the `CARVE_LOG_LEVEL` environment variable (`MERGE_LOG_LEVEL` for `merge-on-edge.py`, `PREVIEW_LOG_LEVEL` for `preview-pairs.py`): `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`, default `INFO`:

```sh
CARVE_LOG_LEVEL=DEBUG carve -i assets/input/wood-elf-bases/25x25 --center --corners
```

What `carve-magnet-holes.py` logs:

- `INFO`: the command line arguments, the input and output filenames, the Boolean solver, the state of the input mesh, the result of the [result checks](#result-checks), how long each phase took (`STL import`, `Input mesh analysis`, `Rectangularity check` (only with `--corners`), `Carving N hole(s)`, `Result check`, `STL export` and `Total`), and the result.
- `DEBUG`: also the bounding box of the model, the detected height axis, the position of each hole and the number of self-intersecting triangle pairs of the input mesh.
- `WARNING`: for example when `--corners` is ignored because the model is not rectangular, when a [result check](#result-checks) fails, or when the script falls back to the `EXACT` solver.
- `ERROR`: when there is nothing to carve, or when the script fails (with the traceback).

`merge-on-edge.py` logs the command line arguments and the input and output filenames at `INFO`, then each step: the move to the same bottom plane, the number of straight edges of each model, the pair of edges it merges on (`INFO` for a perfect match, `WARNING` with the details otherwise), the welded vertices, the result check, and how long import, merge, the check and export took. Errors (no straight edge, no matching edge, models oriented differently, a failed result check) and the warning about non-manifold edges are logged as described above.

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
