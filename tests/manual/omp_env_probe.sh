#!/usr/bin/env bash
# Probe (Toulmin contract, 3.5): which environment variables OMP sets in the subprocesses it runs. Piped into
# an OMP session shell by `make omp-env-probe`; everything runs inside the sandbox, nothing on the host changes.
# The shell pane's env is what a tool-run `tm` would see; the agent's own subprocess env is printed by asking
# OMP to run `env` as a tool call (the third block), which needs a model and so is skipped without one.
#
# The output goes to a terminal and /tmp/omp-env-probe.log, so NO VALUE that could be a secret is printed:
# every line passes through `redact`, which keeps values only for names that cannot hold one (AGENTBOX_*,
# OMP_*, PI_*, the proxy variables and a few shell basics), replaces secret-looking names' values with
# "<redacted, N chars>" and every other value with "<N chars>". The model is asked for names only, and its
# answer is redacted too, in case it pastes more. (The run of 11 Nov 2026 printed the session's AB_KEY_*
# values through an unescaped `env` in an echo; the keys had to be rotated.)
set -u

redact() {
  awk '
    index($0, "=") == 0 { print; next }
    {
      i = index($0, "="); name = substr($0, 1, i - 1); val = substr($0, i + 1); ln = tolower(name)
      if (name ~ /^(AGENTBOX_|OMP_|PI_)/ ||
          ln ~ /^(no_proxy|https?_proxy|all_proxy|path|home|pwd|hostname|container|node_version|shlvl|term|lang|lc_)/)
        print
      else if (ln ~ /(key|token|secret|pass|cred|auth)/)
        printf "%s=<redacted, %d chars>\n", name, length(val)
      else
        printf "%s=<%d chars>\n", name, length(val)
    }'
}

echo "== session environment (the shell agentbox started; values shown only where they cannot be a secret)"
env | sort | redact
echo "== omp version"
omp --version 2>&1 | head -1
echo "== what omp exports to a child: the model is asked to run env (names only), if one answers (up to 10 min)"
# A tool-calling turn is several model round-trips; with a 27B local model behind a load-on-demand server
# and OMP's own startup, 120 s was killing it mid-conversation (the proxy log of 11 Nov 2026 showed two
# completed POSTs and a third aborted by the SIGTERM). Ten minutes: this is a manual probe.
if omp --help 2>&1 | grep -q -- '--print\|-p,'; then
  timeout 600 omp -p 'Run the shell command: env | cut -d= -f1 | sort   and paste its output verbatim. Names only, no values.' 2>&1 \
    | redact | tail -80
else
  echo "(omp has no -p/--print mode in this version; run a session and ask the agent to run: env | cut -d= -f1 | sort)"
fi
