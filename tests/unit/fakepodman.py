#!/usr/bin/env python3
"""A stand-in `podman` for unit tests: records argv, answers from environment knobs, never touches podman.

  FAKE_PODMAN_LOG       file that gets one JSON argv per line
  FAKE_PODMAN_PS        stdout for `ps --format {{.Names}}`
  FAKE_PODMAN_PSJSON    stdout for `ps --format json`
  FAKE_PODMAN_NETLS     stdout for `network ls --format {{.Name}}`
  FAKE_PODMAN_NETJSON   stdout for `network ls --format json`
  FAKE_PODMAN_IMAGES    stdout for `images --format json`
  FAKE_PODMAN_FAIL      substring: any call whose argv contains it exits 1
  FAKE_PODMAN_FAIL_STATUS  the status FAKE_PODMAN_FAIL exits with (default 1). 125 is podman's own "the
                          command could not be run", which for `image exists` is neither yes nor no
  FAKE_PODMAN_RUN_OUT   stdout for `run`
  FAKE_PODMAN_RUN_OUT_BY_IMAGE  {image ref: stdout} for `run`, matched against argv. `ab image build`
                          probes the harness image AND the candidate, and one canned answer for both
                          cannot tell "the layer lost a tool" from "neither has it"
  FAKE_PODMAN_RUN_SLEEP seconds `run` waits before answering, so a test can signal a launcher mid-session
  FAKE_PODMAN_LOGS      stdout for `logs` (default: squid's ready line)
  FAKE_PODMAN_STATE     stdout for `inspect -f {{.State.Status}}`
  FAKE_PODMAN_GATEWAY   stdout for `network inspect`
  FAKE_PODMAN_IMAGE_JSON  {ref: inspect doc} for `image inspect <ref> --format json`; a ref that is not a
                          key answers like real podman does for a missing image (exit 125, empty stdout)
  FAKE_PODMAN_IMAGE_TAGS  space-separated refs `image exists` says yes to (default: every key of
                          FAKE_PODMAN_IMAGE_JSON). `image rm` and `tag` update it in the file it came from,
                          so a test can build, tag and then look -- see FAKE_PODMAN_IMAGE_TAGS_FILE
  FAKE_PODMAN_IMAGE_TAGS_FILE  a file holding that same list; when set, `tag` and `image rm` rewrite it

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
    sys.exit(int(os.environ.get("FAKE_PODMAN_FAIL_STATUS") or 1))

fmt = ""
for i, a in enumerate(argv[:-1]):
    if a in ("--format", "-f"):
        fmt = argv[i + 1]
for prefix, bad, status, message in REJECTED_FORMATS:
    if tuple(argv[:len(prefix)]) == prefix and bad.search(fmt):
        sys.stderr.write(message + "\n")
        sys.exit(status)

def image_docs():
    """The inspect docs, from the file when one is set -- a `build` or `tag` in one fake process has to be
    visible to the next, and an environment variable cannot carry that across processes."""
    path = os.environ.get("FAKE_PODMAN_IMAGE_JSON_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            return json.loads(f.read() or "{}")
    return json.loads(os.environ.get("FAKE_PODMAN_IMAGE_JSON") or "{}")


def save_docs(docs):
    path = os.environ.get("FAKE_PODMAN_IMAGE_JSON_FILE")
    if path:
        with open(path, "w") as f:
            f.write(json.dumps(docs))


def tags():
    path = os.environ.get("FAKE_PODMAN_IMAGE_TAGS_FILE")
    if path and os.path.exists(path):
        with open(path) as f:
            return [t for t in f.read().split() if t]
    listed = os.environ.get("FAKE_PODMAN_IMAGE_TAGS")
    return [t for t in listed.split() if t] if listed is not None else sorted(image_docs())


def set_tags(names):
    path = os.environ.get("FAKE_PODMAN_IMAGE_TAGS_FILE")
    if path:
        with open(path, "w") as f:
            f.write(" ".join(sorted(set(names))))


if argv[:2] == ["image", "inspect"] and fmt == "json":
    ref = argv[2] if len(argv) > 2 else ""
    doc = image_docs().get(ref)
    if doc is None or ref not in tags():
        # What real podman does for an image it does not have: exit 125, nothing on stdout. Answering
        # with an empty list instead would let "no such image" read as a valid, featureless image.
        sys.stderr.write(f"Error: {ref}: image not known\n")
        sys.exit(125)
    sys.stdout.write(json.dumps([doc]))
    sys.exit(0)
elif argv[:2] == ["image", "exists"]:
    sys.exit(0 if (len(argv) > 2 and argv[2] in tags()) else 1)
elif argv[:2] == ["image", "rm"]:
    gone = {a for a in argv[2:] if not a.startswith("-")}
    set_tags([t for t in tags() if t not in gone])
    sys.exit(0)
elif argv[:1] == ["tag"]:
    args = [a for a in argv[1:] if not a.startswith("-")]
    if len(args) == 2:
        docs = image_docs()
        if args[0] in docs:                       # the new tag names the same image
            docs[args[1]] = docs[args[0]]
            save_docs(docs)
        set_tags(list(tags()) + [args[1]])
    sys.exit(0)
elif argv[:1] == ["build"]:
    # The build "produces" whatever the test said it would: the -t tag becomes a known image, taking the
    # inspect doc the test put under that tag (or under "BUILT" as a default for the candidate).
    tag = ""
    for i, a in enumerate(argv[:-1]):
        if a == "-t":
            tag = argv[i + 1]
    docs = image_docs()
    if tag and "BUILT" in docs:
        docs[tag] = docs["BUILT"]
        save_docs(docs)
    if tag:
        set_tags(list(tags()) + [tag])
    sys.exit(0)

if argv[:1] == ["ps"]:
    if "json" in argv:
        sys.stdout.write(os.environ.get("FAKE_PODMAN_PSJSON", "[]"))
    else:
        sys.stdout.write(os.environ.get("FAKE_PODMAN_PS", ""))
elif argv[:1] == ["images"]:
    # --filter label=k=v is honoured, because `any_built` asks exactly that question and a fake that
    # answered every filter with the same list could not tell "no layer" from "some layer".
    want = {}
    for i, a in enumerate(argv[:-1]):
        if a == "--filter" and argv[i + 1].startswith("label="):
            k, _, v = argv[i + 1][len("label="):].partition("=")
            want[k] = v
    try:
        listed = json.loads(os.environ.get("FAKE_PODMAN_IMAGES") or "[]")
    except ValueError:
        listed = []
    if want:
        listed = [img for img in listed
                  if all((img.get("Labels") or {}).get(k) == v for k, v in want.items())]
    sys.stdout.write(json.dumps(listed))
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
    if os.environ.get("FAKE_PODMAN_RUN_SLEEP"):
        import time
        time.sleep(float(os.environ["FAKE_PODMAN_RUN_SLEEP"]))
    try:
        by_image = json.loads(os.environ.get("FAKE_PODMAN_RUN_OUT_BY_IMAGE") or "{}")
    except ValueError:
        by_image = {}
    out = None
    for a in argv:
        if a in by_image:
            out = by_image[a]
            break
    sys.stdout.write(out if out is not None else os.environ.get("FAKE_PODMAN_RUN_OUT", ""))
sys.exit(0)
