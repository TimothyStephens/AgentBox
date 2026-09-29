# shellcheck shell=bash
# common.sh — shared helpers for the agentbox spikes. Sourced, not run.
#
# Safety: everything a spike creates is named spike-* and/or labelled
# agentbox-spike=1, runs against a private podman API socket in a temp dir,
# and is removed on exit. Nothing here touches your existing containers,
# networks, volumes or systemd units.

set -uo pipefail

# ---- knobs (override with environment variables) -------------------------
SPIKE_LAN_TARGET="${SPIKE_LAN_TARGET:-10.0.0.103:8088}"   # a LAN service that answers from the host
SPIKE_MODEL_TARGET="${SPIKE_MODEL_TARGET:-10.0.0.2:8080}" # the local model server
SPIKE_IMAGE="${SPIKE_IMAGE:-docker.io/library/alpine:3.20}"
SPIKE_CURL_IMAGE="${SPIKE_CURL_IMAGE:-docker.io/curlimages/curl:latest}"

SPIKE_LABEL="agentbox-spike=1"
# shellcheck disable=SC2034  # used by the spike scripts
SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/agentbox-spike.XXXXXX")"
chmod 700 "$SPIKE_TMP"

PASS=0; FAIL=0; SKIP=0; INFO=0
_deferred=()

say()  { printf '%s\n' "$*"; }
hdr()  { printf '\n=== %s ===\n' "$*"; }
pass() { PASS=$((PASS+1)); printf '  PASS  %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf '  FAIL  %s\n' "$*"; }
skip() { SKIP=$((SKIP+1)); printf '  SKIP  %s\n' "$*"; }
info() { INFO=$((INFO+1)); printf '  INFO  %s\n' "$*"; }

# Register a cleanup command; they run in reverse order on exit.
defer() { _deferred+=("$*"); }

_on_exit() {
  # Containers first (they hold networks), then everything deferred, newest first.
  podman ps -aq --filter "label=$SPIKE_LABEL" 2>/dev/null | xargs -r podman rm -f >/dev/null 2>&1 || true
  local i
  for ((i=${#_deferred[@]}-1; i>=0; i--)); do
    eval "${_deferred[$i]}" >/dev/null 2>&1 || true
  done
  rm -rf "$SPIKE_TMP"
}
trap _on_exit EXIT

summary() {
  printf '\nRESULT %s: %d pass, %d fail, %d skip, %d info\n' "$1" "$PASS" "$FAIL" "$SKIP" "$INFO"
  [ "$FAIL" -eq 0 ]
}

need() { command -v "$1" >/dev/null 2>&1 || { say "missing required command: $1"; exit 2; }; }

# HTTP status of http://host:port/ as seen from the host itself ("000" = nothing answered).
host_code() {
  local c
  c=$(curl -s -o /dev/null -m 4 -w '%{http_code}' "http://$1/" 2>/dev/null)
  printf '%s' "${c:-000}"
}

# Raw TCP check from the host (for non-HTTP ports).
host_tcp_open() { timeout 3 bash -c "</dev/tcp/${1%:*}/${1##*:}" >/dev/null 2>&1; }

# A private podman API service on a temp socket. Your own podman.socket (if any) is untouched.
start_api() {
  API_SOCK="$SPIKE_TMP/podman.sock"
  podman system service --time=0 "unix://$API_SOCK" >"$SPIKE_TMP/api.log" 2>&1 &
  defer "kill $!"
  local _
  for _ in $(seq 1 60); do
    if [ -S "$API_SOCK" ] && curl -sf --unix-socket "$API_SOCK" http://d/_ping >/dev/null 2>&1; then
      return 0
    fi
    sleep 0.25
  done
  say "podman API service did not start:"; cat "$SPIKE_TMP/api.log"; exit 2
}

# podman in remote mode against a given unix socket.
premote() { local sock="$1"; shift; podman --remote --url "unix://$sock" "$@"; }

ensure_image() { podman image exists "$1" || podman pull -q "$1" >/dev/null; }

# net_create NAME [podman network create args...]
net_create() {
  local n="$1"; shift
  podman network rm -f "$n" >/dev/null 2>&1 || true
  podman network create --label "$SPIKE_LABEL" "$@" "$n" >/dev/null || { say "could not create network $n"; exit 2; }
  defer "podman network rm -f $n"
}

# Names of the networks a container is attached to, space separated.
ctr_nets() { podman inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$1" 2>/dev/null | xargs; }

wait_for_socket() { local _; for _ in $(seq 1 60); do [ -S "$1" ] && return 0; sleep 0.25; done; return 1; }
