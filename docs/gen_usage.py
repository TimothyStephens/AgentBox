#!/usr/bin/env python3
"""Generate docs/USAGE.md from the live `ab --help` output, so the reference is always exactly what the
installed launcher prints. Run from the repo root:

    python3 docs/gen_usage.py > docs/USAGE.md
"""
import os
import subprocess
import sys
import tempfile

# (section heading, [(command title, argv)]) in the order the CLI presents them.
GROUPS = [
    ("Top level", [("ab", [])]),
    ("ab init", [("ab init", ["init"])]),
    ("ab run", [("ab run", ["run"])]),
    ("ab selftest", [("ab selftest", ["selftest"])]),
    ("ab project", [
        ("ab project", ["project"]),
        ("ab project add", ["project", "add"]),
        ("ab project set", ["project", "set"]),
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


# A one-line, high-level statement of what each command is for, shown above its --help.
INTENT = {
    "ab": "The launcher. `ab project` manages saved configuration; `ab session` manages live runs; "
          "`ab run` and `ab init` create them.",
    "ab init": "Scaffold a new project with a safe-by-construction layout (secrets masked, live data in a "
               "named volume) and register it, so a fresh project starts a session with no warnings.",
    "ab run": "Start an agent session against a project in the foreground — build the sandbox, run the agent, "
              "tear it down on exit.",
    "ab selftest": "Spin up a throwaway session and prove the sandbox boundaries hold (posture, egress, "
                   "mounts, podman scoping).",
    "ab project": "Manage the durable registry entries — a project names a code grant, a preset and any extra "
                  "data grants. One project has many sessions over time.",
    "ab project add": "Register a project with a preset, validating it against overlap and grant exposure.",
    "ab project set": "Change a registered project's settings in place, re-validated, with the same flags "
                      "`ab init`/`ab project add` take. Refused while a session for the project is live.",
    "ab project show": "Print a project's effective settings and whether each came from the preset or an override.",
    "ab project ls": "List the registered projects.",
    "ab project rm": "Remove projects from the registry; their files, state and backups are left untouched.",
    "ab project reset-state": "Wipe a project's agent state (memory, settings, transcripts), keeping a copy.",
    "ab project accept-grant-exposure": "Record that you accept the secrets/live data grantcheck found in a "
                                        "project's grant.",
    "ab project accept-history-secrets": "Record that you accept (after rotating) the masked file names in a "
                                         "project's git history.",
    "ab session": "Manage the live instances — list, preview, kill and garbage-collect running sessions.",
    "ab session ls": "List sessions with their project, start time, age and idle time.",
    "ab session preview": "Publish one dev-stack service of a session on 127.0.0.1 so you can reach it over ssh -L.",
    "ab session kill": "Tear down a session and everything it created, keeping its logs and end-of-session scan.",
    "ab session gc": "Remove containers, networks and sockets left by dead sessions (also runs after boot).",
    "ab keys": "Manage model tokens and provider API keys; they live outside every grant and reach a session "
               "read-only as AB_KEY_<PROVIDER>.",
    "ab keys rotate": "Replace the Claude subscription token.",
    "ab keys set": "Set a provider's API key (use a dedicated, spend-capped key).",
    "ab keys ls": "List providers that have a key (names only, never values).",
    "ab keys rm": "Remove a provider's key file.",
}


EMPTY_CONFIG = tempfile.mkdtemp(prefix="ab-usage-")


def help_text(args):
    # An empty config dir, so `--preset`'s dynamic list shows the built-in presets only: the generated
    # reference must not depend on whose machine ran it (or on their ~/.config/agentbox/presets/).
    env = dict(os.environ, AB_CONFIG_DIR=EMPTY_CONFIG)
    p = subprocess.run([sys.executable, "-m", "agentbox", *args, "--help"], capture_output=True, text=True,
                       env=env)
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
            out.append(f"### `{title}`")
            out.append("")
            if INTENT.get(title):
                out += [INTENT[title], ""]
            out += ["```text", help_text(args), "```", ""]
    sys.stdout.write("\n".join(out).rstrip("\n") + "\n")


if __name__ == "__main__":
    main()
