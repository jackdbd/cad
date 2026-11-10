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
blender -b -P blender/subtract-magnet-hole.py -- --help
```

```sh
blender -b -P blender/subtract-magnet-hole.py -- --in assets/input/base.stl --out assets/output/base_with_hole.stl --diameter 5.0 --height 2.0
```

To carve magnet sockets into every STL file in a directory, run `carve` (a devenv script wrapping `carve.sh`). Output goes to the matching `assets/output/...` directory, with a `_with_<diameter>x<height>_magnet` suffix on each filename.

```sh
carve -i assets/input/wood-elf-bases/25x25 -d 5 -H 2
# -> assets/output/wood-elf-bases/25x25/base 1_with_5x2_magnet.stl, ...
```

## OpenSCAD scripts

First, center the model using `trimesh` (WIP):

```sh
python3 -c "import trimesh, sys; m=trimesh.load(sys.argv[1]); m.apply_translation(-m.bounding_box.centroid); m.export(sys.argv[2])" base.stl base_centered.stl
```

Then run the openscad program against the centered model:

```sh
openscad -o base_with_hole.stl openscad/hole-for-magnet.scad
```
