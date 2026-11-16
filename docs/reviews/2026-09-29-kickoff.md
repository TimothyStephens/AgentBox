Read CLAUDE.md and docs/design.md in full, then look at spikes/ (especially spike_gate.py and common.sh).

We are starting the build. Before writing code, give me a plan:

1. The repo layout for the `ab` launcher (Python, standard library only at runtime).
2. How you will do steps 1 and 2 of the design's build order (core launcher with the Claude Code adapter; per-session network with the squid egress sidecar). The model key gateway is deferred: Claude Code authenticates with `CLAUDE_CODE_OAUTH_TOKEN` read from a 0600 token file.
3. The tests that gate each step: which run in `make test` (no podman) and which in `make itest` (real podman, abt- names, label agentbox-test=1).
4. Anything in the design you think is wrong or underspecified, and the questions you need me to answer.

Wait for my go-ahead. After that, work step by step: write the tests, make them pass, run `make test` and `make itest` yourself, fix what fails, commit, and move on. Stop and ask me only when a decision is mine, when the guard blocks something you need, or when a test needs a real credential (write that target and tell me the command to run).
