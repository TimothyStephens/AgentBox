#!/usr/bin/env bash
# Spike 5 — does podman-compose pass `isolate` through to the network, and does it isolate?
#
# Three throwaway compose projects:
#   spike5a  web server, network with driver_opts isolate: "strict"
#   spike5b  probe client, plain network
#   spike5c  web server, plain network (baseline: are plain networks reachable from each other?)
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
need podman
hdr "Spike 5: podman-compose network isolation"

if ! command -v podman-compose >/dev/null 2>&1; then
  skip "podman-compose not installed"; summary spike5; exit $?
fi
# Alpine's busybox has no httpd (it moved to busybox-extras); the busybox image has it.
WEB_IMAGE="${SPIKE_WEB_IMAGE:-docker.io/library/busybox:1.36}"
ensure_image "$SPIKE_IMAGE"
ensure_image "$WEB_IMAGE"

WEB_CMD='mkdir -p /www && echo spike5 > /www/index.html && exec httpd -f -p 8080 -h /www'

mkproj() { # name isolate(yes|no) role(web|probe)
  local dir="$SPIKE_TMP/$1"; mkdir -p "$dir"
  local cmd image
  if [ "$3" = web ]; then cmd="[\"sh\", \"-c\", \"$WEB_CMD\"]"; image=$WEB_IMAGE
  else cmd='["sleep", "600"]'; image=$SPIKE_IMAGE; fi
  {
    echo "services:"
    echo "  $3:"
    echo "    image: $image"
    echo "    command: $cmd"
    echo "    labels:"
    echo "      - $SPIKE_LABEL"
    if [ "$2" = yes ]; then
      echo "networks:"
      echo "  default:"
      echo "    driver_opts:"
      echo "      isolate: \"strict\""
    fi
  } >"$dir/compose.yml"
  # deferred commands run newest first: compose down, then the network
  defer "podman network rm -f ${1}_default"
  defer "podman-compose -p $1 -f $dir/compose.yml down"
  if ! podman-compose -p "$1" -f "$dir/compose.yml" up -d >"$SPIKE_TMP/$1.log" 2>&1; then
    fail "podman-compose up for $1 failed: $(tail -5 "$SPIKE_TMP/$1.log" | tr '\n' ' ' | cut -c1-300)"
    return 1
  fi
}

ctr_of() { podman ps -q --filter "label=io.podman.compose.project=$1" | head -1; }
# Explain a missing container: exited, and why.
why_missing() {
  local id
  id=$(podman ps -aq --filter "label=io.podman.compose.project=$1" | head -1)
  if [ -z "$id" ]; then printf '%s: no container created' "$1"; return; fi
  printf '%s: %s; logs: %s' "$1" "$(podman inspect -f '{{.State.Status}} (exit {{.State.ExitCode}})' "$id" 2>/dev/null)" \
    "$(podman logs --tail 3 "$id" 2>&1 | tr '\n' ' ' | cut -c1-120)"
}
# podman-compose puts containers in a pod by default; they then share the pod's
# network and have no IP of their own, so fall back to the pod's infra container.
ip_of() {
  local ip pod infra
  ip=$(podman inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$1" | awk '{print $1}')
  if [ -z "$ip" ]; then
    pod=$(podman inspect -f '{{.Pod}}' "$1" 2>/dev/null)
    if [ -n "$pod" ]; then
      infra=$(podman pod inspect -f '{{.InfraContainerID}}' "$pod" 2>/dev/null)
      [ -n "$infra" ] && ip=$(podman inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "$infra" | awk '{print $1}')
    fi
  fi
  printf '%s' "$ip"
}

mkproj spike5a yes web   || { summary spike5; exit $?; }
mkproj spike5b no  probe || { summary spike5; exit $?; }
mkproj spike5c no  web   || { summary spike5; exit $?; }
sleep 2

opts=$(podman network inspect spike5a_default --format '{{.Options}}' 2>/dev/null)
echo "$opts" | grep -q "isolate:strict" && pass "spike5a_default has isolate=strict ($opts)" \
  || fail "spike5a_default options: '$opts' (isolate not passed through)"

A=$(ctr_of spike5a); B=$(ctr_of spike5b); C=$(ctr_of spike5c)
if [ -z "$A" ] || [ -z "$B" ] || [ -z "$C" ]; then
  fail "not all three containers are running"
  [ -z "$A" ] && info "$(why_missing spike5a)"
  [ -z "$B" ] && info "$(why_missing spike5b)"
  [ -z "$C" ] && info "$(why_missing spike5c)"
  summary spike5; exit $?
fi
A_IP=$(ip_of "$A"); C_IP=$(ip_of "$C")
if [ -z "$A_IP" ] || [ -z "$C_IP" ]; then
  fail "could not find container IPs (a='$A_IP' c='$C_IP')"; summary spike5; exit $?
fi

if podman exec "$B" wget -q -T 3 -O - "http://$A_IP:8080/" 2>/dev/null | grep -q spike5; then
  fail "spike5b reached the isolated spike5a web server ($A_IP)"
else
  pass "spike5b cannot reach the isolated spike5a web server ($A_IP)"
fi

if podman exec "$B" wget -q -T 3 -O - "http://$C_IP:8080/" 2>/dev/null | grep -q spike5; then
  info "baseline: two plain networks CAN reach each other (spike5b -> spike5c $C_IP); isolation must be set explicitly"
else
  info "baseline: two plain networks could NOT reach each other here (spike5b -> spike5c $C_IP)"
fi

summary spike5
