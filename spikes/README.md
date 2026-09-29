# agentbox spikes

Five small experiments that test the assumptions the agentbox design depends on. Run them on ubuntu-dev as `vmuser` before building anything. Each prints `PASS` / `FAIL` / `SKIP` / `INFO` lines and a `RESULT` summary.

| Spike | Question | If it fails |
|---|---|---|
| 1 `spike1_gate.sh` | Can a thin gate pin containers and builds to one network and refuse port publishing? | The gate needs a full API proxy, or the dev tier drops builds |
| 2 `spike2_sockguard.sh` | Does sockguard scope a client to one compose project on rootless podman, including `exec`? | Scope by name, or enforce exec in the gate |
| 3 `spike3_squid.sh` | Do the `locked` and `open` egress modes behave, including names that resolve to private IPs? | Rework the squid rules |
| 4 `spike4_gateway.sh` | Can Claude Code (and OMP) work through a key-holding gateway with only a dummy key? | Keys stay in the sandbox environment |
| 5 `spike5_compose_isolate.sh` | Does podman-compose pass `isolate: "strict"` through, and does it isolate? | The launcher creates networks itself |

## Safety

- Everything created is named `spike-*` and/or labelled `agentbox-spike=1`, and is removed when each script exits, including on Ctrl-C.
- Spikes 1 and 2 talk to a private podman API service started on a temp socket. Your `podman.socket` unit is not touched.
- Nothing touches your existing containers, networks, volumes or compose projects.
- Images pulled (kept afterwards; remove with `podman rmi` if you like): `alpine:3.20`, `curlimages/curl`, `ubuntu/squid`, `berriai/litellm`, `codeswhat/sockguard`.
- `spike_gate.py` is a feasibility prototype, not a security boundary.
- The results file never contains your API key; spike 4 redacts it from any log it prints.

## Prerequisites

- podman 5.x (rootless), `curl`, `python3` (standard library only), `podman-compose` for spike 5.
- Internet access from the host to pull images and, for spikes 3 and 4, from containers.

## Run

```bash
tar xzf agentbox-spikes.tar.gz && cd agentbox-spikes
./run_all.sh                          # all five; saves spike-results-<timestamp>.txt
./run_all.sh 1 2 5                    # only some, still saved to a results file
./spike3_squid.sh                     # or one at a time, printed only
```

Spike 4 needs an API key from an active Anthropic organization. It is only ever passed to the gateway container. Put it in a file only you can read, so it never appears on a command line, in your shell history, or in this chat:

```bash
install -m 600 /dev/null ~/.spike-key && nano ~/.spike-key     # paste the key, save
export SPIKE_ANTHROPIC_KEY_FILE=~/.spike-key
export SPIKE_ANTHROPIC_MODEL=claude-sonnet-4-5                 # a model NAME, never the key
./run_all.sh 4
rm ~/.spike-key                                                # afterwards
```

## Settings

| Variable | Default | Used by |
|---|---|---|
| `SPIKE_LAN_TARGET` | `10.0.0.103:8088` | 1, 3: a LAN service that answers from the host |
| `SPIKE_MODEL_TARGET` | `10.0.0.2:8080` | 3: the local model server |
| `SPIKE_SOCKGUARD_IMAGE` | `docker.io/codeswhat/sockguard:latest` | 2: pin a release tag if you know one |
| `SPIKE_SOCKGUARD_ARGS` | `serve -c /config/sockguard.yaml` | 2: if sockguard's CLI differs |
| `SPIKE_SQUID_IMAGE` | `docker.io/ubuntu/squid:latest` | 3 |
| `SPIKE_GATEWAY_IMAGE` | `ghcr.io/berriai/litellm:main-stable` | 4 |
| `SPIKE_ANTHROPIC_KEY_FILE` | none | 4: path to a 0600 file holding the key (preferred) |
| `SPIKE_ANTHROPIC_API_KEY` | none | 4: the key itself, if not using a file |
| `SPIKE_ANTHROPIC_MODEL` | `claude-sonnet-4-5` | 4 |
| `SPIKE_CLAUDE_IMAGE` | `localhost/claudecode:latest` | 4 |
| `SPIKE_OMP_IMAGE` | `localhost/omp-env:latest` | 4 |
| `SPIKE_OMP_MODELS`, `SPIKE_OMP_ARGS` | none | 4: optional OMP test, below |

## Optional: OMP in spike 4

OMP's `models.yml` format is yours, so this part is manual:

1. Copy your `~/.omp/agent/models.yml` to a new file.
2. Keep one OpenAI-compatible provider entry. Set its `baseUrl` to `http://spike-gateway:4000/v1`, its key to the literal text `SPIKE_MASTER_KEY` (the script substitutes the dummy key), and its model to the same name as `SPIKE_ANTHROPIC_MODEL` (the gateway passes any Anthropic model name through).
3. Make that model the default, or put the flag that selects it in `SPIKE_OMP_ARGS` (check `omp --help`).
4. `export SPIKE_OMP_MODELS=/path/to/that/file` and run spike 4.

Delete the copy afterwards if it still holds any real keys.

## Reading the results

- **Spike 1:** rootless builds here cannot use a named network (v3: even `podman build --network <name>` failed on the host). So v4 tests two ways to contain a build:
    - **offline** (`none`): works, but breaks any RUN step that downloads packages;
    - **joining a holder container's network namespace** (`ns:/proc/<pid>/ns/net`), where the holder sits on the internal project network next to a stand-in proxy.
  Each build line shows whether the RUN step reached the LAN, whether it reached the stand-in proxy, and its addresses. The observe part shows what podman's own client sends for each. The first `KEY:` line decides whether builds can download through the project proxy; the second whether they can at least be forced offline.
- **Spike 2:** the "sockguard alone" part records a known finding: a client that drives the API directly can create and start an exec session in the other project's container. The `KEY:` line then puts the gate in front of sockguard and repeats it; it must be refused. The gate must also refuse to start an exec session that was created outside it. The bind-mount lines should name sockguard's bind-mount rule as the reason.
- **Spike 3:** in both modes the LAN, the private-resolving name and other ports on the model host must be `403`. Direct attempts must be unreachable, never `refused`.
- **Spike 4:** the Claude Code line decides whether keys can stay out of the sandbox. An `INFO` line saying the gateway reached Anthropic but the account was refused means the plumbing works and the key or organization is the problem.
- **Spike 5:** the `isolate=strict` line shows compose files can carry the option; the reachability line shows whether it actually isolates. If containers don't start, `INFO` lines say which one and why.

Paste `spike-results-<timestamp>.txt` back and the design doc gets updated from it.

## What was already checked, and a gap it found

- `spike_gate.py` passed 51 checks against a fake podman API:
    - pinning for libpod, docker-compat and pod creates, including chunked bodies and `--network host`;
    - refusal of `-p`, `-P` and pod port mappings before podman sees them;
    - the build strategies, using the encoding podman 5.7's client actually sends (numeric `networkmode`, network in `nsoptions`), including joining a holder's namespace and overriding a client that asks for host networking;
    - exec scoping: allowed label, the session's owner label, another project's container, a container upstream hides, the docker-compat path, and sessions created outside the gate;
    - untouched pass-through, and exec/attach upgrade streams in both directions.
  What it can't check here is podman itself honouring the rewritten requests; that is spike 1's job.
- Spike 1's build matrix was run end to end against a stub `podman` that sends the exact query your first runs captured, and echoes each step the way podman does. v2 missed that echo: it contains the words the test searches for, so every v2 build read as a leak. v3 matches whole output lines only.
- All scripts pass `shellcheck`, and each one's skip and failure paths were run against a stub `podman`. That run caught and fixed a bug in the host liveness check.
- **Gap for the real gate:** a container that joins an existing pod keeps that pod's network. A pod created through the gate is pinned, but an agent could name a pod the gate never saw, such as a live stack's pod. The real gate must refuse joining pods, and `container:` namespaces, the session doesn't own.

## Guesses baked in

These are flagged because they may need a tweak on your machine:

- sockguard's image name, CLI (`validate -c`, `serve -c`) and YAML keys (`listen.socket`, `upstream.socket`, `upstream.flavor`).
- That podman's build server takes the RUN network from `nsoptions` (confirmed: the client sends the network name there and `networkmode` as `0`/`1`/`2`). Spike 1 tests whether the server honours it.
- LiteLLM's `/v1/messages` endpoint and Claude Code's `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` variables.
- The `ubuntu/squid` image reading `/etc/squid/squid.conf`.
- Squid and the gateway resolving public names through the egress network's DNS; containers on an `--internal` network may not.
- podman-compose putting each project in a pod (its default); spike 5 finds IPs through the pod's infra container.
- The `busybox` image including `httpd` (Alpine's busybox does not).
