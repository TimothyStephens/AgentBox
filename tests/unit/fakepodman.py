#!/usr/bin/env python3
"""A stand-in `podman` for unit tests: records argv, answers from environment knobs, never touches podman.

  FAKE_PODMAN_LOG       file that gets one JSON argv per line
  FAKE_PODMAN_PS        stdout for `ps --format {{.Names}}`
  FAKE_PODMAN_PSJSON    stdout for `ps --format json`
  FAKE_PODMAN_NETLS     stdout for `network ls --format {{.Name}}`
  FAKE_PODMAN_NETJSON   stdout for `network ls --format json`
  FAKE_PODMAN_IMAGES    stdout for `images --format json`
  FAKE_PODMAN_FAIL      substring: any call whose argv contains it exits 1
  FAKE_PODMAN_RUN_OUT   stdout for `run`
  FAKE_PODMAN_LOGS      stdout for `logs` (default: squid's ready line)
  FAKE_PODMAN_STATE     stdout for `inspect -f {{.State.Status}}`
  FAKE_PODMAN_GATEWAY   stdout for `network inspect`

It also REFUSES the `--format` templates real podman refuses (see REJECTED_FORMATS). Answering every
format with the same canned string is what let `network ls --format '{{index .Labels "x"}}'` -- which
podman 5.7 rejects outright -- live in the code for a whole build: the call was swallowed, so the feature
had never worked, and no unit test could tell. Every entry below is a message copied from the host;
`tests/manual/podman_formats.sh` is what finds more.
"""
import json
import os
import re
import sys

# (argv prefix, pattern the format must not match) -> podman's own error, and its exit status.
REJECTED_FORMATS = [
    # podman 5.7, host, 28 Oct 2026: `network ls`'s template context cannot be indexed by a string key.
    (("network", "ls"), re.compile(r"index\s+\.Labels"), 125,
     'Error: template: list:1:12: executing "list" at <index .Labels "agentbox.session">: '
     "error calling index: cannot index slice/array with type string"),
]

argv = sys.argv[1:]
with open(os.environ["FAKE_PODMAN_LOG"], "a") as f:
    f.write(json.dumps(argv) + "\n")
fail = os.environ.get("FAKE_PODMAN_FAIL")
if fail and any(fail in a for a in argv):
    sys.stderr.write(f"fake podman: failing on {fail}\n")
    sys.exit(1)

fmt = ""
for i, a in enumerate(argv[:-1]):
    if a in ("--format", "-f"):
        fmt = argv[i + 1]
for prefix, bad, status, message in REJECTED_FORMATS:
    if tuple(argv[:len(prefix)]) == prefix and bad.search(fmt):
        sys.stderr.write(message + "\n")
        sys.exit(status)

if argv[:1] == ["ps"]:
    if "json" in argv:
        sys.stdout.write(os.environ.get("FAKE_PODMAN_PSJSON", "[]"))
    else:
        sys.stdout.write(os.environ.get("FAKE_PODMAN_PS", ""))
elif argv[:1] == ["images"]:
    sys.stdout.write(os.environ.get("FAKE_PODMAN_IMAGES", "[]"))
elif argv[:2] == ["network", "ls"]:
    if fmt == "json":
        sys.stdout.write(os.environ.get("FAKE_PODMAN_NETJSON", "[]"))
    else:
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
