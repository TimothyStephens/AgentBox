"""`ab project`: the saved configuration -- init, add, set, show, check, rm, purge, clean, sync, selftest,
reset-state and the accept-* acknowledgements (split out of cli.py; the parser scaffolding stays there)."""
import argparse
import json
import os

from . import adapters, layout, presets, projectkeys, registry, review, session
from .cli_common import warn
from .config import HOME
from .errors import AgentboxError
from . import paths
from .paths import check_grant
from .podman import Podman


def cmd_init(s, a):
    from . import initproj
    return initproj.make(s, a, out=print, warn=warn)


def _check(name, key, value, **kw):
    """One value, through its own validator in the settings table (projectkeys.KEYS)."""
    return projectkeys.validate_value(key, f"project {name}", value, **kw)


def _grant_kw(s):
    """check_grant's context: is a path safe in itself? Cross-project overlap is paths.check_overlaps,
    called once per entry (it compares every grant pair, not just roots).

    It took a registry and an `exclude` name until check_overlaps took that job over; both were ignored,
    while passing them read as if they still scoped the check (audit F-12)."""
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
    """Apply the override flags shared by `ab project init`, `ab project add` and `ab project set` onto a registry
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
    if getattr(a, "code", None):
        entry["code"] = a.code
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
    if a.profile:
        entry["profile"] = _check(a.name, "profile", a.profile)
    if a.base_branch:
        entry["base_branch"] = _check(a.name, "base_branch", a.base_branch)
    if a.mask_skip:
        entry["mask_skip"] = list(a.mask_skip)
    if a.mask:
        entry["mask"] = _check(a.name, "mask", list(a.mask))
    if getattr(a, "toulmin", None) is not None:
        entry["toulmin"] = a.toulmin
    for key in session.TRUST_KEYS:
        if getattr(a, key, None):
            entry[key] = _check(a.name, key, getattr(a, key))
    if getattr(a, "mount", None):
        entry["mount"] = _check(a.name, "mount", a.mount)
    if getattr(a, "reviews", None):
        entry["reviews"] = _check(a.name, "reviews", a.reviews)
    if a.podman:
        # `--podman TIER` sets the tier component only. `live` is a separate capability (design.md: a set,
        # not a ladder) that no flag addresses, so it is kept from what the project would otherwise have: its
        # own stored value, else the preset's (build and operate both carry live = observe). It used to be
        # dropped silently (audit item 46); `--unset podman` is still the way back to the preset's whole value.
        # A stored value set against an OLD preset is not carried into a newly chosen one (audit F2): there the
        # new preset's live applies.
        stored = None if (newly_chosen and old_preset) else entry.get("podman")
        current = stored or presets_map.get(entry.get("preset"), {}).get("podman") or {}
        # `--podman none` means no podman access at all: it clears `live` too, so "none" on build, pods or
        # operate no longer leaves the live stack observable (review 16 Nov; Tim chose this, 17 Nov).
        keep_live = a.podman != "none" and current.get("live")
        entry["podman"] = {"tier": a.podman, **({"live": current["live"]} if keep_live else {})}
    if a.compose_project:
        entry["compose_project"] = _check(a.name, "compose_project", a.compose_project)
    if a.observe_logs is not None:
        entry["observe_logs"] = a.observe_logs
    if a.dev_image:
        entry["dev_images"] = _check(a.name, "dev_images", list(a.dev_image))
    if a.swap:
        entry["swap"] = _check(a.name, "swap", _parse_swap(a.swap))
    if getattr(a, "lan_addr", None):
        entry["lan_addr"] = _check(a.name, "lan_addr", a.lan_addr)
    if getattr(a, "deploy_port", None):
        entry["deploy_ports"] = _check(a.name, "deploy_ports", list(a.deploy_port))
    if getattr(a, "deploy_bind", None):
        entry["deploy_binds"] = [check_grant(d, kind="deploy_bind", **grant_kw) for d in a.deploy_bind]
    if getattr(a, "deploy_file", None):
        entry["deploy_files"] = _check(a.name, "deploy_files", list(a.deploy_file))
    if getattr(a, "deploy_compose", None) is not None:
        entry["deploy_compose"] = _check(a.name, "deploy_compose", a.deploy_compose)
    if getattr(a, "deploy_external", None):
        entry["deploy_external"] = _check(a.name, "deploy_external", list(a.deploy_external))
    return cleared


def _parse_swap(pairs):
    """`--swap TARGET=STANDIN` (repeatable) into the map the registry stores. The validator then applies
    F6's two rules: the target must be a masked name and the stand-in must not be."""
    out = {}
    for pair in pairs:
        target, sep, standin = pair.partition("=")
        if not sep or not target.strip() or not standin.strip():
            raise AgentboxError(f"--swap {pair!r}: write it as TARGET=STANDIN, "
                                "e.g. --swap .env.local=.env.dev")
        out[target.strip()] = standin.strip()
    return out


def precheck_project_settings(s, a):
    """Validate the project-setting flags before anything is written. `ab project init` calls this first, so a bad
    flag leaves no half-scaffolded tree behind (which would then make a corrected retry fail with "already
    exists; refusing to scaffold over it"). Everything here is re-checked by cmd_project_add afterwards;
    only the checks that need the project root to exist are left to it."""
    registry.load(s.registry_path)        # for its checks: a broken registry file refuses before the scaffold
    presets_map = presets.load_presets(s.presets_dir)
    entry = {}
    _apply_project_settings(entry, a, _grant_kw(s), presets_map)
    eff = presets.effective(a.name, entry, presets_map)
    _check_trust(a.name, entry, eff)
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


def _grants_more(name, before, after, presets_map):
    """What this edit grants that the project did not have. A new project (`before` is None) counts as
    granting everything: there is nothing it could be safer than."""
    if before is None:
        return ["this is a new project"]
    return projectkeys.loosenings(presets.effective(name, before, presets_map),
                                  presets.effective(name, after, presets_map), before, after)


def _settle_history_secrets(name, entry, a, presets_map, before, found):
    """What an add/set does about masked file names in git history (audit F-11, and F-5 applied to this
    acknowledgement too).

    The whole found set is compared, so a name that LEFT the history re-prompts as well; a clean history
    drops the record, so `ab project show` stops listing secrets that are no longer there to rotate; and an
    edit that grants nothing new is never blocked by a stale acknowledgement."""
    accepted = list((before or {}).get("history_secrets_accepted") or [])
    if entry.get("data_env") == "on":       # the root's env files are shown anyway: their names expose nothing
        accepted = session.without_real_env(accepted, entry["root"],
                                            entry.get("data_rw", []) + entry.get("data_ro", []))
    if not found:
        entry.pop("history_secrets_accepted", None)     # nothing in the history: the record is stale
        return
    from . import grantcheck
    if grantcheck.fingerprint(found) == grantcheck.fingerprint(accepted):
        entry["history_secrets_accepted"] = accepted    # unchanged: nothing to decide
        return
    if a.accept_history_secrets:
        entry["history_secrets_accepted"] = found
        new, gone = session.history_secrets_changes(found, accepted)
        warn(f"ab: accepted {len(found)} masked file name(s) in git history: {', '.join(found[:15])}."
             + (f" New since the last acknowledgement: {', '.join(new)}." if new and accepted else "")
             + (f" No longer in the history: {', '.join(gone)}." if gone else "")
             + " The agent can read them through git: rotate them if you have not already.")
        return
    gained = _grants_more(name, before, entry, presets_map)
    if gained:
        raise AgentboxError(session.history_secrets_message(name, found, accepted))
    warn("ab: " + session.history_secrets_message(name, found, accepted)
         + "\n  this edit grants nothing new, so it proceeds. A SESSION WILL REFUSE to start until they are "
           "accepted.")


def _settle_grant_exposure(s, name, entry, a, presets_map, before, exposure):
    """What an add/set does about a grant exposure (audit F-5).

    `set` used to demand --i-understand-grant-exposure again on EVERY edit to a project with an accepted
    exposure, including `--egress locked`, which only makes the project safer. The stored acknowledgement is
    honoured instead, and an edit that grants nothing new is never blocked by one. The session's own check is
    untouched, so an unacknowledged exposure still refuses to RUN -- this only decides whether the registry
    may be edited."""
    from . import grantcheck
    accepted = (before or {}).get("grant_exposure_accepted")
    if not exposure:
        entry.pop("grant_exposure_accepted", None)      # the exposure is gone, so the ack is stale
        return
    if grantcheck.matches(accepted, exposure):
        entry["grant_exposure_accepted"] = accepted     # unchanged: nothing to decide
        return
    if a.i_understand_grant_exposure:
        entry["grant_exposure_accepted"] = [f["sig"] for f in exposure]
        warn(grantcheck.acceptance_message(name, exposure, accepted))
        return
    # An `add` has no "before" to be safer than, so a new project with an exposure still needs the flag.
    gained = _grants_more(name, before, entry, presets_map)
    if gained:
        raise AgentboxError(grantcheck.change_message(name, exposure, accepted, gained))
    warn(grantcheck.unblocked_message(name, exposure, accepted))


def _check_trust(name, entry, eff):
    """project.check_trust over a registry entry: the trusted opt-outs' refusals, before anything is saved.
    `ab project init` checks before its root exists; the root it makes is a git repository, so there Toulmin
    means clone mode, as it will at the first session."""
    root, mount = entry.get("root") or "", entry.get("mount", "")
    if not root:
        mount = mount or ("clone" if entry.get("toulmin") else "checkout")
    session.check_trust(name, root, eff, data_env=entry.get("data_env", "off"),
                        data_bind=entry.get("data_bind", "off"), mount=mount, toulmin=bool(entry.get("toulmin")),
                        mask=entry.get("mask", []))


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
    _check_trust(name, entry, presets.effective(name, entry, presets_map))
    paths.check_overlaps(name, entry, reg)
    if entry.get("deploy_binds"):
        entry["deploy_binds"] = paths.check_deploy_binds(name, entry["deploy_binds"], entry, reg, **grant_kw)
    review.check(name, entry, reg)
    from . import grantcheck
    exposure, warnings, _fp = grantcheck.preflight(s, name, entry)
    for w in warnings:
        warn(f"ab: grant warning: {w}")
    _settle_grant_exposure(s, name, entry, a, presets_map, reg.get(name), exposure)
    _settle_history_secrets(name, entry, a, presets_map, reg.get(name), _history_secrets(entry))
    reg[name] = entry
    registry.save(s.registry_path, reg)


def cmd_project_add(s, a):
    registry.check_name(a.name)
    reg = registry.load(s.registry_path)
    if a.name in reg:
        raise AgentboxError(f"project {a.name} already exists")
    presets_map = presets.load_presets(s.presets_dir)
    grant_kw = _grant_kw(s)
    made_clone = None
    if getattr(a, "reviews", None):
        # A reviewer (review.py): its root is a clone of the builder's repository, made here when it is not
        # there yet -- next to the builder's root unless a root is given.
        if a.reviews not in reg:
            raise AgentboxError(f"--reviews {a.reviews}: no project named {a.reviews}; register the builder first")
        root = a.root or review.default_root(reg[a.reviews]["root"], a.name)
        if review.make_clone(reg[a.reviews]["root"], root):
            made_clone = os.path.abspath(os.path.expanduser(root))
            print(f"cloned {a.reviews} ({reg[a.reviews]['root']}) to {made_clone}")
        a.root = root
    elif not a.root:
        raise AgentboxError("ab project add: a root is required (it is optional only with --reviews, where it "
                            "defaults to a clone next to the builder)")
    try:
        _add_registered(s, a, reg, presets_map, grant_kw)
    except BaseException:
        if made_clone:
            from .fsutil import rmtree
            rmtree(made_clone, ignore_errors=True)
            warn(f"ab: removed the clone {made_clone} made for {a.name}, since it was not registered")
        raise


def _add_registered(s, a, reg, presets_map, grant_kw):
    entry = {"root": check_grant(a.root, kind="root", **grant_kw)}
    cleared = _apply_project_settings(entry, a, grant_kw, presets_map)
    # `ab project rm` keeps the agent state, so adding the same name back picks it up. Right for the same
    # project; a quiet merge for a different one under an old name, so the state's identity file is checked
    # BEFORE anything is saved: a different root is refused unless --reattach or --fresh says what to do.
    action = layout.check_registration(s, a.name, entry["root"], reattach=getattr(a, "reattach", False),
                                       fresh=getattr(a, "fresh", False))
    _validate_and_save(s, reg, a.name, entry, a, presets_map)
    print(f"added project {a.name} ({entry['root']}, preset {entry['preset']})")
    if action == "fresh":
        moved = session.reset_state(s, a.name, built_record=True)
        if moved:
            print(f"  --fresh: the state that was under {moved[0]} (every agent's, and the built-image "
                  f"record) is archived in {moved[1]}; grant backups stay in "
                  f"{s.project_backups_dir(a.name)}")
        layout.write_identity(s, a.name, entry["root"])
    elif action == "reattach":
        old = layout.read_identity(s, a.name)
        prev = list(old.get("previous_roots") or []) + [old["root"]]
        layout.write_identity(s, a.name, entry["root"], created=old.get("created"), previous=prev)
        print(f"  --reattach: the state made for {old['root']} now belongs to {entry['root']}")
    else:
        ident = layout.read_identity(s, a.name)
        if ident is None or ident["root"] != entry["root"]:     # new, or an identity with no state behind it
            layout.write_identity(s, a.name, entry["root"])
    if action in ("keep", "reattach"):
        lines, unreviewed = _state_summary(s, a.name)
        if lines:
            print(f"  reattached existing agent state under {s.project_dir(a.name)}:")
            for line in lines:
                print(line)
            print(f"  this session will start with that memory. `ab project reset-state {a.name}` keeps a copy "
                  f"and clears it; `ab project purge {a.name}` deletes it outright"
                  + (f"; `ab profile harvest` shows the {unreviewed} unreviewed inbox item(s)" if unreviewed else ""))
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
    before = dict(reg[a.name])          # for the "what did this grant?" report, before anything is applied
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
    grant_kw = _grant_kw(s)
    entry["root"] = check_grant(entry["root"], kind=f"project {a.name} root", **grant_kw)
    cleared = _apply_project_settings(entry, a, grant_kw, presets_map, unset=unset)
    _validate_and_save(s, reg, a.name, entry, a, presets_map)
    print(f"updated project {a.name} ({entry['root']}, preset {entry['preset']})")
    if dropped:
        print(f"  unset (back to the preset): {', '.join(dropped)}")
    if absent:
        print(f"  not stored on this project, so nothing to unset: {', '.join(absent)}")
    _print_cleared(cleared, a.name, entry, presets_map)
    _print_granted(a.name, before, entry, presets_map)
    _print_effective(s, a.name)


def _history_secrets(entry):
    grants = [entry["root"]] + entry.get("data_rw", []) + entry.get("data_ro", [])
    found = session.history_secrets(entry["root"], grants, entry.get("mask_skip", []))
    if entry.get("data_env") == "on":
        found = session.without_real_env(found, entry["root"], grants[1:])
    return found


def cmd_project_check(s, a):
    """Read-only: what the grant scan finds now, what was accepted, and whether those are the same thing.

    The stored acknowledgement is a fingerprint -- a hash of each finding's path, size, mtime and inode --
    so `ab project show` can only print an opaque number. This runs the same scan a session runs and shows
    the findings behind it, so "what did I accept?" is answerable without starting one. It writes nothing,
    and unlike rm/set/accept-* it is not refused while a session is live: seeing what a running project
    accepted is exactly when you want it. Exit 0 if a session would start, 1 if it would refuse."""
    from . import grantcheck
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}; `ab project ls` lists them")
    e = reg[a.name]
    exposure, warnings, now = grantcheck.preflight(s, a.name, e)
    stored = e.get("grant_exposure_accepted")
    accepted = grantcheck.acceptance_fingerprint(stored)

    print(f"project {a.name}: grant exposure")
    print(f"  scanned (read-write)  {', '.join([e['root']] + e.get('data_rw', []))}")
    if e.get("data_ro"):
        print(f"  scanned (read-only)   {', '.join(e['data_ro'])}   warnings only; never refused")
    if exposure:
        print(f"  findings now ({len(exposure)}):")
        for f in exposure:
            print(f"    - {f['text']}")
    else:
        print("  findings now          none")
    for w in warnings:
        print(f"  warning: {w}")
    if exposure or accepted:
        print(f"  fingerprint now       {now or '(nothing to fingerprint)'}")
        print(f"  accepted              {accepted or '(nothing accepted)'}")
        print("  the fingerprint covers each finding's path, size, mtime and inode, so editing a flagged")
        print("  file re-prompts; a change inside a flagged directory, or to a credential in a git remote")
        print("  URL, does not.")

    if not exposure and accepted:
        print("\n  => nothing is exposed any more: the stored acceptance is stale, and the next")
        print(f"     `ab project set`/`add` or `ab project accept-grant-exposure {a.name}` drops it.")
        print("     A session will start.")
        return 0
    if not exposure:
        print("\n  => nothing in the grant the agent should not have. A session will start.")
        return 0
    if accepted == now:
        print("\n  => the acceptance covers exactly these findings. A session will start.")
        if e.get("egress") == "open":
            print("     note: this project's egress is open, so what the agent reads here can leave the")
            print(f"     box. `ab project set {a.name} --egress packages` narrows that.")
        return 0
    if accepted:
        print("\n  => these findings have CHANGED since they were accepted, so a session will REFUSE.")
        for line in grantcheck._diff_lines(stored, exposure):
            print(f"   {line}")
        print("     Move them out of the grant, or accept the new state with")
        print(f"     `ab project accept-grant-exposure {a.name}`.")
    else:
        print("\n  => not accepted, so a session will REFUSE. Move them out of the grant, or accept with")
        print(f"     `ab project accept-grant-exposure {a.name}`.")
    return 1


def cmd_project_accept_grant_exposure(s, a):
    from . import grantcheck
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}")
    _refuse_if_live(s, [a.name], f"record a grant-exposure acknowledgement for {a.name}")
    e = reg[a.name]
    exposure, warnings, _fp = grantcheck.preflight(s, a.name, e)
    for w in warnings:
        warn(f"ab: grant warning: {w}")
    if not exposure:
        reg[a.name].pop("grant_exposure_accepted", None)
        registry.save(s.registry_path, reg)
        print(f"project {a.name}: no secrets or live data in the grant; nothing to accept")
        return 0
    # show the full findings before recording the ack, not a count afterwards (audit 6 Oct, M4)
    print(grantcheck.message(a.name, exposure).split("\nTo run anyway")[0])
    reg[a.name]["grant_exposure_accepted"] = [f["sig"] for f in exposure]
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


def _print_granted(name, before, after, presets_map):
    """What this edit grants that the project did not have (audit F-8: one of the things the settings table's
    `security` column decides). Only security-relevant settings and grants are listed, so an edit to, say,
    build_memory says nothing."""
    gained = _grants_more(name, before, after, presets_map)
    if gained:
        print("  this edit grants more than the project had:")
        for g in gained:
            print(f"    {g}")


def _print_effective(s, name):
    """Every effective setting, where it came from, and whether it actually does anything right now.

    Driven by the settings table (projectkeys.KEYS), so a key cannot be forgotten here: every stored key is
    printed, and one with no effect under the current posture is MARKED `(ignored: ...)` rather than hidden
    (audit F8 -- the old display gated compose_project/observe_logs/dev_images on the podman tier, which hid
    a waived live-stack ownership check and a dead image allowlist). Printed by `ab project show`, and after
    `ab project init` / `ab project add` / `ab project set`."""
    p = session.load_project(s, name)
    stored = registry.load(s.registry_path)[name]
    presets_map = presets.load_presets(s.presets_dir)
    ctx = projectkeys.posture(p)
    # The preset's own posture, with none of this project's overrides, so an override that LOOSENS it can be
    # marked (audit F-8). Only security-relevant keys are compared; the rest are never "looser".
    base = presets.effective(name, {"preset": p.preset}, presets_map)
    resolved = {"root": p.root, "preset": p.preset, "data_ro": p.data_ro, "data_rw": p.data_rw,
                "mask_skip": p.mask_skip, "extra_domains": p.extra_domains, "swap": p.swap,
                "compose_project": p.compose_project, "observe_logs": p.observe_logs,
                "dev_images": p.dev_images, "build_memory": f"{p.build_memory >> 20} MB",
                "profile": p.profile, "base_branch": p.base_branch, "reviews": p.reviews,
                "toulmin": p.toulmin, "mount": p.mount, "lan_addr": p.lan_addr, "deploy_ports": p.deploy_ports,
                "deploy_binds": p.deploy_binds, "deploy_files": p.deploy_files, "deploy_compose": p.deploy_compose,
                "deploy_external": p.deploy_external, "deploy_adopted": p.deploy_adopted,
                "history_secrets_accepted": p.history_secrets_accepted,
                "grant_exposure_accepted": p.grant_exposure_accepted,
                "data_env": p.data_env, "data_bind": p.data_bind}
    print(f"project {p.name}")
    for key in projectkeys.KEYS:
        if key.display == "stored" and key.name not in stored:
            continue
        if key.kind == "preset":
            value, source = p.eff[key.name]
        else:
            # A key missing from `resolved` falls back to the Project field of the same name (a stored
            # deploy_adopted once printed "(none)" because it was left out of the map above).
            value = resolved[key.name] if key.name in resolved else getattr(p, key.name)
            source = "project" if key.name in stored else "default"
        notes = []
        reason = projectkeys.inert_reason(key.name, ctx)
        if reason:
            notes.append(f"(ignored: {reason})")
        if key.name == "grant_exposure_accepted" and key.name in stored:
            notes.append(f"(a fingerprint of the findings; `ab project check {name}` shows what it covers)")
        if key.name == "preset" and presets_map.get(value, {}).get("ceiling"):
            notes.append("(a ceiling: an override may only tighten it)")
        if (key.name in stored and key.security and key.name in base
                and projectkeys.is_looser(key.name, value, base[key.name][0])):
            notes.append(f"(loosens preset {p.preset}, whose value is {_render(key, base[key.name][0])[0]})")
        if key.name == "code" and value == "ro":
            notes.append("(the project root is read-only in sessions; writable: "
                         + (", ".join(p.data_rw) if p.data_rw else "no grant -- add one outside the root with "
                            f"`ab project set {name} --data-rw DIR` for the agent's plans and notes") + ")")
        if key.name == "data_env" and value == "on":
            notes.append(f"(WARNING: sessions see the REAL env files (.env, .env.*, *.env) in {p.root}, not the dev "
                         f"swap; {session.TRUST_WARNING})")
        if key.name == "data_bind" and value == "on":
            from . import mounts
            notes.append(f"(WARNING: {mounts.data_dir(p.root)} is live data, visible and writable in sessions, and "
                         f"not in the pre-session snapshot: it is yours to back up; {session.TRUST_WARNING})")
        if key.name == "compose_project" and key.name in stored and ctx["live_access"]:
            notes.append("(explicit: this stack's containers are not required to come from inside the "
                         "project root)" if p.compose_project_explicit else
                         "(the same name the project would have by default, so this stack's containers must "
                         "still come from inside the project root)")
        for line in _render(key, value):
            print(f"  {key.name:<24} {line:<26} from {source}"
                  + ("  " + " ".join(notes) if notes else ""))
            notes = []                                  # the note belongs to the first line only


def _render(key, value):
    """A stored value as the one or more lines `ab project show` prints for it."""
    if key.name == "grant_exposure_accepted":
        from . import grantcheck            # stored as the accepted signatures; shown as their short id
        return [grantcheck.acceptance_fingerprint(value) or "(none)"]
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


def _refuse_if_live(s, names, what):
    """No registry edit while anything of one of these projects is running -- or while that cannot be
    determined. session.project_is_live is the single predicate (see its docstring: three audits got this
    wrong by trusting one signal and reading "unreadable" as "nothing running").

    There is deliberately no --force: ending live work is `ab session kill`'s job (decisions.md, 6 Oct, on
    why auto-reaping was rejected)."""
    podman = Podman(s.podman)
    blocked = []
    for name in names:
        live, why = session.project_is_live(s, podman, name)
        if live:
            blocked.append(f"{name}: {why}")
    if not blocked:
        return
    raise AgentboxError(f"cannot {what} while a session may be running:\n  " + "\n  ".join(blocked)
                        + "\nEnd the session named above and try again; its logs and end-of-session scan "
                          "are kept.")


def cmd_project_reset_state(s, a):
    session.load_project(s, a.name)
    _refuse_if_live(s, [a.name], f"reset the agent state of {a.name}")
    src = s.project_dir(a.name)
    if not s.agent_state_dirs(a.name):
        print(f"{a.name} has no agent state")
        return 0
    if not a.yes:
        try:
            answer = input(f"Reset the agent state of {a.name} (memory, settings, transcripts of every agent, "
                           f"in {src})? It is moved to {s.project_backups_dir(a.name)}/agent-state-<stamp>/ "
                           f"first. [y/N] ")
        except EOFError:              # no terminal (a script, `< /dev/null`): no answer is a no (item 47)
            print()
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("not reset")
            return 1
    moved = session.reset_state(s, a.name)
    print(f"reset: the agent state of {a.name} is now in {moved[1]}; the next session starts empty")
    return 0


def cmd_project_ls(s, a):
    for name, q in sorted(registry.load(s.registry_path).items()):
        print(f"{name:<20} {q.get('preset', '?'):<10} {q.get('root', '?')}")


def _state_summary(s, project):
    """(lines, unreviewed count) describing what is on disk for a project outside the registry: each agent's
    state and inbox, the grant backups and the built-image record (config.py's project layout)."""
    from . import backup, builtrec, profile
    lines, unreviewed = [], 0
    if not os.path.isdir(s.project_dir(project)):
        return lines, 0
    for agent, state in s.agent_state_dirs(project):
        inbox = s.inbox_dir(project, agent)
        names = profile.items(inbox)
        new = [n for n in names if not profile.read_sidecar(inbox, n).get("reviewed")]
        unreviewed += len(new)
        lines.append(f"  {agent}: state {state}" + (f", inbox {len(names)} item(s)"
                                                    f" ({len(new)} unreviewed)" if names else ", inbox empty"))
    snaps = backup._snapshots(s.project_backups_dir(project))
    if snaps:
        lines.append(f"  backups: {len(snaps)} grant snapshot(s) in {s.project_backups_dir(project)}")
    built = builtrec.ids(s.built_record(project))
    if built:
        lines.append(f"  built images: {len(built)} recorded in {s.built_record(project)} (the dev tier may run them)")
    return lines, unreviewed


def cmd_project_purge(s, a):
    """Delete the project's directory: agent state, inbox, grant backups, built-image record, identity. The
    one command that does -- and since one project is one directory, nothing of it is left anywhere else."""
    from .fsutil import rmtree
    registry.check_name(a.name)       # registered or not, but a NAME: `../x` must not become the rmtree target
    base = s.project_dir(a.name)
    if not os.path.isdir(base):
        print(f"{a.name} has no agent state at {base}")
        return 0
    _refuse_if_live(s, [a.name], f"purge the agent state of {a.name}")
    lines, unreviewed = _state_summary(s, a.name)
    print(f"purge would delete {base} (agent state, inbox, grant backups, built-image record):")
    for line in lines:
        print(line)
    print(f"{unreviewed} unreviewed inbox item(s) would be lost"
          + ("" if not unreviewed else " -- `ab profile harvest` reviews them first"))
    if not a.yes:
        print("nothing was deleted. Add --yes to go ahead.")
        return 1
    rmtree(base)
    print(f"deleted {base}")
    return 0


def cmd_project_sync(s, a):
    """`ab project sync`: review.sync for one reviewer, or for every reviewer of a builder. Allowed while
    either project has a live session: bringing the reviewer up to date mid-session is the point."""
    reg = registry.load(s.registry_path)
    if a.name not in reg:
        raise AgentboxError(f"no project named {a.name}; `ab project ls` lists them")
    targets = [a.name] if reg[a.name].get("reviews") else review.reviewers_of(reg, a.name)
    if not targets:
        raise AgentboxError(f"{a.name} is not a reviewer and has none; make one with "
                            f"`ab project add {a.name}-review --reviews {a.name} --preset <p>`")
    for t in targets:
        review.sync(reg, t, branch=a.branch, clones_dir=s.clones_dir(reg[t]["reviews"]))
    return 0


def cmd_project_clean(s, a):
    """Remove a project's per-session clones (clone mode) that no live session uses."""
    from .fsutil import rmtree
    from .podman import Podman
    registry.check_name(a.name)       # as purge: the name goes straight into a path that is rmtree'd
    base = s.clones_dir(a.name)
    if not os.path.isdir(base):
        print(f"{a.name} has no clones at {base}")
        return 0
    podman = Podman(s.podman)
    # a session still being torn down counts: its end-of-session scan reads its clone
    live = session.sessions_in_use(s, podman, a.name)
    gone = []
    for n in sorted(os.listdir(base)):
        path = os.path.join(base, n)
        if n in live:
            print(f"  kept {path}: session {n} is live")
            continue
        if not a.yes:
            print(f"  would remove {path}")
            continue
        rmtree(path, ignore_errors=True)
        gone.append(path)
        print(f"  removed {path}")
    if not a.yes and os.listdir(base):
        print("nothing was removed. Add --yes to go ahead (the owner merges or fetches from a clone first).")
        return 1
    return 0


def cmd_project_rm(s, a):
    reg = registry.load(s.registry_path)
    names = list(dict.fromkeys(a.name))          # de-duplicate (a repeated name would del twice -> KeyError)
    missing = [n for n in names if n not in reg]
    if missing:                                  # all-or-nothing: a typo in one name removes none
        raise AgentboxError(f"no project named {', '.join(missing)}")
    _refuse_if_live(s, names, f"remove {', '.join(names)}")   # all-or-nothing here too
    orphans = sorted(f"{r} (reviews {e['reviews']})" for r, e in reg.items()
                     if e.get("reviews") in names and r not in names)
    if orphans:
        raise AgentboxError(f"cannot remove {', '.join(names)}: {', '.join(orphans)} still review it. Remove the "
                            "reviewer too, or `ab project set <reviewer> --unset reviews` first")
    clones = [(n, reg[n]["root"]) for n in names if reg[n].get("reviews")]
    for n in names:
        del reg[n]
    registry.save(s.registry_path, reg)
    print(f"removed project {', '.join(names)} from the registry (files, state and backups are untouched)")
    for n, root in clones:
        print(f"  {n} was a reviewer: its clone {root} is still there, with any reviews under "
              f"{review.REVIEWS_DIR}/. Delete it when you are done: rm -rf {root}")
    for n in names:
        lines, unreviewed = _state_summary(s, n)
        for line in lines:
            print(line)
        if unreviewed:
            print(f"  {unreviewed} unreviewed inbox item(s) remain -- `ab profile harvest` still shows them, "
                  f"project removed or not")
        if lines:
            print(f"  `ab project purge {n}` deletes that state; re-adding {n} reattaches it")


def cmd_selftest(s, a):
    from . import selftest
    results, _raw = selftest.run(s, a.agent, a.name, warn=warn)
    fails = [r for r in results if r.verdict == "FAIL"]
    if a.json:
        print(json.dumps([r.__dict__ for r in results], indent=1))
    else:
        for r in results:
            print(f"  {r.verdict:<4}  {r.label}" + (f"  [{r.detail}]" if r.verdict != "PASS" else ""))
        print(f"\nRESULT selftest {a.agent}/{a.name}: {sum(r.verdict == 'PASS' for r in results)} pass, "
              f"{len(fails)} fail, {sum(r.verdict == 'SKIP' for r in results)} skip")
    return 1 if fails else 0


# The argparse help's form of project.TRUST_WARNING, worded as the user asked for it to appear (19 Nov 2026).
_TRUST_HELP = ("only for an agent you trust: the agent can read/print/send these secrets; masks are the safety net "
               "you are turning off")


def _add_project_setting_args(x, *, preset_required, preset_default=None, preset_choices=None,
                              preset_note="", presets_map=None):
    """The override flags shared by `ab project init`, `ab project add` and `ab project set`: same flag spellings,
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
    x.add_argument("--code", choices=projectkeys.BY_NAME["code"].values, metavar="ro|rw",
                   help="the project root: `ro` mounts it read-only (write plans and notes to a --data-rw "
                        "directory outside it), `rw` read-write. Overrides the preset (planner is ro, the "
                        "others rw)")
    x.add_argument("--extra-domain", action="append", default=[], metavar="DOMAIN",
                   help="add a domain to the egress allowlist (repeatable)")
    x.add_argument("--build-memory", metavar="SIZE",
                   help="memory limit for each build in the dev tier (default: 2g, e.g. 4g)")
    x.add_argument("--base-branch", metavar="BRANCH",
                   help="the branch this project's work lands on (e.g. mags/integration). The "
                        "end-of-session scan names that branch's new commits one by one instead of "
                        "reporting a count. agentbox neither creates nor checks out the branch; when a "
                        "tool like MAGS-CodeDev has its own base_branch setting, give the same name here")
    x.add_argument("--profile", choices=projectkeys.BY_NAME["profile"].values, metavar="on|off",
                   help="whether this project's sessions load the host-owned profile (skills, sub-agents, "
                        "commands, instructions, settings) and the agent's plugins (default: on). `off` also "
                        "means the harness image rather than any user image layer; agentbox's own session "
                        "notes still apply. `ab session start --no-profile` does the same for one run")
    x.add_argument("--mask-skip", action="append", default=[], metavar="DIR",
                   help="a folder (relative to root) the secret-mask walk skips for speed on huge trees; NOT "
                        "an exemption — grantcheck still scans it (repeatable)")
    x.add_argument("--mask", action="append", default=[], metavar="PATH",
                   help="mask a file or directory in sessions, on top of the secret masks: it exists, reads "
                        "empty and is read-only, in the agent's container and in any it starts. A bare name "
                        "(`data`) masks every file or directory of that name anywhere in the grants, as `.env` "
                        "is; `./data` or `deploy/state` masks that one path under the root. A masked "
                        "directory is left out of the pre-session backup and the end-of-session scan: it is "
                        "yours to back up (repeatable)")
    x.add_argument("--podman", choices=presets.PODMAN_TIERS, metavar="TIER",
                   help="podman tier: `none`, `observe` (read the live stack), or `dev` (run the agent's own "
                        "<project>-dev stack). `observe` and `dev` keep the preset's live-stack component "
                        "(`live = observe` on build, pods and operate); `none` removes it too, so the agent "
                        "has no podman access at all. `--unset podman` returns the whole value to the preset's. "
                        "`exec` is refused — not built (default: inherited from the preset)")
    x.add_argument("--compose-project", metavar="NAME",
                   help="the live stack's compose project name for the observe tier (default: the project name)")
    x.add_argument("--dev-image", action="append", default=[], metavar="PATTERN",
                   help="dev tier: also allow running or building from images matching this name pattern "
                        "(e.g. localhost/myapp:*); by default only allowlisted registries and this project's "
                        "builds (repeatable)")
    from . import mounts                  # the mask list is mounts.py's; the help must not keep its own copy
    x.add_argument("--swap", action="append", default=[], metavar="TARGET=STANDIN",
                   help="show the agent `STANDIN` in place of the masked file `TARGET`, in every directory "
                        "that has one (repeatable), e.g. --swap .env.local=.env.dev. TARGET must be a name "
                        f"the masks cover ({', '.join(mounts.MASK_FILES)}; anything under "
                        f"{', '.join(d + '/' for d in mounts.MASK_DIRS)}) and STANDIN must not be, so a swap "
                        "can never uncover a secret. Merged with the default .env=.env.dev; a mapping for the "
                        "same target replaces it")
    x.add_argument("--toulmin", action=argparse.BooleanOptionalAction, default=None,
                   help="the Toulmin integration: sessions ask `tm` for a socket directory and a bridge, mount "
                        "them, hand the agent an MCP entry for the tool server, and report the session's "
                        "clone, base and head back (docs/agentbox_toulmin_integration.md). Default: off")
    x.add_argument("--data-env", choices=projectkeys.BY_NAME["data_env"].values, metavar="on|off",
                   help="`on`: sessions see the REAL env files (`.env`, `.env.*`, `*.env`) anywhere in the project "
                        "root, read-write, instead of the masks and the dev swap (`.env.dev` shown at `.env`). "
                        "`.netrc`, `.pgpass`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.jks`, `secrets/` and every "
                        "env file in a --data-ro/--data-rw grant stay masked. WARNING: " + _TRUST_HELP + ". "
                        "Refused under a ceiling preset (lockdown) and in clone mode. Default: off")
    x.add_argument("--data-bind", choices=projectkeys.BY_NAME["data_bind"].values, metavar="on|off",
                   help="`on`: the live data in the project's `./data` is visible and WRITABLE to the agent; the "
                        "grant check does not refuse live data or compose data binds under it, and the "
                        "pre-session snapshot leaves it out (it is not a data backup: `./data` is yours to back "
                        "up). Secret files in it stay masked. WARNING: " + _TRUST_HELP + ". Refused under a "
                        "ceiling preset (lockdown), in clone mode, with a read-only root (`--code ro`), with a "
                        "read-only-data preset and with a mask over ./data. Default: off")
    x.add_argument("--mount", choices=projectkeys.MOUNT_MODES, metavar="MODE",
                   help="how the root reaches a session: `checkout` binds your working tree (the default); "
                        "`clone` binds a fresh clone on branch agentbox/<session> at the same path and never "
                        "mounts your checkout, its .git or anything untracked (the default when --toulmin "
                        "is on and the root is a git repository; the clone stays under the project's "
                        "directory until `ab project clean`)")
    x.add_argument("--reviews", metavar="BUILDER",
                   help="make this a reviewer of project BUILDER: its root is a clone of BUILDER's repository "
                        "(`ab project add` makes one next to BUILDER's root when no root is given), which "
                        "`ab project sync` brings to BUILDER's last commit, handing the reviewer's reviews/*.md "
                        "to BUILDER's .review/")
    x.add_argument("--observe-logs", action=argparse.BooleanOptionalAction, default=None,
                   help="let the observe tier read the live stack's logs (default: off; logs can hold "
                        "secrets, and what the agent reads goes to its model provider)")
    x.add_argument("--lan-addr", metavar="ADDR",
                   help="`ab deploy`: the IPv4 address of this host the live stack publishes on (`ab deploy "
                        "--lan ADDR:PORT` stores it too)")
    x.add_argument("--deploy-port", action="append", default=[], type=int, metavar="PORT",
                   help="`ab deploy`: a host port the live stack may publish on the LAN address (repeatable; "
                        "`ab deploy --lan ADDR:PORT` adds one)")
    x.add_argument("--deploy-bind", action="append", default=[], metavar="DIR",
                   help="`ab deploy`: a host directory the live stack may bind (a media library, a data dir; "
                        "repeatable). Not the project's root, another project's grant, $HOME, a system or "
                        "agentbox directory")
    x.add_argument("--deploy-file", action="append", default=[], metavar="PATH",
                   help="`ab deploy`: an untracked file (root-relative) copied into the deploy copy next to the "
                        "real .env, which is always copied (repeatable)")
    x.add_argument("--deploy-compose", metavar="PATH",
                   help="`ab deploy`: the compose file, root-relative, when it is not at the root (e.g. "
                        "deploy/compose.yaml); the real .env is still the root's")
    x.add_argument("--deploy-external", action="append", default=[], metavar="NAME",
                   help="`ab deploy`: an `external:` volume or network the live stack may use (repeatable); "
                        "agentbox never creates or removes it")
    x.add_argument("--accept-history-secrets", action="store_true",
                   help="register even though masked file names (.env, *.key, ...) are in git history")
    x.add_argument("--i-understand-grant-exposure", action="store_true",
                   help="register even though the grant holds secrets or live data (a grant is for code; "
                        "move them outside it or add a mask)")


def _add_identity_args(x):
    """`ab project add` and `ab project init`: what to do when the name already has state on disk made for another
    root (layout.py). Without either, that is refused."""
    g = x.add_mutually_exclusive_group()
    g.add_argument("--reattach", action="store_true",
                   help="the name has agent state on disk that was made for a different root: carry it over "
                        "to this root on purpose (memory, inbox, backups and built-image record included)")
    g.add_argument("--fresh", action="store_true",
                   help="the name has agent state on disk: archive it first (every agent's state and the "
                        "built-image record go to <project>/backups/agent-state-<stamp>/) and start empty")


def _add_project_unset_args(x):
    """`ab project set`'s own flags for taking a setting away again (the add/init side has nothing to
    unset)."""
    x.add_argument("--unset", action="append", default=[], choices=UNSETTABLE, metavar="KEY",
                   help="drop a stored setting so the preset's value applies again (repeatable); one of: "
                        + ", ".join(UNSETTABLE))
    for dest, key in CLEAR_FLAGS.items():
        x.add_argument("--" + dest.replace("_", "-"), action="store_true",
                       help=f"empty the {key} list (the same as --unset {key})")


def add_verbs(proj, add, ex, preset_choices, presets_map):
    """The `ab project` subparsers, built with the CLI's own `add` and `ex` helpers (as screen.add_verbs is);
    `--preset` takes its choices and help from the presets that exist (parser() loads them)."""
    from . import initproj
    x = add(proj, "init",
            "scaffold a new project with a grantcheck-clean layout (secrets masked, live data in a named "
            "volume), then register it",
            ex("ab project init pod filelab",
               "ab project init python dataproc --upstream git@github.com:me/dataproc.git"))
    x.add_argument("template", choices=initproj.TEMPLATES,
                   help="which scaffold to create: `pod` (a compose/dev-stack service, live data in a named "
                        "volume) or `python` (a script/library, data path from DATA_DIR)")
    x.add_argument("name", help="project name; the repo is created at <repos_dir>/<name> and registered under it")
    x.add_argument("--upstream", metavar="URL",
                   help="git URL to add as origin and push to (an off-box CODE backup; it does NOT back up "
                        ".env or the data volume)")
    _add_project_setting_args(x, preset_required=False, preset_choices=preset_choices,
                              presets_map=presets_map,
                              preset_note=" (default: the template's own — pod: build, python: script)")
    _add_identity_args(x)
    x.set_defaults(fn=cmd_init)

    x = add(proj, "add", "register a project with a preset (validated against overlap and grant exposure)",
            ex("ab project add web ~/repos/web --preset build",
               "ab project add dataproc ~/repos/dataproc --preset script --data-ro ~/data/raw --data-rw ~/data/out",
               "ab project add notes ~/repos/notes --preset research",
               "ab project add svc ~/repos/svc --preset build-open --podman dev",
               "ab project add web-review --reviews web --preset script   clone web to ~/repos/web-review"))
    x.add_argument("name", help="a short name for the project (lower-case letters, digits, '-' or '_')")
    x.add_argument("root", nargs="?",
                   help="path to the project's code directory — the read-write grant; must exist and not overlap "
                        "another project or a protected path. Optional with --reviews: a clone of the builder "
                        "is made next to the builder's root")
    _add_project_setting_args(x, preset_required=True, preset_choices=preset_choices,
                              presets_map=presets_map)
    _add_identity_args(x)
    x.set_defaults(fn=cmd_project_add)
    add(proj, "ls", "list registered projects (name, preset, root)",
        ex("ab project ls")).set_defaults(fn=cmd_project_ls)
    x = add(proj, "show", "print every effective setting and whether it came from the preset or an override",
            ex("ab project show web"))
    x.add_argument("name", help="the registered project to inspect")
    x.set_defaults(fn=cmd_project_show)
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
    x = add(proj, "rm", "remove one or more projects from the registry (files, state and backups are untouched)",
            ex("ab project rm web", "ab project rm web old-thing"))
    x.add_argument("name", nargs="+", help="one or more registered project names (all-or-nothing: a bad name "
                                           "removes none)")
    x.set_defaults(fn=cmd_project_rm)

    x = add(proj, "sync", "bring a reviewer's clone to its builder's last commit and hand the reviewer's "
                          "reviews to the builder (host side; neither agent can reach the other's tree)",
            ex("ab project sync web-review                 fetch, check out web's branch detached, copy reviews",
               "ab project sync web                        every reviewer of web",
               "ab project sync web-review --branch feature-x"))
    x.add_argument("name", help="a reviewer project (`--reviews`), or a builder to sync all its reviewers")
    x.add_argument("--branch", metavar="BRANCH",
                   help="the builder's branch to check out (default: the branch the builder is on)")
    x.set_defaults(fn=cmd_project_sync)

    x = add(proj, "check",
            "show what the grant scan finds now and whether it matches what was accepted (read-only)",
            ex("ab project check web"))
    x.add_argument("name", help="the registered project to scan")
    x.set_defaults(fn=cmd_project_check)
    x = add(proj, "clean", "remove the per-session clones a project's clone-mode sessions left (not a live "
                           "session's); the owner fetches or merges from a clone before removing it",
            ex("ab project clean web", "ab project clean web --yes"))
    x.add_argument("name", help="the project whose clones to remove")
    x.add_argument("--yes", action="store_true", help="remove them (without it: list what would go)")
    x.set_defaults(fn=cmd_project_clean)
    x = add(proj, "selftest", "prove the sandbox boundaries hold for a project (posture, egress, mounts, "
                              "podman scoping), in a throwaway session",
            ex("ab project selftest claude web", "ab project selftest claude web --json"))
    x.add_argument("agent", choices=adapters.NAMES, help="agent to run the checks as")
    x.add_argument("name", help="the registered project to test")
    x.add_argument("--json", action="store_true", help="print results as JSON instead of a table")
    x.set_defaults(fn=cmd_selftest)

    x = add(proj, "accept-grant-exposure",
            "accept that this project's grant holds secrets or live data the agent can read",
            ex("ab project accept-grant-exposure web"))
    x.add_argument("name", help="the registered project to record the acknowledgement for")
    x.set_defaults(fn=cmd_project_accept_grant_exposure)
    x = add(proj, "accept-history-secrets",
            "accept (after rotating them) the masked file names now in a project's git history",
            ex("ab project accept-history-secrets web"))
    x.add_argument("name", help="the registered project to record the acknowledgement for")
    x.set_defaults(fn=cmd_project_accept_history_secrets)
    x = add(proj, "reset-state",
            "wipe a project's agent state (memory, settings, transcripts); asks first, keeps a copy in the "
            "backups dir", ex("ab project reset-state web", "ab project reset-state web --yes"))
    x.add_argument("name", help="the registered project whose agent state to wipe")
    x.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    x.set_defaults(fn=cmd_project_reset_state)
    x = add(proj, "purge", "delete a project's agent state and inbox outright (asks for --yes; the only "
                           "command that deletes them)",
            ex("ab project purge web", "ab project purge web --yes"))
    x.add_argument("name", help="the project whose agent state to delete (registered or not)")
    x.add_argument("--yes", action="store_true", help="go ahead and delete")
    x.set_defaults(fn=cmd_project_purge)
