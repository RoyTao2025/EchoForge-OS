#!/usr/bin/env bash
# Update the github-type inputs in flake.lock without api.github.com or codeload.github.com.
#
# Why: in Claude Code cloud sessions the git proxy serves anonymous git reads of public repos,
# but blocks the GitHub API and tarball downloads that `nix flake update` uses for github: inputs.
# Nix's git fetcher produces the same narHash and lastModified as the github tarball fetcher
# (checked 2026-10-10 against all four EFOS inputs), so we fetch over git and write the result
# back as a normal github-type lock entry. The lock file looks exactly like `nix flake update` made it.
#
# Transitive inputs that are not `follows` (e.g. nixos-hardware's own nixpkgs) are copied from the
# updated input's own flake.lock, which is what `nix flake update` does too.
#
# Usage: update-flake-lock.sh [path/to/flake.lock]
#        update-flake-lock.sh --prefetch [path/to/flake.lock]
# --prefetch changes nothing: it fetches every github input at its locked rev over git, so the
# store already holds it and nix eval/build never needs api.github.com. Run it on a fresh machine
# before evaluating a flake whose lock you did not just produce.
# Prints one line per input. Exit 0 = success (changed or not), non-zero = could not update.
set -euo pipefail

fail() { echo "ERROR: $*" >&2; exit 1; }

if [[ ${1:-} == --prefetch ]]; then
  lock=${2:-flake.lock}
  jq -r '.nodes | to_entries[] | select(.value.locked.type == "github")
         | "\(.key) \(.value.locked.owner) \(.value.locked.repo) \(.value.locked.rev) \(.value.locked.narHash)"' "$lock" |
    while read -r key owner repo rev narHash; do
      got=$(nix flake prefetch --json "git+https://github.com/$owner/$repo?rev=$rev&shallow=1" 2>/dev/null | jq -r .hash) ||
        fail "could not fetch $owner/$repo@$rev"
      [[ $got == "$narHash" ]] || fail "$key: git narHash $got != locked $narHash"
      echo "$key: $owner/$repo@${rev:0:12} in store"
    done
  exit 0
fi

lock=${1:-flake.lock}
work=$(mktemp)
trap 'rm -f "$work"' EXIT
cp "$lock" "$work"

# A repo using export-ignore/export-subst would make the github tarball differ from the git tree.
check_gitattributes() {
  local src=$1 name=$2
  if [[ -f $src/.gitattributes ]] && grep -Eq 'export-(ignore|subst)' "$src/.gitattributes"; then
    fail "$name uses export-ignore/export-subst; git narHash may differ from the GitHub tarball. Update this input manually."
  fi
}

for name in $(jq -r '.nodes.root.inputs | keys[]' "$lock"); do
  node=$(jq -r --arg n "$name" '.nodes.root.inputs[$n] | if type == "string" then . else empty end' "$lock")
  [[ -n $node ]] || { echo "$name: follows another input, skipped"; continue; }

  original=$(jq -c --arg k "$node" '.nodes[$k].original' "$lock")
  type=$(jq -r '.type' <<<"$original")
  if [[ $type != github ]]; then
    echo "$name: type $type, skipped (only github inputs are handled)"
    continue
  fi
  if [[ $(jq -r '.rev // empty' <<<"$original") != "" ]]; then
    echo "$name: pinned to a rev in flake.nix, skipped"
    continue
  fi

  owner=$(jq -r '.owner' <<<"$original")
  repo=$(jq -r '.repo' <<<"$original")
  ref=$(jq -r '.ref // empty' <<<"$original")
  url="git+https://github.com/$owner/$repo?shallow=1${ref:+&ref=$ref}"

  json=$(nix flake prefetch --json "$url" 2>/dev/null) || fail "could not fetch $url"
  rev=$(jq -r '.locked.rev' <<<"$json")
  narHash=$(jq -r '.hash' <<<"$json")
  lastModified=$(jq -r '.locked.lastModified' <<<"$json")
  src=$(jq -r '.storePath' <<<"$json")
  oldRev=$(jq -r --arg k "$node" '.nodes[$k].locked.rev' "$lock")

  if [[ $rev == "$oldRev" ]]; then
    echo "$name: unchanged ($rev)"
    continue
  fi
  check_gitattributes "$src" "$name"

  oldTime=$(jq -r --arg k "$node" '.nodes[$k].locked.lastModified' "$lock")
  echo "$name: $owner/$repo ${ref:-HEAD} $oldRev ($(date -u -d "@$oldTime" +%F)) -> $rev ($(date -u -d "@$lastModified" +%F))"

  jq --arg k "$node" --arg owner "$owner" --arg repo "$repo" --arg rev "$rev" \
    --arg narHash "$narHash" --argjson lastModified "$lastModified" \
    '.nodes[$k].locked = {lastModified: $lastModified, narHash: $narHash, owner: $owner, repo: $repo, rev: $rev, type: "github"}' \
    "$work" >"$work.new" && mv "$work.new" "$work"

  # Transitive inputs that are not follows: take them from the input's own lock file.
  for child in $(jq -r --arg k "$node" '.nodes[$k].inputs // {} | to_entries[] | select(.value | type == "string") | .key' "$lock"); do
    childNode=$(jq -r --arg k "$node" --arg c "$child" '.nodes[$k].inputs[$c]' "$lock")
    [[ -f $src/flake.lock ]] || fail "$name has input '$child' but no flake.lock of its own; update manually"
    depNode=$(jq -r --arg c "$child" '.nodes.root.inputs[$c] | if type == "string" then . else empty end' "$src/flake.lock")
    [[ -n $depNode ]] || fail "$name/$child is not a plain input in $name's own flake.lock; update manually"
    if [[ $(jq -r --arg k "$depNode" '.nodes[$k].inputs // {} | length' "$src/flake.lock") != 0 ]] ||
      [[ $(jq -r --arg k "$childNode" '.nodes[$k].inputs // {} | length' "$lock") != 0 ]]; then
      fail "$name/$child has nested inputs of its own; update manually"
    fi
    dep=$(jq -c --arg k "$depNode" '.nodes[$k] | {locked, original}' "$src/flake.lock")
    oldChildRev=$(jq -r --arg k "$childNode" '.nodes[$k].locked.rev // .nodes[$k].locked.narHash' "$lock")
    newChildRev=$(jq -r '.locked.rev // .locked.narHash' <<<"$dep")
    if [[ $oldChildRev != "$newChildRev" ]]; then
      echo "  $name/$child: $oldChildRev -> $newChildRev (from $name's own flake.lock)"
    fi
    jq --arg k "$childNode" --argjson dep "$dep" '.nodes[$k].locked = $dep.locked | .nodes[$k].original = $dep.original' \
      "$work" >"$work.new" && mv "$work.new" "$work"
  done
done

# Nix writes lock files with sorted keys and 2-space indentation.
jq -S --indent 2 . "$work" >"$lock"
