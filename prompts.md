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
