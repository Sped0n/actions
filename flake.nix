{
  description = "Development tools for actions";

  inputs.nixpkgs.url = "github:nixos/nixpkgs?ref=nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    {
      devShells =
        nixpkgs.lib.genAttrs
          [
            "x86_64-linux"
            "aarch64-linux"
            "aarch64-darwin"
          ]
          (
            system:
            let
              pkgs = import nixpkgs { inherit system; };
            in
            {
              default = pkgs.mkShellNoCC {
                packages = with pkgs; [
                  python3
                  prek
                  ruff
                  actionlint
                  nixfmt
                ];
              };
            }
          );
    };
}
