#!/usr/bin/env bash
# Every `--format` agentbox issues, against the real podman on this host, in one shot.
#
# Why: `podman network ls --format '{{.Name}} {{index .Labels "agentbox.session"}}'` is rejected by podman
# 5.7 ("cannot index slice/array with type string"). The call was swallowed, so the feature had never
# worked, and the unit suite could not tell because the fake podman answered any template with the same
# canned string. A template is a contract with podman's internals per subcommand -- `index .Labels` is fine
# on `image inspect` and refused on `network ls` -- so the only real check is running them.
#
# READ-ONLY by construction: this script only lists and inspects. It creates, starts, stops and removes
# nothing, touches no live service, and needs no agentbox session. Safe to run at any time.
#
#   ./tests/manual/podman_formats.sh            # uses whatever objects already exist
#   PODMAN=/usr/bin/podman ./tests/manual/podman_formats.sh
#   AB_FMT_NET=n AB_FMT_CTR=c AB_FMT_IMG=i ./tests/manual/podman_formats.sh   # inspect THESE
#
# Exit 0 if every format podman was asked for worked; 1 if any was rejected. A check SKIPs when the host has
# no object of that kind to inspect -- a skip is not a pass, and the summary says so.
#
# AB_FMT_NET / AB_FMT_CTR / AB_FMT_IMG name the objects the inspect checks run against. Run by hand they
# are unset and the script takes whatever the host happens to have, which is why it used to skip on an idle
# machine and could assert so little. `make itest` creates its own abt-<run> network, container and image
# reference and passes them in, so every check has a target and a skip there is a failure
# (tests/integration/test_podman_formats.py asserts skip=0).
#
# tests/unit/test_podman_formats.py asserts every `--format` literal in agentbox/ appears in this file, so
# the two cannot drift.

set -uo pipefail
PODMAN=${PODMAN:-podman}
pass=0 fail=0 skip=0
failed_list=()

echo "podman: $($PODMAN --version 2>&1)"
echo

# check <label> <format> <argv...>   -- runs `podman <argv...> --format <format>`
check() {
  local label=$1 fmt=$2; shift 2
  local out rc
  out=$("$PODMAN" "$@" --format "$fmt" 2>&1); rc=$?
  if [ $rc -eq 0 ]; then
    pass=$((pass + 1))
    printf 'PASS  %-34s %s\n' "$label" "$fmt"
    [ -n "${VERBOSE:-}" ] && printf '        -> %s\n' "$(echo "$out" | head -c 160)"
  else
    fail=$((fail + 1))
    failed_list+=("$label: --format $fmt")
    printf 'FAIL  %-34s %s\n' "$label" "$fmt"
    printf '        rc=%s %s\n' "$rc" "$(echo "$out" | head -3)"
  fi
}

skip() {
  skip=$((skip + 1))
  printf 'SKIP  %-34s %s\n' "$1" "$2"
}

# --- listings: no target needed ------------------------------------------------------------------
# agentbox/podman.py: ids() and containers_json()/networks_json()
check "ps -a (names)"        '{{.Names}}'                                   ps -a
check "ps -a (json)"         'json'                                         ps -a
check "network ls (name)"    '{{.Name}}'                                    network ls
check "network ls (json)"    'json'                                         network ls
check "pod ps (name)"        '{{.Name}}'                                    pod ps
check "volume ls (name)"     '{{.Name}}'                                    volume ls
# agentbox/session.py: image gc
check "images (json)"        'json'                                         images

# The template that started this. It must FAIL on 5.7; if a later podman accepts it, that is worth knowing,
# but nothing in agentbox may go back to it (the unit suite forbids it).
echo
out=$("$PODMAN" network ls --format '{{.Name}} {{index .Labels "agentbox.session"}}' 2>&1)
if [ $? -eq 0 ]; then
  echo "NOTE  this podman ACCEPTS 'index .Labels' on network ls (5.7 did not). agentbox stays on json."
else
  echo "NOTE  'index .Labels' on network ls is rejected here, as expected:"
  echo "        $(echo "$out" | head -1)"
fi
echo

# --- inspects: need a target, so use whatever already exists -------------------------------------
net=${AB_FMT_NET:-$("$PODMAN" network ls --format '{{.Name}}' 2>/dev/null | head -1)}
ctr=${AB_FMT_CTR:-$("$PODMAN" ps -a --format '{{.Names}}' 2>/dev/null | head -1)}
img=${AB_FMT_IMG:-$("$PODMAN" images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null | grep -v '<none>' | head -1)}

# agentbox/session.py: the egress/internal network's gateway and subnet
if [ -n "$net" ]; then
  check "network inspect (gateway)" '{{range .Subnets}}{{.Gateway}} {{end}}'  network inspect "$net"
  check "network inspect (subnet)"  '{{range .Subnets}}{{.Subnet}} {{end}}'   network inspect "$net"
else
  skip "network inspect (gateway)" 'no network on this host'
  skip "network inspect (subnet)"  'no network on this host'
fi

# agentbox/session.py: the proxy's address on the session network
if [ -n "$ctr" ]; then
  check "inspect (networks json)" '{{json .NetworkSettings.Networks}}'        inspect "$ctr"
else
  skip "inspect (networks json)" 'no container on this host'
fi

# agentbox/podman.py: image_json(), which `ab image build` and the CLI-version check both read. The label
# used to be fetched with `{{index .Labels "..."}}` -- a template that works here but is exactly the kind
# this script exists to doubt, so both readers go through --format json now.
if [ -n "$img" ]; then
  check "image inspect (json)" 'json'                                        image inspect "$img"
  # Kept as a NOTE, not a check: nothing in agentbox issues it any more, but if a future podman stops
  # accepting `index .Labels` here too, that is worth seeing before something reaches for it again.
  if "$PODMAN" image inspect "$img" --format '{{index .Labels "agentbox.cli-version"}}' >/dev/null 2>&1; then
    echo "NOTE  'index .Labels' still works on image inspect (agentbox reads json regardless)"
  else
    echo "NOTE  'index .Labels' is now REJECTED on image inspect too; agentbox already reads json"
  fi
else
  skip "image inspect (json)" 'no tagged image on this host'
fi

# agentbox/podman.py: image_exists() answers by EXIT STATUS, not a format -- 0 yes, 1 no, anything else is
# neither and raises. Checked here because this script is the only place that asks real podman anything.
if [ -n "$img" ]; then
  "$PODMAN" image exists "$img"; rc=$?
  if [ $rc -eq 0 ]; then
    pass=$((pass + 1)); printf 'PASS  %-34s %s\n' "image exists (present)" "exit 0"
  else
    fail=$((fail + 1)); failed_list+=("image exists: an image that IS present exited $rc")
    printf 'FAIL  %-34s exit %s\n' "image exists (present)" "$rc"
  fi
  "$PODMAN" image exists localhost/agentbox-no-such-image:abt 2>/dev/null; rc=$?
  if [ $rc -eq 1 ]; then
    pass=$((pass + 1)); printf 'PASS  %-34s %s\n' "image exists (absent)" "exit 1"
  else
    fail=$((fail + 1)); failed_list+=("image exists: an absent image exited $rc, not 1")
    printf 'FAIL  %-34s exit %s\n' "image exists (absent)" "$rc"
  fi
else
  skip "image exists (present)" 'no tagged image on this host'
  skip "image exists (absent)"  'no tagged image on this host'
fi

echo
echo "pass=$pass fail=$fail skip=$skip"
if [ ${#failed_list[@]} -gt 0 ]; then
  echo
  echo "podman rejected:"
  for f in "${failed_list[@]}"; do echo "  - $f"; done
  echo
  echo "Each of these is a command agentbox issues. A rejected format is a feature that cannot work;"
  echo "fix it by reading --format json in Python, as sessions() now does for network ls."
  exit 1
fi
[ "$skip" -gt 0 ] && echo "(skips are untested, not passing: run again when such an object exists)"
exit 0
