#!/usr/bin/env python3
"""Check and bump the pinned Cardano binaries in EFOS (pkgs/cardano/*.nix).

  cardano-binaries.py status [repo]            current pin vs newest stable-looking tag
  cardano-binaries.py bump <pkg> <ver> [repo]  rewrite version + hashes, verify archive layout

Works without the GitHub API (blocked in Claude Code cloud sessions): tags come from
`git ls-remote`, release assets download from github.com/<repo>/releases/download, and the
Armada Alliance aarch64 node build is read from raw.githubusercontent.com (same bytes as the
github.com/.../raw/main/... URL in node-bin.nix).

A tag is not proof of a release: cardano-node tags pre-releases too. Confirm on the GitHub
releases page (WebFetch) before bumping.
"""
import json, re, subprocess, sys, tarfile, tempfile, zipfile
from pathlib import Path

ARMADA_REPO = "https://github.com/armada-alliance/cardano-node-binaries"
ARMADA_RAW = "https://raw.githubusercontent.com/armada-alliance/cardano-node-binaries/main/static-binaries"


def sh(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def tags(repo):
    out = sh("git", "ls-remote", "--tags", "--refs", f"https://github.com/{repo}")
    return [l.split("refs/tags/")[1] for l in out.splitlines()]


def vkey(v):
    return [int(x) for x in re.findall(r"\d+", v)]


def armada_node_builds():
    """{version: dirname} for Armada Alliance aarch64 cardano-node builds."""
    with tempfile.TemporaryDirectory() as d:
        sh("git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--no-checkout", ARMADA_REPO, d)
        names = sh("git", "-C", d, "ls-tree", "--name-only", "HEAD", "static-binaries/").split()
    builds = {}
    for n in names:
        m = re.fullmatch(r"static-binaries/(cardano-(\d+)_(\d+)_(\d+)-aarch64-static-musl-ghc_\d+)\.tar\.zst", n)
        if m:
            builds.setdefault(".".join(m.group(2, 3, 4)), []).append(m.group(1))
    return builds


def prefetch(url):
    """SRI hash of the file at url, and its local store path."""
    out = json.loads(sh("nix", "store", "prefetch-file", "--json", url))
    return out["hash"], out["storePath"]


def zstd(root):
    """zstd from the repo's own pinned nixpkgs (the container has none)."""
    out = sh("nix", "build", "--no-link", "--print-out-paths", "--inputs-from", str(root), "nixpkgs#zstd.bin")
    return out.split()[0] + "/bin/zstd"


def members(path, root):
    if zipfile.is_zipfile(path):
        return zipfile.ZipFile(path).namelist()
    if open(path, "rb").read(4) == b"\x28\xb5\x2f\xfd":  # zstd magic
        return sh("tar", f"--use-compress-program={zstd(root)} -d", "-tf", path).split("\n")
    return tarfile.open(path).getnames()


def require(path, names, label, root):
    have = {m.rstrip("/").removeprefix("./") for m in members(path, root)}
    missing = [n for n in names if not any(h == n or h.startswith(n + "/") for h in have)]
    if missing:
        sys.exit(f"ERROR: {label} archive is missing {missing}; installPhase would break. Inspect the release layout.")


# ---- per-package knowledge -------------------------------------------------------------------

def node_urls(v, armada_dir):
    return {
        "x86_64-linux": f"https://github.com/IntersectMBO/cardano-node/releases/download/{v}/cardano-node-{v}-linux-amd64.tar.gz",
        "aarch64-linux": f"https://github.com/armada-alliance/cardano-node-binaries/raw/main/static-binaries/{armada_dir}.tar.zst",
    }


def ogmios_urls(v):
    return {s: f"https://github.com/CardanoSolutions/ogmios/releases/download/v{v}/ogmios-v{v}-{a}-linux.zip"
            for s, a in (("x86_64-linux", "x86_64"), ("aarch64-linux", "aarch64"))}


def kupo_urls(v):
    mm = ".".join(v.split(".")[:2])
    return {s: f"https://github.com/CardanoSolutions/kupo/releases/download/v{mm}/kupo-v{v}-{a}-linux.zip"
            for s, a in (("x86_64-linux", "x86_64"), ("aarch64-linux", "aarch64"))}


def mithril_urls(v):
    return {s: f"https://github.com/input-output-hk/mithril/releases/download/{v}/mithril-{v}-linux-{a}.tar.gz"
            for s, a in (("x86_64-linux", "x64"), ("aarch64-linux", "arm64"))}


PKGS = {
    "cardano-node": dict(file="pkgs/cardano/node-bin.nix", repo="IntersectMBO/cardano-node",
                         tag=r"\d+\.\d+\.\d+", ver=lambda t: t),
    "ogmios": dict(file="pkgs/cardano/ogmios.nix", repo="CardanoSolutions/ogmios",
                   tag=r"v\d+\.\d+\.\d+", ver=lambda t: t[1:]),
    "kupo": dict(file="pkgs/cardano/kupo.nix", repo="CardanoSolutions/kupo",
                 tag=r"v\d+\.\d+\.\d+", ver=lambda t: t[1:]),
    "mithril-client": dict(file="pkgs/cardano/mithril-client.nix", repo="input-output-hk/mithril",
                           tag=r"\d{4}\.\d+", ver=lambda t: t),
}


def current(root, pkg):
    text = (root / PKGS[pkg]["file"]).read_text()
    v = re.search(r'version = "([^"]+)";', text).group(1)
    extra = {}
    if pkg == "cardano-node":
        extra["armadaDir"] = re.search(r'armadaDir = "([^"]+)";', text).group(1)
    return v, extra


def urls_for(pkg, v, extra):
    return {"cardano-node": lambda: node_urls(v, extra["armadaDir"]), "ogmios": lambda: ogmios_urls(v),
            "kupo": lambda: kupo_urls(v), "mithril-client": lambda: mithril_urls(v)}[pkg]()


def status(root):
    armada = armada_node_builds()
    rows = []
    for pkg, p in PKGS.items():
        cur, _ = current(root, pkg)
        vs = sorted({p["ver"](t) for t in tags(p["repo"]) if re.fullmatch(p["tag"], t)}, key=vkey)
        newer = [v for v in vs if vkey(v) > vkey(cur)]
        row = dict(package=pkg, current=cur, newer_tags=newer,
                   releases_page=f"https://github.com/{p['repo']}/releases")
        if newer:
            row["major_change"] = vkey(newer[-1])[0] != vkey(cur)[0] and pkg != "mithril-client"
        if pkg == "cardano-node":
            row["armada_aarch64_builds"] = sorted((v for v in armada if vkey(v) >= vkey(cur)), key=vkey)
        rows.append(row)
    print(json.dumps(rows, indent=2))


def bump(root, pkg, new):
    path = root / PKGS[pkg]["file"]
    text = path.read_text()
    old, extra = current(root, pkg)
    if old == new:
        sys.exit(f"{pkg} is already {new}")
    new_extra = {}
    if pkg == "cardano-node":
        dirs = armada_node_builds().get(new, [])
        if len(dirs) != 1:
            sys.exit(f"ERROR: Armada Alliance aarch64 builds for {new}: {dirs or 'none'}. Need exactly one; "
                     "do not bump cardano-node until the aarch64 build exists.")
        new_extra["armadaDir"] = dirs[0]
    old_urls, new_urls = urls_for(pkg, old, extra), urls_for(pkg, new, new_extra)

    hashes = {}
    for system, url in new_urls.items():
        fetch = url
        if pkg == "cardano-node" and system == "aarch64-linux":
            fetch = f"{ARMADA_RAW}/{new_extra['armadaDir']}.tar.zst"
        try:
            hashes[system], store = prefetch(fetch)
        except subprocess.CalledProcessError as e:
            sys.exit(f"ERROR: could not download {fetch}\n{e.stderr}\nThe asset name may have changed; check the releases page.")
        if pkg == "cardano-node":
            base = "bin" if system == "x86_64-linux" else new_extra["armadaDir"]
            need = [f"{base}/cardano-node", f"{base}/cardano-cli",
                    "share" if system == "x86_64-linux" else f"{base}/share"]
        else:
            need = {"ogmios": ["bin/ogmios"], "kupo": ["bin/kupo"], "mithril-client": ["mithril-client"]}[pkg]
        require(store, need, f"{pkg} {new} {system}", root)
        print(f"{pkg} {new} {system}: {hashes[system]}")

    # Rewrite the nix file: version, per-system hash, armadaDir.
    text = text.replace(f'version = "{old}";', f'version = "{new}";', 1)
    if pkg == "cardano-node":
        text = text.replace(f'armadaDir = "{extra["armadaDir"]}";', f'armadaDir = "{new_extra["armadaDir"]}";', 1)
        # one hash per fetchurl block: IntersectMBO (x86_64) first, armada (aarch64) second
        for system in ("x86_64-linux", "aarch64-linux"):
            m = re.search(rf"{system} = \{{.*?hash = \"(sha256-[^\"]+)\";", text, re.S)
            text = text[:m.start(1)] + hashes[system] + text[m.end(1):]
    else:
        for system in ("x86_64-linux", "aarch64-linux"):
            m = re.search(rf"{system} = (?:\{{[^}}]*?hash = )?\"(sha256-[^\"]+)\";", text, re.S)
            text = text[:m.start(1)] + hashes[system] + text[m.end(1):]
    path.write_text(text)

    # Keep scripts/prefetch-hashes.sh in step with the pins.
    pf = root / "scripts/prefetch-hashes.sh"
    ptext = pf.read_text()
    for system in old_urls:
        if old_urls[system] not in ptext:
            print(f"WARNING: {old_urls[system]} not found in scripts/prefetch-hashes.sh; update it by hand")
        ptext = ptext.replace(old_urls[system], new_urls[system])
    if pkg == "kupo":
        ptext = re.sub(r"tag 只有主次位 v[\d.]+，文件名带补丁位 v[\d.]+",
                       f"tag 只有主次位 v{'.'.join(new.split('.')[:2])}，文件名带补丁位 v{new}", ptext)
    pf.write_text(ptext)
    print(f"{pkg}: {old} -> {new} written to {PKGS[pkg]['file']} and scripts/prefetch-hashes.sh")


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["status"]:
        status(Path(args[1] if len(args) > 1 else "."))
    elif args[:1] == ["bump"] and len(args) >= 3 and args[1] in PKGS:
        bump(Path(args[3] if len(args) > 3 else "."), args[1], args[2])
    else:
        sys.exit(__doc__)
