{
  pkgs ? import <nixpkgs> { },
}:

let
  python = pkgs.python3;
  pythonEnv = python.withPackages (ps: [
    ps.questionary
    ps.rich
  ]);
in
pkgs.mkShell {
  packages = [
    pythonEnv
  ];

  shellHook = ''
    echo "Python development shell ready."
    python --version
  '';
}
