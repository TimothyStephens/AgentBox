"""`ab` command line."""
import argparse
import json
import os
import sys

from . import adapters, config, keys, layout, presets, projectkeys, registry, session, tokens
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
    if a.podman:
        entry["podman"] = {"tier": a.podman}
    if a.compose_project:
        entry["compose_project"] = _check(a.name, "compose_project", a.compose_project)
    if a.observe_logs is not None:
        entry["observe_logs"] = a.observe_logs
    if a.dev_image:
        entry["dev_images"] = _check(a.name, "dev_images", list(a.dev_image))
    if a.swap:
        entry["swap"] = _check(a.name, "swap", _parse_swap(a.swap))
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
    reg = registry.load(s.registry_path)
    presets_map = presets.load_presets(s.presets_dir)
    entry = {}
    _apply_project_settings(entry, a, _grant_kw(s), presets_map)
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
    return session.history_secrets(entry["root"], grants, entry.get("mask_skip", []))


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
        print(f"\n  => nothing is exposed any more: the stored acceptance is stale, and the next")
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
        print(f"     Move them out of the grant, or accept the new state with")
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
                "profile": p.profile, "base_branch": p.base_branch,
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
        if key.name == "grant_exposure_accepted" and key.name in stored:
            notes.append(f"(a fingerprint of the findings; `ab project check {name}` shows what it covers)")
        if key.name == "preset" and presets_map.get(value, {}).get("ceiling"):
            notes.append("(a ceiling: an override may only tighten it)")
        if (key.name in stored and key.security and key.name in base
                and projectkeys.is_looser(key.name, value, base[key.name][0])):
            notes.append(f"(loosens preset {p.preset}, whose value is {_render(key, base[key.name][0])[0]})")
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
        answer = input(f"Reset the agent state of {a.name} (memory, settings, transcripts of every agent, in "
                       f"{src})? It is moved to {s.project_backups_dir(a.name)}/agent-state-<stamp>/ first. "
                       f"[y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("not reset")
            return 1
    moved = session.reset_state(s, a.name)
    print(f"reset: the agent state of {a.name} is now in {moved[1]}; the next session starts empty")
    return 0


def cmd_project_ls(s, a):
    for name, q in sorted(registry.load(s.registry_path).items()):
        print(f"{name:<20} {q.get('preset', '?'):<10} {q.get('root', '?')}")


# --- the host-owned profile and the inbox ------------------------------------------------------------

def _inbox_rows(s, agent):
    """Every inbox item of one agent, across all projects, newest first, with its provenance."""
    from . import profile
    reg = registry.load(s.registry_path)
    rows = []
    for project, inbox in profile.all_inboxes(s, agent):
        for name in profile.items(inbox):
            doc = profile.read_sidecar(inbox, name)
            rows.append({"project": project, "name": name, "path": os.path.join(inbox, name),
                         "registered": project in reg, "reviewed": bool(doc.get("reviewed")),
                         "added": doc.get("added", ""), "egress": doc.get("egress", ""),
                         "preset": doc.get("preset", ""), "session": doc.get("session", ""),
                         "content": profile.content_id(os.path.join(inbox, name))})
    return sorted(rows, key=lambda r: (r["added"], r["project"], r["name"]), reverse=True)


def cmd_image_build(s, a):
    from . import userimage
    from .podman import Podman
    userimage.build(s, Podman(s.podman), a.agent, force=a.force)
    return 0


def cmd_profile_harvest(s, a):
    """Read-only: what sessions have put in their inboxes, and nothing else.

    An inbox item is reviewed out of context, often days later, so each line carries where it came from --
    and `egress open` is marked, because an item written by a session that could reach the whole internet
    deserves a closer read before it becomes something every later session loads."""
    from . import profile
    agents = [a.agent] if a.agent else list(adapters.NAMES)
    total = 0
    for agent in agents:
        rows = [r for r in _inbox_rows(s, agent) if a.all or not r["reviewed"]]
        src = s.profile_src(agent)
        print(f"profile {agent}: {src}" + (f"  (commit {profile.commit_id(src)})"
                                           if profile.commit_id(src) else "  (no git repo yet)"))
        if not rows:
            print("  inbox: nothing unreviewed\n")
            continue
        total += len(rows)
        groups = {}
        for r in rows:
            groups.setdefault((r["name"], r["content"]), []).append(r)
        by_name = {}
        for r in rows:
            by_name.setdefault(r["name"], set()).add(r["content"])
        print(f"  {len(rows)} unreviewed item(s) in {len({r['project'] for r in rows})} project(s):")
        for (name, content), same in sorted(groups.items()):
            where = ", ".join(f"{r['project']}{'' if r['registered'] else ' (project removed)'}"
                              for r in same)
            flags = []
            if any(r["egress"] == "open" for r in same):
                flags.append("FROM AN OPEN-EGRESS SESSION")
            if len(same) > 1:
                flags.append(f"identical in {len(same)} projects")
            if len(by_name.get(name, ())) > 1:
                flags.append("same name, DIFFERENT content elsewhere")
            if any(r["reviewed"] for r in same):
                flags.append("reviewed")
            print(f"    {name}  [{content}]  {where}")
            print(f"      added {same[0]['added'] or '?'}  preset {same[0]['preset'] or '?'}  "
                  f"egress {same[0]['egress'] or '?'}  session {same[0]['session'] or '?'}")
            if flags:
                print(f"      ** {'; '.join(flags)}")
            print(f"      promote: ab profile promote {same[0]['project']}/{name} --as skills/{name}")
        print()
    if total:
        print("Nothing above is loaded by any session. `promote` copies an item into the profile and commits "
              "it; `dismiss` marks it reviewed and leaves it where it is.")
    return 0


def _find_item(s, agent, spec):
    rows = [r for r in _inbox_rows(s, agent)
            if spec in (f"{r['project']}/{r['name']}", r["name"])]
    if not rows:
        raise AgentboxError(f"no inbox item {spec!r} for {agent}; `ab profile harvest` lists them")
    if len({(r["project"], r["name"]) for r in rows}) > 1:
        where = ", ".join(f"{r['project']}/{r['name']}" for r in rows)
        raise AgentboxError(f"{spec!r} is ambiguous: {where}. Name it as <project>/<item>.")
    return rows[0]


def cmd_profile_promote(s, a):
    """Copy an inbox item into the host-owned profile and commit it, with where it came from in the message.

    The destination is explicit (`--as`) unless the item is plainly a skill directory: promoting writes into
    the one thing every session loads, in every project, so guessing where it belongs is not good enough."""
    import shutil
    from . import profile
    row = _find_item(s, a.agent, a.item)
    src, prof = row["path"], s.profile_src(a.agent)
    dest = a.as_path
    if not dest:
        if os.path.isdir(src) and os.path.isfile(os.path.join(src, "SKILL.md")):
            dest = f"skills/{row['name']}"
        else:
            raise AgentboxError(f"say where it goes in the profile: --as skills/<name> (one of "
                                f"{', '.join(profile.ITEMS)}). {src} is not plainly a skill directory.")
    if dest.startswith("/") or ".." in dest.split("/"):
        raise AgentboxError(f"--as {dest!r} must be a relative path inside the profile")
    dst = os.path.join(prof, dest)
    if os.path.lexists(dst):
        raise AgentboxError(f"{dst} already exists; remove it or choose another --as")
    profile.ensure_repo(prof, out=print)
    os.makedirs(os.path.dirname(dst) or prof, mode=0o700, exist_ok=True)
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=False,
                        ignore=shutil.ignore_patterns("*" + profile.SIDECAR))
    else:
        shutil.copyfile(src, dst)
    doc = profile.read_sidecar(s.inbox_dir(row["project"], a.agent), row["name"])
    msg = (f"profile: add {dest} (promoted from {row['project']}/{row['name']})\n\n"
           f"project: {row['project']}\nagent: {a.agent}\nsession: {doc.get('session', '?')}\n"
           f"added: {doc.get('added', '?')}\npreset: {doc.get('preset', '?')}\n"
           f"egress: {doc.get('egress', '?')}\n")
    profile._git(prof, "add", "-A")
    profile._git(prof, "-c", "user.name=agentbox", "-c", "user.email=agentbox@localhost",
                 "commit", "-qm", msg)
    doc.update({"reviewed": True, "promoted_to": dest, "promoted_commit": profile.commit_id(prof)})
    profile.write_sidecar(s.inbox_dir(row["project"], a.agent), row["name"], doc)
    print(f"promoted {row['project']}/{row['name']} -> {dst}")
    print(f"  profile commit {profile.commit_id(prof)}; the next session of every project loads it")
    print(f"  the inbox copy is kept and marked reviewed ({row['path']})")
    return 0


def cmd_profile_install(s, a):
    """`ab profile install <agent>`. The declaration is the profile's; what is installed is agentbox's.

    Not a session: the profile is not a grant, so there is no project, no lock, no scan and no gate --
    just one hardened container behind the same sealed networks and squid a session gets, with the tree
    mounted at the path a session will see it at (installer.py says why that matters)."""
    from . import installer, plugins
    doc = installer.install(s, a.agent, podman=Podman(s.podman), warn=warn, dry_run=a.dry_run)
    if a.dry_run:
        print(f"would install {len(doc['entries'])} entry(ies); the container would run:\n")
        print(doc["script"])
        return 0
    tree = installer.tree_dir(s, a.agent)
    print(f"installed into {tree}")
    print(f"  against {doc['image']['ref']} ({doc['image'].get('cli_version') or 'version unknown'}, "
          f"node {doc['image'].get('node_major') or '?'})")
    for kind in ("npm", "marketplace"):
        for item in doc["state"].get(kind, []) or []:
            detail = item.get("version") or (item.get("commit") or "")[:12]
            if item.get("plugins"):
                detail += f" ({', '.join(item['plugins'])})"
            print(f"  {kind:<12} {item.get('name') or item.get('id')} {detail}".rstrip())
    print(f"  lockfile {os.path.join(tree, installer.LOCKFILE)}")
    print("Sessions mount this read-only. `ab profile install` again after editing plugins.toml.")
    return 0


def cmd_profile_dismiss(s, a):
    from . import profile
    row = _find_item(s, a.agent, a.item)
    inbox = s.inbox_dir(row["project"], a.agent)
    doc = profile.read_sidecar(inbox, row["name"])
    doc.update({"reviewed": True, "dismissed": True})
    profile.write_sidecar(inbox, row["name"], doc)
    print(f"dismissed {row['project']}/{row['name']} (kept at {row['path']}; nothing was deleted)")
    return 0


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
    import shutil
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
    shutil.rmtree(base)
    print(f"deleted {base}")
    return 0


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
    for n in names:
        lines, unreviewed = _state_summary(s, n)
        for line in lines:
            print(line)
        if unreviewed:
            print(f"  {unreviewed} unreviewed inbox item(s) remain -- `ab profile harvest` still shows them, "
                  f"project removed or not")
        if lines:
            print(f"  `ab project purge {n}` deletes that state; re-adding {n} reattaches it")


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
    """`ab session start <agent> <project>`: flags like --shell are parsed normally, and anything after a
    `--` is passed through to the agent or shell (split off in main() as a.passthrough)."""
    agent, project_name = a.agent, a.project
    _require_registered(s, project_name)
    podman = Podman(s.podman)
    # One live session per project: the lock is taken before any work, so two `ab session start`s racing cannot both
    # get past here, and it is released by teardown however this session ends.
    with session.take_project_lock(s, podman, project_name, warn=warn) as lock:
        project = session.load_project(s, project_name)
        sess = session.make_session(s, agent, project, warn=warn, no_profile=a.no_profile)
        sess.prepare()
        lock.note(sess.name)
        command = sess.agent_command(shell=a.shell, extra=list(getattr(a, "passthrough", []) or []))
        if sys.stdin.isatty() and not a.no_tty:
            return sess.run_interactive(command, tty=True)
        p = sess.run_captured(command, stdin=sys.stdin.read() if not sys.stdin.isatty() else "",
                              timeout=a.timeout)
        sys.stdout.write(p.stdout or "")
        sys.stderr.write(p.stderr or "")
        return p.returncode


def _require_registered(s, project_name):
    """A project is a grant, so registering one is a decision -- never a side effect of `ab session start`.

    The message is the command to run, filled in: the root guessed from the repos dir when something is
    actually there, and left as a placeholder when it is not."""
    reg = registry.load(s.registry_path)
    if project_name in reg:
        return
    registry.check_name(project_name)
    guess = os.path.join(os.path.expanduser(s.repos_dir), project_name)
    root = guess if os.path.isdir(guess) else "<path to its code>"
    known = ", ".join(sorted(reg)) or "none yet"
    raise AgentboxError(
        f"no project named {project_name!r}. agentbox does not register one for you -- a project names a "
        f"grant the agent gets read-write, which is a decision, not a default. Register it, then run:"
        f"\n  ab project add {project_name} {root} --preset <preset>"
        f"\n  ab session start {project_name}"
        f"\n`ab project add --help` lists the presets. Registered now: {known}.")


def cmd_attach(s, a):
    """Re-attach a terminal to the project's running session.

    The agent is a container the launcher started, so this is `podman attach` on it: the same stdio the
    launcher has. If the launcher's own terminal is still open, both are attached to the one agent -- which
    is the honest shape of re-attaching to a process rather than to a multiplexer. Detach with ctrl-p ctrl-q
    (`ab screen`, the tmux workspace, is the planned version of this; design.md)."""
    podman = Podman(s.podman)
    name = a.project
    known = session.sessions(s, podman)
    # A project name (the usual) or a session id (what `ab session ls` prints): every session verb takes
    # either, through the same resolution as `kill` and `preview`.
    sid = session.live_session_of(s, podman, name)
    if not sid and name in known and session.session_is_alive(s, name, known[name])[0]:
        sid = name
    if not sid:
        if name in known:
            raise AgentboxError(f"session {name} is not running any more; `ab session ls` shows what is")
        _require_registered(s, name)
        raise AgentboxError(f"no session of {name} is running; start one with "
                            f"`ab session start <agent> {name}`")
    info = known.get(sid) or {}
    project = (info.get("meta") or {}).get("project") or next(
        (c.get("project") for c in info.get("containers") or [] if c.get("project")), name)
    agents = [c["name"] for c in info.get("containers") or []
              if c.get("role") == "agent" and c.get("state") == "running"]
    if not agents:
        raise AgentboxError(f"session {sid} of {project} is alive but its agent container is not running "
                            f"(it may be starting, or it may have exited). `ab session ls` shows it; "
                            f"`ab session kill {project}` ends it.")
    print(f"attaching to {project} (session {sid}); detach with ctrl-p ctrl-q", file=sys.stderr)
    return podman.interactive(["attach", agents[0]])


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
        # The PROJECT is what a user names (`ab session attach web`, `ab session kill web`); the session id is kept
        # because it is what the logs, the container labels and the inbox provenance carry.
        print(f"{meta.get('project', '?'):<16} {state:<7} {meta.get('agent', '?'):<7} "
              f"start {start_str}  age {age:<6} idle {_dur(idle):<6} "
              f"{len(info['containers'])}c/{len(info['networks'])}n  id {name}{mark}")
    for name, idle in flagged:
        project = (rows[name]["meta"] or {}).get("project") or name
        warn(f"ab: {project} has been idle {_dur(idle)} — if it is forgotten, end it with "
             f"`ab session kill {project}` (its logs and end-of-session scan are kept).")


def cmd_preview(s, a):
    from . import preview
    podman = Podman(s.podman)
    sid = _resolve_session(s, podman, a.session)       # a project name or a session id, as kill and attach
    if a.stop:
        gone = preview.stop(s, podman, sid)
        print("removed: " + (" ".join(gone) if gone else "no previews"))
        return 0
    if not a.target:
        raise AgentboxError("give <service>:<port> (or --stop)")
    port, desk = preview.start(s, podman, sid, a.target, a.port)
    print(f"preview of {a.target} in {sid}: http://127.0.0.1:{port}/ (on this host only)\n"
          f"from your desktop: {desk}\n  then open http://127.0.0.1:{port}/\n"
          "it ends with the session (or ab session preview --stop)")
    return 0


def _resolve_session(s, podman, name):
    """A session id or a project name. One session per project, so a project name is unambiguous -- and it
    is what the user sees everywhere else."""
    known = session.sessions(s, podman)
    if name in known:
        return name
    sid = session.live_session_of(s, podman, name)
    if sid:
        return sid
    of_project = sorted(k for k, info in known.items()
                        if (info.get("meta") or {}).get("project") == name
                        or any(c.get("project") == name for c in info.get("containers") or []))
    if len(of_project) == 1:
        return of_project[0]
    if of_project:
        raise AgentboxError(f"{name} has more than one session on disk ({', '.join(of_project)}); name the "
                            f"one you mean")
    raise AgentboxError(f"no session of {name} (and no session named {name}); `ab session ls` lists them")


def cmd_kill(s, a):
    podman = Podman(s.podman)
    sid = _resolve_session(s, podman, a.session)
    session.teardown(s, podman, sid, warn=warn)
    print(f"killed {sid}")


def cmd_gc(s, a):
    removed = session.gc(s, Podman(s.podman), warn=warn)
    print("removed: " + (" ".join(removed) if removed else "nothing"))


def cmd_selftest(s, a):
    from . import selftest
    results, raw = selftest.run(s, a.agent, a.name, warn=warn)
    fails = [r for r in results if r.verdict == "FAIL"]
    if a.json:
        print(json.dumps([r.__dict__ for r in results], indent=1))
    else:
        for r in results:
            print(f"  {r.verdict:<4}  {r.label}" + (f"  [{r.detail}]" if r.verdict != "PASS" else ""))
        print(f"\nRESULT selftest {a.agent}/{a.name}: {sum(r.verdict == 'PASS' for r in results)} pass, "
              f"{len(fails)} fail, {sum(r.verdict == 'SKIP' for r in results)} skip")
    return 1 if fails else 0


def cmd_screen(s, a):
    """`ab screen`: the tmux workspace (agentbox/screen.py), every agent pane entering the sandbox through
    `ab session start`."""
    from . import screen
    return screen.VERBS[a.sub](s, a, out=print, err=warn)


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
                        "<project>-dev stack). `exec` is refused — not built (default: inherited from the "
                        "preset)")
    x.add_argument("--compose-project", metavar="NAME",
                   help="the live stack's compose project name for the observe tier (default: the project name)")
    x.add_argument("--dev-image", action="append", default=[], metavar="PATTERN",
                   help="dev tier: also allow running or building from images matching this name pattern "
                        "(e.g. localhost/myapp:*); by default only allowlisted registries and this project's "
                        "builds (repeatable)")
    x.add_argument("--swap", action="append", default=[], metavar="TARGET=STANDIN",
                   help="show the agent `STANDIN` in place of the masked file `TARGET`, in every directory "
                        "that has one (repeatable), e.g. --swap .env.local=.env.dev. TARGET must be a name "
                        "the masks cover (.env, .env.*, *.pem, *.key) and STANDIN must not be, so a swap can "
                        "never uncover a secret. Merged with the default .env=.env.dev; a mapping for the "
                        "same target replaces it")
    x.add_argument("--observe-logs", action=argparse.BooleanOptionalAction, default=None,
                   help="let the observe tier read the live stack's logs (default: off; logs can hold "
                        "secrets, and what the agent reads goes to its model provider)")
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

    def demote(subparsers, names):
        """Advanced verbs (or nouns): they still parse, but leave the listed block of `--help` for an
        `advanced:` section underneath, so the everyday ones read first. Returns [(name, summary)] in the
        order given, for `finish`."""
        rows = {}
        keep = []
        for act in subparsers._choices_actions:        # argparse's own list of what `--help` shows
            if act.dest in names:
                rows[act.dest] = act.help
            else:
                keep.append(act)
        subparsers._choices_actions[:] = keep           # the choice itself stays: it still parses
        return [(n, rows[n]) for n in names if n in rows]

    def finish(parser_obj, rows, what):
        """Put the demoted entries into the parser's epilog, above its examples."""
        if not rows:
            return
        import textwrap
        lines = [f"advanced {what} (rarely needed; `--help` on each):"]
        for n, h in rows:
            lines.append(textwrap.fill(h, width=100, initial_indent=f"  {n:<26} ", subsequent_indent=" " * 29))
        parser_obj.epilog = "\n".join(lines) + "\n\n" + (parser_obj.epilog or "")

    p = argparse.ArgumentParser(
        prog="ab", formatter_class=raw,
        description="agentbox: coding agents (Claude Code, OMP) in hardened rootless podman sandboxes, one "
                    "project per session.",
        epilog="Two nouns: `ab project` is the saved configuration (init, add, set, ...), `ab session` is the "
               "live run (start, attach, kill, ...). `ab screen` opens the tmux workspace. The advanced "
               "commands below are host setup and rare repairs. Full reference: docs/USAGE.md.\n\n"
               + ex("ab project init pod web              scaffold and register a new project",
                    "ab session start claude web          start a session",
                    "ab session ls                     list running sessions",
                    "ab project show web               a project's effective settings"))
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    from . import initproj
    proj_noun = sub.add_parser(
        "project", help="manage registered projects (the saved configuration)", formatter_class=raw,
        description="manage registered projects (the saved configuration). A project names a code grant, a "
                    "preset and any extra data grants.",
        epilog=ex("ab project add web ~/repos/web --preset build",
                  "ab project set web --egress open", "ab project show web", "ab project ls",
                  "ab project rm web"))
    proj = proj_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
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
               "ab project add svc ~/repos/svc --preset build-open --podman dev"))
    x.add_argument("name", help="a short name for the project (lower-case letters, digits, '-' or '_')")
    x.add_argument("root", help="path to the project's code directory — the read-write grant; must exist and "
                                "not overlap another project or a protected path")
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

    x = add(proj, "check",
            "show what the grant scan finds now and whether it matches what was accepted (read-only)",
            ex("ab project check web"))
    x.add_argument("name", help="the registered project to scan")
    x.set_defaults(fn=cmd_project_check)
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
    finish(proj_noun, demote(proj, ['check', 'selftest', 'accept-grant-exposure', 'accept-history-secrets', 'reset-state', 'purge']), "verbs")

    sess_noun = sub.add_parser(
        "session", help="the live runs: start, attach, list, preview, kill, gc", formatter_class=raw,
        description="the live runs. `start` creates one in the foreground (one per project); every other "
                    "verb names a project or a session id.",
        epilog=ex("ab session start claude web", "ab session attach web", "ab session ls",
                  "ab session preview web web:8000", "ab session kill web", "ab session gc"))
    sess = sess_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    x = add(sess, "start", "start an agent session in the foreground (one per project)",
            ex("ab session start claude web",
               "ab session start claude web --shell         a bash shell in the sandbox instead of the agent",
               "ab session start claude web -- --resume     pass everything after -- to the agent",
               "ab session attach web                       re-attach to the session already running"))
    x.add_argument("agent", choices=adapters.NAMES, help="the agent to run")
    x.add_argument("project", help="the registered project to run against; anything after a `--` is passed "
                                   "through to the agent or shell")
    x.add_argument("--shell", action="store_true", help="run a bash shell in the sandbox instead of the agent")
    x.add_argument("--no-tty", action="store_true",
                   help="do not allocate a TTY (for non-interactive or piped runs)")
    x.add_argument("--no-profile", action="store_true",
                   help="for this run only: load no host-owned profile (skills, sub-agents, commands, "
                        "instructions, settings) and no plugins, and use the harness image. agentbox's own "
                        "session notes still apply. `ab project set <p> --profile off` makes it permanent")
    x.add_argument("--timeout", type=int, default=3600, metavar="SECONDS",
                   help="time limit for a non-interactive run (default 3600; ignored for interactive sessions)")
    x.set_defaults(fn=cmd_run)

    x = add(sess, "attach", "re-attach a terminal to the session already running for a project",
            ex("ab session attach web", "ab session attach ab-claude-web-1   by session id"))
    x.add_argument("project", help="the project whose running session to attach to (one per project), or the "
                                   "session id itself")
    x.set_defaults(fn=cmd_attach)
    add(sess, "ls", "list sessions with their project, start time, age and idle time",
        ex("ab session ls")).set_defaults(fn=cmd_ls)
    x = add(sess, "kill", "tear down a session and everything it created (keeps its logs and end-of-session scan)",
            ex("ab session kill web", "ab session kill ab-claude-web-1   by session id"))
    x.add_argument("session", help="the project whose session to tear down, or the session id itself "
                                   "(`ab session ls` shows both)")
    x.set_defaults(fn=cmd_kill)
    x = add(sess, "preview",
            "open a session's dev service on 127.0.0.1 (reach it from your desktop with ssh -L)",
            ex("ab session preview web web:8000",
               "ab session preview web web:8000 --port 18080",
               "ab session preview web --stop"))
    x.add_argument("session", help="the project whose session to preview, or the session id itself "
                                   "(`ab session ls` shows both)")
    x.add_argument("target", nargs="?", metavar="SERVICE:PORT",
                   help="the dev-stack service (or container) and the port inside it, e.g. web:8000; omit with "
                        "--stop")
    x.add_argument("--port", type=int, metavar="PORT",
                   help="host port to publish on 127.0.0.1 (default: a free one, printed on start)")
    x.add_argument("--stop", action="store_true", help="remove this session's previews instead of opening one")
    x.set_defaults(fn=cmd_preview)
    add(sess, "gc", "remove what dead sessions left (containers, networks, sockets) and storage nothing "
                    "can use: session-built images, superseded personal image layers, unused profile "
                    "snapshots (runs automatically after boot)",
        ex("ab session gc")).set_defaults(fn=cmd_gc)
    finish(sess_noun, demote(sess, ['preview', 'gc']), "verbs")

    scr_noun = sub.add_parser(
        "screen", help="tmux workspaces: one per project, agent panes and host shells in a layout",
        formatter_class=raw,
        description="tmux workspaces, one per project (`ab-<project>`): agent panes that enter the sandbox "
                    "through `ab session start` and host shells, laid out from ~/.config/agentbox/screen.toml. "
                    "Both projects must be registered; every pane opens in the project's root.",
        epilog=ex("ab screen start web", "ab screen start web web-review --layout wide", "ab screen ls",
                  "ab screen attach web", "ab screen kill web", "ab screen layouts",
                  "ab screen config > ~/.config/agentbox/screen.toml"))
    scr = scr_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    from . import screen as _screen
    _screen.add_verbs(scr, add, ex)
    for verb in _screen.VERBS:
        scr.choices[verb].set_defaults(fn=cmd_screen)
    finish(scr_noun, demote(scr, ["layouts", "config"]), "verbs")

    prof_p_noun = sub.add_parser(
        "profile", help="the host-owned agent profile: what every session loads, and the inbox it feeds",
        formatter_class=raw,
        description="The host-owned profile (~/.config/agentbox/<agent>/profile/) holds the skills, "
                    "sub-agents, commands and instructions every session loads, read-only, in every "
                    "project and under every preset. Sessions cannot write it; what they want to keep goes "
                    "to the project's inbox, which nothing loads. These commands review that queue.",
        epilog=ex("ab profile harvest", "ab profile harvest --agent claude --all",
                  "ab profile promote web/skills-deploy --as skills/deploy",
                  "ab profile dismiss web/notes.md"))
    prof_p = prof_p_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    x = add(prof_p, "harvest", "list what sessions have put in their inboxes, unreviewed first (read-only)",
            ex("ab profile harvest", "ab profile harvest --agent claude --all"))
    x.add_argument("--agent", choices=adapters.NAMES, help="only this agent's profile (default: both)")
    x.add_argument("--all", action="store_true", help="include items already reviewed")
    x.set_defaults(fn=cmd_profile_harvest)
    x = add(prof_p, "promote", "copy an inbox item into the profile and commit it, with its provenance in "
                               "the commit message",
            ex("ab profile promote web/skills-deploy --as skills/deploy"))
    x.add_argument("item", help="the item, as <project>/<name> (or just <name> when unambiguous)")
    x.add_argument("--agent", default="claude", choices=adapters.NAMES, help="whose profile (default: claude)")
    x.add_argument("--as", dest="as_path", metavar="PATH",
                   help="where it goes inside the profile, e.g. skills/deploy (required unless the item is "
                        "plainly a skill directory)")
    x.set_defaults(fn=cmd_profile_promote)
    x = add(prof_p, "install", "install the plugins the profile declares, in a sandbox, into a tree "
                               "sessions mount read-only",
            ex("ab profile install omp", "ab profile install claude", "ab profile install omp --dry-run"))
    x.add_argument("agent", choices=adapters.NAMES, help="whose plugins.toml to install")
    x.add_argument("--dry-run", action="store_true",
                   help="print what would be installed and the script that would run; change nothing")
    x.set_defaults(fn=cmd_profile_install)
    x = add(prof_p, "dismiss", "mark an inbox item reviewed and leave it where it is (deletes nothing)",
            ex("ab profile dismiss web/notes.md"))
    x.add_argument("item", help="the item, as <project>/<name> (or just <name> when unambiguous)")
    x.add_argument("--agent", default="claude", choices=adapters.NAMES, help="whose profile (default: claude)")
    x.set_defaults(fn=cmd_profile_dismiss)

    img_p_noun = sub.add_parser(
        "image", help="your own image layer on top of an agent's harness image", formatter_class=raw,
        description="A personal layer for what you want in every session -- a language toolchain, a "
                    "linter, a private CLI -- without it going into agentbox's own images, and without a "
                    "session being able to put it there. Write a Containerfile in your profile "
                    "(~/.config/agentbox/<agent>/profile/Containerfile); it must begin `ARG BASE` then "
                    "`FROM ${BASE}`, and `ab image build` supplies BASE as the current harness image.\n\n"
                    "The built image is checked before it is tagged: USER, ENTRYPOINT, CMD and PATH must "
                    "match the harness image, and the image must still report the pinned agent version and "
                    "have git, rg, python3 and the podman client. A build that fails any of those is "
                    "discarded, so what you were using stays in place.\n\n"
                    "The tag covers the harness image ID and the Containerfile. When the harness image is "
                    "rebuilt your layer no longer matches it, and `ab session start` says `personal layer out of "
                    "date` and runs the plain harness image -- it never runs a layer built on an older "
                    "harness. Build it again and the next session picks it up.",
        epilog=ex("ab image build claude", "ab image build omp --force"))
    img_p = img_p_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    x = add(img_p, "build", "build your profile's Containerfile on top of the agent's harness image",
            ex("ab image build claude", "ab image build omp --force"))
    x.add_argument("agent", choices=adapters.NAMES, help="whose harness image to build on")
    x.add_argument("--force", action="store_true",
                   help="rebuild even when an image for this harness and Containerfile already exists")
    x.set_defaults(fn=cmd_image_build)

    ks_noun = sub.add_parser(
        "keys", help="model tokens and provider API keys", formatter_class=raw,
        description="manage model tokens and provider API keys. Keys live in ~/.config/agentbox/keys/ (0600), "
                    "outside every grant, and reach a session read-only as AB_KEY_<PROVIDER>.",
        epilog=ex("ab keys rotate claude", "ab keys set openai", "ab keys ls", "ab keys rm openai"))
    ks = ks_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    x = add(ks, "rotate", "replace the Claude subscription token (from `claude setup-token`)",
            ex("ab keys rotate claude"))
    x.add_argument("agent", choices=adapters.NAMES, help="the agent whose subscription token to replace (currently: claude)")
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

    # Everyday nouns first; the host-setup and repair nouns still parse but list under `advanced`.
    finish(p, demote(sub, ["profile", "image", "keys"]), "commands")
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
    # `ab session start ... -- <args>` passes everything after the first `--` to the agent/shell. Split it off
    # before argparse so the positionals and flags on the left parse normally (a REMAINDER would swallow --shell).
    passthrough = []
    if argv[:2] == ["session", "start"] and "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    try:
        s = config.load()
        built = parser(s)          # the parser reads the presets, so a broken preset file reports cleanly
    except AgentboxError as e:
        print(f"ab: {e}", file=sys.stderr)
        return 2
    try:
        layout.migrate(s, warn)    # a flat data dir moves into projects/ once (layout.py)
    except OSError as e:
        print(f"ab: could not move the data directory's project state into {s.projects_dir}: {e}",
              file=sys.stderr)
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
