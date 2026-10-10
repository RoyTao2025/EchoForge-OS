# deps-bot

Tooling for the daily EFOS dependency routine (a Claude Code routine). This branch is never
merged anywhere; it only holds the scripts the routine runs.

Each morning the routine:

1. checks the flake inputs (nixpkgs, home-manager, sops-nix, nixos-hardware) and the pinned
   Cardano binaries (cardano-node/cli, ogmios, kupo, mithril-client) against upstream;
2. applies any update on `deps/flake-inputs` or `deps/cardano-binaries` in this fork, rebuilt
   from `echoforge-dev/echoforge-os` main;
3. evaluates every `nixosConfigurations` profile and builds the bumped x86_64 packages;
4. opens or refreshes one PR per branch into `echoforge-dev/echoforge-os`. It never merges.

## Scripts

`update-flake-lock.sh [flake.lock]` updates the github inputs. Claude Code cloud sessions block
api.github.com and codeload.github.com, which `nix flake update` needs, but allow git. Nix's git
fetcher gives the same narHash and lastModified as the github tarball (checked against all four
EFOS inputs on 2026-10-10), so the script fetches over git and writes ordinary github-type locks.
`--prefetch` fetches the current locks into the store so `nix eval` works offline from GitHub.

`cardano-binaries.py status [repo]` lists newer upstream tags and Armada Alliance aarch64 builds.
`cardano-binaries.py bump <pkg> <version> [repo]` rewrites version, hashes, armadaDir and
`scripts/prefetch-hashes.sh`, and checks each archive contains what `installPhase` installs.
A tag is not a release: confirm on the GitHub releases page before bumping.

On a normal machine you don't need any of this: `nix flake update` and
`scripts/prefetch-hashes.sh` do the same job.
