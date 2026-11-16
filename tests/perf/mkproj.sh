#!/usr/bin/env bash
# Build and register the projects tests/perf/run.sh profiles. Host only; run by Tim.
#
#   ABPERF_DIR    where the project roots go        (default ~/repos/abperf; must not overlap a project)
#   ABPERF_FILES  text files in the big tree        (default 20000)
#   ABPERF_BLOBS  1 MiB binary files in the big tree, git-ignored (default 100)
#   ABPERF_COMMITS commits of history in the big tree (default 100)
#
# Five projects, all --no-toulmin (Toulmin's own cost is not under test):
#   abperf-small       a 40-file repo, preset script (no podman access)       -> the fixed cost of a session
#   abperf-small-dev   the same, preset build (dev tier: sockguard + gate)    -> what the dev tier adds
#   abperf-big         the big tree, preset script                            -> what grant size costs
#   abperf-big-dev     the big tree, preset build                             -> big tree + a build context
#   abperf-big-clone   the big tree, preset script, --mount clone             -> what clone mode costs
# Remove them with: ab project rm abperf-small ...; ab project purge <name> --yes; rm -rf "$ABPERF_DIR".
set -euo pipefail
if [ -n "${AGENTBOX_SESSION:-}" ]; then echo "host only: run this on the host" >&2; exit 2; fi
DIR=${ABPERF_DIR:-$HOME/repos/abperf}
FILES=${ABPERF_FILES:-20000}
BLOBS=${ABPERF_BLOBS:-100}
COMMITS=${ABPERF_COMMITS:-100}
IMG=docker.io/library/busybox:1.36
if [ -e "$DIR" ]; then echo "$DIR exists; remove it or set ABPERF_DIR" >&2; exit 2; fi
mkdir -p "$DIR"

mkrepo() {  # mkrepo <dir> <files> <blobs> <commits>
  local d=$1
  mkdir -p "$d"
  python3 - "$d" "$2" "$3" <<'PY'
import os, random, sys
d, files, blobs = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rnd = random.Random(42)
words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma".split()
for i in range(files):
    sub = os.path.join(d, "src", f"pkg{i % 50:02d}", f"mod{i % 7}")
    os.makedirs(sub, exist_ok=True)
    with open(os.path.join(sub, f"f{i:06d}.txt"), "w") as f:
        for _ in range(rnd.randint(10, 80)):
            f.write(" ".join(rnd.choice(words) for _ in range(8)) + "\n")
os.makedirs(os.path.join(d, "assets"), exist_ok=True)
for i in range(blobs):
    with open(os.path.join(d, "assets", f"blob{i:04d}.bin"), "wb") as f:
        f.write(rnd.randbytes(1 << 20))
with open(os.path.join(d, "Containerfile.perf"), "w") as f:
    f.write("FROM docker.io/library/busybox:1.36\nRUN true\n")
with open(os.path.join(d, ".gitignore"), "w") as f:
    f.write("assets/\n")   # untracked bulk, as build outputs and datasets are: rsync copies it, git does not
PY
  git -C "$d" init -q -b main
  git -C "$d" add -A
  git -C "$d" -c user.name=abperf -c user.email=abperf@example.invalid commit -q -m "initial tree"
  local n=1
  while [ "$n" -lt "$4" ]; do
    # Each commit touches 20 text files, so the history has real depth for history and scan reads.
    find "$d/src" -name '*.txt' | sort | sed -n "$(( (n * 20) % ${2} + 1 )),+19p" | while read -r f; do
      echo "change $n" >> "$f"; done
    git -C "$d" add -A
    git -C "$d" -c user.name=abperf -c user.email=abperf@example.invalid commit -q -m "change $n"
    n=$((n + 1))
  done
}

echo "== building the trees in $DIR (big: $FILES files, $BLOBS MiB of blobs, $COMMITS commits)"
mkrepo "$DIR/small" 40 0 5
cp -a "$DIR/small" "$DIR/small-dev"
mkrepo "$DIR/big" "$FILES" "$BLOBS" "$COMMITS"
cp -a "$DIR/big" "$DIR/big-dev"
cp -a "$DIR/big" "$DIR/big-clone"
du -sh "$DIR"/*

echo "== registering"
ab project add abperf-small     "$DIR/small"     --preset script --no-toulmin --mount checkout
ab project add abperf-small-dev "$DIR/small-dev" --preset build  --no-toulmin --mount checkout --dev-image "$IMG"
ab project add abperf-big       "$DIR/big"       --preset script --no-toulmin --mount checkout
ab project add abperf-big-dev   "$DIR/big-dev"   --preset build  --no-toulmin --mount checkout --dev-image "$IMG"
ab project add abperf-big-clone "$DIR/big-clone" --preset script --no-toulmin --mount clone
echo "== done; now: tests/perf/run.sh"
