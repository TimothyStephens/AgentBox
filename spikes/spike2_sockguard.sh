#!/usr/bin/env bash
# Spike 2 — does sockguard, in front of rootless podman 5.x, scope a client to one compose project?
#
# Two containers are labelled as two compose projects (spike-a, spike-b). Sockguard is
# configured to show only spike-a. The key question: is exec into spike-b refused?
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
need podman; need curl
hdr "Spike 2: sockguard label scoping on rootless podman"

SOCKGUARD_IMAGE="${SPIKE_SOCKGUARD_IMAGE:-docker.io/codeswhat/sockguard:latest}"
SOCKGUARD_ARGS="${SPIKE_SOCKGUARD_ARGS:-serve -c /config/sockguard.yaml}"
ALLOWED_BIND="$SPIKE_TMP/allowed-bind"; mkdir -p "$ALLOWED_BIND"

if ! podman image exists "$SOCKGUARD_IMAGE" && ! podman pull -q "$SOCKGUARD_IMAGE" >/dev/null 2>&1; then
  skip "could not pull $SOCKGUARD_IMAGE; set SPIKE_SOCKGUARD_IMAGE to a pinned sockguard image"
  summary spike2; exit $?
fi
ensure_image "$SPIKE_IMAGE"
start_api

# Two "projects" to tell apart.
podman run -d --name spike-a1 --label "$SPIKE_LABEL" --label io.podman.compose.project=spike-a \
  "$SPIKE_IMAGE" sh -c 'echo spike-a-log; sleep 600' >/dev/null
podman run -d --name spike-b1 --label "$SPIKE_LABEL" --label io.podman.compose.project=spike-b \
  "$SPIKE_IMAGE" sh -c 'echo spike-b-log; sleep 600' >/dev/null

SG_DIR="$SPIKE_TMP/sg"; mkdir -p "$SG_DIR"
cat >"$SPIKE_TMP/sockguard.yaml" <<EOF
listen:
  socket: /run/sg/sockguard.sock
upstream:
  socket: /run/podman/podman.sock
  flavor: podman
insecure_allow_read_exfiltration: true
response:
  visible_resource_labels:
    - io.podman.compose.project=spike-a
request_body:
  exec:
    allow_root_user: true
    allowed_commands:
      - ["echo", "spike-exec-ok"]
  libpod_container_create:
    allowed_bind_mounts:
      - $ALLOWED_BIND
rules:
  - match: { method: GET, path: "/**" }
    action: allow
  - match: { method: HEAD, path: "/**" }
    action: allow
  - match: { method: POST, path: "/libpod/containers/*/exec" }
    action: allow
  - match: { method: POST, path: "/libpod/exec/*/start" }
    action: allow
  - match: { method: POST, path: "/libpod/exec/*/resize" }
    action: allow
  - match: { method: POST, path: "/libpod/exec/*/remove" }
    action: allow
  - match: { method: POST, path: "/libpod/containers/create" }
    action: allow
  - match: { method: POST, path: "/libpod/images/pull" }
    action: allow
  - match: { method: DELETE, path: "/libpod/containers/*" }
    action: allow
  - match: { method: "*", path: "/**" }
    action: deny
EOF

SG_RUN=(podman run --label "$SPIKE_LABEL" --userns=keep-id --user "$(id -u):$(id -g)" --network none
        --security-opt label=disable
        -v "$API_SOCK:/run/podman/podman.sock" -v "$SG_DIR:/run/sg"
        -v "$SPIKE_TMP/sockguard.yaml:/config/sockguard.yaml:ro")

# Validate first, so a config or CLI mismatch shows up clearly.
if "${SG_RUN[@]}" --rm "$SOCKGUARD_IMAGE" validate -c /config/sockguard.yaml >"$SPIKE_TMP/validate.log" 2>&1; then
  info "sockguard accepted the config"
else
  fail "sockguard rejected the config or the CLI differs (set SPIKE_SOCKGUARD_ARGS):"
  sed 's/^/    /' "$SPIKE_TMP/validate.log" | tail -20
  summary spike2; exit $?
fi

# shellcheck disable=SC2086  # SOCKGUARD_ARGS is intentionally word-split
"${SG_RUN[@]}" -d --name spike-sockguard "$SOCKGUARD_IMAGE" $SOCKGUARD_ARGS >/dev/null
SG="$SG_DIR/sockguard.sock"
if ! wait_for_socket "$SG"; then
  fail "sockguard did not open $SG:"; podman logs spike-sockguard 2>&1 | tail -20 | sed 's/^/    /'
  summary spike2; exit $?
fi

sg() { premote "$SG" "$@"; }

# The reason sockguard logged for its most recent denial.
sg_last_reason() {
  podman logs spike-sockguard 2>&1 | grep '"request_denied"' | tail -1 | python3 -c '
import json, sys
line = sys.stdin.read().strip()
try:
    d = json.loads(line)
    print(d.get("reason") or d.get("reason_code") or "?", "|", d.get("normalized_path", "?"))
except Exception:
    print("?")'
}

# Raw API call: api SOCKET METHOD PATH [JSON] -> body in $API_OUT, status in $API_CODE
api() {
  local data=()
  [ -n "${4:-}" ] && data=(-H 'Content-Type: application/json' -d "$4")
  API_CODE=$(curl -s --unix-socket "$1" -X "$2" "${data[@]}" --max-time 10 \
    -o "$SPIKE_TMP/api.out" -w '%{http_code}' "http://d/v5.0.0$3" 2>/dev/null)
  API_OUT=$(tr -d '\000' <"$SPIKE_TMP/api.out" 2>/dev/null)
}

# --- list ---------------------------------------------------------------------
names=$(sg ps -a --format '{{.Names}}' 2>"$SPIKE_TMP/ps.err")
if [ -z "$names" ] && [ -s "$SPIKE_TMP/ps.err" ]; then
  fail "podman ps through sockguard failed: $(tr '\n' ' ' <"$SPIKE_TMP/ps.err" | cut -c1-200)"
else
  echo "$names" | grep -qx spike-a1 && pass "list shows spike-a1" || fail "list does not show spike-a1"
  echo "$names" | grep -qx spike-b1 && fail "list shows spike-b1 (not scoped)" || pass "list hides spike-b1"
fi

# --- read -----------------------------------------------------------------------
sg inspect spike-b1 >/dev/null 2>&1 && fail "inspect spike-b1 allowed" || pass "inspect spike-b1 refused"
sg logs spike-b1 2>/dev/null | grep -q spike-b-log && fail "logs spike-b1 allowed" || pass "logs spike-b1 refused"
sg logs spike-a1 2>/dev/null | grep -q spike-a-log && pass "logs spike-a1 allowed" || fail "logs spike-a1 refused (visible project)"

# --- exec: the key question ---------------------------------------------------
out=$(sg exec spike-a1 echo spike-exec-ok 2>"$SPIKE_TMP/exa.err")
[ "$out" = "spike-exec-ok" ] && pass "exec into spike-a1 (allowed command) works" \
  || fail "exec into spike-a1 failed: $(tr '\n' ' ' <"$SPIKE_TMP/exa.err" | cut -c1-200)"
sg exec spike-a1 sh -c id >/dev/null 2>&1 && fail "exec of a non-allowlisted command allowed" || pass "exec of a non-allowlisted command refused"
out=$(sg exec spike-b1 echo spike-exec-ok 2>/dev/null)
[ "$out" = "spike-exec-ok" ] && fail "exec into spike-b1 via the podman client ALLOWED" \
  || pass "exec into spike-b1 via the podman client refused"

# --- exec through the raw API ---------------------------------------------------
# The podman client gives up when the follow-up inspect is hidden. A hostile client
# can skip that step, so create the exec session and start it directly.
EXEC_BODY='{"AttachStdout":true,"AttachStderr":true,"Cmd":["echo","spike-exec-ok"]}'
raw_exec() { # socket container -> sets RAW_CREATE, RAW_START, RAW_RAN (yes|no), RAW_ID
  RAW_RAN=no; RAW_START=n/a; RAW_ID=""
  api "$1" POST "/libpod/containers/$2/exec" "$EXEC_BODY"; RAW_CREATE=$API_CODE
  RAW_ID=$(printf '%s' "$API_OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("Id",""))
except Exception: pass' 2>/dev/null)
  [ -n "$RAW_ID" ] || return 0
  raw_start "$1" "$RAW_ID"
}
raw_start() { # socket exec-id -> sets RAW_START, RAW_RAN
  api "$1" POST "/libpod/exec/$2/start" '{"Detach":false,"Tty":false}'
  RAW_START=$API_CODE
  printf '%s' "$API_OUT" | grep -q spike-exec-ok && RAW_RAN=yes || RAW_RAN=no
}

say ""; say "  -- sockguard alone, raw API"
raw_exec "$SG" spike-a1
[ "$RAW_RAN" = yes ] && pass "raw exec into spike-a1 runs (sanity check)" \
  || info "raw exec into spike-a1 did not run (create $RAW_CREATE, start $RAW_START); lines below are inconclusive"
raw_exec "$SG" spike-b1
B1_EXEC_ID=$RAW_ID
if [ "$RAW_RAN" = yes ]; then
  info "finding: sockguard alone lets a raw client exec into spike-b1 (create $RAW_CREATE, start $RAW_START)"
else
  info "sockguard alone refused raw exec into spike-b1 (create $RAW_CREATE, start $RAW_START)"
fi

say ""; say "  -- gate in front of sockguard (agent -> gate -> sockguard -> podman)"
GATE="$SPIKE_TMP/gate.sock"
python3 "$SPIKE_DIR/spike_gate.py" --listen "$GATE" --upstream "$SG" --network spike-unused \
  --exec-scope io.podman.compose.project=spike-a --label "$SPIKE_LABEL" >"$SPIKE_TMP/gate.log" 2>&1 &
defer "kill $!"
wait_for_socket "$GATE" || { fail "gate did not start"; cat "$SPIKE_TMP/gate.log"; summary spike2; exit $?; }

raw_exec "$GATE" spike-a1
[ "$RAW_RAN" = yes ] && pass "raw exec into spike-a1 through the gate runs" \
  || fail "raw exec into spike-a1 through the gate did not run (create $RAW_CREATE, start $RAW_START)"
out=$(premote "$GATE" exec spike-a1 echo spike-exec-ok 2>"$SPIKE_TMP/gexa.err")
[ "$out" = "spike-exec-ok" ] && pass "podman exec into spike-a1 through the gate works" \
  || fail "podman exec into spike-a1 through the gate failed: $(tr '\n' ' ' <"$SPIKE_TMP/gexa.err" | cut -c1-160)"
raw_exec "$GATE" spike-b1
if [ "$RAW_RAN" = yes ]; then
  fail "KEY: raw exec into spike-b1 through the gate RAN (create $RAW_CREATE, start $RAW_START)"
elif [ "$RAW_CREATE" = 403 ]; then
  pass "KEY: raw exec into spike-b1 through the gate refused at creation (403)"
else
  pass "KEY: raw exec into spike-b1 through the gate did not run (create $RAW_CREATE, start $RAW_START)"
fi
if [ -n "${B1_EXEC_ID:-}" ]; then
  raw_start "$GATE" "$B1_EXEC_ID"
  [ "$RAW_RAN" = yes ] && fail "starting an exec session made outside the gate RAN" \
    || pass "starting an exec session made outside the gate refused ($RAW_START)"
fi

# --- create: bind mounts --------------------------------------------------------
# Image pulls are allowed now (podman's client pulls even with --pull=never), so a
# refusal here must come from the bind-mount policy. --systemd=false: sockguard refuses
# podman's default systemd=true, which would also mask the real reason.
CREATE=(sg create --pull=never --systemd=false --label "$SPIKE_LABEL")
if "${CREATE[@]}" -v "$HOME:/h" "$SPIKE_IMAGE" true >/dev/null 2>&1; then
  fail "create with -v \$HOME:/h allowed"
else
  r=$(sg_last_reason)
  case "$r" in
    *bind*|*mount*) pass "create with -v \$HOME:/h refused by the bind-mount policy ($r)" ;;
    *) fail "create with -v \$HOME:/h refused, but not by the bind-mount policy ($r)" ;;
  esac
fi
if "${CREATE[@]}" -v "$ALLOWED_BIND:/d" "$SPIKE_IMAGE" true >/dev/null 2>"$SPIKE_TMP/cr.err"; then
  pass "create with an allowlisted bind mount works"
else
  fail "create with an allowlisted bind mount refused ($(sg_last_reason))"
fi

say ""; say "  gate log:"; grep -e '^EXEC' -e '^DENY' -e '^ERROR' "$SPIKE_TMP/gate.log" | sed 's/^/    /'
say ""; say "  sockguard log (last 20 lines):"; podman logs spike-sockguard 2>&1 | tail -20 | sed 's/^/    /'
summary spike2
