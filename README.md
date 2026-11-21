# CAD

Some Blender and OpenSCAD scripts.

## Table of Contents

- [Development environment](#development-environment)
- [Blender scripts](#blender-scripts)
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

## Blender scripts

Show the help for the `subtract-magnet-hole.py` script:

```sh
blender -b -P blender/subtract-magnet-hole.py -- --help
```

Show the help for the `cad:subtract-magnet-hole` task:

```sh
devenv tasks run cad:subtract-magnet-hole:help
```

Run the `subtract-magnet-hole.py` script to carve a magnet socket into an STL file:

```sh
blender -b -P blender/subtract-magnet-hole.py -- --in assets/input/base.stl --out assets/output/base_with_hole.stl --diameter 5.0 --height 2.0
```

Run the `cad:subtract-magnet-hole` task to carve a magnet socket into an STL file:

```sh
devenv tasks run cad:subtract-magnet-hole:run
```

Carve a magnet socket into every STL file found in a directory. Output goes to the matching `assets/output/...` directory, with a `_with_<diameter>x<height>_magnet` suffix on each filename.

```sh
carve -i assets/input/wood-elf-bases/25x25 -d 5 -H 2
# -> assets/output/wood-elf-bases/25x25/base 1_with_5x2_magnet.stl, ...

./carve -i assets/input/wood-elf-bases/50x75 -d 5 -H 2
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
