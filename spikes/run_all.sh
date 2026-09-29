#!/usr/bin/env bash
# Run the spikes and save one results file to paste back.
#   ./run_all.sh          all five
#   ./run_all.sh 1 2 5    only these
cd "$(dirname "$0")" || exit 2
all=(spike1_gate.sh spike2_sockguard.sh spike3_squid.sh spike4_gateway.sh spike5_compose_isolate.sh)
if [ $# -eq 0 ]; then
  run=("${all[@]}")
else
  run=()
  for n in "$@"; do
    case "$n" in
      [1-5]) run+=("${all[$((n-1))]}") ;;
      *) echo "unknown spike: $n (use 1-5)" >&2; exit 2 ;;
    esac
  done
fi
out="spike-results-$(date +%Y%m%d-%H%M%S).txt"
{
  echo "host: $(hostname)  kernel: $(uname -r)  $(podman --version)"
  echo "netavark: $(podman info --format '{{.Host.NetworkBackendInfo.Version}}' 2>/dev/null)"
  echo "podman-compose: $(podman-compose version 2>/dev/null | tail -1)"
  for s in "${run[@]}"; do
    bash "$s"
    echo "exit=$?"
  done
} 2>&1 | tee "$out"
echo
echo "Saved to $out. It contains no API keys; paste it back."
