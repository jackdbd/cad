{
  config,
  pkgs,
  ...
}:

{
  enterShell = "";

  env = {
    # https://code.claude.com/docs/en/env-vars
    CLAUDE_CONFIG_DIR = "/home/jack/.claude-zartis/";
    MAGNET_DIAMETER = "5.0";
    MAGNET_HEIGHT = "2.0";
  };

  languages.python = {
    enable = true;
    venv.enable = true; # Creates a local .devenv/state/venv virtualenv
  };

  packages = with pkgs; [
    claude-code
    git
    git-lfs
    openscad
  ];

  scripts = {
    carve.exec = ''
      ${config.devenv.root}/carve.sh "$@"
    '';

    cdoctor.exec = ''
      claude doctor
    '';

    task-list.exec = ''
      devenv tasks list
    '';

    versions.exec = ''
      echo "Versions"
      claude --version
      git --version
      openscad --version
      python --version
    '';
  };

  tasks = {
    "cad:carve-magnet-holes:help" = {
      exec = "blender -b -P blender/carve-magnet-holes.py -- --help";
    };
    "cad:carve-magnet-holes:run" = {
      exec = ''
        blender -b -P blender/carve-magnet-holes.py -- \
        --in assets/input/high-elf-bases/20x20/base-2.stl \
        --out assets/output/high-elf-bases/20x20-with-holes/base-2_with_5x2_magnet.stl \
        --diameter $MAGNET_DIAMETER \
        --height $MAGNET_HEIGHT \
        --center
      '';
    };
  };
}
