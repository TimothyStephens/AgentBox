"""`ab` command line."""
import argparse
import json
import os
import sys

from . import config, presets, registry, session, tokens
from .config import HOME
from .errors import AgentboxError
from .paths import check_grant
from .podman import Podman


def warn(msg):
    print(msg, file=sys.stderr)


def cmd_project_add(s, a):
    registry.check_name(a.name)
    reg = registry.load(s.registry_path)
    if a.name in reg:
        raise AgentboxError(f"project {a.name} already exists")
    others = [(n, q["root"]) for n, q in reg.items() if q.get("root")]
    kw = dict(home=HOME, extra_protected=session.protected_extra(s), other_roots=others)
    entry = {"root": check_grant(a.root, kind="root", **kw), "preset": a.preset}
    if a.data_ro:
        entry["data_ro"] = [check_grant(d, kind="data_ro", **kw) for d in a.data_ro]
    if a.data_rw:
        entry["data_rw"] = [check_grant(d, kind="data_rw", **kw) for d in a.data_rw]
    if a.egress:
        entry["egress"] = a.egress
    if a.extra_domain:
        from .egress import check_domains
        entry["extra_domains"] = check_domains(a.extra_domain, "--extra-domain")
    if a.mask_skip:
        entry["mask_skip"] = list(a.mask_skip)
    # Validate the whole thing (preset exists, lockdown ceiling) before saving.
    presets.effective(a.name, entry, presets.load_presets(s.presets_dir))
    reg[a.name] = entry
    registry.save(s.registry_path, reg)
    print(f"added project {a.name} ({entry['root']}, preset {a.preset})")


def cmd_project_show(s, a):
    p = session.load_project(s, a.name)
    print(f"project {p.name}")
    print(f"  root           {p.root}")
    for d in p.data_ro:
        print(f"  data_ro        {d}")
    for d in p.data_rw:
        print(f"  data_rw        {d}")
    for k in presets.SETTINGS:
        v, src = p.eff[k]
        v = v.get("tier") + (f" (live: {v['live']})" if v.get("live") else "") if isinstance(v, dict) else v
        print(f"  {k:<14} {v:<24} from {src}")
    if p.mask_skip:
        print(f"  mask_skip      {', '.join(p.mask_skip)}")
    if p.extra_domains:
        print(f"  extra_domains  {', '.join(p.extra_domains)}")


def cmd_project_ls(s, a):
    for name, q in sorted(registry.load(s.registry_path).items()):
        print(f"{name:<20} {q.get('preset', '?'):<10} {q.get('root', '?')}")


def cmd_project_rm(s, a):
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}")
    del reg[a.name]
    registry.save(s.registry_path, reg)
    print(f"removed project {a.name} from the registry (files, state and backups are untouched)")


def cmd_run(s, a):
    project = session.load_project(s, a.project, with_secrets=a.with_secrets)
    sess = session.make_session(s, a.agent, project, with_secrets=a.with_secrets, warn=warn)
    sess.prepare()
    command = sess.agent_command(shell=a.shell, extra=a.args)
    if sys.stdin.isatty() and not a.no_tty:
        return sess.run_interactive(command, tty=True)
    p = sess.run_captured(command, stdin=sys.stdin.read() if not sys.stdin.isatty() else "",
                          timeout=a.timeout)
    sys.stdout.write(p.stdout or "")
    sys.stderr.write(p.stderr or "")
    return p.returncode


def cmd_ls(s, a):
    podman = Podman(s.podman)
    rows = session.sessions(s, podman)
    for name, info in sorted(rows.items()):
        meta = info["meta"] or {}
        state = "running" if info["alive"] else "dead"
        print(f"{name:<40} {meta.get('project', '?'):<16} {state:<8} "
              f"{len(info['containers'])} containers, {len(info['networks'])} networks")


def cmd_kill(s, a):
    podman = Podman(s.podman)
    known = session.sessions(s, podman)
    if a.session not in known:
        raise AgentboxError(f"no session named {a.session}")
    session.teardown(s, podman, a.session)
    print(f"killed {a.session}")


def cmd_gc(s, a):
    removed = session.gc(s, Podman(s.podman))
    print("removed: " + (" ".join(removed) if removed else "nothing"))


def cmd_selftest(s, a):
    from . import selftest
    results, raw = selftest.run(s, a.agent, a.project, warn=warn)
    fails = [r for r in results if r.verdict == "FAIL"]
    if a.json:
        print(json.dumps([r.__dict__ for r in results], indent=1))
    else:
        for r in results:
            print(f"  {r.verdict:<4}  {r.label}" + (f"  [{r.detail}]" if r.verdict != "PASS" else ""))
        print(f"\nRESULT selftest {a.agent}/{a.project}: {sum(r.verdict == 'PASS' for r in results)} pass, "
              f"{len(fails)} fail, {sum(r.verdict == 'SKIP' for r in results)} skip")
    return 1 if fails else 0


def cmd_keys_rotate(s, a):
    if a.agent != "claude":
        raise AgentboxError("only `ab keys rotate claude` is supported")
    tokens.rotate(s.token_file(a.agent), a.agent)


def parser():
    p = argparse.ArgumentParser(prog="ab", description="agentbox: coding agents in hardened podman sandboxes")
    sub = p.add_subparsers(dest="cmd", required=True)

    proj = sub.add_parser("project", help="manage registered projects").add_subparsers(dest="sub", required=True)
    x = proj.add_parser("add")
    x.add_argument("name")
    x.add_argument("root")
    x.add_argument("--preset", required=True)
    x.add_argument("--data-ro", action="append", default=[])
    x.add_argument("--data-rw", action="append", default=[])
    x.add_argument("--egress", choices=presets.EGRESS)
    x.add_argument("--extra-domain", action="append", default=[], help="add a domain to the egress allowlist")
    x.add_argument("--mask-skip", action="append", default=[], help="folder (relative to root) the mask walk skips")
    x.set_defaults(fn=cmd_project_add)
    x = proj.add_parser("show")
    x.add_argument("name")
    x.set_defaults(fn=cmd_project_show)
    proj.add_parser("ls").set_defaults(fn=cmd_project_ls)
    x = proj.add_parser("rm")
    x.add_argument("name")
    x.set_defaults(fn=cmd_project_rm)

    x = sub.add_parser("run", help="start an agent session in the foreground")
    x.add_argument("--agent", required=True)
    x.add_argument("--project", required=True)
    x.add_argument("--with-secrets", action="store_true")
    x.add_argument("--shell", action="store_true", help="a bash shell in the sandbox instead of the agent")
    x.add_argument("--no-tty", action="store_true")
    x.add_argument("--timeout", type=int, default=3600, help="non-interactive runs only (seconds)")
    x.add_argument("args", nargs=argparse.REMAINDER, help="after --: extra arguments for the agent or shell")
    x.set_defaults(fn=cmd_run)

    sub.add_parser("ls", help="list sessions").set_defaults(fn=cmd_ls)
    x = sub.add_parser("kill", help="tear down a session")
    x.add_argument("session")
    x.set_defaults(fn=cmd_kill)
    sub.add_parser("gc", help="remove what dead sessions left behind").set_defaults(fn=cmd_gc)

    x = sub.add_parser("selftest", help="prove the sandbox boundaries hold")
    x.add_argument("--agent", required=True)
    x.add_argument("--project", required=True)
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_selftest)

    keys = sub.add_parser("keys", help="model tokens").add_subparsers(dest="sub", required=True)
    x = keys.add_parser("rotate")
    x.add_argument("agent")
    x.set_defaults(fn=cmd_keys_rotate)
    return p


def main(argv=None):
    a = parser().parse_args(argv)
    if getattr(a, "args", None) and a.args[:1] == ["--"]:
        a.args = a.args[1:]
    try:
        s = config.load()
        rc = a.fn(s, a)
        return rc or 0
    except AgentboxError as e:
        print(f"ab: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
