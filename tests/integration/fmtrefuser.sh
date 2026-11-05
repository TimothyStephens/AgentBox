#!/usr/bin/env bash
# A stand-in `podman` that refuses every `--format`, used by test_podman_formats.py as a CONTROL: it proves
# tests/manual/podman_formats.sh actually reports a rejected template. Without it, "the script exited 0"
# would also be true of a script that checks nothing.
#
# Not a general fake podman. It creates, starts and removes nothing.
set -u
for a in "$@"; do
  if [ "$a" = "--format" ]; then
    echo "fake podman: this format is not supported here" >&2
    exit 125
  fi
done
case "${1:-}" in
  --version) echo "podman version 0.0.0-fmtrefuser" ;;
esac
exit 0
