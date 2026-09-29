#!/usr/bin/env bash
# Spike 1 — can a thin host-side gate pin containers and builds to one network and block port publishing?
#
# Confirms the gap first (default podman networking reaches your LAN), then repeats the
# same actions through spike_gate.py, which rewrites requests on their way to podman.
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
need podman; need python3; need curl
hdr "Spike 1: host-side gate (network pinning, builds, port publishing)"

NET=spike-gate-int      # the pinned network: internal, no route out
BRIDGE=spike-gate-bridge # an ordinary bridge network, like a compose project's default
if [ "$(host_code "$SPIKE_LAN_TARGET")" = "000" ]; then
  skip "LAN target $SPIKE_LAN_TARGET does not answer from the host; set SPIKE_LAN_TARGET to a live host:port"
  summary spike1; exit $?
fi

ensure_image "$SPIKE_IMAGE"
net_create "$NET" --internal
net_create "$BRIDGE"
start_api

start_gate() { # name socket build-network build-strategy [template-file]
  local name=$1 sock=$2 bnet=$3 strat=$4 tmpl=${5:-}
  local args=(--listen "$sock" --upstream "$API_SOCK" --network "$NET" --build-network "$bnet"
              --build-strategy "$strat" --label "$SPIKE_LABEL")
  [ -n "$tmpl" ] && args+=(--build-template "$tmpl")
  python3 "$SPIKE_DIR/spike_gate.py" "${args[@]}" >>"$SPIKE_TMP/gate-$name.log" 2>&1 &
  defer "kill $!"
  wait_for_socket "$sock" || { say "gate $name did not start:"; cat "$SPIKE_TMP/gate-$name.log"; exit 2; }
}
GATE="$SPIKE_TMP/gate.sock"
start_gate main "$GATE" "$NET" merge

PROBE="wget -q -T 3 -O /dev/null http://$SPIKE_LAN_TARGET/"

# --- T1: confirm the gap -------------------------------------------------------
# Rootless podman's default (pasta) gives the container the host's own address, so it
# usually cannot reach services bound to that address; bridge networks (what compose
# uses) route out through the host and usually can.
if podman run --rm --label "$SPIKE_LABEL" "$SPIKE_IMAGE" sh -c "$PROBE" >/dev/null 2>&1; then
  info "gap: a container on podman's default network reaches $SPIKE_LAN_TARGET"
else
  info "a container on podman's default network did not reach $SPIKE_LAN_TARGET"
fi
if podman run --rm --label "$SPIKE_LABEL" --network "$BRIDGE" "$SPIKE_IMAGE" sh -c "$PROBE" >/dev/null 2>&1; then
  info "gap: a container on a plain bridge network (like a compose project's) reaches $SPIKE_LAN_TARGET"
else
  info "a container on a plain bridge network did not reach $SPIKE_LAN_TARGET"
fi

# --- T2: default create through the gate lands on the pinned network -----------
cid=$(premote "$GATE" run -d "$SPIKE_IMAGE" sleep 120 2>"$SPIKE_TMP/t2.err")
if [ -z "$cid" ]; then
  fail "gated create failed: $(tr '\n' ' ' <"$SPIKE_TMP/t2.err")"
else
  nets=$(ctr_nets "$cid")
  [ "$nets" = "$NET" ] && pass "gated container is on '$NET' only" || fail "gated container networks: '$nets' (expected '$NET')"
  if podman exec "$cid" sh -c "$PROBE" >/dev/null 2>&1; then
    fail "gated container reached $SPIKE_LAN_TARGET"
  else
    pass "gated container cannot reach $SPIKE_LAN_TARGET"
  fi
fi

# --- T3: --network host through the gate is overridden or refused --------------
cid3=$(premote "$GATE" run -d --network host "$SPIKE_IMAGE" sleep 120 2>"$SPIKE_TMP/t3.err")
if [ -z "$cid3" ]; then
  pass "gated '--network host' refused: $(tr '\n' ' ' <"$SPIKE_TMP/t3.err" | cut -c1-160)"
else
  nets=$(ctr_nets "$cid3")
  [ "$nets" = "$NET" ] && pass "gated '--network host' was rewritten to '$NET'" || fail "gated '--network host' container networks: '$nets'"
fi

# --- T4: port publishing through the gate is refused -----------------------------
cid4=$(premote "$GATE" run -d -p 127.0.0.1:18999:80 "$SPIKE_IMAGE" sleep 30 2>"$SPIKE_TMP/t4.err")
if [ -z "$cid4" ] && grep -q "agentbox-gate" "$SPIKE_TMP/t4.err"; then
  pass "gated port publishing refused by the gate"
elif [ -z "$cid4" ]; then
  info "gated port publishing failed, but not by the gate: $(tr '\n' ' ' <"$SPIKE_TMP/t4.err" | cut -c1-160)"
else
  fail "gated port publishing was allowed"
fi

# --- T5+: builds -------------------------------------------------------------------
# v3 showed rootless builds cannot use a named network here: even `podman build --network
# <name>` failed on the host. So builds try to JOIN the network namespace of a small
# "holder" container that sits on $NET. A stand-in "proxy" (busybox httpd) also sits on
# $NET: a RUN step that reaches it could reach the real squid proxy, and nothing else.
WEB_IMAGE="${SPIKE_WEB_IMAGE:-docker.io/library/busybox:1.36}"
ensure_image "$WEB_IMAGE"
podman run -d --name spike-holder --label "$SPIKE_LABEL" --network "$NET" "$SPIKE_IMAGE" sleep 3600 >/dev/null
podman run -d --name spike-standin --label "$SPIKE_LABEL" --network "$NET" "$WEB_IMAGE" \
  sh -c 'mkdir -p /www && echo ok > /www/index.html && exec httpd -f -p 8080 -h /www' >/dev/null
HOLDER_PID=$(podman inspect -f '{{.State.Pid}}' spike-holder 2>/dev/null)
STANDIN_IP=$(podman inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' spike-standin 2>/dev/null)
NSPATH="/proc/${HOLDER_PID:-0}/ns/net"
if [ -z "$HOLDER_PID" ] || [ "$HOLDER_PID" = 0 ] || [ -z "$STANDIN_IP" ]; then
  fail "could not start the holder or stand-in containers (pid='$HOLDER_PID' ip='$STANDIN_IP')"
  summary spike1; exit $?
fi
info "holder container on $NET: namespace $NSPATH; stand-in proxy at $STANDIN_IP:8080"

CTX="$SPIKE_TMP/ctx"; mkdir -p "$CTX"
cat >"$CTX/Containerfile" <<EOF
FROM $SPIKE_IMAGE
RUN echo "SPIKE_IFACES \$(ip -4 -o addr show 2>/dev/null | awk '{print \$4}' | grep -v '^127\.' | tr '\n' ' ')"; (wget -q -T 3 -O /dev/null http://$SPIKE_LAN_TARGET/ && echo SPIKE_BUILD_LEAK) || echo SPIKE_BUILD_BLOCKED; (wget -q -T 3 -O /dev/null http://$STANDIN_IP:8080/ && echo SPIKE_PROXY_OK) || echo SPIKE_PROXY_NO
EOF
IMG=localhost/agentbox-spike-build
_img_tags=()
cleanup_images() { local t; for t in "${_img_tags[@]}"; do podman rmi -f "$IMG:$t" >/dev/null 2>&1; done; }
defer cleanup_images
NET_SUBNET=$(podman network inspect "$NET" --format '{{range .Subnets}}{{.Subnet}} {{end}}' | awk '{print $1}')
NET_PREFIX=$(printf '%s' "$NET_SUBNET" | cut -d. -f1-3)

# Match whole output lines only: podman echoes each RUN command before running it,
# and that echo contains these same words.
build_result() { # file
  if grep -qx 'SPIKE_BUILD_LEAK' "$1"; then echo leak
  elif grep -qx 'SPIKE_BUILD_BLOCKED' "$1"; then echo blocked
  else echo error; fi
}
build_ifaces() { grep '^SPIKE_IFACES' "$1" | head -1 | cut -c14- | tr -s ' ' | sed 's/^ //; s/ $//'; }

# build TAG CMD... -> sets BR (leak|blocked|error), BI (RUN step addresses), BP (yes|no: stand-in reached)
build() {
  local tag=$1; shift
  local logf="$SPIKE_TMP/build-$tag.log"
  _img_tags+=("$tag")
  "$@" --no-cache -t "$IMG:$tag" "$CTX" >"$logf" 2>&1
  BR=$(build_result "$logf"); BI=$(build_ifaces "$logf")
  if grep -qx 'SPIKE_PROXY_OK' "$logf"; then BP=yes; else BP=no; fi
  [ "$BR" = error ] && BI="error: $(grep -v -e '^STEP' -e '^-->' "$logf" | tail -2 | tr '\n' ' ' | cut -c1-160)"
}
describe() { # result addresses proxy
  case "$1" in
    leak)    printf 'REACHED the LAN; stand-in %s (addresses: %s)' "$([ "$3" = yes ] && echo reachable || echo unreachable)" "${2:-none}" ;;
    blocked) printf 'LAN blocked; stand-in %s (addresses: %s)' "$([ "$3" = yes ] && echo reachable || echo unreachable)" "${2:-none}" ;;
    *)       printf 'did not run (%s)' "$2" ;;
  esac
}
on_net() { case " $1 " in *" $NET_PREFIX."*) return 0 ;; esac; return 1; }

# name -> the --network value asked for
net_for() { case "$1" in none) echo none ;; holder) echo "ns:$NSPATH" ;; *) echo "" ;; esac; }

say ""; say "  -- builds run directly on the host (baseline)"
build direct podman build;                                info "default build: $(describe "$BR" "$BI" "$BP")"
build direct-none podman build --network none;            info "build --network none: $(describe "$BR" "$BI" "$BP")"
build direct-holder podman build --network "ns:$NSPATH";  info "build --network ns:<holder> ($NET_SUBNET): $(describe "$BR" "$BI" "$BP")"

say ""; say "  -- what the remote client sends (gate in observe-only mode)"
for want in default none holder; do
  sock="$SPIKE_TMP/obs-$want.sock"
  start_gate "obs-$want" "$sock" "$NET" observe
  n=$(net_for "$want")
  if [ -z "$n" ]; then build "obs-$want" premote "$sock" build
  else build "obs-$want" premote "$sock" build --network "$n"; fi
  q=$(grep '^BUILDQ' "$SPIKE_TMP/gate-obs-$want.log" | tail -1 | cut -c8-)
  [ -n "$q" ] && printf '%s' "$q" >"$SPIKE_TMP/tmpl-$want.json"
  info "remote build asking for '$want': $(describe "$BR" "$BI" "$BP")"
  info "   client sent: ${q:-<no build request seen>}"
done

say ""; say "  -- forcing a build that asked for nothing"
say "     template: replay what the client itself sent for that network"
say "     merge:    keep the client's other options, replace only the network one"
ok_holder=(); ok_none=()
for target in holder none; do
  for strat in template merge; do
    tag="force-$strat-$target"
    tmpl=""
    if [ "$strat" = template ]; then
      tmpl="$SPIKE_TMP/tmpl-$target.json"
      [ -s "$tmpl" ] || { info "strategy=$strat target=$target: skipped, nothing observed to replay"; continue; }
    fi
    sock="$SPIKE_TMP/$tag.sock"
    start_gate "$tag" "$sock" "$(net_for "$target")" "$strat" "$tmpl"
    build "$tag" premote "$sock" build
    info "strategy=$strat target=$target: $(describe "$BR" "$BI" "$BP")"
    if [ "$BR" = blocked ]; then
      if [ "$target" = none ]; then ok_none+=("$strat")
      elif [ "$BP" = yes ] && on_net "$BI"; then ok_holder+=("$strat"); fi
    fi
  done
done

if [ ${#ok_holder[@]} -gt 0 ]; then
  pass "KEY: gated builds can join the project network and reach its proxy, not the LAN (works: ${ok_holder[*]})"
else
  fail "KEY: no strategy put gated builds on the project network with the proxy reachable"
fi
if [ ${#ok_none[@]} -gt 0 ]; then
  pass "KEY: gated builds can be forced offline (works: ${ok_none[*]})"
else
  fail "KEY: no strategy forced gated builds offline"
fi

say ""; say "  gate logs (build requests, denials, errors):"
for f in "$SPIKE_TMP"/gate-*.log; do
  n=$(basename "$f" .log); n=${n#gate-}
  grep -e '^BUILDQ' -e '^DENY' -e '^ERROR' "$f" | sed "s/^/    [$n] /"   # BUILDQ = received, BUILDQ-SENT = forced
done
summary spike1
