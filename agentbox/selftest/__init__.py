"""Orchestrates `ab selftest`: a real session whose command is the probe."""
import os

from .. import session as sessionmod
from .. import sockguard
from ..config import HOME
from ..registry import load as load_registry
from . import runner
from .runner import Result


def _token_value(path):
    if not path or not os.path.isfile(path):
        return ""
    with open(path) as f:
        return f.read().strip()


def _secret_values(sess):
    return [v for v in (_token_value(p) for p in sess.secret_files()) if v]


def _state_mounts(sess):
    if sess.adapter.state_mount:
        return [sess.adapter.state_mount]
    return [dst for src, dst, ro in sess.setup.mounts if not ro and _inside(src, sess.state_dir)]


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _grep_tree(root, needle):
    """Paths under root whose contents contain needle (bytes)."""
    hits = []
    if not needle or not os.path.exists(root):
        return hits
    if os.path.isfile(root):
        roots = [(os.path.dirname(root), [], [os.path.basename(root)])]
    else:
        roots = os.walk(root)
    for dirpath, _dirs, files in roots:
        for fn in files:
            p = os.path.join(dirpath, fn)
            try:
                if os.path.islink(p) or os.path.getsize(p) > 64 << 20:
                    continue
                with open(p, "rb") as f:
                    if needle in f.read():
                        hits.append(p)
            except OSError:
                pass
    return hits


def build_cases(sess, settings):
    p = sess.project
    reg = load_registry(settings.registry_path)
    others = [(n, q["root"]) for n, q in reg.items() if n != p.name and q.get("root")]
    other_state = [settings.project_dir(n) for n, _ in others]
    host_protected = [os.path.join(HOME, ".ssh"), os.path.join(HOME, ".claude"), settings.config_dir,
                      settings.data_dir, settings.projects_dir]
    b = runner.Builder()
    runner.filesystem_cases(b, project=p, plan=sess.plan, other_roots=others, other_state_dirs=other_state,
                            secret_envs=sess.secret_env_names, state_mounts=_state_mounts(sess),
                            host_protected=host_protected, uid=os.getuid(),
                            required_tools=sess.adapter.required_tools)
    sess.add_network_cases(b)
    dev = sess.podman_dev
    runner.podman_cases(b, socket_path=sessionmod.LIVE_SOCKET if dev else sockguard.AGENT_SOCKET,
                        compose_project=sess.podman_live, own=getattr(sess, "live_containers", []),
                        canary=getattr(sess, "canary", None), logs=sess.project.observe_logs, absent_ok=not dev)
    if dev:
        protected = [sess.name, sess.proxy_name, sess.sockguard_dev_name, sess.holder_name]
        if sess.podman_live:
            protected.append(sess.sockguard_name)
        runner.dev_cases(b, socket_path=sockguard.AGENT_SOCKET, canary=getattr(sess, "canary", None),
                         protected=protected)
    return b


CANARY_IMAGE = "docker.io/library/busybox:1.36"


def make_canary(sess, settings):
    """A stopped container in a compose project of its own, standing in for every other project on the host.
    It carries the session's labels, so teardown removes it. Returns its name, or None if it cannot be made."""
    name = f"{sess.name}-canary"
    args = ["create", f"--name={name}", "--network=none", "--pull=never", "--cap-drop=ALL"]
    args += [f"--label={k}={v}" for k, v in sessionmod.labels_for(settings, sess.project.name, sess.name,
                                                                  "canary").items()]
    args += [f"--label={sockguard.COMPOSE_LABEL}={name}", "--env=CANARY_SECRET=canary", CANARY_IMAGE, "true"]
    p = sess.podman.run(args, check=False)
    return name if p.returncode == 0 else None


def live_containers(sess):
    p = sess.podman.run(["ps", "-a", "--format", "{{.Names}}", "--filter",
                         f"label={sockguard.COMPOSE_LABEL}={sess.podman_live}"], check=False)
    return sorted((p.stdout or "").split())


def cli_version_result(agent, cli_ver):
    """PASS with the image's recorded agent CLI version, or SKIP when the image carries no version label
    (every Result needs a detail — an earlier two-arg PASS crashed once the label was present, 16 Oct)."""
    if cli_ver:
        return Result(f"{agent} CLI version", "PASS", cli_ver)
    return Result(f"{agent} CLI version", "SKIP",
                  f"the image has no agentbox.cli-version label; rebuild with `make {agent}-image`")


def run(settings, agent, project_name, *, podman=None, warn=None):
    """Returns (results, raw_output)."""
    project = sessionmod.load_project(settings, project_name)
    sess = sessionmod.make_session(settings, agent, project, podman=podman, warn=warn)
    sess.prepare()
    extra = []
    if sess.podman_live or sess.podman_dev:
        sess.live_containers = live_containers(sess) if sess.podman_live else []
        sess.canary = make_canary(sess, settings)
        if not sess.canary:
            extra.append(Result("canary container in another project", "SKIP",
                                f"could not create it ({CANARY_IMAGE} not present?); cross-project cases skipped"))
    b = build_cases(sess, settings)
    sess.probe_mounts = ((runner.PROBE, sessionmod.PROBE_PATH),)
    config_check = []
    proc = sess.run_captured(["bash", sessionmod.PROBE_PATH], stdin=b.script(), timeout=900,
                             before=lambda: config_check.append(podman_config_check(sess)))
    out = (proc.stdout or "")
    results = runner.score(b.cases, out)
    _diagnose(sess, b.cases, results, warn)
    if proc.returncode != 0 and not out.strip():
        results.append(Result("probe container ran", "FAIL", (proc.stderr or "").strip()[-400:]))
    results += config_check + extra
    results.append(cli_version_result(agent, sess.agent_cli_version()))
    results += host_checks(sess, settings, out + (proc.stderr or ""))
    secrets = _secret_values(sess)
    for r in results:
        r.detail = runner.scrub(r.detail, secrets)
    return results, runner.scrub(out, secrets)


def _diagnose(sess, cases, results, warn):
    """A failed proxied check gets the proxy's log lines for its host, the DNS answer and the time taken, in its
    detail and in <logs>/<session>-<start>-selftest-diag.log (Tim, 2 Oct: diagnose the egress flake)."""
    if not any(r.verdict == "FAIL" and runner.proxied_target(c) for c, r in zip(cases, results)):
        return
    try:
        with open(sess.log_path("proxy"), errors="replace") as f:
            proxy_log = f.read()
    except OSError:
        proxy_log = ""
    found = runner.diagnose_proxied_failures(cases, results, proxy_log)
    if found:
        path = sess.log_path("selftest-diag")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a") as f:
                f.write("\n\n".join(text for _l, text in found) + "\n")
            (warn or (lambda m: None))(f"agentbox: proxied checks failed; diagnostics in {path}")
        except OSError:
            pass


def podman_config_check(sess):
    """Create (not start) a container from the session's exact argv and read podman's stored config: no token
    or key may be in it, under any name."""
    secrets = _secret_values(sess)
    args = sess.run_args(["true"], extra_mounts=sess.probe_mounts)
    name = f"{sess.name}-cfgcheck"
    args = ["create"] + [f"--name={name}" if a.startswith("--name=") else a for a in args[2:]]
    created = sess.podman.run(args, check=False)
    if created.returncode != 0:
        return Result("model token and keys not in podman's container config", "FAIL",
                      f"could not create the check container: {(created.stderr or '').strip()[-200:]}")
    try:
        doc = sess.podman.run(["inspect", name], check=False).stdout or ""
    finally:
        sess.podman.run(["rm", "-f", name], check=False)
    bad = []
    if any(v in doc for v in secrets):
        bad.append("a token or key value is present")
    for var in sess.secret_env_names:
        if f'"{var}=' in doc:
            bad.append(f"{var} set in the stored environment")
    return Result("model token and keys not in podman's container config", "FAIL" if bad else "PASS",
                  "; ".join(bad) or "absent")


def host_checks(sess, settings, output):
    res = []
    p = sess.project
    snaps = settings.project_backups_dir(p.name)
    have = any(sess.name in d for d in os.listdir(snaps)) if os.path.isdir(snaps) else False
    res.append(Result("pre-session backup exists", "PASS" if have else "FAIL", snaps))
    leftovers = sess.podman.ids("container", {"agentbox.session": sess.name, **settings.labels})
    leftovers += sess.podman.ids("network", {"agentbox.session": sess.name, **settings.labels})
    res.append(Result("session torn down", "PASS" if not leftovers else "FAIL", " ".join(leftovers) or "clean"))
    secrets = [v.encode() for v in _secret_values(sess)]
    if secrets:
        hits = []
        for secret in secrets:
            for where in (sess.state_dir, snaps, os.path.join(settings.data_dir, "logs"),
                          settings.session_run_dir(sess.name)):
                hits += _grep_tree(where, secret)
            if secret in output.encode():
                hits.append("selftest output")
        res.append(Result("model token and keys not on disk (state, backups, logs) or in output",
                          "PASS" if not hits else "FAIL", " ".join(sorted(set(hits))) or "absent"))
    return res
