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

