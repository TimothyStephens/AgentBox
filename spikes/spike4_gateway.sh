#!/usr/bin/env bash
# Spike 4 — can agents use models through a key-holding gateway, with no real key in the sandbox?
#
# A LiteLLM gateway holds the real Anthropic key and sits on an internal network plus an
# egress network. Clients sit on the internal network only and know just a dummy key.
#
# Needs: SPIKE_ANTHROPIC_KEY_FILE (a 0600 file) or SPIKE_ANTHROPIC_API_KEY; never printed.
# Optional: SPIKE_ANTHROPIC_MODEL (a real model id),
# SPIKE_CLAUDE_IMAGE, SPIKE_OMP_MODELS + SPIKE_OMP_ARGS (see README).
# shellcheck source=common.sh
source "$(dirname "$0")/common.sh"
need podman; need curl
hdr "Spike 4: model key gateway"

GW_IMAGE="${SPIKE_GATEWAY_IMAGE:-ghcr.io/berriai/litellm:main-stable}"
MODEL="${SPIKE_ANTHROPIC_MODEL:-claude-sonnet-4-5}"
CLAUDE_IMAGE="${SPIKE_CLAUDE_IMAGE:-localhost/claudecode:latest}"
OMP_IMAGE="${SPIKE_OMP_IMAGE:-localhost/omp-env:latest}"
INT=spike-gw-int
EGR=spike-gw-egress
MASTER="sk-agentbox-spike-$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"   # the only key clients see

case "$MODEL" in sk-*) say "SPIKE_ANTHROPIC_MODEL looks like an API key, not a model name; refusing to use it"; exit 2 ;; esac
# Preferred: the key in a 0600 file, so it never appears in shell history or the environment.
if [ -z "${SPIKE_ANTHROPIC_API_KEY:-}" ] && [ -n "${SPIKE_ANTHROPIC_KEY_FILE:-}" ] && [ -r "$SPIKE_ANTHROPIC_KEY_FILE" ]; then
  SPIKE_ANTHROPIC_API_KEY=$(tr -d ' \r\n' <"$SPIKE_ANTHROPIC_KEY_FILE")
fi
if [ -z "${SPIKE_ANTHROPIC_API_KEY:-}" ]; then
  skip "no API key: set SPIKE_ANTHROPIC_KEY_FILE (see README)"; summary spike4; exit $?
fi
redact() { local s; s=$(cat); printf '%s\n' "${s//$SPIKE_ANTHROPIC_API_KEY/[REDACTED]}"; }

ensure_image "$SPIKE_CURL_IMAGE"
if ! podman image exists "$GW_IMAGE" && ! podman pull -q "$GW_IMAGE" >/dev/null 2>&1; then
  skip "could not pull $GW_IMAGE; set SPIKE_GATEWAY_IMAGE"; summary spike4; exit $?
fi
net_create "$INT" --internal
net_create "$EGR"

# Wildcard route: any Anthropic model name passes through, so clients use real model ids
# (Claude Code refuses names it does not know).
cat >"$SPIKE_TMP/litellm.yaml" <<EOF
model_list:
  - model_name: "*"
    litellm_params:
      model: "anthropic/*"
      api_key: os.environ/ANTHROPIC_API_KEY
general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
EOF

ANTHROPIC_API_KEY="$SPIKE_ANTHROPIC_API_KEY" LITELLM_MASTER_KEY="$MASTER" \
  podman run -d --name spike-gateway --label "$SPIKE_LABEL" --network "$EGR" --network "$INT" \
  -e ANTHROPIC_API_KEY -e LITELLM_MASTER_KEY \
  -v "$SPIKE_TMP/litellm.yaml:/app/config.yaml:ro" \
  "$GW_IMAGE" --config /app/config.yaml --port 4000 >/dev/null

cl() { podman run --rm --label "$SPIKE_LABEL" --network "$INT" "$SPIKE_CURL_IMAGE" "$@"; }

ready=no
for _ in $(seq 1 60); do
  [ "$(cl -s -o /dev/null -m 3 -w '%{http_code}' http://spike-gateway:4000/health/liveliness 2>/dev/null)" = "200" ] && { ready=yes; break; }
  sleep 2
done
if [ "$ready" != yes ]; then
  fail "gateway did not become ready:"; podman logs spike-gateway 2>&1 | tail -20 | redact | sed 's/^/    /'
  summary spike4; exit $?
fi
pass "gateway is up on the internal network"

# --- the client network has no direct route out --------------------------------
c=$(cl -s -o /dev/null -m 6 -w '%{http_code}' https://api.anthropic.com/ 2>/dev/null)
[ "$c" = "000" ] && pass "client cannot reach api.anthropic.com directly" || fail "client reached api.anthropic.com directly ($c)"

# --- Anthropic-format call through the gateway with the dummy key --------------
body="{\"model\":\"$MODEL\",\"max_tokens\":16,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: SPIKE_OK\"}]}"
resp=$(cl -s -m 60 -H "x-api-key: $MASTER" -H "Authorization: Bearer $MASTER" \
  -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d "$body" http://spike-gateway:4000/v1/messages 2>/dev/null)
# An error that came back from Anthropic means the gateway forwarded the call with the real
# key: the plumbing worked and the problem is the account or model name.
account_error() { printf '%s' "$1" | grep -q -e 'AnthropicException' -e 'authentication_error' -e 'permission_error' -e 'organization'; }
if echo "$resp" | grep -q SPIKE_OK; then
  pass "Anthropic /v1/messages through the gateway works"
elif account_error "$resp"; then
  info "the gateway reached Anthropic with the real key, but Anthropic refused the account or model: $(echo "$resp" | redact | cut -c1-240)"
  fail "gateway /v1/messages: fix the key/account (see INFO above), then rerun"
else
  fail "gateway /v1/messages: $(echo "$resp" | redact | cut -c1-300)"
fi

# --- Claude Code through the gateway --------------------------------------------
if podman image exists "$CLAUDE_IMAGE"; then
  out=$(timeout 180 podman run --rm --label "$SPIKE_LABEL" --network "$INT" --entrypoint "" --tmpfs /tmp \
    -e ANTHROPIC_BASE_URL=http://spike-gateway:4000 -e ANTHROPIC_API_KEY="$MASTER" \
    -e ANTHROPIC_MODEL="$MODEL" \
    -e CLAUDE_CONFIG_DIR=/tmp/cc -e DISABLE_TELEMETRY=1 -e CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    "$CLAUDE_IMAGE" claude -p "Reply with exactly: SPIKE_OK" 2>&1)
  if echo "$out" | grep -q SPIKE_OK; then
    pass "Claude Code answered through the gateway with only the dummy key"
  elif account_error "$out"; then
    fail "Claude Code reached Anthropic through the gateway, but the account was refused: $(echo "$out" | redact | tail -3 | tr '\n' ' ' | cut -c1-240)"
  else
    fail "Claude Code through the gateway: $(echo "$out" | redact | tail -5 | tr '\n' ' ' | cut -c1-300)"
  fi
else
  skip "Claude Code image $CLAUDE_IMAGE not found (set SPIKE_CLAUDE_IMAGE)"
fi

# --- OMP through the gateway (semi-manual) ------------------------------------
if [ -n "${SPIKE_OMP_MODELS:-}" ] && [ -f "$SPIKE_OMP_MODELS" ] && podman image exists "$OMP_IMAGE"; then
  sed "s/SPIKE_MASTER_KEY/$MASTER/g" "$SPIKE_OMP_MODELS" >"$SPIKE_TMP/omp-models.yml"
  out=$(timeout 180 podman run --rm --label "$SPIKE_LABEL" --network "$INT" --entrypoint "" \
    --cap-drop=ALL --security-opt=no-new-privileges \
    -v "$HOME/.omp:/root/.omp:O" -v "$SPIKE_TMP/omp-models.yml:/spike/models.yml:ro" \
    "$OMP_IMAGE" bash -lc "cp /spike/models.yml /root/.omp/agent/models.yml && omp ${SPIKE_OMP_ARGS:-} -p 'Reply with exactly: SPIKE_OK'" 2>&1)
  if echo "$out" | grep -q SPIKE_OK; then
    pass "OMP answered through the gateway with only the dummy key"
  elif account_error "$out"; then
    fail "OMP reached Anthropic through the gateway, but the account was refused: $(echo "$out" | redact | tail -3 | tr '\n' ' ' | cut -c1-240)"
  else
    fail "OMP through the gateway: $(echo "$out" | redact | tail -5 | tr '\n' ' ' | cut -c1-300)"
  fi
else
  skip "OMP test needs SPIKE_OMP_MODELS (a models.yml pointing at the gateway) and image $OMP_IMAGE — see README"
fi

summary spike4
