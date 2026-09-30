#!/usr/bin/env python3
"""A stand-in `podman` for unit tests: records argv, answers from environment knobs, never touches podman.

  FAKE_PODMAN_LOG       file that gets one JSON argv per line
  FAKE_PODMAN_PS        stdout for `ps --format {{.Names}}`
  FAKE_PODMAN_PSJSON    stdout for `ps --format json`
  FAKE_PODMAN_NETLS     stdout for `network ls`
  FAKE_PODMAN_FAIL      substring: any call whose argv contains it exits 1
  FAKE_PODMAN_RUN_OUT   stdout for `run`
  FAKE_PODMAN_LOGS      stdout for `logs` (default: squid's ready line)
  FAKE_PODMAN_STATE     stdout for `inspect -f {{.State.Status}}`
  FAKE_PODMAN_GATEWAY   stdout for `network inspect`
"""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ["FAKE_PODMAN_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
fail = os.environ.get("FAKE_PODMAN_FAIL")
if fail and any(fail in a for a in argv):
    sys.stderr.write(f"fake podman: failing on {fail}\n")
    sys.exit(1)
if argv[:1] == ["ps"]:
    if "json" in argv:
        sys.stdout.write(os.environ.get("FAKE_PODMAN_PSJSON", "[]"))
    else:
        sys.stdout.write(os.environ.get("FAKE_PODMAN_PS", ""))
elif argv[:2] == ["network", "ls"]:
    sys.stdout.write(os.environ.get("FAKE_PODMAN_NETLS", ""))
elif argv[:2] == ["network", "inspect"]:
    sys.stdout.write(os.environ.get("FAKE_PODMAN_GATEWAY", "10.89.9.1") + " \n")
elif argv[:1] == ["logs"]:
    sys.stdout.write(os.environ.get("FAKE_PODMAN_LOGS", "Accepting HTTP Socket connections at local=[::]:3128\n"))
elif argv[:1] == ["inspect"]:
    sys.stdout.write(os.environ.get("FAKE_PODMAN_STATE", "running") + "\n")
elif argv[:1] == ["run"] and "-d" in argv:
    sys.stdout.write("0123456789ab\n")
elif argv[:1] == ["run"]:
    sys.stdin.read()
    sys.stdout.write(os.environ.get("FAKE_PODMAN_RUN_OUT", ""))
sys.exit(0)
