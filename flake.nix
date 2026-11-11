{
  description = "Synchronize mutable Jujutsu work between development hosts";
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

  outputs = { self, nixpkgs }: let
    systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
    eachSystem = nixpkgs.lib.genAttrs systems;
  in {
    packages = eachSystem (system: let
      pkgs = nixpkgs.legacyPackages.${system};
      package = pkgs.callPackage ./jj-repo-sync.nix { };
    in { default = package; jj-repo-sync = package; });
    checks = eachSystem (system: { inherit (self.packages.${system}) jj-repo-sync; });
    devShells = eachSystem (system: let
      pkgs = nixpkgs.legacyPackages.${system};
    in { default = pkgs.mkShell {
      packages = [ pkgs.jujutsu pkgs.gitMinimal pkgs.openssh pkgs.python3.pkgs.pytest ];
    }; });
  };
}
