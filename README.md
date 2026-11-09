# Scripts for OpenSCAD

## Table of Contents

- [Development environment](#development-environment)
- [Blender scripts](#blender-scripts)
- [OpenSCAD scripts](#openscad-scripts)

## Development environment

This project defines a development environment in `devenv.nix`. You can obtain details about the development environment by running:

```sh
devenv info
```

When you want to work on the project, activate the devenv shell:

```sh
devenv shell
```

The project can be managed with a combination of [devenv tasks](https://devenv.sh/tasks/) and [scripts](https://devenv.sh/scripts/). You can see the list of tasks with this command:

```sh
task-list
```

## Blender scripts

```sh
blender -b -P subtract-magnet-hole.py -- --help
```

```sh
blender -b -P subtract-magnet-hole.py -- basetta.stl basetta_con_buco.stl --diameter 5.0 --height 2.0
```

```sh
for f in bases/*.stl; do
    blender -b -P subtract-magnet-hole.py -- "$f" "magnet_$f" -d 5.0 -H 2.0
done
```

Make the script execubale with `chmod +x convert.sh`. Then launch it to convert all bases.

```sh
./convert.sh
```

## OpenSCAD scripts

First, center the model using `trimesh` (WIP):

```sh
python3 -c "import trimesh, sys; m=trimesh.load(sys.argv[1]); m.apply_translation(-m.bounding_box.centroid); m.export(sys.argv[2])" basetta.stl basetta_centrata.stl
```

Then run the openscad program against the centered model:

```sh
openscad -o basetta_forata.stl hole-for-magnet.scad
```
