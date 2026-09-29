# The loop: make test (fast, no podman) -> make itest (real podman, self-cleaning) -> make clean
.PHONY: test itest spikes clean guard install-slice claude-live

guard:
	python3 .claude/hooks/test_guard.py

test: guard
	python3 -m unittest discover -s tests/unit -t . $(if $(V),-v,)

# Real podman. Every resource is named abt-<run>-* and labelled agentbox-test=1 plus
# agentbox-test-run=<run>; the suite removes its own run even on failure.
itest:
	python3 -m unittest discover -s tests/integration -t . -v

spikes:
	./spikes/run_all.sh

# Run by Tim, not by the builder: uses the real token in ~/.config/agentbox/ (read in place).
# AB_LIVE_EGRESS=packages or open tries another mode (default locked).
claude-live:
	python3 -m tests.live.claude_live

# Run by Tim, not by the builder: installs the cgroup slice every agentbox container runs in.
install-slice:
	mkdir -p $(HOME)/.config/systemd/user
	install -m 644 deploy/agentbox.slice $(HOME)/.config/systemd/user/agentbox.slice
	systemctl --user daemon-reload
	systemctl --user show agentbox.slice -p LoadState -p MemoryMax -p MemorySwapMax -p TasksMax

# Removes only what the tests created (label agentbox-test=1).
clean:
	-podman ps -aq --filter label=agentbox-test=1 | xargs -r podman rm -f
	-podman pod ps -q --filter label=agentbox-test=1 | xargs -r podman pod rm -f
	-podman network ls -q --filter label=agentbox-test=1 | xargs -r podman network rm
	-podman volume ls -q --filter label=agentbox-test=1 | xargs -r podman volume rm
