"""The end of a session, whoever ends it (the launcher, `ab session kill`, `ab session gc`): logs kept, the
end-of-session scan, removal; and gc's sweeps of what nothing can use any more. Moved out of session.py
(item 54); session re-exports it."""
import json
import os
import re
import shutil
import sys
import time

from . import adapters, buildcap, builtrec, profile, projectlock, scan, sockguard, toulmin, userimage
from .errors import AgentboxError
from .fsutil import write_private
from .liveness import session_is_alive, sessions
from .podman import label_filters
from .progress import Step
from .state import _free_name, ephemeral_state, service_tmp


SCAN_STATE = "scan-state.json"


SIDECAR_LOGS = ("proxy", "sockguard", "sockguard-dev")      # container `<session>-<name>`, kept as <name>
# agentbox's own containers, removed without a stop timeout: their logs are kept before the removal and none
# holds state. `-t 5` sat out the full 5 s on the proxy and the holder (sleep as PID 1 ignores SIGTERM), 16 Nov.
# Every other container of the session -- the agent's, and whatever the agent ran in the dev tier (a
# database, say) -- keeps a graceful stop.
SIDECARS = SIDECAR_LOGS + ("holder",)


def log_path(settings, session, started, kind):
    """Where a sidecar's log is kept after the session. Session names are reused once a session ends, so
    the start time is part of the name: two sessions never share a log file."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
    return os.path.join(settings.data_dir, "logs", f"{session}-{stamp}-{kind}.log")


def _keep(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        write_private(path, data, atomic=False)
    except FileExistsError:
        return False                 # a second teardown of the same session: its log is already kept
    return True


def _keep_logs(settings, podman, session, started, warn):
    """The gate's log (API calls and verdicts), the proxy's access log (destinations and verdicts only) and
    sockguard's audit logs, kept for review."""
    gate_log = os.path.join(settings.service_dir(session), "gate.log")
    if os.path.isfile(gate_log):
        with open(gate_log, "rb") as src:
            _keep(log_path(settings, session, started, "gate"), src.read())
    for kind in SIDECAR_LOGS:
        try:
            logs = podman.run(["logs", f"{session}-{kind}"], check=False)
            if logs.returncode == 0 and logs.stdout:
                # stdout is squid's access log and sockguard's audit; stderr is squid's cache.log, which says why
                # an upstream connection failed (the egress flake, 2 Oct)
                text = (logs.stdout or "") + (f"\n--- stderr ---\n{logs.stderr}" if logs.stderr else "")
                _keep(log_path(settings, session, started, kind), text.encode())
        except (AgentboxError, OSError) as e:
            warn(f"agentbox: could not keep the {kind} log: {e}")


def _harvest_per_session_dirs(box, inbox, warn):
    """Move what a per-session directory collected into the inbox, before the session's run dir goes.

    OMP's autolearn writes learned skills to `agent/managed-skills`, which agentbox gives it empty every
    session: a skill the agent wrote itself is a proposal, so it goes to the review queue rather than
    straight into what later sessions load. Copied, then left to `_inbox_report` to give provenance to --
    so a harvested skill carries the same record as anything else in the inbox."""
    for src in box.get("harvest") or []:
        if not os.path.isdir(src):
            continue
        for name in sorted(os.listdir(src)):
            os.makedirs(inbox, mode=0o700, exist_ok=True)
            dst = _free_name(inbox, f"learned-{name}")
            try:
                shutil.move(os.path.join(src, name), dst)
            except OSError as e:
                warn(f"agentbox: could not move {name} out of {src} into the inbox: {e}")


def _inbox_report(settings, doc, session, warn, now=None):
    """Give every inbox item this session added its provenance sidecar, and say how many there are.

    Teardown is the only place the host knows which items are new -- nothing hooks the agent's writes -- so
    it is also where provenance is recorded, from the before/after difference."""
    box = doc.get("inbox") or {}
    if not box.get("project"):
        return
    inbox = box.get("dir") or settings.inbox_dir(box["project"], box["agent"])
    _harvest_per_session_dirs(box, inbox, warn)
    if box.get("ephemeral"):
        # Nothing is recorded or promised: this inbox is about to be deleted with the rest of the session's
        # state. Saying how many items are going is still worth it -- silently dropping the agent's work
        # would be the surprise.
        going = [n for n in profile.items(inbox) if n not in set(box.get("items") or ())]
        if going:
            warn(f"agentbox: {len(going)} item(s) were written to this lockdown session's inbox "
                 f"({', '.join(going[:10])}) and are being DELETED with the rest of its state, as this "
                 f"preset keeps nothing. Copy anything worth keeping out of {inbox} now if the session is "
                 f"still up.")
        return
    try:
        new = profile.record_new_items(settings, box["project"], box["agent"], box.get("items") or [],
                                       session=session, preset=box.get("preset", ""),
                                       egress=box.get("egress", ""), now=now)
    except OSError as e:
        warn(f"agentbox: could not record the inbox's provenance: {e}")
        return
    if new:
        warn(f"agentbox: {len(new)} new item(s) in {inbox}: {', '.join(new[:10])}"
             + (" ..." if len(new) > 10 else "")
             + ". Nothing there is loaded; `ab profile harvest` reviews it.")


def _scan(settings, session, started, warn):
    """The end-of-session scan, from the state recorded at start (scan.py). Never raises."""
    path = os.path.join(settings.session_run_dir(session), SCAN_STATE)
    try:
        with open(path) as f:
            doc = json.load(f)
    except FileNotFoundError:
        return
    except (OSError, ValueError) as e:
        warn(f"agentbox: end-of-session scan skipped: its start state is unreadable ({e})")
        return
    _inbox_report(settings, doc, session, warn)
    try:
        # base_branch comes from the project registry, recorded in the scan state at session start, so
        # `ab session kill` and `gc` name the same branch the launcher would have.
        summary, text = scan.report(doc["state"], doc["grants"], skip=doc.get("skip"),
                                    base_branch=doc.get("base_branch"))
        st = doc.get("agent_state") or {}
        if st.get("dir"):
            s2, lines = scan.state_report(st.get("files") or {}, st["dir"])
            summary, text = summary + s2, text + "\n" + "\n".join(lines)
    except Exception as e:           # the scan informs; it must never stop the teardown (review step 5C, H3)
        warn(f"agentbox: end-of-session scan failed: {e!r}")
        return
    out = log_path(settings, session, started, "scan")
    _keep(out, text.encode())
    if summary:
        warn("agentbox: end-of-session scan (review before you run or push anything):\n"
             + "\n".join(f"  - {line}" for line in summary) + f"\n  full report: {out}")


def _session_started(settings, session):
    try:
        with open(os.path.join(settings.session_run_dir(session), "session.json")) as f:
            return float(json.load(f).get("started") or time.time())
    except (OSError, ValueError, TypeError, AttributeError):
        return time.time()


def teardown(settings, podman, session, warn=None, exit_reason="error"):
    """Remove everything of a session: the launcher's own teardown, `ab session kill` and `ab session gc` all come here, so
    each keeps the logs and runs the end-of-session scan. The service and gate stop first (nothing new can be
    created), the scan runs last, and no failure of either skips the removal. `exit_reason` is what the
    Toulmin end message carries: agent_exit, signal, kill, gc or error (toulmin.EXITS)."""
    warn = warn or (lambda m: print(m, file=sys.stderr))
    sel = {"agentbox.session": session, **settings.labels}
    started = _session_started(settings, session)
    try:
        sockguard.stop_service(settings.service_dir(session))
    finally:
        try:
            with Step("keeping the session's logs", warn):
                _keep_logs(settings, podman, session, started, warn)
        except Exception as e:
            warn(f"agentbox: could not keep the session's logs: {e!r}")
        shutil.rmtree(settings.service_dir(session), ignore_errors=True)
        # A listing that cannot be read must not look like "nothing to remove": warn per kind, keep going
        # with the others, and never let a failed listing pass for a clean teardown (audit F-2/F-10).
        with Step("removing the session's containers and networks", warn):
            quick = {f"{session}-{k}" for k in SIDECARS}
            for kind, argv in (("container", ["rm", "-f", "-v", "-t", "5"]),   # -v: its anonymous volumes too
                               ("pod", ["pod", "rm", "-f"]),
                               ("volume", ["volume", "rm", "-f"]),
                               ("network", ["network", "rm", "-f"])):
                try:
                    found = podman.ids(kind, sel)
                except AgentboxError as e:
                    warn(f"agentbox: could not list this session's {kind}s, so some may be left behind: {e}. "
                         f"Check with `podman {kind if kind != 'container' else 'ps -a'} --filter "
                         f"label=agentbox.session={session}`.")
                    continue
                for target in found:
                    # a removal that fails leaves a container or network behind under a name the next session
                    # may reuse; say so with the command, instead of a teardown that looks clean (item 17)
                    if kind == "container" and target in quick:
                        argv = ["rm", "-f", "-v", "-t", "0"]
                    elif kind == "container":
                        argv = ["rm", "-f", "-v", "-t", "5"]
                    try:
                        r = podman.run(argv + [target], check=False)
                        why = "" if r.returncode == 0 else f"exit {r.returncode}: {(r.stderr or '').strip()[-200:]}"
                    except AgentboxError as e:
                        why = str(e)
                    if why:
                        warn(f"agentbox: could not remove {kind} {target} of session {session} ({why}); remove it "
                             f"with `podman {' '.join(argv)} {target}`")
        if settings.cgroup_parent:
            # the per-session build cgroup the gate names; empty once the builds are gone
            build_cg = "/sys/fs/cgroup" + buildcap.slice_cgroup_path(settings.cgroup_parent) + f"/{session}-build"
            try:
                os.rmdir(build_cg)
            except OSError:
                pass
        shutil.rmtree(service_tmp(settings, session), ignore_errors=True)
        with Step("running the end-of-session scan", warn):
            _scan(settings, session, started, warn)
        _toulmin_end(settings, session, exit_reason, warn)
        # Whatever ended this session -- its own exit, an exception, a signal, `ab session kill`, `gc` --
        # the project's slot is free again. Silently: a lock that works is not news.
        projectlock.release_for_session(settings, session)
        shutil.rmtree(ephemeral_state(settings, session), ignore_errors=True)   # a lockdown session's state
        run_dir = settings.session_run_dir(session)
        if os.path.isdir(run_dir):
            for fn in os.listdir(run_dir):
                try:
                    os.unlink(os.path.join(run_dir, fn))
                except IsADirectoryError:
                    shutil.rmtree(os.path.join(run_dir, fn), ignore_errors=True)
            os.rmdir(run_dir)


def _toulmin_end(settings, session, exit_reason, warn):
    """`tm session end` from every teardown path, with only session.json to go on; then the MCP entry the
    session put in the per-project state is taken out again. Never raises."""
    try:
        with open(os.path.join(settings.session_run_dir(session), "session.json")) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return
    if not (meta or {}).get("toulmin"):
        return
    toulmin.end(meta, settings.session_run_dir(session), exit_reason, warn)
    try:
        adapters.get(meta["agent"]).mcp_remove(settings, meta["project"], toulmin.MCP_NAME)
    except Exception as e:
        warn(f"agentbox: could not remove the {toulmin.MCP_NAME} MCP entry from {meta.get('project')}'s state: {e!r}")


def gc_images(settings, podman, now=None, warn=lambda m: None):
    """Session-built images (labelled agentbox.role=dev, with their project and session) older than
    image_keep_days are removed, except the newest image carrying each tag. Never forced: an image a
    container still uses stays. ab session kill leaves images alone (decision, 1 Oct)."""
    now = time.time() if now is None else now
    filters = label_filters({**settings.labels, "agentbox.role": "dev"})
    p = podman.run(["images", "--format", "json"] + filters, check=False)
    try:
        images = json.loads(p.stdout or "[]") or []
    except ValueError:
        return []
    newest = {}
    for img in images:
        for tag in img.get("Names") or []:
            if tag not in newest or img.get("Created", 0) > newest[tag].get("Created", 0):
                newest[tag] = img
    keep = {img["Id"] for img in newest.values()}
    cutoff = now - float(settings.image_keep_days) * 86400
    removed = []
    for img in images:
        labels = img.get("Labels") or {}
        if img["Id"] in keep or img.get("Created", now) >= cutoff or not labels.get("agentbox.project"):
            continue
        r = podman.run(["rmi", img["Id"]], check=False)
        if r.returncode == 0:
            removed.append(f"image {img['Id'][:12]} ({labels.get('agentbox.project')})")
            builtrec.remove_everywhere(settings.data_dir, [img["Id"]])   # the gate's record of its builds
        else:
            warn(f"agentbox: kept image {img['Id'][:12]}: {(r.stderr or '').strip()[-120:]}")
    return removed


def _current_user_layer(settings, podman, agent):
    """The personal image layer a session would run for this agent right now, or None when that cannot be
    answered -- no Containerfile in the profile, or no readable harness image to key on.

    None means "sweep nothing", deliberately. Without a current key every layer looks superseded, so the
    permissive reading of an unknown state would delete all of them: the same shape as the liveness
    fail-open the third audit closed. A deleted Containerfile and an unreadable profile directory are not
    distinguishable from here, and `podman rmi` is always available by hand."""
    recipe = profile.containerfile(settings.profile_src(agent))
    if recipe is None:
        return None
    try:
        harness_id, _pin = userimage.harness_pin(podman, adapters.get(agent).image)
    except AgentboxError:
        return None
    return profile.user_image_ref(agent, harness_id, recipe) if harness_id else None


def gc_user_layers(settings, podman, now=None, warn=lambda m: None):
    """Personal image layers (`ab image build`, labelled agentbox.user-layer=<agent>) that are superseded
    and past the keep window.

    `gc_images` cannot see these: it filters on agentbox.role=dev plus the settings labels, and a layer
    carries neither. They are produced routinely -- every harness rebuild and every Containerfile edit is a
    new key, and the old tag stays in storage.

    Two rules, and the reason for each:
      * the CURRENT key is never removed, however old it is. Age is not staleness here: a layer built in
        September that still matches today's harness image and Containerfile is the one a session runs.
      * a superseded layer stays for image_keep_days, because the documented rollback is `podman tag` onto
        the previous layer (decisions.md, 4 Nov). A sweep that ran the moment a rebuild superseded it would
        take away the thing being rolled back to.

    Never forced, like gc_images: a layer a container still uses, or one carrying a rollback tag of its
    own, makes `rmi` fail and is reported and kept."""
    now = time.time() if now is None else now
    cutoff = now - float(settings.image_keep_days) * 86400
    removed = []
    for agent in adapters.NAMES:
        current = _current_user_layer(settings, podman, agent)
        if current is None:
            continue
        for img in podman.images_json({userimage.USER_LAYER_LABEL: agent}):
            names = img.get("Names") or []
            if current in names or img.get("Created", now) >= cutoff:
                continue
            shown = names[0] if names else (img.get("Id") or "")[:19]
            r = podman.run(["rmi", img["Id"]], check=False)
            if r.returncode == 0:
                removed.append(f"layer {shown}")
            else:
                warn(f"agentbox: kept {shown}: {(r.stderr or '').strip()[-120:]}")
    return removed


SNAPSHOT_DIR = re.compile(r"^[0-9a-f]{16}$")      # profile.user_image_key's shape; nothing else is ours


def gc_profile_snapshots(settings, podman, now=None, warn=lambda m: None):
    """Profile snapshots (<data>/profiles/<agent>/<hash>) that are neither the agent's current profile
    content nor used within the keep window.

    Small (text) but unbounded: a directory per distinct profile content, so one per skill edit.

    **Nothing is swept while any session is alive.** A snapshot is a live session's read-only mount source,
    and session.json does not record which hash a session took -- the metadata is written before the
    profile is loaded (`_write_meta` runs first, on purpose, so a session starting right now is visible).
    So "which snapshots are in use" cannot be answered, and the lesson of three liveness audits is to
    refuse an unanswerable question rather than guess at it. `sessions()` raising on an unreadable podman
    keeps that fail-closed: gc removes nothing rather than deciding the host is idle.

    Only directories shaped like a hash are ever removed, so a `.<hash>.<pid>.tmp` another process is
    half-way through writing, and anything a person put there, are not this sweep's to touch."""
    now = time.time() if now is None else now
    # Silently: gc runs by itself on the first `ab session start`/`ab ls` after boot, and "a session is running so
    # I did not tidy up" is not news. (It was a warning for one test run, which `ab ls` then printed at
    # anyone with a live session -- tests/unit/test_cli.py's test_ls_does_not_flag_a_busy_session.)
    for name, info in sessions(settings, podman).items():
        if session_is_alive(settings, name, info)[0]:
            return []
    cutoff = now - float(settings.image_keep_days) * 86400
    removed = []
    for agent in adapters.NAMES:
        base = os.path.join(settings.profiles_dir, agent)
        src = settings.profile_src(agent)
        current = profile.tree_hash(src) if os.path.isdir(src) else ""
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            d = os.path.join(base, n)
            if n == current or not SNAPSHOT_DIR.match(n) or not os.path.isdir(d) or os.path.islink(d):
                continue
            try:
                if os.stat(d).st_mtime >= cutoff:
                    continue
                shutil.rmtree(d)
            except OSError as e:
                warn(f"agentbox: kept the profile snapshot {agent}/{n}: {e}")
                continue
            removed.append(f"snapshot {agent}/{n}")
    return removed


def gc(settings, podman, warn=lambda m: None):
    """Tear down sessions that are no longer doing work: the launcher is gone and no agent container is
    running, so what is left is a husk -- an orphaned proxy, sockguard, gate or network. `session_is_alive`
    is the predicate, and its docstring says why this is NOT the question `project_is_live` answers.

    A session whose agent is still running is left alone even if its launcher died (`ab session kill` ends
    that one explicitly, and the warning says so). Nothing is reaped on a guess: an unreadable podman makes
    `sessions()` raise, so gc removes nothing rather than deciding a session is dead.

    Then the storage nothing can use any more, each with its own rules: session-built images
    (`gc_images`), superseded personal image layers (`gc_user_layers`) and unused profile snapshots
    (`gc_profile_snapshots`)."""
    removed, kept = [], set()
    for s, info in sessions(settings, podman).items():
        alive, why = session_is_alive(settings, s, info)
        if alive:
            if why == "agent":
                warn(f"agentbox: {s} has no live launcher but its agent is running; left alone "
                     f"(use `ab session kill {s}`)")
            kept.add(s)
            continue
        teardown(settings, podman, s, warn=warn, exit_reason="gc")
        removed.append(s)
    # A podman service whose session is dead, even if its run dir and containers are already gone.
    live = {s for s, info in sessions(settings, podman).items()
            if session_is_alive(settings, s, info)[0]} | kept
    for pid, s in sockguard.services_under(settings.run_dir).items():
        if s not in live:
            sockguard.kill_service(pid)
            warn(f"agentbox: stopped an orphaned podman service of {s} (pid {pid})")
            removed.append(f"{s} (podman service)")
    removed += gc_images(settings, podman, warn=warn)
    removed += gc_user_layers(settings, podman, warn=warn)
    removed += gc_profile_snapshots(settings, podman, warn=warn)
    return removed
