"""Build options for agentbox's own images that the user sets once in agentbox.toml, for the Makefile; and
reading back, from an image's labels, what a build actually put in.

`make base-image` asks `python3 -m agentbox.imageopts playwright` for the default of its PLAYWRIGHT flag, so the
choice is durable: a rebuild weeks later does not silently drop Playwright because nobody retyped
`PLAYWRIGHT=1`. A value on the make command line still wins (make's own rule for `?=`).

Prints `1` or `0` and nothing else. A malformed agentbox.toml, or a value that is not a TOML boolean, is an
error with exit 2 rather than a guess: `image_playwright = "false"` read as truthy would put a few hundred MB
into every image, and read as falsy would drop what the user asked for. Standard library only; it reads the
same file, with the same AB_CONFIG_DIR override, as the launcher (`config.load`).
"""
import sys

from . import config
from .errors import AgentboxError

# option name on the command line -> agentbox.toml key
OPTIONS = {"playwright": "image_playwright"}
# The label the base image's two stages set: the Playwright version, or "none" (images/base/Containerfile).
PLAYWRIGHT_LABEL = "agentbox.playwright"


def playwright_version(labels):
    """The Playwright version an image carries, from the label `make base-image` sets (and every image built on
    agentbox-base inherits), or None: "none" for an opted-out build, and no label at all for an image built
    before the option existed. Sessions and the selftest read this, never agentbox.toml: the setting is what
    the next build will do, the label is what this image has."""
    v = ((labels or {}).get(PLAYWRIGHT_LABEL) or "").strip()
    return v if v and v not in ("none", "<no value>") else None


def flag(values, key):
    """"1" or "0" for a boolean setting; AgentboxError for anything that is not a real boolean."""
    v = values.get(key, config.DEFAULTS[key])
    if not isinstance(v, bool):
        raise AgentboxError(f"agentbox.toml: {key} must be true or false (a TOML boolean, unquoted), "
                            f"not {v!r}")
    return "1" if v else "0"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in OPTIONS:
        print(f"usage: python3 -m agentbox.imageopts {{{','.join(sorted(OPTIONS))}}}", file=sys.stderr)
        return 2
    try:
        print(flag(config.load().values, OPTIONS[argv[0]]))
    except AgentboxError as e:
        print(f"agentbox.imageopts: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
