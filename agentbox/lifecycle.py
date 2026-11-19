"""The end of a session, whoever ends it (the launcher, `ab session kill`, `ab session gc`): logs kept, the
end-of-session scan, removal; and gc's sweeps of what nothing can use any more. Moved out of session.py
(item 54); session re-exports it."""
import json
import os
import re
import shutil
import time

from . import adapters, buildcap, builtrec, profile, projectlock, scan, sockguard, toulmin, userimage
from .errors import AgentboxError
from .fsutil import write_private
from .liveness import STOPPING, _start_time, session_is_alive, sessions
from .podman import label_filters
from .progress import Step, stderr_line
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


def _scan_doc(settings, session, warn):
    """The scan's start state (scan.py), read once, or None (no scan for this session, or unreadable)."""
    path = os.path.join(settings.session_run_dir(session), SCAN_STATE)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        warn(f"agentbox: end-of-session scan skipped: its start state is unreadable ({e})")
        return None


def _scan_shared(settings, doc, session, warn):
    """The part of the scan that reads what the project's NEXT session writes: the inbox (provenance for the
    new items, the per-session harvest) and the agent state (memory, instructions, settings). It runs before
    the project lock is released, so a session started the moment the lock is free cannot have its items
    recorded as this one's or its memory edits reported as this one's. Both are small. Returns the agent
    state's (summary, report lines), or None. Never raises."""
    try:
        _inbox_report(settings, doc, session, warn)
    except Exception as e:
        warn(f"agentbox: could not report the inbox: {e!r}")
    st = doc.get("agent_state") or {}
    if not st.get("dir"):
        return None
    try:
        return scan.state_report(st.get("files") or {}, st["dir"])
    except Exception as e:
        warn(f"agentbox: end-of-session scan of the agent state failed: {e!r}")
        return None


def _scan_grants(settings, session, started, doc, state, warn, note=lambda: None):
    """The grant scan (the long part: it walks every writable grant) and the kept report. Returns the summary
    lines to show, or None. Never raises. `note()` may return a line the report leads with."""
    try:
        # base_branch comes from the project registry, recorded in the scan state at session start, so
        # `ab session kill` and `gc` name the same branch the launcher would have.
        summary, text = scan.report(doc["state"], doc["grants"], skip=doc.get("skip"),
                                    base_branch=doc.get("base_branch"))
        if state:
            s2, lines = state
            summary, text = summary + s2, text + "\n" + "\n".join(lines)
    except Exception as e:           # the scan informs; it must never stop the teardown (review step 5C, H3)
        warn(f"agentbox: end-of-session scan failed: {e!r}")
        return None
    trust = [f"WARNING (recorded at the start): {w}" for w in doc.get("trust") or []]
    if trust:
        # data_env/data_bind were on: the record of this session says so, first (decisions.md, 19 Nov 2026)
        summary, text = trust + list(summary), "\n".join(trust) + "\n" + text
    extra = note()
    if extra:
        summary, text = list(summary) + [extra], extra + "\n" + text
    out = log_path(settings, session, started, "scan")
    _keep(out, text.encode())
    if summary:
        return ["agentbox: end-of-session scan (review before you run or push anything):\n"
                + "\n".join(f"  - {line}" for line in summary) + f"\n  full report: {out}"]
    return []


def _scan(settings, session, started, warn):
    """The whole end-of-session scan in one call, its summary warned. Never raises."""
    doc = _scan_doc(settings, session, warn)
    if doc is None:
        return
    for line in _scan_grants(settings, session, started, doc, _scan_shared(settings, doc, session, warn), warn) or []:
        warn(line)


def _session_meta(settings, session):
    try:
        with open(os.path.join(settings.session_run_dir(session), "session.json")) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _session_started(settings, session):
    try:
        return float(_session_meta(settings, session).get("started") or time.time())
    except (TypeError, ValueError):
        return time.time()


class _Marker:
    """`stopping.json` in the session run dir for as long as a teardown runs: who runs it (pid and start
    time, so liveness can tell a teardown still running from one that died), when it began, the step it is
    on and since when, and whether the project is `released` yet (liveness.stopping_marker reads it). Written
    only while the run dir exists -- a husk with no run dir gets none -- and removed with the run dir."""

    def __init__(self, settings, session):
        self.path = os.path.join(settings.session_run_dir(session), STOPPING)
        now = time.time()
        self.doc = {"pid": os.getpid(), "pid_start": _start_time(os.getpid()), "began": now, "step": "",
                    "step_since": now, "released": False}

    def step(self, name, **extra):
        self.doc.update(step=name, step_since=time.time(), **extra)
        if not os.path.isdir(os.path.dirname(self.path)):
            return
        try:
            write_private(self.path, json.dumps(self.doc))
        except OSError:
            pass                     # the marker informs; it must never stop the teardown


def teardown(settings, podman, session, warn=None, exit_reason="error"):
    """Remove everything of a session: the launcher's own teardown, `ab session kill` and `ab session gc` all
    come here, so each keeps the logs and runs the end-of-session scan. `exit_reason` is what the Toulmin end
    message carries: agent_exit, signal, kill, gc or error (toulmin.EXITS).

    The order is the point (19 Nov 2026, after a launcher held its project for 22 hours past its scan):
      1. stop the service and gate (nothing new can be created), keep the logs, remove the containers, pods,
         volumes and networks -- no failure here skips the rest;
      2. the bookkeeping that touches what the project's next session uses: the inbox and agent-state part of
         the scan, the Toulmin MCP entry in the project's agent state;
      3. mark the session released and free the project lock: a new session of the project may start now;
      4. what only concerns this session: `tm session end`, the grant scan (the long part), its run dir.
    """
    warn = warn or stderr_line
    sel = {"agentbox.session": session, **settings.labels}
    started = _session_started(settings, session)
    mark = _Marker(settings, session)
    mark.step("stopping the podman service")
    try:
        sockguard.stop_service(settings.service_dir(session))
    finally:
        mark.step("keeping the session's logs")
        try:
            with Step("keeping the session's logs", warn):
                _keep_logs(settings, podman, session, started, warn)
        except Exception as e:
            warn(f"agentbox: could not keep the session's logs: {e!r}")
        shutil.rmtree(settings.service_dir(session), ignore_errors=True)
        mark.step("removing the session's containers and networks")
        with Step("removing the session's containers and networks", warn):
            _remove(settings, podman, session, sel, warn)
        if settings.cgroup_parent:
            # the per-session build cgroup the gate names; empty once the builds are gone
            build_cg = "/sys/fs/cgroup" + buildcap.slice_cgroup_path(settings.cgroup_parent) + f"/{session}-build"
            try:
                os.rmdir(build_cg)
            except OSError:
                pass
        shutil.rmtree(service_tmp(settings, session), ignore_errors=True)
        meta = _session_meta(settings, session)
        mark.step("reporting the inbox and the agent state")
        doc = _scan_doc(settings, session, warn)
        state = _scan_shared(settings, doc, session, warn) if doc is not None else None
        _mcp_remove(settings, meta, warn)
        # Whatever ended this session -- its own exit, an exception, a signal, `ab session kill`, `gc` -- the
        # project's slot is free again, as soon as nothing of the session runs. Silently: a lock that works is
        # not news. The marker says so first, so no reader ever sees the lock free and the session still live.
        mark.step("ending the Toulmin session", released=True)
        released_at = time.time()
        projectlock.release_for_session(settings, session)
        _toulmin_end(settings, meta, session, exit_reason, warn)
        if doc is not None:
            mark.step("running the end-of-session scan")
            with Step("running the end-of-session scan", warn):
                for line in _scan_grants(settings, session, started, doc, state, warn,
                                         note=lambda: _started_meanwhile(settings, meta, session, released_at)) or []:
                    warn(line)
        shutil.rmtree(ephemeral_state(settings, session), ignore_errors=True)   # a lockdown session's state
        # Last: while the run dir exists its session number is taken (Session.reserve_name), so a new session
        # of the project cannot be given this one's name while this teardown still runs.
        run_dir = settings.session_run_dir(session)
        if os.path.isdir(run_dir):
            for fn in os.listdir(run_dir):
                try:
                    os.unlink(os.path.join(run_dir, fn))
                except IsADirectoryError:
                    shutil.rmtree(os.path.join(run_dir, fn), ignore_errors=True)
            os.rmdir(run_dir)


def _remove(settings, podman, session, sel, warn):
    """Every container, pod, volume and network labelled with the session.

    A listing that cannot be read must not look like "nothing to remove": warn per kind, keep going with the
    others, and never let a failed listing pass for a clean teardown (audit F-2/F-10)."""
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


def _started_meanwhile(settings, meta, session, released_at):
    """A line for the scan report when another session of the project took the lock after this one freed
    it: the grant scan runs after that, so it may see that session's first changes as well."""
    project = meta.get("project")
    holder = projectlock.read(settings, project) if project else None
    if not holder or holder.get("unreadable") or holder.get("session") == session:
        return None
    try:
        taken = float(holder.get("taken") or 0)
    except (TypeError, ValueError):
        return None
    if taken < released_at:
        return None
    other = holder.get("session") or "a new session"
    return (f"note: {other} of {project} started at {time.strftime('%H:%M:%S', time.localtime(taken))}, while "
            f"this scan ran; a change made after that may be that session's")


def _mcp_remove(settings, meta, warn):
    """Take the MCP entry the session put in the per-project agent state out again. Before the project lock
    is released: the next session of the project adds its own entry under the same name, and removing it
    after that would take that session's away."""
    if not meta.get("toulmin"):
        return
    try:
        adapters.get(meta["agent"]).mcp_remove(settings, meta["project"], toulmin.MCP_NAME)
    except Exception as e:
        warn(f"agentbox: could not remove the {toulmin.MCP_NAME} MCP entry from {meta.get('project')}'s state: {e!r}")


def _toulmin_end(settings, meta, session, exit_reason, warn):
    """`tm session end` from every teardown path, with only session.json to go on. Never raises."""
    if not meta.get("toulmin"):
        return
    toulmin.end(meta, settings.session_run_dir(session), exit_reason, warn)


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
