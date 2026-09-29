#!/usr/bin/env bash
# Spike 3 — does squid enforce the egress modes as designed?
#
# A client container on an internal network can reach the internet only through squid.
# Squid runs twice: in `locked` mode (model endpoint only) and `open` mode (any public host),
# and must refuse private and host addresses in both, including public names that resolve to them.
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
need podman; need curl
hdr "Spike 3: squid egress modes"

SQUID_IMAGE="${SPIKE_SQUID_IMAGE:-docker.io/ubuntu/squid:latest}"
INT=spike-sq-int
EGR=spike-sq-egress

ensure_image "$SPIKE_CURL_IMAGE"
if ! podman image exists "$SQUID_IMAGE" && ! podman pull -q "$SQUID_IMAGE" >/dev/null 2>&1; then
  skip "could not pull $SQUID_IMAGE; set SPIKE_SQUID_IMAGE"; summary spike3; exit $?
fi
net_create "$INT" --internal
net_create "$EGR"

MODEL_HOST="${SPIKE_MODEL_TARGET%:*}"; MODEL_PORT="${SPIKE_MODEL_TARGET##*:}"
LAN_HOST="${SPIKE_LAN_TARGET%:*}";     LAN_PORT="${SPIKE_LAN_TARGET##*:}"
NIP_NAME="${LAN_HOST//./-}.nip.io"     # a public DNS name that resolves to the LAN address

model_live=yes; [ "$(host_code "$SPIKE_MODEL_TARGET")" = "000" ] && model_live=no
lan_live=yes;   [ "$(host_code "$SPIKE_LAN_TARGET")"   = "000" ] && lan_live=no

# Squid resolves names through the egress network's DNS (aardvark on its gateway):
# DNS on an internal network may not forward public names.
EGR_DNS=$(podman network inspect "$EGR" --format '{{range .Subnets}}{{.Gateway}} {{end}}' 2>/dev/null | awk '{print $1}')

write_conf() { # mode
  cat >"$SPIKE_TMP/squid.conf" <<EOF
http_port 3128
${EGR_DNS:+dns_nameservers $EGR_DNS}
acl spike_localmodel dst $MODEL_HOST
acl spike_modelport  port $MODEL_PORT
acl spike_private    dst 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 127.0.0.0/8 169.254.0.0/16 100.64.0.0/10 fc00::/7 fe80::/10 ::1
acl spike_connect    method CONNECT
acl spike_sslport    port 443
http_access allow spike_localmodel spike_modelport
http_access deny  spike_private
EOF
  if [ "$1" = open ]; then
    cat >>"$SPIKE_TMP/squid.conf" <<EOF
http_access allow spike_connect spike_sslport
http_access allow !spike_connect
EOF
  fi
  cat >>"$SPIKE_TMP/squid.conf" <<EOF
http_access deny all
cache deny all
access_log stdio:/dev/stdout
cache_log /dev/stderr
EOF
}

start_squid() { # mode
  podman rm -f spike-squid >/dev/null 2>&1 || true
  write_conf "$1"
  podman run -d --name spike-squid --label "$SPIKE_LABEL" --network "$EGR" --network "$INT" \
    -v "$SPIKE_TMP/squid.conf:/etc/squid/squid.conf:ro" "$SQUID_IMAGE" >/dev/null || return 1
  local _
  for _ in $(seq 1 40); do
    [ "$(cl -s -o /dev/null -m 3 -w '%{http_code}' -x http://spike-squid:3128 http://spike-squid:3128/ 2>/dev/null)" != "000" ] && return 0
    sleep 0.5
  done
  return 1
}

# curl from a client that only sits on the internal network
cl() { podman run --rm --label "$SPIKE_LABEL" --network "$INT" "$SPIKE_CURL_IMAGE" "$@"; }
px()  { cl -s -o /dev/null -m 10 -w '%{http_code}'  -x http://spike-squid:3128 "$1" 2>/dev/null; }
pxs() { cl -s -o /dev/null -m 10 -w '%{http_connect}' -x http://spike-squid:3128 "$1" 2>/dev/null; }

expect_403() { # label url
  local c; c=$(px "$2"); [ "$c" = "403" ] && pass "$1 -> 403" || fail "$1 -> $c (expected 403)"
}
expect_ok() { # label url
  local c; c=$(px "$2"); { [ "$c" != "403" ] && [ "$c" != "000" ]; } && pass "$1 -> $c (allowed)" || fail "$1 -> $c (expected allowed)"
}

for mode in locked open; do
  say ""; say "  -- mode: $mode"
  if ! start_squid "$mode"; then
    fail "squid did not start in $mode mode:"; podman logs spike-squid 2>&1 | tail -15 | sed 's/^/    /'
    continue
  fi

  if [ "$model_live" = yes ]; then
    expect_ok "model endpoint http://$SPIKE_MODEL_TARGET/v1/models" "http://$SPIKE_MODEL_TARGET/v1/models"
  else
    skip "model endpoint $SPIKE_MODEL_TARGET is not answering from the host"
  fi
  expect_403 "same host, other port http://$MODEL_HOST:22/" "http://$MODEL_HOST:22/"
  if [ "$lan_live" = yes ]; then
    expect_403 "LAN service http://$SPIKE_LAN_TARGET/" "http://$SPIKE_LAN_TARGET/"
  else
    skip "LAN target $SPIKE_LAN_TARGET not answering from the host"
  fi
  expect_403 "public name resolving to a private IP http://$NIP_NAME:$LAN_PORT/" "http://$NIP_NAME:$LAN_PORT/"

  c=$(pxs https://example.com/)
  if [ "$mode" = locked ]; then
    [ "$c" = "403" ] && pass "HTTPS to example.com refused (CONNECT 403)" || fail "HTTPS to example.com: CONNECT $c (expected 403)"
  else
    [ "$c" = "200" ] && pass "HTTPS to example.com tunnelled (CONNECT 200)" || fail "HTTPS to example.com: CONNECT $c (expected 200)"
    expect_ok "plain HTTP to example.com" "http://example.com/"
  fi
done

# Direct (proxy ignored): must fail at the network level, never reach a host.
say ""; say "  -- direct, bypassing the proxy"
for t in "$SPIKE_LAN_TARGET" "$SPIKE_MODEL_TARGET" "example.com"; do
  r=$(cl -s -o /dev/null -m 6 -w '%{http_code} %{errormsg}' --noproxy '*' "http://$t/" 2>/dev/null)
  code=${r%% *}
  if [ "$code" = "000" ] && ! echo "$r" | grep -qi refused; then
    pass "direct $t unreachable ($r)"
  else
    fail "direct $t: $r"
  fi
done

summary spike3
