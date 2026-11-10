#!/usr/bin/env bash
# Probe (Toulmin contract, 3.5): which environment variables OMP sets in the subprocesses it runs. Piped into
# an OMP session shell by `make omp-env-probe`; everything runs inside the sandbox, nothing on the host changes.
# The shell pane's env is what a tool-run `tm` would see; the agent's own subprocess env is printed by asking
# OMP to run `env` as a tool call (the second block), which needs a model and so is skipped without one.
set -u
echo "== session environment (the shell agentbox started; AGENTBOX_* set by the launcher)"
env | grep -E '^(AGENTBOX_|OMP_|PI_|OPENAI_|ANTHROPIC_)' | sort
echo "== omp version"
omp --version 2>&1 | head -1
echo "== what omp exports to a child: run `env` through omp in non-interactive mode, if a model answers"
if omp --help 2>&1 | grep -q -- '--print\|-p,'; then
  timeout 120 omp -p 'Run the shell command `env | grep -E "^(AGENTBOX_|OMP_|PI_)" | sort` and paste its output verbatim.' 2>&1 | tail -40
else
  echo "(omp has no -p/--print mode in this version; run a session and ask the agent to run env)"
fi
