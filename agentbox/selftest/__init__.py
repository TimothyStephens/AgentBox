"""Orchestrates `ab selftest`: a real session whose command is the probe."""
import os

from .. import session as sessionmod
from ..config import HOME
from ..registry import load as load_registry
from . import runner
from .runner import Result


def _token_value(path):
    if not path or not os.path.isfile(path):
        return ""
    with open(path) as f:
        return f.read().strip()


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
    other_state = [os.path.join(settings.data_dir, n) for n, _ in others]
    host_protected = [os.path.join(HOME, ".ssh"), os.path.join(HOME, ".claude"), settings.config_dir,
                      settings.data_dir, settings.backups_dir]
    b = runner.Builder()
    runner.filesystem_cases(b, project=p, plan=sess.plan, other_roots=others, other_state_dirs=other_state,
                            token_env=sess.adapter.token_env, state_mount=sess.adapter.state_mount,
                            host_protected=host_protected, uid=os.getuid(),
                            required_tools=sess.adapter.required_tools)
    sess.add_network_cases(b)
    return b


def run(settings, agent, project_name, *, podman=None, warn=None):
    """Returns (results, raw_output)."""
    project = sessionmod.load_project(settings, project_name)
    sess = sessionmod.make_session(settings, agent, project, podman=podman, warn=warn)
    sess.prepare()
    b = build_cases(sess, settings)
    sess.probe_mounts = ((runner.PROBE, sessionmod.PROBE_PATH),)
    config_check = []
    proc = sess.run_captured(["bash", sessionmod.PROBE_PATH], stdin=b.script(), timeout=900,
                             before=lambda: config_check.append(podman_config_check(sess)))
    out = (proc.stdout or "")
    results = runner.score(b.cases, out)
    if proc.returncode != 0 and not out.strip():
        results.append(Result("probe container ran", "FAIL", (proc.stderr or "").strip()[-400:]))
    results += config_check
    results += host_checks(sess, settings, out + (proc.stderr or ""))
    token = _token_value(sess.token_file)
    for r in results:
        r.detail = runner.scrub(r.detail, [token])
    return results, runner.scrub(out, [token])


def podman_config_check(sess):
    """Create (not start) a container from the session's exact argv and read podman's stored config: the
    token must not be in it, under any name."""
    token = _token_value(sess.token_file)
    args = sess.run_args(["true"], extra_mounts=sess.probe_mounts)
    name = f"{sess.name}-cfgcheck"
    args = ["create"] + [f"--name={name}" if a.startswith("--name=") else a for a in args[2:]]
    created = sess.podman.run(args, check=False)
    if created.returncode != 0:
        return Result("model token not in podman's container config", "FAIL",
                      f"could not create the check container: {(created.stderr or '').strip()[-200:]}")
    try:
        doc = sess.podman.run(["inspect", name], check=False).stdout or ""
    finally:
        sess.podman.run(["rm", "-f", name], check=False)
    bad = []
    if token and token in doc:
        bad.append("token value present")
    if f'"{sess.adapter.token_env}=' in doc:
        bad.append(f"{sess.adapter.token_env} set in the stored environment")
    return Result("model token not in podman's container config", "FAIL" if bad else "PASS",
                  "; ".join(bad) or "absent")


def host_checks(sess, settings, output):
    res = []
    p = sess.project
    snaps = os.path.join(settings.backups_dir, p.name)
    have = any(sess.name in d for d in os.listdir(snaps)) if os.path.isdir(snaps) else False
    res.append(Result("pre-session backup exists", "PASS" if have else "FAIL", snaps))
    leftovers = sess.podman.ids("container", {"agentbox.session": sess.name, **settings.labels})
    leftovers += sess.podman.ids("network", {"agentbox.session": sess.name, **settings.labels})
    res.append(Result("session torn down", "PASS" if not leftovers else "FAIL", " ".join(leftovers) or "clean"))
    token = _token_value(sess.token_file).encode()
    if token:
        hits = []
        for where in (sess.state_dir, snaps, os.path.join(settings.data_dir, "logs"),
                      settings.session_run_dir(sess.name)):
            hits += _grep_tree(where, token)
        if token in output.encode():
            hits.append("selftest output")
        res.append(Result("model token not on disk (state, backups, logs) or in output",
                          "PASS" if not hits else "FAIL", " ".join(hits) or "absent"))
    return res
