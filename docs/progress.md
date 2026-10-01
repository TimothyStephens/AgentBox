# Progress

The builder's running state, updated in every commit, so a crashed session loses nothing. The spec is `docs/design.md`; deviations are in `docs/decisions.md`.

**Updated:** 1 Oct 2026

## Current step

Step 5 (host-side gate, dev tier, end-of-session scan) is built and green. **Stopped:** step 5 gets its own independent review before anything uses it on real projects.

## Done

- **Steps 1-4**, the follow-ups and the review of steps 3-4 (`b151415`).
- **Step 5** (this commit; details in `decisions.md`, "Step 5: the host-side gate and the dev tier, as built"):
  - **The gate** (`agentbox/gate.py`): in front of a dev sockguard with owner isolation. It:
    - pins creates and pods to the session network;
    - refuses ports, privilege, devices, host and container namespaces, binds outside the grants, and client cgroup parents;
    - stamps labels, limits, the slice and OOM 1000, and adds proxy variables;
    - routes builds through the holder's namespace with the build cap, no pulls, proxy build args, and the client's security knobs dropped;
    - allowlists pull registries;
    - scopes every named resource and exec session to the session's own dev stack.
  - **The `pods` preset:** the dev tier, plus the live stack read-only on `/run/agentbox/podman-live.sock`.
  - **Masks** are a read-only empty file, not `/dev/null`: device nodes broke rootless builds of the project.
  - **End-of-session scan** (`agentbox/scan.py`).
  - **Gate:**
    - `make itest`, step 5 modules: discovery, a compose stack reached by name, the refusals, builds that cannot reach the LAN, a forged namespace ignored, selftest for both projects, cleanup;
    - the scan in a real session;
    - unit tests for every gate rewrite and refusal.

## Next

1. **Independent review of step 5.** Points to look at first:
   - the gate is new code at the security boundary (`agentbox/gate.py`);
   - proxy variables versus bare service names (see Open decisions);
   - built images stay in the store after a session;
   - podman-compose `down` is refused deleting the session network (harmless, but it prints an error).
2. Then step 6 (`ab preview`, `ab deploy`).

## Open decisions (Tim)

- **OMP repo-provided code in `lockdown`** (review #2): accept, or run lockdown OMP sessions with `--no-extensions`, which also drops the plugins?
- **Dev services calling each other over HTTP by bare name** (`http://web:8080`) go to squid when the client uses the proxy variables, and squid cannot resolve them. The agent itself uses `--noproxy`. Options:
  - leave it, and document per project;
  - give squid the dev containers' names (the gate learns them at create);
  - set no proxy variables in dev containers, so they can only reach their own network.

## Waiting on Tim

- **`make install`:** remove your temporary `~/.local/bin/ab` first (`make install` refuses to overwrite it).
- **`make claude-live`:** token-handling files changed again, so `make itest` warns until a new pass is recorded.
- **`make omp-live`:** the OMP adapter changed in the review fixes. Rerun when convenient.
