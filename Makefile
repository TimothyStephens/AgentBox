# The loop: make test (fast, no podman) -> make itest (real podman, self-cleaning) -> make clean
.PHONY: test itest spikes clean guard

guard:
	python3 .claude/hooks/test_guard.py

test: guard
	@echo "unit tests: add them under tests/ (no podman needed)"

itest:
	@echo "integration tests: add them under tests/integration/ (real podman, abt-* names, label agentbox-test=1)"

spikes:
	./spikes/run_all.sh

# Removes only what the tests created (label agentbox-test=1).
clean:
	-podman ps -aq --filter label=agentbox-test=1 | xargs -r podman rm -f
	-podman pod ps -q --filter label=agentbox-test=1 | xargs -r podman pod rm -f
	-podman network ls -q --filter label=agentbox-test=1 | xargs -r podman network rm
	-podman volume ls -q --filter label=agentbox-test=1 | xargs -r podman volume rm
