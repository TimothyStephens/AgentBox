"""`ab` command line."""
import argparse
import json
import os
import sys

from . import config, keys, presets, projectkeys, registry, session, tokens
from .config import HOME
from .errors import AgentboxError
from . import paths
from .paths import check_grant
from .podman import Podman


def warn(msg):
    print(msg, file=sys.stderr)


def cmd_init(s, a):
    from . import initproj
    return initproj.make(s, a, out=print, warn=warn)


def _check(name, key, value, **kw):
    """One value, through its own validator in the settings table (projectkeys.KEYS)."""
    return projectkeys.validate_value(key, f"project {name}", value, **kw)


def _grant_kw(s, _reg=None, exclude=None):
    """check_grant's context: is a path safe in itself? Cross-project overlap is paths.check_overlaps,
    called once per entry (it compares every grant pair, not just roots)."""
    return dict(home=HOME, extra_protected=session.protected_extra(s))


# All three derive from the one settings table (projectkeys.KEYS), which is where each key's properties are
# decided: which flag sets it, whether a preset change clears it, whether --unset takes it. They used to be
# hand-maintained lists, and five audit findings were keys missing from one of them.
SETTING_FLAGS = projectkeys.SETTING_FLAGS
SECURITY_OVERRIDES = projectkeys.CLEARED_ON_PRESET_CHANGE


def _given(a, key):
    """Was this entry key given explicitly on this command line?"""
    flag = SETTING_FLAGS.get(key)
    if not flag:
        return False
    value = getattr(a, flag[0], None)
    return value is not None and value != []


# `--unset KEY` takes every key the table marks unsettable (`root`/`preset` are what the project IS, and
# the acks have their own commands); `--clear-<list>` is the list-valued subset.
UNSETTABLE = projectkeys.UNSETTABLE
CLEAR_FLAGS = projectkeys.CLEAR_FLAGS


def _unset_keys(a):
    """The keys this command removes from the entry: --unset KEY (repeatable) plus the --clear-<list> flags."""
    keys = list(getattr(a, "unset", None) or [])
    for dest, key in CLEAR_FLAGS.items():
        if getattr(a, dest, False):
            keys.append(key)
    return list(dict.fromkeys(keys))


def _apply_project_settings(entry, a, grant_kw, presets_map, unset=()):
    """Apply the override flags shared by `ab init`, `ab project add` and `ab project set` onto a registry
    entry dict. For add/init, `entry` starts empty, so "if a.x:" writes only what was given. For `set`,
    `entry` starts as a copy of the project's existing stored entry, so omitting a flag leaves its stored
    value untouched, and giving one replaces it (a list flag replaces the whole list).

    Returns [(key, the value it held)] for every override the preset change dropped, so the caller can
    report what replaced it and whether that loosened the project (audit F7)."""
    cleared = []
    # Was a preset NEWLY chosen? True for a fresh entry (add/init, nothing to change from) and for a real
    # switch; False when no --preset was given, or when it names the same preset (an alias included). It
    # gates both the clearing and the open-egress write.
    old_preset = entry.get("preset")
    newly_chosen = bool(a.preset) and not (old_preset
                                           and presets.same_preset(old_preset, a.preset, presets_map))
    if newly_chosen and old_preset:
        for key in SECURITY_OVERRIDES:
            if key in entry and not _given(a, key):
                cleared.append((key, entry.pop(key)))
    if a.preset:
        entry["preset"] = a.preset
    if a.egress:
        entry["egress"] = a.egress
    if newly_chosen and "egress" not in unset:
        # Only a newly chosen preset may write its open default (audit F1): it used to run on every edit,
        # so `ab project set p1 --build-memory 4g` on a research project wrote egress=open as a side
        # effect. `--unset egress` means "no override of my own", so the fail-closed resolution applies
        # there instead (a preset-sourced open demotes to packages).
        presets.materialize_open_egress(entry, presets_map)
    if a.data_ro:
        entry["data_ro"] = [check_grant(d, kind="data_ro", **grant_kw) for d in a.data_ro]
    if a.data_rw:
        entry["data_rw"] = [check_grant(d, kind="data_rw", **grant_kw) for d in a.data_rw]
    if a.extra_domain:
        entry["extra_domains"] = _check(a.name, "extra_domains", list(a.extra_domain))
    if a.build_memory:
        entry["build_memory"] = a.build_memory
    if a.mask_skip:
        entry["mask_skip"] = list(a.mask_skip)
    if a.podman:
        entry["podman"] = {"tier": a.podman}
    if a.compose_project:
        entry["compose_project"] = _check(a.name, "compose_project", a.compose_project)
    if a.observe_logs is not None:
        entry["observe_logs"] = a.observe_logs
    if a.dev_image:
        entry["dev_images"] = _check(a.name, "dev_images", list(a.dev_image))
    return cleared


def precheck_project_settings(s, a):
    """Validate the project-setting flags before anything is written. `ab init` calls this first, so a bad
    flag leaves no half-scaffolded tree behind (which would then make a corrected retry fail with "already
    exists; refusing to scaffold over it"). Everything here is re-checked by cmd_project_add afterwards;
    only the checks that need the project root to exist are left to it."""
    reg = registry.load(s.registry_path)
    presets_map = presets.load_presets(s.presets_dir)
    entry = {}
    _apply_project_settings(entry, a, _grant_kw(s, reg), presets_map)
    presets.effective(a.name, entry, presets_map)
    if "build_memory" in entry:
        _check(a.name, "build_memory", entry["build_memory"], settings=s)


def _print_cleared(cleared, name, entry, presets_map):
    """What a preset change dropped, as before → after, labelled tightened/loosened/cleared.

    "cleared: egress" did not say that the project had just gone from locked to open (audit F7), so each
    line now names the value that replaced it: the new preset's for a setting it resolves, its tightest for
    an ordered project key (observe_logs), (none) otherwise."""
    if not cleared:
        return
    eff = presets.effective(name, entry, presets_map)
    print("  dropped with the old preset (set them again if you still want them):")
    for key, old in cleared:
        k = projectkeys.BY_NAME[key]
        if k.kind == "preset":
            new = eff[key][0]
        elif k.values:
            new = k.tightest
        else:
            new = None
        label = projectkeys.change_label(key, old, new) if new is not None else ""
        new_text = _render(k, new)[0] if new is not None else "(none)"
        print(f"    {key}: {_render(k, old)[0]} → {new_text} "
              f"({label or 'cleared'} by the preset change)")


def _validate_and_save(s, reg, name, entry, a, presets_map):
    """Validate a registry entry (preset exists, lockdown ceiling, grant overlap, build_memory, grantcheck,
    history secrets) and save it. Shared by `ab project add` and `ab project set`."""
    # The WHOLE entry, not just what this command touched: unknown keys and every stored value, then the
    # grants and their overlap (audit F11). All of it before anything is written.
    entry.update(projectkeys.validate_entry(name, entry, settings=s))
    grant_kw = _grant_kw(s)
    entry["root"] = check_grant(entry["root"], kind=f"project {name} root", **grant_kw)
    for key in ("data_ro", "data_rw"):
        if entry.get(key):
            entry[key] = [check_grant(d, kind=key, **grant_kw) for d in entry[key]]
    presets.effective(name, entry, presets_map)
    paths.check_overlaps(name, entry, reg)
    from . import grantcheck
    exposure, warnings = grantcheck.scan(entry["root"], [entry["root"]] + entry.get("data_rw", []),
                                         entry.get("mask_skip", []), covered=_mask_dsts(s, entry),
                                         warn_grants=entry.get("data_ro", []))
    for w in warnings:
        warn(f"ab: grant warning: {w}")
    if exposure and not a.i_understand_grant_exposure:
        raise AgentboxError(grantcheck.message(name, exposure))
    if exposure:
        entry["grant_exposure_accepted"] = grantcheck.fingerprint(exposure)
        warn(grantcheck.message(name, exposure).split("\nTo run anyway")[0]
             + f"\naccepted (--i-understand-grant-exposure); move secrets/live data out of the grant when you can.")
    else:
        entry.pop("grant_exposure_accepted", None)   # a stale ack from before the exposure was cleaned up
    found = _history_secrets(entry)
    if found and not a.accept_history_secrets:
        raise AgentboxError(session.history_secrets_message(name, found))
    if found:
        entry["history_secrets_accepted"] = found
        warn(f"ab: accepted {len(found)} masked file name(s) in git history: {', '.join(found[:15])}. "
             "The agent can read them through git: rotate them if you have not already.")
    reg[name] = entry
    registry.save(s.registry_path, reg)


def cmd_project_add(s, a):
    registry.check_name(a.name)
    reg = registry.load(s.registry_path)
    if a.name in reg:
        raise AgentboxError(f"project {a.name} already exists")
    presets_map = presets.load_presets(s.presets_dir)
    grant_kw = _grant_kw(s, reg)
    entry = {"root": check_grant(a.root, kind="root", **grant_kw)}
    cleared = _apply_project_settings(entry, a, grant_kw, presets_map)
    _validate_and_save(s, reg, a.name, entry, a, presets_map)
    print(f"added project {a.name} ({entry['root']}, preset {entry['preset']})")
    _print_cleared(cleared, a.name, entry, presets_map)
    _print_effective(s, a.name)


def cmd_project_set(s, a):
    """Change a registered project in place. Everything `ab project add` validates is validated again, so a
    `set` can never leave an entry a fresh `add` would have refused. Retires rm + add (which orphaned a live
    session) and hand-editing projects.toml; moving `root` is still rm + add."""
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}; `ab project ls` lists them")
    _refuse_if_live(s, [a.name], f"change {a.name}")
    entry = dict(reg[a.name])
    unset = _unset_keys(a)
    for key in unset:
        if _given(a, key):
            raise AgentboxError(f"--unset {key} and {SETTING_FLAGS[key][1]} in the same command: pick one")
    # Only the keys that were actually stored are "unset"; the rest had nothing to drop (audit F10: it used
    # to report every --unset as a change, whether or not the project held that key).
    dropped = [k for k in unset if k in entry]
    absent = [k for k in unset if k not in entry]
    for key in dropped:
        entry.pop(key, None)
    presets_map = presets.load_presets(s.presets_dir)
    grant_kw = _grant_kw(s, reg, exclude=a.name)
    entry["root"] = check_grant(entry["root"], kind=f"project {a.name} root", **grant_kw)
    cleared = _apply_project_settings(entry, a, grant_kw, presets_map, unset=unset)
    _validate_and_save(s, reg, a.name, entry, a, presets_map)
    print(f"updated project {a.name} ({entry['root']}, preset {entry['preset']})")
    if dropped:
        print(f"  unset (back to the preset): {', '.join(dropped)}")
    if absent:
        print(f"  not stored on this project, so nothing to unset: {', '.join(absent)}")
    _print_cleared(cleared, a.name, entry, presets_map)
    _print_effective(s, a.name)


def _mask_dsts(settings, entry):
    """The paths agentbox will mask or swap for this project (so an already-covered secret is not flagged)."""
    from . import mounts
    plan = mounts.plan(root=entry["root"], data_ro=entry.get("data_ro", []), data_rw=entry.get("data_rw", []),
                       compose=presets.effective(entry.get("name", "?"), entry,
                                                 presets.load_presets(settings.presets_dir))["compose"][0],
                       mask_skip=entry.get("mask_skip", []), swap=entry.get("swap", mounts.DEFAULT_SWAP))
    return [m.dst for m in plan.mounts if m.kind in ("mask", "tmpfs")
            or (m.kind == "bind" and os.path.realpath(m.src) != m.dst)]


def _history_secrets(entry):
    from .mounts import DEFAULT_SWAP
    grants = [entry["root"]] + entry.get("data_rw", []) + entry.get("data_ro", [])
    return session.history_secrets(entry["root"], grants, entry.get("swap", DEFAULT_SWAP),
                                   entry.get("mask_skip", []))


def cmd_project_accept_grant_exposure(s, a):
    from . import grantcheck
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}")
    _refuse_if_live(s, [a.name], f"record a grant-exposure acknowledgement for {a.name}")
    e = reg[a.name]
    exposure, warnings = grantcheck.scan(e["root"], [e["root"]] + e.get("data_rw", []), e.get("mask_skip", []),
                                         covered=_mask_dsts(s, dict(e, name=a.name)),
                                         warn_grants=e.get("data_ro", []))
    for w in warnings:
        warn(f"ab: grant warning: {w}")
    if not exposure:
        reg[a.name].pop("grant_exposure_accepted", None)
        registry.save(s.registry_path, reg)
        print(f"project {a.name}: no secrets or live data in the grant; nothing to accept")
        return 0
    # show the full findings before recording the ack, not a count afterwards (audit 6 Oct, M4)
    print(grantcheck.message(a.name, exposure).split("\nTo run anyway")[0])
    reg[a.name]["grant_exposure_accepted"] = grantcheck.fingerprint(exposure)
    registry.save(s.registry_path, reg)
    print(f"\naccepted for {a.name}. Move secrets and live data out of the grant when you can.")
    return 0


def cmd_project_accept_history_secrets(s, a):
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}")
    _refuse_if_live(s, [a.name], f"record a history-secrets acknowledgement for {a.name}")
    found = _history_secrets(reg[a.name])
    if not found:
        reg[a.name].pop("history_secrets_accepted", None)
        registry.save(s.registry_path, reg)
        print(f"project {a.name}: no masked file names in git history; nothing to accept")
        return
    reg[a.name]["history_secrets_accepted"] = found
    registry.save(s.registry_path, reg)
    print(f"project {a.name}: accepted {len(found)} masked file name(s) in git history: {', '.join(found)}.\n"
          "The agent can read them through git: rotate them if you have not already.")


def cmd_project_show(s, a):
    _print_effective(s, a.name)


def _print_effective(s, name):
    """Every effective setting, where it came from, and whether it actually does anything right now.

    Driven by the settings table (projectkeys.KEYS), so a key cannot be forgotten here: every stored key is
    printed, and one with no effect under the current posture is MARKED `(ignored: ...)` rather than hidden
    (audit F8 -- the old display gated compose_project/observe_logs/dev_images on the podman tier, which hid
    a waived live-stack ownership check and a dead image allowlist). Printed by `ab project show`, and after
    `ab init` / `ab project add` / `ab project set`."""
    p = session.load_project(s, name)
    stored = registry.load(s.registry_path)[name]
    presets_map = presets.load_presets(s.presets_dir)
    ctx = projectkeys.posture(p)
    resolved = {"root": p.root, "preset": p.preset, "data_ro": p.data_ro, "data_rw": p.data_rw,
                "mask_skip": p.mask_skip, "extra_domains": p.extra_domains, "swap": p.swap,
                "compose_project": p.compose_project, "observe_logs": p.observe_logs,
                "dev_images": p.dev_images, "build_memory": f"{p.build_memory >> 20} MB",
                "history_secrets_accepted": p.history_secrets_accepted,
                "grant_exposure_accepted": p.grant_exposure_accepted}
    print(f"project {p.name}")
    for key in projectkeys.KEYS:
        if key.display == "stored" and key.name not in stored:
            continue
        if key.kind == "preset":
            value, source = p.eff[key.name]
        else:
            value, source = resolved.get(key.name), "project" if key.name in stored else "default"
        notes = []
        reason = projectkeys.inert_reason(key.name, ctx)
        if reason:
            notes.append(f"(ignored: {reason})")
        if key.name == "preset" and presets_map.get(value, {}).get("ceiling"):
            notes.append("(a ceiling: an override may only tighten it)")
        if key.name == "compose_project" and key.name in stored and ctx["live_access"]:
            notes.append("(explicit: this stack's containers are not required to come from inside the "
                         "project root)")
        for line in _render(key, value):
            print(f"  {key.name:<24} {line:<26} from {source}"
                  + ("  " + " ".join(notes) if notes else ""))
            notes = []                                  # the note belongs to the first line only


def _render(key, value):
    """A stored value as the one or more lines `ab project show` prints for it."""
    if key.arity == "list":
        return [", ".join(str(v) for v in value)] if value else ["(none)"]
    if key.arity == "bool":
        return ["true" if value else "false"]
    if key.name == "podman":
        value = value or {}
        return [value.get("tier", "none") + (f" (live: {value['live']})" if value.get("live") else "")]
    if key.name == "swap":
        return [", ".join(f"{dev} shown as {target}" for target, dev in (value or {}).items()) or "(none)"]
    return [str(value)]


def _live_containers(s, names):
    """Running containers of these projects, as (project, session, container name).

    Asked of podman, not of session.json: the project comes from the container's own `agentbox.project`
    label and liveness from its state, so this sees the case `ab session gc` deliberately leaves alone --
    launcher gone, containers still up -- which is exactly when a registry edit used to orphan them (audit
    F2). The old check read the project from session.json and required a live launcher PID, so a session
    whose launcher had died looked absent."""
    wanted = set(names)
    out = []
    for c in Podman(s.podman).containers_json(s.labels):
        labels = c.get("Labels") or {}
        project = labels.get("agentbox.project")
        if project not in wanted or (c.get("State") or "").lower() != "running":
            continue
        names_ = c.get("Names") or []
        out.append((project, labels.get("agentbox.session") or "",
                    names_[0] if names_ else (c.get("Id") or "")[:12]))
    return sorted(out)


def _refuse_if_live(s, names, what):
    """No registry edit while a container of one of these projects is running.

    `ab project rm` used to orphan the session: the entry went away, the containers kept running, and the
    grant lost its overlap protection, so another project could be registered over the live grant. There is
    deliberately no --force -- ending live work is `ab session kill`'s job (decisions.md, 6 Oct, on why
    auto-reaping was rejected)."""
    blocked = _live_containers(s, names)
    if not blocked:
        return
    detail = ", ".join(f"{project}: {container}" for project, _sess, container in blocked)
    sessions = sorted({sess for _p, sess, _c in blocked if sess})
    how = " ".join(f"`ab session kill {x}`" for x in sessions) or "`ab session ls` to find the session"
    raise AgentboxError(f"cannot {what} while containers of that project are running ({detail}); end it "
                        f"with {how} first (its logs and end-of-session scan are kept), then try again")


def cmd_project_reset_state(s, a):
    session.load_project(s, a.name)
    _refuse_if_live(s, [a.name], f"reset the agent state of {a.name}")
    src = os.path.join(s.data_dir, a.name)
    if not os.path.isdir(src) or not os.listdir(src):
        print(f"{a.name} has no agent state")
        return 0
    if not a.yes:
        answer = input(f"Reset the agent state of {a.name} (memory, settings, transcripts of every agent, in "
                       f"{src})? It is moved to {s.backups_dir}/{a.name}/ first. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not reset")
            return 1
    moved = session.reset_state(s, a.name)
    print(f"reset: the agent state of {a.name} is now in {moved[1]}; the next session starts empty")
    return 0


def cmd_project_ls(s, a):
    for name, q in sorted(registry.load(s.registry_path).items()):
        print(f"{name:<20} {q.get('preset', '?'):<10} {q.get('root', '?')}")


def cmd_project_rm(s, a):
    reg = registry.load(s.registry_path)
    names = list(dict.fromkeys(a.name))          # de-duplicate (a repeated name would del twice -> KeyError)
    missing = [n for n in names if n not in reg]
    if missing:                                  # all-or-nothing: a typo in one name removes none
        raise AgentboxError(f"no project named {', '.join(missing)}")
    _refuse_if_live(s, names, f"remove {', '.join(names)}")   # all-or-nothing here too
    for n in names:
        del reg[n]
    registry.save(s.registry_path, reg)
    print(f"removed project {', '.join(names)} from the registry (files, state and backups are untouched)")


def _dur(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def cmd_run(s, a):
    # Agent and project may be given as --agent/--project or positionally (`ab run <agent> <project>`), in any
    # combination; flags like --shell are parsed normally, and anything after a `--` is passed through to the
    # agent or shell (split off in main() as a.passthrough). Positionals fill whichever of agent/project the
    # flags did not set, in order.
    agent, project_name = a.agent, a.project
    for val in a.names:
        if agent is None:
            agent = val
        elif project_name is None:
            project_name = val
        else:
            raise AgentboxError(f"unexpected argument {val!r}; pass agent/shell arguments after `--`")
    if not agent or not project_name:
        raise AgentboxError("give an agent and a project: `ab run --agent <a> --project <p>` "
                            "or `ab run <a> <p>`")
    running = [n for n, info in session.sessions(s, Podman(s.podman)).items()
               if info["alive"] and (info["meta"] or {}).get("project") == project_name]
    for n in running:
        warn(f"ab: a session for project {project_name} is already running ({n}); starting another means two "
             f"at once. `ab session ls` shows them; `ab session kill {n}` ends that one (keeping its logs and scan).")
    project = session.load_project(s, project_name)
    sess = session.make_session(s, agent, project, warn=warn)
    sess.prepare()
    command = sess.agent_command(shell=a.shell, extra=list(getattr(a, "passthrough", []) or []))
    if sys.stdin.isatty() and not a.no_tty:
        return sess.run_interactive(command, tty=True)
    p = sess.run_captured(command, stdin=sys.stdin.read() if not sys.stdin.isatty() else "",
                          timeout=a.timeout)
    sys.stdout.write(p.stdout or "")
    sys.stderr.write(p.stderr or "")
    return p.returncode


def cmd_ls(s, a):
    import time
    podman = Podman(s.podman)
    rows = session.sessions(s, podman)
    now = time.time()
    idle_warn = float(s.session_idle_warn_hours) * 3600
    flagged = []
    for name, info in sorted(rows.items()):
        meta = info["meta"] or {}
        state = "running" if info["alive"] else "dead"
        started = meta.get("started")
        start_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(started)) if started else "?"
        age = _dur(now - started) if started else "?"
        idle = session.idle_seconds(s, meta, now=now) if info["alive"] else None
        mark = ""
        if idle is not None and idle > idle_warn:
            mark = "  <-- idle"
            flagged.append((name, idle))
        print(f"{name:<36} {meta.get('project', '?'):<14} {state:<7} "
              f"start {start_str}  age {age:<6} idle {_dur(idle):<6} "
              f"{len(info['containers'])}c/{len(info['networks'])}n{mark}")
    for name, idle in flagged:
        warn(f"ab: session {name} has been idle {_dur(idle)} — if it is forgotten, end it with "
             f"`ab session kill {name}` (its logs and end-of-session scan are kept).")


def cmd_preview(s, a):
    from . import preview
    podman = Podman(s.podman)
    if a.stop:
        gone = preview.stop(s, podman, a.session)
        print("removed: " + (" ".join(gone) if gone else "no previews"))
        return 0
    if not a.target:
        raise AgentboxError("give <service>:<port> (or --stop)")
    port, desk = preview.start(s, podman, a.session, a.target, a.port)
    print(f"preview of {a.target} in {a.session}: http://127.0.0.1:{port}/ (on this host only)\n"
          f"from your desktop: {desk}\n  then open http://127.0.0.1:{port}/\n"
          "it ends with the session (or ab session preview --stop)")
    return 0


def cmd_kill(s, a):
    podman = Podman(s.podman)
    known = session.sessions(s, podman)
    if a.session not in known:
        raise AgentboxError(f"no session named {a.session}")
    session.teardown(s, podman, a.session, warn=warn)
    print(f"killed {a.session}")


def cmd_gc(s, a):
    removed = session.gc(s, Podman(s.podman), warn=warn)
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


def cmd_keys_set(s, a):
    if a.provider == "claude":
        raise AgentboxError("Claude Code uses a subscription token: `ab keys rotate claude`")
    keys.set_key(s, a.provider)


def cmd_keys_ls(s, a):
    found = keys.present(s)
    for provider in found:
        known = ", ".join(keys.PROVIDER_DOMAINS.get(provider, [])) or "domain from the model config's baseUrl"
        print(f"{provider:<16} {keys.env_name(provider):<24} {known}")
    tok = s.token_file("claude")
    print(f"{'claude (token)':<16} {'CLAUDE_CODE_OAUTH_TOKEN':<24} "
          f"{'set' if os.path.exists(tok) else 'not set'} ({tok})")


def cmd_keys_rm(s, a):
    keys.remove(s, a.provider)


def cmd_keys_rotate(s, a):
    if a.agent != "claude":
        raise AgentboxError("only `ab keys rotate claude` is supported")
    tokens.rotate(s.token_file(a.agent), a.agent)


def _add_project_setting_args(x, *, preset_required, preset_default=None, preset_choices=None,
                              preset_note="", presets_map=None):
    """The override flags shared by `ab init`, `ab project add` and `ab project set`: same flag spellings,
    choices and help text everywhere, so they cannot drift (one shared definition instead of three)."""
    catalog = presets_map if presets_map is not None else dict(presets.BUILTIN)
    x.add_argument("--preset", required=preset_required, default=preset_default, choices=preset_choices,
                   metavar="PRESET",
                   help="security profile" + preset_note + ". One of: " + presets.describe(catalog)
                        + ". A file in ~/.config/agentbox/presets/ adds one of your own (it inherits "
                          "lockdown's values for every key it omits). `ab project show` prints what a "
                          "preset resolves to, and where each value came from")
    x.add_argument("--data-ro", action="append", default=[], metavar="DIR",
                   help="an extra directory to mount read-only (repeatable); for input data that lives outside "
                        "the code grant")
    x.add_argument("--data-rw", action="append", default=[], metavar="DIR",
                   help="an extra directory to mount read-write (repeatable); for output data outside the code "
                        "grant")
    x.add_argument("--egress", choices=presets.EGRESS, metavar="MODE",
                   help="network egress: `locked` (model API only), `packages` (+ package registries), or "
                        "`open` (+ any public host); the LAN and host are always refused. Overrides the "
                        "preset (default: inherited from the preset)")
    x.add_argument("--extra-domain", action="append", default=[], metavar="DOMAIN",
                   help="add a domain to the egress allowlist (repeatable)")
    x.add_argument("--build-memory", metavar="SIZE",
                   help="memory limit for each build in the dev tier (default: 2g, e.g. 4g)")
    x.add_argument("--mask-skip", action="append", default=[], metavar="DIR",
                   help="a folder (relative to root) the secret-mask walk skips for speed on huge trees; NOT "
                        "an exemption — grantcheck still scans it (repeatable)")
    x.add_argument("--podman", choices=presets.PODMAN_TIERS, metavar="TIER",
                   help="podman tier: `none`, `observe` (read the live stack), or `dev` (run the agent's own "
                        "<project>-dev stack). `exec` is refused — not built (default: inherited from the "
                        "preset)")
    x.add_argument("--compose-project", metavar="NAME",
                   help="the live stack's compose project name for the observe tier (default: the project name)")
    x.add_argument("--dev-image", action="append", default=[], metavar="PATTERN",
                   help="dev tier: also allow running or building from images matching this name pattern "
                        "(e.g. localhost/myapp:*); by default only allowlisted registries and this project's "
                        "builds (repeatable)")
    x.add_argument("--observe-logs", action=argparse.BooleanOptionalAction, default=None,
                   help="let the observe tier read the live stack's logs (default: off; logs can hold "
                        "secrets, and what the agent reads goes to its model provider)")
    x.add_argument("--accept-history-secrets", action="store_true",
                   help="register even though masked file names (.env, *.key, ...) are in git history")
    x.add_argument("--i-understand-grant-exposure", action="store_true",
                   help="register even though the grant holds secrets or live data (a grant is for code; "
                        "move them outside it or add a mask)")


def _add_project_unset_args(x):
    """`ab project set`'s own flags for taking a setting away again (the add/init side has nothing to
    unset)."""
    x.add_argument("--unset", action="append", default=[], choices=UNSETTABLE, metavar="KEY",
                   help="drop a stored setting so the preset's value applies again (repeatable); one of: "
                        + ", ".join(UNSETTABLE))
    for dest, key in CLEAR_FLAGS.items():
        x.add_argument("--" + dest.replace("_", "-"), action="store_true",
                       help=f"empty the {key} list (the same as --unset {key})")


def parser(settings=None):
    raw = argparse.RawDescriptionHelpFormatter
    # `--preset`'s choices and help come from the presets that actually exist, so an unknown name is
    # refused at parse time with the full list, user presets included.
    presets_map = presets.load_presets(settings.presets_dir) if settings else dict(presets.BUILTIN)
    preset_choices = presets.names_in_order(presets_map)

    def ex(*lines):
        """Format an examples epilog block."""
        return "examples:\n" + "\n".join("  " + l for l in lines)

    def add(subparsers, name, summary, epilog=None):
        """A subparser whose one-line help also serves as its description, with an examples epilog that
        keeps its own line breaks."""
        return subparsers.add_parser(name, help=summary, description=summary, epilog=epilog, formatter_class=raw)

    p = argparse.ArgumentParser(
        prog="ab", formatter_class=raw,
        description="agentbox: coding agents (Claude Code, OMP) in hardened rootless podman sandboxes, one "
                    "project per session.",
        epilog="`ab project` manages the saved configuration; `ab session` manages the live runs; `ab run` "
               "and `ab init` are the verbs that create them. Full reference: docs/USAGE.md.\n\n"
               + ex("ab init pod --name web            scaffold and register a new project",
                    "ab run --agent claude --project web   start a session (also: ab run claude web)",
                    "ab session ls                     list running sessions",
                    "ab project show web               a project's effective settings"))
    sub = p.add_subparsers(dest="cmd", required=True)

    from . import initproj
    x = add(sub, "init",
            "scaffold a new project with a grantcheck-clean layout (secrets masked, live data in a named "
            "volume), then register it",
            ex("ab init pod --name filelab",
               "ab init python --name dataproc --upstream git@github.com:me/dataproc.git"))
    x.add_argument("template", choices=initproj.TEMPLATES,
                   help="which scaffold to create: `pod` (a compose/dev-stack service, live data in a named "
                        "volume) or `python` (a script/library, data path from DATA_DIR)")
    x.add_argument("--name", required=True, metavar="NAME",
                   help="project name; the repo is created at <repos_dir>/<name> and registered under it")
    x.add_argument("--upstream", metavar="URL",
                   help="git URL to add as origin and push to (an off-box CODE backup; it does NOT back up "
                        ".env or the data volume)")
    _add_project_setting_args(x, preset_required=False, preset_choices=preset_choices,
                              presets_map=presets_map,
                              preset_note=" (default: the template's own — pod: build, python: script)")
    x.set_defaults(fn=cmd_init)

    proj = sub.add_parser(
        "project", help="manage registered projects (the saved configuration)", formatter_class=raw,
        description="manage registered projects (the saved configuration). A project names a code grant, a "
                    "preset and any extra data grants.",
        epilog=ex("ab project add web ~/repos/web --preset build",
                  "ab project set web --egress open", "ab project show web", "ab project ls",
                  "ab project rm web")).add_subparsers(
        dest="sub", required=True)
    x = add(proj, "add", "register a project with a preset (validated against overlap and grant exposure)",
            ex("ab project add web ~/repos/web --preset build",
               "ab project add dataproc ~/repos/dataproc --preset script --data-ro ~/data/raw --data-rw ~/data/out",
               "ab project add notes ~/repos/notes --preset research",
               "ab project add svc ~/repos/svc --preset build-open --podman dev"))
    x.add_argument("name", help="a short name for the project (lower-case letters, digits, '-' or '_')")
    x.add_argument("root", help="path to the project's code directory — the read-write grant; must exist and "
                                "not overlap another project or a protected path")
    _add_project_setting_args(x, preset_required=True, preset_choices=preset_choices,
                              presets_map=presets_map)
    x.set_defaults(fn=cmd_project_add)
    x = add(proj, "set", "change a registered project's settings in place (re-validated; refused while a "
                         "session for it is live)",
            ex("ab project set web --egress open",
               "ab project set web --preset analysis-offline",
               "ab project set web --unset egress --clear-extra-domains"))
    x.add_argument("name", help="the registered project to change (its root cannot be changed: use "
                                "`ab project rm` + `ab project add` to move a code grant)")
    _add_project_setting_args(x, preset_required=False, preset_choices=preset_choices,
                              presets_map=presets_map,
                              preset_note=" (default: the project's current preset)")
    _add_project_unset_args(x)
    x.set_defaults(fn=cmd_project_set)
    x = add(proj, "accept-history-secrets",
            "accept (after rotating them) the masked file names now in a project's git history",
            ex("ab project accept-history-secrets web"))
    x.add_argument("name", help="the registered project to record the acknowledgement for")
    x.set_defaults(fn=cmd_project_accept_history_secrets)
    x = add(proj, "accept-grant-exposure",
            "accept that this project's grant holds secrets or live data the agent can read",
            ex("ab project accept-grant-exposure web"))
    x.add_argument("name", help="the registered project to record the acknowledgement for")
    x.set_defaults(fn=cmd_project_accept_grant_exposure)
    x = add(proj, "show", "print every effective setting and whether it came from the preset or an override",
            ex("ab project show web"))
    x.add_argument("name", help="the registered project to inspect")
    x.set_defaults(fn=cmd_project_show)
    add(proj, "ls", "list registered projects (name, preset, root)",
        ex("ab project ls")).set_defaults(fn=cmd_project_ls)
    x = add(proj, "reset-state",
            "wipe a project's agent state (memory, settings, transcripts); asks first, keeps a copy in the "
            "backups dir", ex("ab project reset-state web", "ab project reset-state web --yes"))
    x.add_argument("name", help="the registered project whose agent state to wipe")
    x.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    x.set_defaults(fn=cmd_project_reset_state)
    x = add(proj, "rm", "remove one or more projects from the registry (files, state and backups are untouched)",
            ex("ab project rm web", "ab project rm web old-thing"))
    x.add_argument("name", nargs="+", help="one or more registered project names (all-or-nothing: a bad name "
                                           "removes none)")
    x.set_defaults(fn=cmd_project_rm)

    x = add(sub, "run", "start an agent session in the foreground",
            ex("ab run --agent claude --project web",
               "ab run claude web                 positional form of --agent/--project",
               "ab run claude web --shell         a bash shell in the sandbox instead of the agent",
               "ab run claude web -- --resume     pass everything after -- to the agent"))
    x.add_argument("--agent", metavar="AGENT",
                   help="agent to run: `claude` or `omp` (or give it positionally: `ab run <agent> <project>`)")
    x.add_argument("--project", metavar="PROJECT",
                   help="the registered project to run against (or give it positionally)")
    x.add_argument("--shell", action="store_true", help="run a bash shell in the sandbox instead of the agent")
    x.add_argument("--no-tty", action="store_true",
                   help="do not allocate a TTY (for non-interactive or piped runs)")
    x.add_argument("--timeout", type=int, default=3600, metavar="SECONDS",
                   help="time limit for a non-interactive run (default 3600; ignored for interactive sessions)")
    x.add_argument("names", nargs="*", metavar="[AGENT [PROJECT]]",
                   help="positional AGENT and PROJECT (an alternative to --agent/--project); anything after a "
                        "`--` is passed through to the agent or shell")
    x.set_defaults(fn=cmd_run)

    # Running sessions are managed under `ab session`, mirroring `ab project`; `ab run` stays the top-level
    # verb that starts one.
    sess = sub.add_parser(
        "session", help="manage running sessions (the live instances)", formatter_class=raw,
        description="manage running sessions (the live instances). Start one with `ab run`.",
        epilog=ex("ab session ls", "ab session preview ab-claude-web-1 web:8000",
                  "ab session kill ab-claude-web-1", "ab session gc")).add_subparsers(dest="sub", required=True)
    add(sess, "ls", "list sessions with their project, start time, age and idle time",
        ex("ab session ls")).set_defaults(fn=cmd_ls)
    x = add(sess, "preview",
            "open a session's dev service on 127.0.0.1 (reach it from your desktop with ssh -L)",
            ex("ab session preview ab-claude-web-1 web:8000",
               "ab session preview ab-claude-web-1 web:8000 --port 18080",
               "ab session preview ab-claude-web-1 --stop"))
    x.add_argument("session", help="the session name (from `ab session ls`)")
    x.add_argument("target", nargs="?", metavar="SERVICE:PORT",
                   help="the dev-stack service (or container) and the port inside it, e.g. web:8000; omit with "
                        "--stop")
    x.add_argument("--port", type=int, metavar="PORT",
                   help="host port to publish on 127.0.0.1 (default: a free one, printed on start)")
    x.add_argument("--stop", action="store_true", help="remove this session's previews instead of opening one")
    x.set_defaults(fn=cmd_preview)
    x = add(sess, "kill", "tear down a session and everything it created (keeps its logs and end-of-session scan)",
            ex("ab session kill ab-claude-web-1"))
    x.add_argument("session", help="the session name to tear down (from `ab session ls`)")
    x.set_defaults(fn=cmd_kill)
    add(sess, "gc", "remove containers, networks and sockets left by dead sessions (runs automatically after boot)",
        ex("ab session gc")).set_defaults(fn=cmd_gc)

    x = add(sub, "selftest", "prove the sandbox boundaries hold (posture, egress, mounts, podman scoping)",
            ex("ab selftest --agent claude --project web", "ab selftest --agent claude --project web --json"))
    x.add_argument("--agent", required=True, metavar="AGENT", help="agent to run the checks as (claude or omp)")
    x.add_argument("--project", required=True, metavar="PROJECT", help="the registered project to test")
    x.add_argument("--json", action="store_true", help="print results as JSON instead of a table")
    x.set_defaults(fn=cmd_selftest)

    ks = sub.add_parser(
        "keys", help="model tokens and provider API keys", formatter_class=raw,
        description="manage model tokens and provider API keys. Keys live in ~/.config/agentbox/keys/ (0600), "
                    "outside every grant, and reach a session read-only as AB_KEY_<PROVIDER>.",
        epilog=ex("ab keys rotate claude", "ab keys set openai", "ab keys ls", "ab keys rm openai")).add_subparsers(
        dest="sub", required=True)
    x = add(ks, "rotate", "replace the Claude subscription token (from `claude setup-token`)",
            ex("ab keys rotate claude"))
    x.add_argument("agent", help="the agent whose subscription token to replace (currently: claude)")
    x.set_defaults(fn=cmd_keys_rotate)
    x = add(ks, "set", "set a provider's API key (pay-per-use: make it dedicated and spend-capped)",
            ex("ab keys set openai"))
    x.add_argument("provider", help="the provider name; its key becomes AB_KEY_<PROVIDER> in sessions "
                                    "(you are prompted for the value, which is never on the command line)")
    x.set_defaults(fn=cmd_keys_set)
    add(ks, "ls", "list providers that have a key (names only, never values)",
        ex("ab keys ls")).set_defaults(fn=cmd_keys_ls)
    x = add(ks, "rm", "remove a provider's key file", ex("ab keys rm openai"))
    x.add_argument("provider", help="the provider whose key file to remove")
    x.set_defaults(fn=cmd_keys_rm)
    return p


GC_ON_FIRST_USE = {"cmd_run", "cmd_selftest", "cmd_ls"}


def gc_once_per_boot(s):
    """The design's "gc runs automatically on the launcher's first use after boot": a marker named after the
    kernel's boot id, in the run dir (review 2026-10-01, doc claims)."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
    except OSError:
        return False
    marker = os.path.join(s.run_dir, f".gc-{boot}")
    if os.path.exists(marker):
        return False
    removed = session.gc(s, Podman(s.podman), warn=warn)
    if removed:
        warn(f"ab: first use since boot: removed what dead sessions left: {' '.join(removed)}")
    os.makedirs(s.run_dir, mode=0o700, exist_ok=True)
    with open(marker, "w"):
        pass
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `ab run ... -- <args>` passes everything after the first `--` to the agent/shell. Split it off before
    # argparse so the positionals and flags on the left parse normally (a REMAINDER would swallow --shell).
    passthrough = []
    if argv[:1] == ["run"] and "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    try:
        s = config.load()
        built = parser(s)          # the parser reads the presets, so a broken preset file reports cleanly
    except AgentboxError as e:
        print(f"ab: {e}", file=sys.stderr)
        return 2
    a = built.parse_args(argv)
    if getattr(a, "fn", None) is cmd_run:
        a.passthrough = passthrough
    try:
        if a.fn.__name__ in GC_ON_FIRST_USE:
            gc_once_per_boot(s)
        rc = a.fn(s, a)
        return rc or 0
    except AgentboxError as e:
        print(f"ab: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
