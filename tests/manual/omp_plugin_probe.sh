#!/usr/bin/env bash
# Round 2. Learn the rest of OMP's plugin mechanics, so `ab profile install` can be written from facts.
# Run by `make omp-plugin-probe`, which pipes this into a session shell (`ab session start omp ... --shell`).
#
# WHAT ROUND 1 SETTLED (5 Nov; full reasoning in docs/decisions.md):
#   * `omp plugin marketplace add` CANNOT be pinned -- `owner/repo@sha`, `--ref` and `--commit` are all
#     refused -- but its own error message says a LOCAL PATH is a valid source. So agentbox clones the
#     catalog at the declared sha and adds that path. Pinning is agentbox's job.
#   * the tree is `~/.omp/plugins/` (cache/marketplaces/<name>, cache/plugins/<id>___<version>,
#     node_modules, package.json) plus `~/.omp/marketplaces.json` beside it.
#   * `plugin install --json` prints human text, so read state with `plugin list --json` instead.
#   * `omp plugin <action> --help` is the same generic help for every action.
#
# WHAT THIS ROUND HAS TO SETTLE:
#   1. Is `node` in a session the image's node 26, or Bun's shim reporting v22? (Round 1 printed v22.23.4
#      on an image built FROM node:26-bookworm-slim.)
#   2. Does `omp plugin install <pkg>@<version>` take an npm package, and where does it land?
#   3. What does `omp plugin link <path>` do, and how does it report?
#   4. Does the pinned route work: clone a catalog, check out the sha, `marketplace add <path>`, install?
#   5. THE DECIDING ONE: does OMP tolerate a READ-ONLY ~/.omp/plugins? If it insists on writing there,
#      the whole "host-owned tree, mounted read-only" design needs rethinking before it is built.
#
# Safe by construction: inside a session `~/.omp` is a tmpfs, so everything here vanishes with the session
# and nothing touches the host. Writes go to $HOME and /tmp only. No `set -e`: each failure is an answer.
set -u

EGM_SOURCE="https://github.com/google/elephant-goldfish-model.git"
EGM_REF="464db031e7dd1b9a79803ac8ea45f55c7c20c5bc"
NPM_PKG="pi-rules"
NPM_VERSION="0.1.1"
REPO_URL="https://github.com/TimothyStephens/pi-session-stats.git"
REPO_REF="9c7a0c2dd1900dcd98aa7be0d84bbee214967d96"

say() { printf '\n========== %s ==========\n' "$*"; }
run() { printf '\n$ %s\n' "$*"; "$@" 2>&1 | head -60; printf '[exit %s]\n' "${PIPESTATUS[0]}"; }

say "1. which node is this, really"
printf '\nPATH=%s\n' "$PATH"
run which -a node
run bash -c 'readlink -f "$(command -v node)"'
run bash -c 'ls -l /root/.bun/bin 2>&1 | head -20'
run bash -c 'for n in /usr/local/bin/node /usr/bin/node; do [ -x $n ] && printf "%s -> %s\n" $n "$($n --version)"; done'
run bun --version
run npm --version
run omp --version

say "2. an npm package as a plugin target"
run omp plugin install "${NPM_PKG}@${NPM_VERSION}" --dry-run --json
run omp plugin install "${NPM_PKG}@${NPM_VERSION}"
run omp plugin list --json
printf '\n$ what is under ~/.omp/plugins now\n'
find "$HOME/.omp/plugins" -maxdepth 3 -not -path "*/node_modules/.*" 2>/dev/null | sort | head -40
printf '\n$ cat ~/.omp/plugins/package.json\n'
cat "$HOME/.omp/plugins/package.json" 2>&1 | head -30

say "3. a local checkout, linked"
rm -rf /tmp/ab-probe-repo
run git clone --quiet "$REPO_URL" /tmp/ab-probe-repo
run git -C /tmp/ab-probe-repo checkout --quiet "$REPO_REF"
run git -C /tmp/ab-probe-repo log -1 --format=%H
run omp plugin link /tmp/ab-probe-repo
run omp plugin list --json
run omp plugin doctor --json

say "4. the pinned marketplace route: our clone, our sha, add the path"
rm -rf /tmp/ab-probe-egm
run git clone --quiet "$EGM_SOURCE" /tmp/ab-probe-egm
run git -C /tmp/ab-probe-egm checkout --quiet "$EGM_REF"
run git -C /tmp/ab-probe-egm log -1 --format=%H
run omp plugin marketplace add /tmp/ab-probe-egm
run omp plugin marketplace list --json
run omp plugin install egm@egm --json
run omp plugin list --json
printf '\n$ cat ~/.omp/marketplaces.json\n'
cat "$HOME/.omp/marketplaces.json" 2>&1 | head -40
printf '\n$ is the catalog in the cache at OUR sha?\n'
git -C "$HOME/.omp/plugins/cache/marketplaces/egm" log -1 --format=%H 2>&1
git -C "$HOME/.omp/plugins/cache/marketplaces/egm" remote -v 2>&1 | head -4

say "5. does OMP tolerate a read-only plugins tree?"
# A bind-mounted read-only tree answers EROFS where this answers EACCES, but the question is whether OMP
# TRIES to write at all, and that is the same either way.
chmod -R a-w "$HOME/.omp/plugins" 2>&1 | head -5
chmod a-w "$HOME/.omp/marketplaces.json" 2>&1 | head -5
run omp plugin list --json
run omp plugin doctor --json
run omp --version
printf '\n$ anything that changed under the read-only tree (should be nothing)\n'
find "$HOME/.omp/plugins" -newer "$HOME/.omp/marketplaces.json" 2>/dev/null | head -20
chmod -R u+w "$HOME/.omp/plugins" "$HOME/.omp/marketplaces.json" 2>/dev/null

say "done -- nothing here outlived the session"
