#!/usr/bin/env bash
# Learn OMP's plugin CLI and where an installed plugin LANDS. Run by `make omp-plugin-probe`, which pipes
# this into a session shell (`ab run --agent omp ... --shell`).
#
# Why a probe and not a guess: `ab profile install` has to run the non-interactive form of "add this
# marketplace at this commit, then install this plugin id", and then mount the result read-only in every
# session. Both the commands and the layout are OMP's to tell us. An installer written from a guess would
# run the wrong commands and report success.
#
# Safe by construction. It runs INSIDE a session, where `~/.omp` is a tmpfs and the project root is the
# only thing that persists -- so an install here writes nothing to the host and vanishes with the session.
# Nothing is written outside $HOME and /tmp. No `set -e`: every step's failure is itself an answer.
#
# What the output has to settle:
#   1. `omp plugin marketplace <?> <source>` -- the exact action word and argument order.
#   2. Whether a commit can be pinned on the add (the profile's `ref` is a 40-char sha), and how.
#   3. `omp plugin install <id>` -- flags, and what --json reports.
#   4. WHERE the installed plugin content goes, which is the tree agentbox must own and mount.
#   5. Whether `--scope user` vs `--scope project` changes that path.
set -u

EGM_SOURCE="google/elephant-goldfish-model"
EGM_ID="egm@egm"
EGM_REF="464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"

say() { printf '\n========== %s ==========\n' "$*"; }
run() { printf '\n$ %s\n' "$*"; "$@" 2>&1 | head -60; printf '[exit %s]\n' "${PIPESTATUS[0]}"; }

say "versions"
run omp --version
run node --version

say "the plugin actions"
run omp plugin --help
for action in marketplace install list link discover upgrade doctor; do
  run omp plugin "$action" --help
done

say "state before anything is installed"
run omp plugin list --json
run omp plugin marketplace list --json
printf '\n$ ls -A ~/.omp and ~/.omp/agent\n'
ls -A "$HOME/.omp" 2>&1
ls -A "$HOME/.omp/agent" 2>&1 | head -40

# Everything created from here is new, so the find below says exactly where OMP put things.
: > /tmp/ab-probe-stamp

say "add the marketplace (unpinned)"
run omp plugin marketplace add "$EGM_SOURCE"
run omp plugin marketplace list --json

say "can the add be pinned to a commit?"
run omp plugin marketplace add "${EGM_SOURCE}@${EGM_REF}"
run omp plugin marketplace add "$EGM_SOURCE" --ref "$EGM_REF"
run omp plugin marketplace add "$EGM_SOURCE" --commit "$EGM_REF"

say "install, dry run first"
run omp plugin install "$EGM_ID" --dry-run --json
run omp plugin install "$EGM_ID" --json

say "state after"
run omp plugin list --json
run omp plugin doctor --json

say "WHERE IT LANDED (new or changed under HOME since the stamp)"
find "$HOME" -maxdepth 7 -newer /tmp/ab-probe-stamp \
     -not -path "$HOME/.omp/agent/sessions/*" -not -path "$HOME/.omp/logs/*" 2>/dev/null | sort | head -80

say "project scope, for comparison"
run omp plugin install "$EGM_ID" --scope project --dry-run --json

say "done -- nothing here outlived the session"
