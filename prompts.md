# Prompts

## 09/11/2026

I have this Blender script @subtract-magnet-hole.py that takes an input STL file, subtracts a cylindrical hole from it, and outputs a new STL file. I can invoke it from the command line like this:

blender -b -P subtract-magnet-hole.py -- basetta.stl basetta_con_buco.stl --diameter 5.0 --height 2.0

I would like to:

1. move the `subtract-magnet-hole.py` script to the `blender` director
2. move the @basetta.stl file to the `assets/input` directory
3. generate the output file in the `assets/output` directory
4. modify the script so any positional parameters are passed as named parameters, so that from the repo root I can run the following command:

blender -b -P blender/subtract-magnet-hole.py -- --in assets/input/basetta.stl --out assets/output/basetta_con_buco.stl --diameter 5.0 --height 2.0

---

I would like a script that takes all the STL files from a directory and runs the Blender script on each of them, generating the output files in another directory. The script should be executable and should be able to be run from the repo root.

The directory for the generated files should match the input one, but should be `output` instead of `input`. For example, if the input files are in `assets/input/wood-elf-bases/25x25`, the output files should be generated in `assets/output/wood-elf-bases/25x25`. The shell script should also accept the diameter and height parameters as command line arguments. The filename of the generated file should match the input one, but should be suffixed with `_with_5x2_magnet`, where 5 and 2 are the magnet diameter and height, if I pass `--diameter 5` and `--height 2` as named parameters to the script. If instead I pass `--diameter 6` and `--height 3`, the suffix should be `_with_6x3_magnet`. The script should also create the output directory if it does not exist.

---

I would like to invoke this script from the command line like this:

carve -i assets/input/wood-elf-bases/25x25 -d 6 -H 3

So `convert.sh` should probably be renamed to `carve.sh`.

Please add this as a devenv script too, like `hello` or `task-list`.

---

Update the README.md file to include the new script and its usage. Then commit.

## 21/11/2026

I have this Blender script @subtract-magnet-hole.py that takes an input STL file, subtracts a cylindrical hole from the center of the 3d model, and outputs a new STL file.

I now want to update the script so that it does this only when passed the --center flag.

Then I want to add a new flag, --corners, that will subtract a hole (same diameter and height) from the 4 corners of the model instead of the center. Each corner should take into account the magnet diameter, so a 5mm diameter magnet should be subtracted from each corner, with the center of the hole being 2.5mm from each edge of the model.

One can pass both the --center and the --corners flags, so one could carve a total of 5 holes (1 center + 4 corners). If no flag is passed, the script should print the help message and exit.

The script should also print a warning if the --corners flag is passed but the model is not rectangular, and then ignore the flag.

---

I noticed the holes at the corners need more space from the edges, so I want to change the distance from the edge to be equal to the magnet diameter, instead of half the diameter. So a 5mm diameter magnet should be subtracted from each corner, with the center of the hole being 5mm from each edge of the model. I don't want this "diameter multiplier from the edge" to be configurable, but I think it's a good idea to define it as a constant in the script, so that if I want to change it in the future I can do it in one place.

---

Rename the `subtract-magnet-hole.py` into `carve-magnet-holes.py`. Make sure to update all references to the script in `README.md`, `devenv.nix` and `carve.sh`.

---

I think it's time to implement proper logging in `blender/carve-magnet-holes.py`. Remove all print statements and replace them with logging calls. The script should log to stdout and at the same time log a file in the `logs` directory (which should be gitignored except for its README), and the log filename should be the same as the python script, but with an UTC timestamp (e.g. 2026-11-21_23-58-59) and the `.log` extension. Make sure to create the `logs` directory if it does not exist. It would be nice to have colors in the stdout output, but not in the log file. The logging level should be configurable via an environment variable, with a default of INFO. The script should log the command line arguments it was called with, and also log the input and output filenames.

---

The Blender scrpt is quite slow. I want more insights in which phases of the script are taking more time. I want to add timers to the STL import and export phases, and log how long they took. For now I can only see these timers:

```txt
Timer 'STL Import' took 386.80 ms
Timer 'STL Export' took 193.03 ms
```

---

In @logs/README.md, mention that the timestamp in the log filename is in UTC, and that the log files are gitignored except for this README.md file.

---

Every time you open a pull request, include an h2 section named "Description", where you explain what the PR does, and an h2 section named "How to test these changes", where you explain how to test the changes you made. For each command in this section, start with a brief explanation of what a command does, write the exact command in a code block so it's easy to copy and paste in a terminal, the describe the expected output.

If the PR is about a bug fix, include a command that reproduces the bug before your fix, and then show that it works after your fix.

If the PR is about a performance improvement, include a table with the performance numbers before and after your change, and explain how you measured them. Never rely on a single measurement, run the same command 3 times and take the average.

Write about this in @CLAUDE.local.md.

---

Make sure each environment variable and command line argument is documented in the README.md file, and that the README.md file is up to date with the latest changes in the script.

No need for a pull request for this change.

---

Never write the log files in `logs/`. I will do it myself. Write this to `CLAUDE.md`.

---

Switch on a new branch to add a new command line argument in `carve-magnet-holes.py` for the solver. This argment allows to use the FAST solver instead of the EXACT one. Add this help for that flag: "Use the FAST solver instead of the EXACT one. The FAST solver is much quicker, but it does not handle meshes that are not clean: non-manifold edges, self-intersections, coplanar faces." When testing this flag, include the solver type in the output folder, so I can easily compare the results of the two solvers. For example, if I run the script with `--solver FAST`, the output folder should be `assets/output/wood-elf-bases/25x25/FAST-solver`. If I run it with `--solver EXACT`, the output folder should be `assets/output/wood-elf-bases/25x25/EXACT-solver`. The default solver should still be EXACT.

---

The FAST solver seems to work well. Is there a way to notify the user when it has some issues with the mesh? For example, if the mesh is not clean (non-manifold edges, self-intersections, coplanar faces), it can silently produce broken or missing geometry. I want to be able to detect this and log a warning message. Is there a way to do this in Blender?

---

I have 2 STL files that have an edge that matches. I want to merge them on that edge. Can I do this in Blender?

---

On a new branch, implement a new Python script for Blender called `merge-on-edge.py`. This script should take 2 STL files as input, and merge them on the edge that matches. If there are multiple algorithms to choose from for selecting which edges match, please ask me in this session. The output of this script should be a new STL file. The script should be able to handle cases where the edge is not perfectly aligned, and should merge the meshes in a way that preserves the geometry of both meshes. The script should also log the input and output filenames, and any warnings or errors that occur during the merging process. For example, if either one of the two files have no edges that would allow merging (e.g. circle, oval), or if none of the edges have at least a coarse match, the script should log an error and exit. If two edges match, but not perfectly, the script should log a warning and proceed with the output. If the two edge perfectly match, the script should log an INFO level log.

---

I put two files in @assets/input/merge-on-edge-test. I saw that those two models match on an edge. Validate `merge-on-edge.py` on those files.

---

If `carve-magnet-holes.py` and `merge-on-edge.py` share some code, put it in a module. Also, tell me if there is a standard way to do it with Blender scripts.

---

Are there any bases from `assets/input/wood-elf-bases/50x100` that could be merged with a base from `assets/input/wood-elf-bases/50x100`? Is there a relatively fast way to check this, maybe without having to merge any edges, like some sort of "dry run"? Consider that the bases I put in `in assets/input/merge-on-edge-test` are `assets/input/wood-elf-bases/50x100/base 3.stl` and `assets/input/wood-elf-bases/100x100/base 2.stl`.

---
