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

I think it's time to implement proper logging in `blender/subtract-magnet-hole.py`. Remove all print statements and replace them with logging calls. The script should log to stdout and at the same time log a file in the `logs` directory (which should be gitignored except for its README), and the log filename should be the same as the python script, but with an UTC timestamp (e.g. 2026-11-21_23-58-59) and the `.log` extension. Make sure to create the `logs` directory if it does not exist. It would be nice to have colors in the stdout output, but not in the log file. The logging level should be configurable via an environment variable, with a default of INFO. The script should log the command line arguments it was called with, and also log the input and output filenames.
