#!/usr/bin/env bash
# Profile `ab` on the projects tests/perf/mkproj.sh made. Host only; run by Tim.
#
#   ABPERF_RUNS   rounds; each round runs every scenario once, interleaved (default 3)
#   ABPERF_OUT    results directory (default ~/repos/agentbox-dev/perf-results/<timestamp>)
#   ABPERF_ONLY   space-separated scenario names to run (default: all)
#
# Every `ab` call runs under cProfile, so where Python waits on podman, git or rsync shows up under the
# calling function, and ab.log gives each podman call's own duration. A session runs a bash shell with
# --no-tty instead of an agent, so no model call is in the timing.
set -uo pipefail
if [ -n "${AGENTBOX_SESSION:-}" ]; then echo "host only: run this on the host" >&2; exit 2; fi
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
RUNS=${ABPERF_RUNS:-3}
OUT=${ABPERF_OUT:-$HOME/repos/agentbox-dev/perf-results/$(date +%Y%m%d-%H%M%S)}
DATA=${AB_DATA_DIR:-$HOME/.local/share/agentbox}
ABLOG=$DATA/logs/ab.log
IMG=docker.io/library/busybox:1.36
mkdir -p "$OUT"

# Run inside the session. Each step prints `INSESSION <name> <ms>` so the analysis can time it.
T='t() { local n=$1; shift; local s=$(date +%s%N); "$@" >/dev/null 2>&1; local rc=$?; echo "INSESSION $n $(( ($(date +%s%N)-s)/1000000 )) rc=$rc"; }'
DEV="$T; for i in 1 2 3; do t podman-run podman run --rm $IMG true; done; t podman-build podman build -t localhost/abperf-TAG:1 -f Containerfile.perf .; t podman-images podman images"
CHURN="$T; t churn sh -c 'find src -name \"*.txt\" | head -n 2000 | while read f; do echo x >> \"\$f\"; done'"

# name | ab arguments | command run in the session's shell (empty: not a session)
SCENARIOS=(
  "small|session start claude abperf-small --shell --no-tty|true"
  "small-dev|session start claude abperf-small-dev --shell --no-tty|$DEV"
  "big|session start claude abperf-big --shell --no-tty|$CHURN"
  "big-dev|session start claude abperf-big-dev --shell --no-tty|$DEV; $CHURN"
  "big-clone|session start claude abperf-big-clone --shell --no-tty|true"
  "project-check-big|project check abperf-big|"
  "session-ls|session ls|"
  "selftest|project selftest claude abperf-small-dev|"
)

{ echo "host $(hostname)  $(date -Is)"; echo "commit $(git -C "$REPO" rev-parse --short HEAD)"; podman --version
  python3 --version; nproc; free -m | sed -n 2p; df -h "$HOME" | tail -1; } > "$OUT/host.txt" 2>&1

for round in $(seq 1 "$RUNS"); do
  for sc in "${SCENARIOS[@]}"; do
    IFS='|' read -r name args cmd <<< "$sc"
    cmd=${cmd//TAG/$name}   # one build tag per project: the gate refuses a name another project built
    if [ -n "${ABPERF_ONLY:-}" ] && [[ " $ABPERF_ONLY " != *" $name "* ]]; then continue; fi
    if [ "$name" = selftest ] && [ "$round" -gt 1 ]; then continue; fi   # minutes long; once is enough
    base="$OUT/$name.$round"
    echo "== round $round: $name"
    logsize=$(stat -c %s "$ABLOG" 2>/dev/null || echo 0)
    touch "$base.marker"
    # shellcheck disable=SC2086
    if [ -n "$cmd" ]; then
      argv=($args -- -c "$cmd")
    else
      argv=($args)
    fi
    t0=$EPOCHREALTIME
    PYTHONPATH="$REPO" python3 -m cProfile -o "$base.prof" -m agentbox "${argv[@]}" > "$base.out" 2>&1
    t1=$EPOCHREALTIME
    # cProfile swallows the SystemExit `ab` raises, so its exit status says nothing: read the output instead.
    # A refused or failed `ab` prints `ab: ...`; a failed step inside the session prints INSESSION ... rc!=0.
    if grep -q '^ab: ' "$base.out"; then status=ab-error
    elif grep '^INSESSION ' "$base.out" | grep -qv ' rc=0$'; then status=step-failed
    else status=ok; fi
    echo "$name $round status=$status wall=$(python3 -c "print(round($t1-$t0,3))")" | tee -a "$OUT/walls.txt"
    [ "$status" = ok ] || { echo "   see $base.out"; grep -m3 '^ab: \|^INSESSION .* rc=[^0]' "$base.out" | sed 's/^/   /'; }
    tail -c +"$((logsize + 1))" "$ABLOG" > "$base.ablog" 2>/dev/null || true
    mkdir -p "$base.logs"
    find "$DATA/logs" -maxdepth 1 -type f -newer "$base.marker" ! -name ab.log -exec cp -t "$base.logs" {} + 2>/dev/null
    rm -f "$base.marker"
    # clone mode keeps each session's clone for the owner; these are throwaway
    if [[ "$args" == *abperf-big-clone* ]]; then PYTHONPATH="$REPO" python3 -m agentbox project clean abperf-big-clone --yes >/dev/null 2>&1; fi
  done
done
echo "== results in $OUT"
echo "   summary: python3 $HERE/analyze.py $OUT"
python3 "$HERE/analyze.py" "$OUT" > "$OUT/summary.md" && echo "   written: $OUT/summary.md"
