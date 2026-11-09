{
  pkgs,
  ...
}:

{
  enterShell = ''
    hello
  '';

  env = {
    # https://code.claude.com/docs/en/env-vars
    CLAUDE_CONFIG_DIR = "/home/jack/.claude-zartis/";
    GREET = "world";
  };

  languages.python = {
    enable = true;
    venv.enable = true; # Creates a local .devenv/state/venv virtualenv
  };

  packages = with pkgs; [
    claude-code
    git
    openscad
  ];

  scripts = {
    cdoctor.exec = ''
      claude doctor
    '';

    hello.exec = ''
      echo hello from $GREET
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
    "cad:hello" = {
      exec = ''echo "Hello, $GREET!"'';
    };
    "cad:goodbye" = {
      exec = ''echo "Goodbye, $GREET!"'';
    };
  };
}
