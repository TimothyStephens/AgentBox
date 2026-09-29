#!/usr/bin/env python3
"""A stand-in `podman` for unit tests: records argv, answers from environment knobs, never touches podman.

  FAKE_PODMAN_LOG       file that gets one JSON argv per line
  FAKE_PODMAN_PS        stdout for `ps` (container names / json)
  FAKE_PODMAN_NETLS     stdout for `network ls`
  FAKE_PODMAN_FAIL      substring: any call whose argv contains it exits 1
  FAKE_PODMAN_RUN_OUT   stdout for `run`
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
    sys.stdout.write(os.environ.get("FAKE_PODMAN_PS", "[]" if "json" in argv else ""))
elif argv[:2] == ["network", "ls"]:
    sys.stdout.write(os.environ.get("FAKE_PODMAN_NETLS", ""))
elif argv[:1] == ["run"]:
    sys.stdin.read()
    sys.stdout.write(os.environ.get("FAKE_PODMAN_RUN_OUT", ""))
sys.exit(0)
