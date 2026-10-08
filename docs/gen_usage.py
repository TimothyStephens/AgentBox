#!/usr/bin/env python3
"""Generate docs/USAGE.md from the live `ab --help` output, so the reference is always exactly what the
installed launcher prints. Run from the repo root:

    python3 docs/gen_usage.py > docs/USAGE.md
"""
import subprocess
import sys

# (section heading, [(command title, argv)]) in the order the CLI presents them.
GROUPS = [
    ("Top level", [("ab", [])]),
    ("ab init", [("ab init", ["init"])]),
    ("ab run", [("ab run", ["run"])]),
    ("ab selftest", [("ab selftest", ["selftest"])]),
    ("ab project", [
        ("ab project", ["project"]),
        ("ab project add", ["project", "add"]),
        ("ab project show", ["project", "show"]),
        ("ab project ls", ["project", "ls"]),
        ("ab project rm", ["project", "rm"]),
        ("ab project reset-state", ["project", "reset-state"]),
        ("ab project accept-grant-exposure", ["project", "accept-grant-exposure"]),
        ("ab project accept-history-secrets", ["project", "accept-history-secrets"]),
    ]),
    ("ab session", [
        ("ab session", ["session"]),
        ("ab session ls", ["session", "ls"]),
        ("ab session preview", ["session", "preview"]),
        ("ab session kill", ["session", "kill"]),
        ("ab session gc", ["session", "gc"]),
    ]),
    ("ab keys", [
        ("ab keys", ["keys"]),
        ("ab keys rotate", ["keys", "rotate"]),
        ("ab keys set", ["keys", "set"]),
        ("ab keys ls", ["keys", "ls"]),
        ("ab keys rm", ["keys", "rm"]),
    ]),
]


def help_text(args):
    p = subprocess.run([sys.executable, "-m", "agentbox", *args, "--help"], capture_output=True, text=True)
    return (p.stdout or p.stderr).rstrip("\n")


def main():
    out = [
        "# `ab` command reference",
        "",
        "The full `--help` for every `agentbox` (`ab`) command and subcommand. This file is **generated** from "
        "the CLI, so it is exactly what the installed launcher prints. Regenerate it with:",
        "",
        "```bash",
        "python3 docs/gen_usage.py > docs/USAGE.md",
        "```",
        "",
        "For the two command namespaces and when to reach for each, see `docs/design.md` (Usage) and "
        "`docs/PERSONALIZATION.md`. In short: **`ab project`** manages the durable registry entries "
        "(configuration), **`ab session`** manages the live sandboxed runs, and `ab run` / `ab init` are the "
        "verbs that create them.",
        "",
        "## Contents",
        "",
    ]
    for section, _cmds in GROUPS:
        anchor = section.lower().replace(" ", "-").replace("`", "")
        out.append(f"- [{section}](#{anchor})")
    out.append("")
    for section, cmds in GROUPS:
        out.append(f"## {section}")
        out.append("")
        for title, args in cmds:
            out += [f"### `{title}`", "", "```text", help_text(args), "```", ""]
    sys.stdout.write("\n".join(out).rstrip("\n") + "\n")


if __name__ == "__main__":
    main()
