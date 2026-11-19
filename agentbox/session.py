"""Sessions: build, start and tear down everything one agent session needs."""
import errno
import json
import os
import re
import signal
import subprocess
import time

from . import adapters, backup, cgroup, egress, grantcheck, hardening, mounts, profile, tokens
from . import keys as keysmod
from . import imageopts, layout, scan, sockguard, toulmin, userimage
from .config import HOME
from .errors import AgentboxError
from .fsutil import inside, write_private
from .podman import Podman, set_log
from .progress import Step, Steps, stderr_line as _stderr_line
from .project import (  # noqa: F401
    check_trust, clone_root, dev_tier, load_project, mount_mode_of, podman_access, Project, protected_extra, _check,
    _git_env, _no_commas, TRUST_KEYS, TRUST_WARNING, trust_warnings
)
from .historysecrets import (  # noqa: F401
    history_secrets, history_secrets_changes, history_secrets_message, _history_masked, without_real_env
)
from .notes import dev_notes, profile_notes, sandbox_notes  # noqa: F401
from .infra import (net_create_argv, proxy_run_argv, SIDECAR_LOG_DRIVER, SIDECAR_LOG_SIZE,  # noqa: F401
                    SQUID_READY, holder_run_argv, sockguard_run_argv)
from .state import ephemeral_state, migrate_state_to_inbox, reset_state, service_tmp, _free_name  # noqa: F401
from .liveness import (  # noqa: F401
    idle_seconds, INFRA_ROLES, project_is_live, session_is_alive, session_metas, sessions, _alive, _newest_mtime,
    _start_time, _young
)
from .lifecycle import (  # noqa: F401
    gc, gc_images, gc_profile_snapshots, gc_user_layers, log_path, SCAN_STATE, SIDECAR_LOGS, SNAPSHOT_DIR, teardown,
    _current_user_layer, _harvest_per_session_dirs, _inbox_report, _keep, _keep_logs, _scan, _session_started,
    _toulmin_end
)
from .sessionlock import (  # noqa: F401
    live_session_of, lock_holder_state, ProjectLock, sessions_in_use, take_project_lock, _take_project_lock
)

TOKEN_PATH = "/run/agentbox/token"
PROBE_PATH = "/run/agentbox/probe.sh"
LIVE_SOCKET = "/run/agentbox/podman-live.sock"
PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def git_user(settings):
    """(name, email) of the user: the `git_name`/`git_email` settings, else the host's global git config.
    Each is "" when unknown or when it holds a newline. The one lookup, for sessions and `ab project init`."""
    out = []
    for key, setting in (("name", "git_name"), ("email", "git_email")):
        val = getattr(settings, setting, "") or ""
        if not val:
            try:
                val = subprocess.run(["git", "config", "--global", f"user.{key}"], capture_output=True,
                                     text=True, timeout=10).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                val = ""
        out.append(val if "\n" not in val else "")
    return tuple(out)


def git_identity(settings, agent):
    """Author and committer for commits made in the sandbox, as environment variables only; no git config
    file enters the sandbox. The committer is "<name> via agentbox-<agent>" so agent commits stand out."""
    out = {}
    for key, val in zip(("NAME", "EMAIL"), git_user(settings)):
        if val:
            out[f"GIT_AUTHOR_{key}"] = out[f"GIT_COMMITTER_{key}"] = val
    if "GIT_COMMITTER_NAME" in out:
        out["GIT_COMMITTER_NAME"] = f"{out['GIT_COMMITTER_NAME']} via agentbox-{agent}"
    return out


def labels_for(settings, project, session, role):
    out = dict(settings.labels)
    out.update({"agentbox.session": session, "agentbox.project": project,
                "agentbox.owner": f"{project}/{session}", "agentbox.role": role})
    return out


class Session:
    """One session: the agent's container, the two networks it lives between, and the sidecars.

    <session>-int  internal, isolate=strict: the agent and the proxy; no route anywhere else
    <session>-egr  isolate=strict bridge: the proxy only; its route out
    <session>-proxy squid, on both, with this session's egress mode

    There is ONE kind of session. There used to be two in the code -- this base, with no network at all,
    and a `NetSession` on top -- but `make_session` only ever returned the networked one, so the base's
    `--network=none`, `egress = "none"`, `start_infra` no-op, `pin_args` stub and "only loopback" selftest
    case were a path that had never run (D-E9, removed 5 Nov). A session with no network at all is a thing
    to build deliberately, with tests, not to keep eight untested sites warm for: `analysis-offline` runs
    `locked` egress, through the proxy, and is not a user of it. decisions.md says why that was a decision
    rather than a cleanup.
    """

    def __init__(self, settings, agent, project, *, podman=None, warn=None, no_profile=False):
        self.settings = settings
        self.adapter = adapters.get(agent)
        self.project = project
        self.podman = podman or Podman(settings.podman)
        self.warn = warn or _stderr_line
        self.no_profile = bool(no_profile)      # `ab session start --no-profile`, for this run only
        self.name = None
        self.plan = None
        self.networks = []
        self.env = {}
        self.setup = adapters.SessionSetup()
        self.podman_live = None          # compose project the agent may observe through sockguard
        self.podman_dev = False          # the dev tier: a stack of the agent's own, through the gate
        self.started = time.time()       # reset when the session is reserved; names its kept logs
        self.egress = project.value("egress")
        self.toulmin = None              # the start message (toulmin.start), once `tm session start` answered
        self.clone = None                # clone mode: {"clone", "branch", "base"} (clone_root)
        self.root_src = project.root     # the host directory bound at the project root: the clone, or the root
        self.plan_host = None            # the mount plan in host paths (the clone's); self.plan is the agent's view
        self.session_mounts = []         # (host src, container dst, ro): the integration's, audited like the adapter's
        self._serve_proc = None
        self._service_proc = None        # the podman service and the gate, once _start_podman_access ran
        self._gate_proc = None
        self.int_subnets = []            # the internal network's subnets, once dev_proxy_env asked podman
        self._meta_written = False       # session.json written once: `started` is fixed then
        set_log(os.path.join(settings.data_dir, "logs", "ab.log"))

    # ---- naming -----------------------------------------------------------------
    def reserve_name(self):
        base = f"{self.settings.name_prefix}{self.adapter.name}-{self.project.name}-"
        os.makedirs(os.path.join(self.settings.run_dir, "sessions"), mode=0o700, exist_ok=True)
        taken = set(self.podman.ids("container", {"agentbox.project": self.project.name, **self.settings.labels}))
        for n in range(1, 1000):
            cand = f"{base}{n}"
            if any(t == cand or t.startswith(cand + "-") for t in taken):
                continue
            # a clone-mode session's clone outlives it (the owner merges from it), and clone_root refuses an
            # existing one; skipping the number lets the next session start without `ab project clean`
            if os.path.lexists(os.path.join(self.settings.clones_dir(self.project.name), cand)):
                continue
            try:
                os.mkdir(self.settings.session_run_dir(cand), 0o700)
            except FileExistsError:
                continue
            self.name = cand
            return cand
        raise AgentboxError("no free session number")

    @property
    def profile_off(self):
        """(off?, why): whether this session loads the host-owned profile, and which switch decided.

        Two switches, because they answer different questions: `--no-profile` is "not this run" (debugging a
        skill, reproducing something without the user's instructions) and `profile = "off"` on the project is
        "not this project, ever". The flag is reported when both say off -- it is the one just typed."""
        if self.no_profile:
            return True, "flag"
        if self.project.profile == "off":
            return True, "project"
        return False, ""

    _image = None                      # set by _prepare_reserved, which also reports a stale layer

    @property
    def image(self):
        """The container image to run: the harness image, or the user's own layer when one is current.

        Resolved once during `prepare` (it asks podman), so reading this is free and every reader -- the
        hardening audit, the argv, the CLI-version check -- sees the same answer."""
        return self._image or self.adapter.image

    def _resolve_image(self):
        """(image to run, note to report or "").

        A user image layer is built on top of the harness image and is profile content like a skill is, so
        `--no-profile` selects the harness image: "no profile" that still ran the user's own build of the
        agent would be a lie.

        With the profile on, the layer is used only when one is built on EXACTLY this harness image from
        exactly this Containerfile (`profile.user_image_ref` keys the tag on both). When the harness image
        has moved on, nothing is tagged under the new key, and the session runs the plain harness image and
        says so -- it never runs a layer built on an older harness, which is the one thing a layer cannot be
        trusted to still fit."""
        off, _why = self.profile_off
        if off:
            return self.adapter.image, ""
        src = self.profile.dir or self.settings.profile_src(self.adapter.name)
        recipe = profile.containerfile(src)
        if recipe is None:
            return self.adapter.image, ""           # no recipe: no layer is expected, nothing to report
        harness_id, _pin = userimage.harness_pin(self.podman, self.adapter.image)
        ref = profile.user_image_ref(self.adapter.name, harness_id, recipe)
        if self.podman.image_exists(ref):
            return ref, ""
        # "Out of date" is only true of a layer that EXISTS and no longer matches. A Containerfile that has
        # never been built is a different thing to say, and saying the wrong one sends the reader looking
        # for an image that was never there (seen in `ab project selftest` output, 4 Nov).
        if userimage.any_built(self.podman, self.adapter.name):
            return (self.adapter.image,
                    f"personal layer out of date: `ab image build {self.adapter.name}`. "
                    f"This session runs {self.adapter.image} instead -- a layer built on an older harness "
                    f"image is never run.")
        # The SOURCE path, not the snapshot's: the snapshot is an internal copy, and the file the reader
        # has to go and look at (or delete) is the one in the profile.
        own = os.path.join(self.settings.profile_src(self.adapter.name), profile.CONTAINERFILE)
        return (self.adapter.image,
                f"{own} describes a personal image layer, but none has been built: "
                f"`ab image build {self.adapter.name}`. This session runs {self.adapter.image}.")

    @property
    def state_dir(self):
        """The project's agent state (memory, settings, transcripts), kept across sessions. Lockdown sessions
        start from empty state every time: theirs is per session and removed at teardown (Tim, 2 Oct)."""
        if self.project.preset == "lockdown" and self.name:
            return os.path.join(ephemeral_state(self.settings, self.name), self.adapter.name)
        return self.settings.state_dir(self.project.name, self.adapter.name)

    @property
    def mount_mode(self):
        """`checkout` binds the owner's working tree; `clone` binds a per-session clone at the same path. The
        stored `mount` wins; otherwise clone when Toulmin is on and the root is a git repository (the
        contract's default), checkout for everything else (non-git roots, projects without the integration)."""
        p = self.project
        if p.value("code") == "ro":
            return "checkout"          # a clone isolates writes, and a read-only root makes none (D5, 18 Nov)
        return mount_mode_of(p.root, p.mount, p.toulmin)

    @property
    def trust_warnings(self):
        """The WARNING lines for the trusted opt-outs that are on (project.trust_warnings): printed at the
        start, kept in session.json and the scan record, repeated by the end-of-session scan."""
        p = self.project
        return trust_warnings(p.name, p.root, p.data_env, p.data_bind)

    @property
    def read_only_root(self):
        return self.project.value("code") == "ro"

    def ro_roots(self):
        """The read-only grants the gate keeps read-only in the agent's containers: data_ro, and the root when
        code = ro (inert while the dev tier is refused with a read-only root; D2)."""
        return ([self.project.root] if self.read_only_root else []) + list(self.project.data_ro)

    def writable_grants(self):
        """The grants the agent can change: what the pre-session backup snapshots and the end-of-session scan
        compares. A read-only root is neither (nothing in it is the session's doing)."""
        return ([] if self.read_only_root else [self.root_src]) + list(self.project.data_rw)

    @property
    def token_file(self):
        return self.settings.token_file(self.adapter.name) if self.adapter.token_env else None

    # ---- host-side preparation ------------------------------------------------------
    steps = None                         # set by prepare (progress.Steps): the numbered plan of this start

    def start_plan(self, do_backup):
        """The steps this start will run, in order, decided before the first one: `[3/7]` in the progress lines
        is a step's place here. The conditions are the ones `_prepare_reserved` and `_backup_grants` use (the
        mount mode, toulmin, do_backup, the writable grants); a test checks the plan against what ran."""
        clone = self.mount_mode == "clone"
        plan = ["history"]
        if self.project.toulmin:
            plan.append("toulmin")
        if clone:
            plan.append("clone")
        plan.append("grant")
        if clone:
            plan.append("clone-masks")
        if do_backup and not clone:
            plan += [f"backup {g}" for g in self.writable_grants()] + ["retain"]
        return plan + ["scan-state", "infra"]

    def _step(self, key, label):
        """A progress Step numbered from this start's plan; unnumbered when there is no plan."""
        if self.steps is None:
            return Step(label, self.warn)
        return self.steps.step(key, label, self.warn)

    def prepare(self, *, do_backup=True):
        cgroup.check(self.settings, self.warn)
        if self.token_file:
            tokens.check_token_file(self.token_file)
        p = self.project
        self.steps = Steps(self.start_plan(do_backup))
        with self._step("history", f"reading the git history under {p.root} for secret file names"):
            found = history_secrets(p.root, [p.root] + p.data_rw + p.data_ro, p.mask_skip)
        accepted = p.history_secrets_accepted
        if p.data_env == "on":            # the root's env files are shown anyway: their names expose nothing more
            found = without_real_env(found, p.root, p.data_rw + p.data_ro)
            accepted = without_real_env(accepted, p.root, p.data_rw + p.data_ro)
        # The whole found set, not just the additions (audit F-11): a name that left the history means the
        # acknowledgement no longer describes it, and `ab project show` was still listing it to rotate.
        new = found if grantcheck.fingerprint(found) != grantcheck.fingerprint(accepted) else []
        if new:
            raise AgentboxError(history_secrets_message(p.name, new, accepted))
        if found:
            self.warn(f"agentbox: {len(found)} accepted secret file name(s) in {p.name}'s git history "
                      "are readable by the agent (accepted as rotated)")
        # The state this session is about to mount must be this project's (layout.py): a registry edited by
        # hand to another root would otherwise hand it another project's memory and built-image record.
        layout.check_session(self.settings, p.name, p.root)
        self.reserve_name()
        try:
            self._prepare_reserved(do_backup)
        except BaseException:
            # nothing is running yet; remove the run dir it reserved -- with the signals held off, as
            # _guarded does, so a second Ctrl-C cannot cut the teardown short and strand a sidecar (item 36)
            self._teardown_shielded("error")
            raise

    profile = None                       # set by _prepare_reserved (profile.Loaded)

    @property
    def mask_file(self):
        return os.path.join(self.settings.session_run_dir(self.name), "mask-empty") if self.name else None

    def _prepare_reserved(self, do_backup):
        p = self.project
        self._write_meta()
        write_private(self.mask_file, "", mode=0o444, atomic=False)
        mode = self.mount_mode
        if self.read_only_root and mount_mode_of(p.root, p.mount, p.toulmin) == "clone":
            self.warn(f"agentbox: {p.name}: the project root is read-only (code = ro), so clone mode is not "
                      "used: there is nothing for a clone to isolate. The checkout is mounted read-only")
        if mode == "clone" and dev_tier(p):
            # The gate resolves the agent's bind sources on the host, where the owner's path IS the owner's
            # checkout: it would have to translate every path into the clone. Not built (decisions.md, 10 Nov).
            raise AgentboxError(f"project {p.name}: mount = clone with the dev podman tier is not built; use "
                                "`--podman none` (or observe) with clone mode, or mount = checkout")
        if p.toulmin:
            with self._step("toulmin", "starting the toulmin session"):
                self.toulmin = toulmin.start(self.settings)  # before anything else of the session's (contract 1.5)
            # recorded at once: a clone_root that fails next must still reach `tm session end` in teardown,
            # which knows only session.json (review 16 Nov, item 18)
            self._write_meta()
        if mode == "clone":
            with self._step("clone", f"making this session's clone of {p.root}"):
                self.clone = clone_root(self.settings, p.name, self.name, p.root)
            self.root_src = self.clone["clone"]
            self.warn(f"agentbox: clone mode: {self.clone['clone']} on branch {self.clone['branch']} is what the "
                      f"session sees at {p.root}; the checkout is not mounted")
        self._write_meta()
        # The grant scan and its acknowledgement are about the OWNER's tree (what `ab project check` and `add`
        # saw), so it runs there in both modes; the clone can only hold less (nothing untracked or ignored).
        with self._step("grant", f"checking {p.root} for secrets and live data"):
            plan_check = mounts.plan(root=p.root, data_ro=p.data_ro, data_rw=p.data_rw,
                                     compose=p.value("compose"), mask_skip=p.mask_skip, swap=p.swap,
                                     mask=p.mask, code=p.value("code"), data_env=p.data_env == "on")
            exposure, warnings, exposure_fp = grantcheck.preflight(
                self.settings, p.name, {"root": p.root, "data_ro": p.data_ro, "data_rw": p.data_rw,
                                        "mask_skip": p.mask_skip, "swap": p.swap, "mask": p.mask,
                                        "data_env": p.data_env, "data_bind": p.data_bind},
                plan=plan_check, code=p.value("code"))
        for w in warnings:
            self.warn(f"agentbox: grant warning: {w}")
        for w in self.trust_warnings:
            self.warn(f"agentbox: WARNING: {w}")
        if exposure and not grantcheck.matches(p.grant_exposure_accepted, exposure):
            raise AgentboxError(grantcheck.message(p.name, exposure))
        if mode == "clone":
            # The mounts come from the clone (host paths), and the agent sees them at the owner's path.
            with self._step("clone-masks", f"finding the masks in the clone {self.root_src}"):
                self.plan_host = mounts.plan(root=self.root_src, data_ro=p.data_ro, data_rw=p.data_rw,
                                             compose=p.value("compose"), mask_skip=p.mask_skip, swap=p.swap,
                                             mask=p.mask, code=p.value("code"), data_env=p.data_env == "on")
            self.plan = mounts.remap(self.plan_host, self.root_src, p.root)
        else:
            self.plan_host = self.plan = plan_check
        for m in self.plan.mounts:
            _no_commas(m.dst, "mount")
        for n in self.plan_host.notes:
            self.warn(f"agentbox: {n}")
        mounts.prepare(self.plan_host)
        self.podman_live = podman_access(p)
        self.podman_dev = bool(dev_tier(p))
        if self.podman_live and not p.compose_project_explicit:
            self._check_live_stack_is_ours()
        off, why = self.profile_off
        self.profile = profile.load_for_session(self.settings, self.adapter, out=self.warn,
                                                off_reason=why)
        moved = [] if off else migrate_state_to_inbox(self.settings, p.name, self.adapter, self.warn)
        # The harness image's label (the user layer is not resolved yet, and inherits it anyway).
        notes = sandbox_notes(p, self.egress, self.podman_live, self.podman_dev,
                              profile=self.profile, inbox=self.inbox_mount,
                              inbox_kept=not self.inbox_is_ephemeral, adapter=self.adapter, clone=self.clone,
                              playwright=imageopts.playwright_version(self.image_labels()))
        self.adapter.seed_state(self.settings, self.state_dir, notes, profile_on=not off)
        self.setup = self.adapter.prepare_session(self, notes)
        if self.toulmin:
            self._toulmin_setup()
        if self.podman_dev:
            self._deploy_check_setup()
        self._check_setup()
        self._add_profile_mounts(notes)
        if self.toulmin:
            self._toulmin_lists()
        self._image, image_note = self._resolve_image()
        _keep(self.log_path("profile"),
              self.profile.log_line().encode() + f"image {self.adapter.name}={self._image}\n".encode()
              + plugin_log_line(self.settings, self.adapter.name).encode())
        if image_note:
            self.warn(f"agentbox: {image_note}")
        if off:
            self.warn(f"agentbox: profile {self.adapter.name}=off (reason={why}): no profile skills, "
                      "instructions or plugins are loaded, and the harness image is used. agentbox's own "
                      "session notes still apply.")
        else:
            self.warn(f"agentbox: profile {self.adapter.name} {self.profile.display}"
                      + (f", loaded: {', '.join(self.profile.names)}" if self.profile.names else
                         " -- nothing to load; put skills in "
                         f"{self.settings.profile_src(self.adapter.name)}")
                      + (f". Moved to the inbox: {', '.join(moved)}" if moved else ""))
        if do_backup and self.mount_mode == "clone":
            self.warn("agentbox: clone mode: no pre-session snapshot (the owner's checkout is not mounted, and "
                      "the clone is kept)")
        elif do_backup:
            self._backup_grants()
        with self._step("scan-state", f"recording the state of {self.root_src} for the end-of-session scan"):
            self._write_scan_state()         # after the agent state is seeded: its changes are the session's

    # ---- the Toulmin integration (toulmin.py; docs/agentbox_toulmin_integration.md) ----------------------
    def _toulmin_setup(self):
        """The socket directory and the bridge as session mounts, the token in a 0600 file mounted beside the
        socket, and the MCP entry for the harness."""
        run_dir = self.settings.session_run_dir(self.name)
        token_path = toulmin.write_token(run_dir, self.toulmin["token"])
        self.session_mounts = toulmin.session_mounts(self.toulmin, token_path)
        self.adapter.mcp_server(self, self.setup, toulmin.MCP_NAME, toulmin.mcp_server(self.toulmin))
        for src, dst, _ro in self.session_mounts:
            _no_commas(src, "session mount")
            _no_commas(dst, "session mount")

    def _deploy_check_setup(self):
        """`deploy-check` for a dev-tier session (deploycheck.py): a COPY of the checker in the session's run dir
        (never the agentbox source, which may sit in a grant) and the project's deploy values, both read-only."""
        from . import deploycheck
        p = self.project
        d = os.path.join(self.settings.session_run_dir(self.name), "deploy-check")
        os.makedirs(d, mode=0o700, exist_ok=True)
        script = os.path.join(d, "deploy-check")
        with open(deploycheck.__file__) as f:
            write_private(script, f.read(), mode=0o755)
        contract = os.path.join(d, "contract.json")
        write_private(contract, json.dumps({
            "root": p.root, "compose": p.deploy_compose, "lan_addr": p.lan_addr, "ports": list(p.deploy_ports),
            "binds": list(p.deploy_binds), "external": list(p.deploy_external),
            "adopted": list(p.deploy_adopted), "compose_project": p.compose_project}), mode=0o644)
        self.session_mounts = list(self.session_mounts) + [
            (script, "/run/agentbox/bin/deploy-check", True),
            (contract, deploycheck.CONTRACT_PATH, True)]

    def _mount_sources(self):
        """Every host path this session binds into the container, for the must-not-mount check."""
        p = self.project
        out = [self.root_src] + list(p.data_ro) + list(p.data_rw) + [self.state_dir]
        if self.token_file:
            out.append(self.token_file)
        out += [src for src, _dst, _ro in self.setup.mounts]
        out += list(self.setup.keys.values())
        out += [src for src, _dst, _ro in self.session_mounts]
        if self.profile and self.profile.dir:
            out.append(self.profile.dir)
        return out

    def _toulmin_lists(self):
        """The must-not-mount list (contract 1.2): a hard refusal, naming the path, before anything is
        confirmed. The socket directory is the one path under the list's own glob that is asked for."""
        bad = toulmin.violations(self.toulmin["must_not_mount"], self._mount_sources(),
                                 allowed=[self.toulmin["mount_socket_dir"]])
        if bad:
            lines = ", ".join(f"{src} (must_not_mount: {entry})" for src, entry in bad)
            raise AgentboxError(f"Toulmin's must-not-mount list refuses this session's mounts: {lines}. The "
                                "session does not start; narrow the grants or the adapter's mounts.")
        self.toulmin["lists_applied"] = True
        self._write_meta()

    def _toulmin_before_run(self, argv):
        """After the argv is complete and audited, before `podman run`: confirm the planned mounts (hashed
        from that argv), then start `tm session serve` beside the session."""
        if not self.toulmin:
            return
        entries = toulmin.mount_list(argv)
        h = toulmin.mounts_hash(entries)
        doc = toulmin.confirm_message(self.toulmin, preset=self.project.preset, network=self.egress,
                                      mounts_hash_value=h, project_root=self.project.root,
                                      agent=self.adapter.name, clone=self.clone)
        run_dir = self.settings.session_run_dir(self.name)
        toulmin.confirm(self.toulmin, doc, run_dir)
        self.toulmin["mounts_hash"] = h
        self.toulmin["mount_list"] = entries
        self._write_meta()
        self._serve_proc = toulmin.serve(self.toulmin, run_dir)

    def _backup_grants(self):
        """Snapshot the read-write grants before the session runs, EXCLUDING every covered/masked path.
        The excludes are exactly `covered_paths()` — the same list the gate enforces — so the backup holds
        code, never a plaintext copy of a secret the agent cannot damage (it is masked), and the two can
        never drift apart. Runs after the adapter setup so `covered_paths()` sees the adapter's covers too."""
        p = self.project
        covered = self.covered_paths()

        def covers_rel(g):
            gr = os.path.realpath(g)
            out = [os.path.relpath(c, gr) for c in covered if c != gr and c.startswith(gr.rstrip("/") + "/")]
            if p.data_bind == "on" and gr == os.path.realpath(self.root_src):
                out.append(mounts.DATA_DIR)          # live data: the snapshot is not a data backup
            return out

        if p.data_bind == "on":
            self.warn(f"agentbox: {mounts.data_dir(p.root)} is not in the pre-session snapshot (data_bind is on; "
                      "the snapshot is not a data backup): it is yours to back up")
        backup.snapshot(self.settings.project_backups_dir(p.name), self.name, self.writable_grants(),
                        skip_rel_for=lambda g: mounts._rel_skips(p.root, g, p.mask_skip),
                        exclude_rel_for=covers_rel,
                        warn_mb=int(self.settings.backup_warn_mb), warn=self.warn,
                        step=lambda g: self._step(f"backup {g}", f"backing up {g} (the pre-session snapshot)"))
        with self._step("retain", "removing old pre-session snapshots"):
            backup.retain(self.settings.project_backups_dir(p.name), keep=int(self.settings.backup_keep),
                          daily_days=int(self.settings.backup_daily_days))

    @property
    def inbox(self):
        """Where this session puts what it wants to keep: read-write, never loaded.

        Per project and persistent -- except under `lockdown`, where it sits inside the session's own
        ephemeral state and goes with it at teardown. Lockdown keeps nothing by design (decisions.md,
        2 Oct), and a review queue that outlived the session would be the single exception to that; the
        agent is told, so it does not write a skill believing it will be kept."""
        if self.inbox_is_ephemeral:
            return os.path.join(self.state_dir, "inbox")
        return self.settings.inbox_dir(self.project.name, self.adapter.name)

    @property
    def inbox_is_ephemeral(self):
        return self.project.preset == "lockdown" and bool(self.name)

    @property
    def inbox_mount(self):
        return self.adapter.inbox_mount

    def _add_profile_mounts(self, notes):
        """The profile's read-only mounts, the generated instruction file, and the inbox.

        Appended after `_check_setup`, with their own equivalent check: these destinations are the agent's
        own config dir (`state_mount`) or the adapter's root, which is exactly the pair `_check_setup`
        allows -- but the adapter does not ask for them, the launcher does, so they are checked here rather
        than pretending an adapter requested them. The inbox goes LAST so that a deeper mount (OMP's inbox
        inside its `agent/` bind) lands on top of the one it sits in."""
        roots = [r for r in (self.adapter.state_mount, self.setup.mount_root) if r]

        def check(dst, what):
            _no_commas(dst, what)
            if not any(inside(dst, r) and dst != r for r in roots):
                raise AgentboxError(f"{what} {dst} is not under the agent's own root ({', '.join(roots) or 'none'})")

        for src, dst, ro in self.profile.mounts:
            _no_commas(src, "profile mount")
            check(dst, "profile mount")
            self._own_mount_point(src, dst)
            self.setup.mounts.append((src, dst, ro))
        text = self.adapter.session_instructions(self.settings, notes,
                                                 profile_on=not self.profile_off[0])
        if text is not None and self.adapter.instructions_name:
            path = os.path.join(self.settings.session_run_dir(self.name),
                                f"{self.adapter.name}-instructions.md")
            write_private(path, text, atomic=False)
            dst = f"{self.adapter.state_mount}/{self.adapter.instructions_name}"
            check(dst, "generated instructions")
            self._own_mount_point(path, dst)
            self.setup.mounts.append((path, dst, True))
        for name, dst in self.adapter.per_session_dirs:
            check(dst, "per-session dir")
            host = os.path.join(self.settings.session_run_dir(self.name), name)
            os.makedirs(host, mode=0o700, exist_ok=True)
            self.setup.mounts.append((host, dst, False))
        if self.inbox_mount:
            check(self.inbox_mount, "inbox mount")
            os.makedirs(self.inbox, mode=0o700, exist_ok=True)
            self.setup.mounts.append((self.inbox, self.inbox_mount, False))

    def host_state_path(self, dst):
        """The host path under the per-project state dir that a container path mirrors, or None.

        The adapter says which container paths are the state dir or part of it (`state_mirrors`): for Claude
        the whole config dir is, for OMP it is `~/.omp/agent`. Anything else -- a tmpfs, another adapter
        mount, a path outside -- is not ours to create or interpret."""
        for prefix, rel in sorted(self.adapter.state_mirrors.items(), key=lambda kv: -len(kv[0])):
            if dst == prefix or not inside(dst, prefix):
                continue
            return os.path.join(self.state_dir, rel, os.path.relpath(dst, prefix))
        return None

    def _own_mount_point(self, src, dst):
        """Create the mount point for a profile mount ourselves, if it falls inside the per-project state.

        A bind whose destination does not exist gets its mount point created by the container RUNTIME, in
        the middle of the state dir, with whatever owner and mode the runtime chooses. Two things went wrong
        because of that: the end-of-session state scan took its baseline before those files existed and
        reported them as changes afterwards (`CLAUDE.md` on every session, found by `make claude-live`), and
        the next session's migration had to judge a file it had not written and might not be able to read.

        So the launcher makes them, before the scan baseline is taken: an empty file for a file mount, an
        empty directory for a directory one. OMP has always done this for its own file mounts
        (`omp._prepare_file`); the profile's were the ones that did not."""
        host = self.host_state_path(dst)
        if not host:
            return                     # not a path that mirrors the state dir: not ours to make
        os.makedirs(os.path.dirname(host), mode=0o700, exist_ok=True)
        if os.path.isdir(src):
            if os.path.islink(host) or (os.path.lexists(host) and not os.path.isdir(host)):
                os.unlink(host)
            os.makedirs(host, mode=0o700, exist_ok=True)
            return
        if os.path.islink(host) or (os.path.lexists(host) and not os.path.isfile(host)):
            os.unlink(host)
        if not os.path.exists(host):
            write_private(host, "", atomic=False)

    def _check_setup(self):
        """Adapter mounts: destinations under the adapter's own root, never on top of a grant or /run/agentbox;
        key names that are safe in a path and a shell variable."""
        st = self.setup
        for dst in st.tmpfs + st.ro_empty_dirs:
            _no_commas(dst, "adapter tmpfs")
            if not st.mount_root or not inside(dst, st.mount_root):
                raise AgentboxError(f"adapter tmpfs {dst} is not under the adapter's root {st.mount_root!r}")
        # The same pair of roots `_add_profile_mounts` allows: the adapter's declared root and the agent's
        # own config dir. Claude's plugin tree goes under the second (its root is /etc/claude-code, and
        # only with approvals on); a mount ON either root, or anywhere else, is refused.
        roots = [r for r in (st.mount_root, self.adapter.state_mount) if r]
        for src, dst, _ro in st.mounts:
            _no_commas(src, "adapter mount")
            _no_commas(dst, "adapter mount")
            if not any(inside(dst, r) and dst != r for r in roots):
                raise AgentboxError(f"adapter mount {dst} is not under the adapter's root "
                                    f"({', '.join(roots) or 'none'})")
        for path in st.project_covers:
            _no_commas(path, "project cover")
            if not inside(path, self.project.root) or path == self.project.root:
                raise AgentboxError(f"adapter cover {path} is not inside the project")
        for provider, path in st.keys.items():
            keysmod.env_name(provider)
            _no_commas(os.path.realpath(path), "key file")
        for k, v in st.env.items():
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", k) or "\n" in str(v):
                raise AgentboxError(f"adapter env {k!r} is not a plain variable")

    @property
    def model_domains(self):
        return sorted(set(self.adapter.model_domains) | set(self.setup.model_domains))

    def _check_live_stack_is_ours(self):
        """The observed stack defaults to the project's name, so a project that happens to share a name with
        another stack on the host would observe it. Unless compose_project is set explicitly, every container
        of that compose project must have been brought up from inside the project root (review 2026-10-01 #7)."""
        p = self.project
        try:
            containers = self.podman.containers_json({sockguard.COMPOSE_LABEL: p.compose_project})
        except AgentboxError as e:
            raise AgentboxError(f"project {p.name}: cannot confirm that compose project "
                                f"{p.compose_project!r} is this project's stack: {e}. Refusing rather than "
                                "observing a stack that may not be yours.")
        deploys = os.path.realpath(self.settings.deploys_dir(p.name))
        for c in containers:
            labels = c.get("Labels") or {}
            wd = labels.get("com.docker.compose.project.working_dir", "")
            real = os.path.realpath(wd) if wd else ""
            # `ab deploy` runs the stack from its copy of a reviewed commit under the project's data dir; the
            # deploy gate stamps these labels and drops any a client sets, and a dev gate never makes them
            deployed = (labels.get("agentbox.deploy") == p.name and labels.get("agentbox.role") == "deploy"
                        and real and inside(real, deploys))
            if not real or not (inside(real, p.root) or deployed):
                name = (c.get("Names") or ["?"])[0]
                raise AgentboxError(
                    f"project {p.name}: compose project {p.compose_project!r} has container {name} from "
                    f"{wd or 'an unknown directory'}, outside {p.root}; it is not this project's stack. If it "
                    "should be observed anyway, set compose_project explicitly (`--compose-project`)")

    def log_path(self, kind):
        return log_path(self.settings, self.name, self.started, kind)

    def _write_scan_state(self):
        """The end-of-session scan's starting point, kept in the run dir so every teardown (the launcher's,
        `ab session kill`, `ab session gc`) can report (review 2026-10-01 step 5C, M2)."""
        p = self.project
        grants = self.writable_grants()
        skip = {g: mounts._rel_skips(self.root_src, g, p.mask_skip) for g in grants}
        # A masked directory is not the agent's to change, and a host service may well write into it during
        # the session (a live data dir is what masks are for): leave it out of the scan rather than report
        # its changes as the session's.
        for m in (self.plan_host.mounts if self.plan_host else []):
            if m.kind == "tmpfs":
                for g in grants:
                    if inside(m.dst, g) and m.dst != g:
                        skip[g].append(os.path.relpath(m.dst, g))
        # data_bind's live data likewise: host services write there, and hashing a database every start and end
        # would make the scan as slow as the data is large.
        if p.data_bind == "on" and self.root_src in skip:
            skip[self.root_src].append(mounts.DATA_DIR)
        doc = {"grants": grants, "skip": skip, "state": scan.snapshot(grants, skip=skip),
               # Recorded at START, so `ab session kill` and `ab session gc` name the branch this session
               # was configured with even if the registry is edited while it runs.
               "base_branch": p.base_branch,
               # The trusted opt-outs that were on: the end-of-session report repeats them (lifecycle._scan).
               "trust": self.trust_warnings,
               "agent_state": {"dir": self.state_dir, "files": scan.state_snapshot(self.state_dir)},
               # What the inbox held at the start: teardown reports (and gives provenance to) the difference.
               "inbox": {"project": p.name, "agent": self.adapter.name, "items": profile.items(self.inbox),
                         "preset": p.preset, "egress": p.value("egress"), "dir": self.inbox,
                         "ephemeral": self.inbox_is_ephemeral,
                         # Harvested into the inbox at teardown, then gone with the session's run dir.
                         "harvest": [os.path.join(self.settings.session_run_dir(self.name), n)
                                     for n, _dst in self.adapter.per_session_dirs]}}
        path = os.path.join(self.settings.session_run_dir(self.name), SCAN_STATE)
        write_private(path, json.dumps(doc), atomic=False)

    def _write_meta(self):
        """session.json: what every teardown path (the launcher's, `kill`, `gc`) needs. Written again whenever
        the Toulmin record or the clone changes, so a teardown without this process still has them."""
        if not self._meta_written:
            self.started = time.time()
            self._meta_written = True
        meta = {"session": self.name, "agent": self.adapter.name, "project": self.project.name,
                "pid": os.getpid(), "pid_start": _start_time(os.getpid()), "started": self.started,
                "preset_egress": self.project.value("egress"), "root": self.project.root,
                "mount": self.mount_mode, "clone": self.clone,
                "data_env": self.project.data_env, "data_bind": self.project.data_bind,
                # never the token (item 35): teardown needs tm, session and project_root, and the token's one
                # copy on disk is the 0600 file toulmin.write_token makes for the container
                "toulmin": ({k: v for k, v in self.toulmin.items() if k != "token"} if self.toulmin else None)}
        path = os.path.join(self.settings.session_run_dir(self.name), "session.json")
        write_private(path, json.dumps(meta))

    # ---- the agent container ------------------------------------------------------------
    def allowed_mount_srcs(self, extra=()):
        p = self.project
        out = [p.root, self.root_src] + p.data_ro + p.data_rw + [self.state_dir, self.settings.session_run_dir(self.name)]
        out += [os.path.realpath(src) for src, _dst, _ro in self.session_mounts]
        if self.profile and self.profile.dir:
            out.append(os.path.realpath(self.profile.dir))
        if self.adapter.state_mount:
            out.append(os.path.realpath(self.inbox))
        if self.token_file:
            out.append(os.path.realpath(self.token_file))
        out += [os.path.realpath(src) for src, _dst, _ro in self.setup.mounts]
        out += [os.path.realpath(k) for k in self.setup.keys.values()]
        return out + list(extra)

    def run_args(self, command, *, interactive=False, tty=False, extra_mounts=()):
        """The complete argv after `podman`, audited."""
        s, a, p = self.settings, self.adapter, self.project
        for check in self.setup.verify:
            check()                          # e.g. OMP's provisioned links still point where they did
        args = ["run", "--rm", f"--name={self.name}", f"--hostname={self.name}"]
        opts = [f"--label={k}={v}" for k, v in labels_for(s, p.name, self.name, "agent").items()]
        if interactive:
            opts.append("--interactive")
        if tty:
            opts.append("--tty")
        opts += hardening.fixed_flags(s, cgroup_parent=s.cgroup_parent)
        opts.append(f"--tmpfs={HOME}:rw,nosuid,nodev,mode=0700,U,notmpcopyup")
        # AGENTBOX_SESSION says "you are inside a session, and which one". A host-only script -- a profile
        # repo's build.sh that deploys or installs, anything that assumes it can reach the LAN or write
        # outside a grant -- can refuse in one line instead of failing halfway through. The id is already a
        # container label and is in the scan state and the inbox provenance; this just lets the session
        # itself see it. First in the dict, so an adapter or the launcher can still override it.
        # AGENTBOX_AGENT names the harness, for anything on the host that reads a sandbox's environment
        # (Toulmin labels owner actions "possibly agent-issued" on these two; contract 1.4).
        env = {"AGENTBOX_SESSION": self.name or "", "AGENTBOX_AGENT": a.name, "HOME": HOME,
               **git_identity(s, a.name), **a.env, **self.setup.env, **self.env}
        if tty and os.environ.get("TERM"):
            env["TERM"] = os.environ["TERM"]
        opts += [f"--env={k}={v}" for k, v in env.items()]
        opts.append(f"--workdir={p.root}")
        opts += self.network_args()
        under = self._dir_covers()
        opts += [m.podman_arg(mask_file=self.mask_file) for m in self.plan.mounts
                 if not any(m.dst.startswith(c.rstrip("/") + "/") for c in under)]     # N2: nothing inside a cover
        opts += self.pin_args()
        if a.state_mount:
            opts.append(f"--mount=type=bind,src={self.state_dir},dst={a.state_mount}")
        if self.token_file:
            opts.append(f"--mount=type=bind,src={os.path.realpath(self.token_file)},dst={TOKEN_PATH},ro")
        opts += [mounts.Mount("tmpfs", d).podman_arg() for d in self.setup.tmpfs]
        opts += [mounts.Mount("tmpfs", d, ro=True).podman_arg() for d in self.setup.ro_empty_dirs]
        for path in self.setup.project_covers:
            if os.path.isdir(path) and not os.path.islink(path):
                opts.append(mounts.Mount("tmpfs", path, ro=True).podman_arg())
            else:
                opts.append(mounts.Mount("mask", path).podman_arg(mask_file=self.mask_file))
        for src, dst, ro in self.setup.mounts:
            opts.append(f"--mount=type=bind,src={os.path.realpath(src)},dst={dst}" + (",ro" if ro else ""))
        for provider, path in sorted(self.setup.keys.items()):
            opts.append(f"--mount=type=bind,src={os.path.realpath(path)},"
                        f"dst={keysmod.CONTAINER_DIR}/{provider},ro")
        for src, dst in self.agent_sockets():
            opts.append(f"--mount=type=bind,src={src},dst={dst}")
        for src, dst, ro in self.session_mounts:          # the integration's: audited like the adapter's
            opts.append(f"--mount=type=bind,src={os.path.realpath(src)},dst={dst}" + (",ro" if ro else ""))
        extra_srcs = []
        for src, dst in extra_mounts:
            opts.append(f"--mount=type=bind,src={src},dst={dst},ro")
            extra_srcs.append(src)
        opts.append("--entrypoint=/bin/sh")
        hardening.audit(opts + [self.image], allowed_mount_srcs=self.allowed_mount_srcs(extra_srcs),
                        allowed_mount_dsts=[p.root] + p.data_ro + p.data_rw + ["/run/agentbox"]
                        + ([self.setup.mount_root] if self.setup.mount_root else []),
                        networks=self.networks, tmpfs_dirs=("/tmp", HOME),
                        allowed_sockets=[src for src, _dst in self.agent_sockets()], mask_file=self.mask_file)
        return args + opts + [self.image, "-c", self.wrapper(), "agentbox"] + list(command)

    def agent_sockets(self):
        """[(host socket, container path)] the agent gets: the gate for the dev tier, sockguard for observe."""
        out = []
        if self.podman_dev:
            out.append((self.gate_socket, sockguard.AGENT_SOCKET))
            if self.podman_live:
                out.append((self.sockguard_socket, LIVE_SOCKET))
        elif self.podman_live:
            out.append((self.sockguard_socket, sockguard.AGENT_SOCKET))
        return out

    @property
    def gate_socket(self):
        return os.path.join(self.settings.session_run_dir(self.name), "gate", "podman.sock")

    def wrapper(self):
        """Exports the token and provider keys from their read-only mounts just before exec; they are never
        on a command line or in podman's config."""
        files = []
        if self.token_file:
            files.append((self.adapter.token_env, TOKEN_PATH))
        for provider in sorted(self.setup.keys):
            files.append((keysmod.env_name(provider), f"{keysmod.CONTAINER_DIR}/{provider}"))
        return "".join(f'if [ -r {path} ]; then IFS= read -r {var} < {path}; export {var}; fi; '
                       for var, path in files) + 'exec "$@"'

    @property
    def secret_env_names(self):
        """The environment variables that legitimately hold a credential in this session."""
        out = [self.adapter.token_env] if self.token_file else []
        return out + [keysmod.env_name(p) for p in sorted(self.setup.keys)]

    def secret_files(self):
        """Host files whose contents are this session's credentials."""
        return ([self.token_file] if self.token_file else []) + [self.setup.keys[k] for k in sorted(self.setup.keys)]

    def agent_command(self, shell=False, extra=()):
        if shell:
            return ["bash"] + list(extra)
        return self.adapter.agent_argv(self.project.value("approvals")) + list(extra)

    def image_labels(self):
        """The labels of the image this session will run, or {} when they cannot be read.

        Deliberately NOT cached. It is read twice per session (the CLI-version line in the log, and the
        plugin tree's staleness check), so a cache saves one `podman image inspect` and costs the ability
        to ask twice and get the truth twice -- which a test that walks three image states on one session
        does, and which is the kind of thing a cache quietly breaks. Read as JSON, never a Go template."""
        try:
            return userimage.image_labels(self.podman.image_json(self.image))
        except AgentboxError:
            return {}

    def agent_cli_version(self):
        """The agent CLI version baked into the image this session runs, from the `agentbox.cli-version`
        label that `make <agent>-image` records. None if the image predates the label (rebuild to get it),
        or cannot be read at all -- this reports, it does not decide anything.

        Read as JSON, the same way `ab image build` reads the same label, so the two cannot come to
        disagree about where the pin lives. It was a Go template, which is the habit `podman network ls`
        taught this project to drop."""
        v = (self.image_labels().get("agentbox.cli-version") or "").strip()
        return v if v and v not in ("<no value>", "unknown") else None

    # ---- lifecycle ----------------------------------------------------------------------
    SHIELDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def _teardown_shielded(self, reason, old=None):
        """teardown() with SIGINT/SIGTERM/SIGHUP ignored for its duration, then `old` (default: the handlers
        in place now) back."""
        old = old or {sig: signal.getsignal(sig) for sig in self.SHIELDED}
        for sig in self.SHIELDED:
            signal.signal(sig, signal.SIG_IGN)
        try:
            self.teardown(reason)
        finally:
            for sig, h in old.items():
                signal.signal(sig, h)

    def _guarded(self, body, *, agent_owns_ctrl_c):
        """Run body() and always tear down: on return, error, Ctrl-C, SIGTERM or SIGHUP (terminal closed).
        Teardown itself runs with those signals ignored, so a second signal cannot cut it short."""
        old = {sig: signal.getsignal(sig) for sig in self.SHIELDED}

        def stop(signum, frame):
            raise KeyboardInterrupt
        reason = "error"
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN if agent_owns_ctrl_c else stop)
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGHUP, stop)
            result = body()
            reason = "agent_exit"
            return result
        except KeyboardInterrupt:
            reason = "signal"
            raise
        finally:
            self._teardown_shielded(reason, old)     # ignores the signals first, then puts the caller's back

    def run_interactive(self, command, tty):
        def body():
            self.start_infra()
            argv = self.run_args(command, interactive=True, tty=tty)
            self._toulmin_before_run(argv)
            return self.podman.interactive(argv)
        return self._guarded(body, agent_owns_ctrl_c=True)   # Ctrl-C belongs to the agent

    def run_captured(self, command, stdin="", timeout=600, before=None):
        def body():
            self.start_infra()
            if before:
                before()
            argv = self.run_args(command, interactive=True, extra_mounts=self.probe_mounts)
            self._toulmin_before_run(argv)
            return self.podman.run(argv, check=False, input=stdin, timeout=timeout)
        return self._guarded(body, agent_owns_ctrl_c=False)

    probe_mounts = ()

    def _dir_covers(self):
        """Directory covers (empty tmpfs): the plan's mask dirs and the adapter's project covers that are dirs.
        Nothing is mounted, pinned or listed beneath one (audit 6 Oct, N2)."""
        out = [m.dst for m in (self.plan.mounts if self.plan else []) if m.kind == "tmpfs"]
        out += [p for p in self.setup.project_covers if os.path.isdir(p) and not os.path.islink(p)]
        return [os.path.realpath(d) for d in out]

    @property
    def int_net(self):
        return f"{self.name}-int"

    @property
    def egr_net(self):
        return f"{self.name}-egr"

    @property
    def proxy_name(self):
        return f"{self.name}-proxy"

    def network_args(self):
        self.networks = [self.int_net]
        return [f"--network={self.int_net}"]

    def squid_conf(self, dns=None):
        s = self.settings
        addrs = sorted(set(s.host_addrs) | set(egress.host_addresses()))
        if not addrs:
            raise AgentboxError("cannot tell the VM's own addresses (enumeration failed and host_addrs is empty); "
                                "refusing to start a proxy without a host-address rule")
        return egress.squid_conf(self.egress, model_domains=self.model_domains, model_host=s.model_host,
                                 model_port=s.model_port, extra_domains=self.project.extra_domains,
                                 host_addrs=addrs, dns=dns)

    def net_create_args(self, name, internal):
        return net_create_argv(self.settings, labels_for(self.settings, self.project.name, self.name, "network"),
                               name, internal=internal, dev_owner=self.name if internal else "")

    def proxy_run_args(self, conf_path):
        return proxy_run_argv(self.settings, labels_for(self.settings, self.project.name, self.name, "proxy"),
                              self.proxy_name, conf_path=conf_path, networks=[self.egr_net, self.int_net])

    def start_infra(self):
        with self._step("infra", "starting the session's network, proxy and sidecars"):
            self._start_infra()

    def _start_infra(self):
        pm, s = self.podman, self.settings
        pm.run(self.net_create_args(self.int_net, internal=True))
        pm.run(self.net_create_args(self.egr_net, internal=False))
        gw = pm.run(["network", "inspect", self.egr_net, "--format", "{{range .Subnets}}{{.Gateway}} {{end}}"])
        dns = (gw.stdout or "").split()[0] if (gw.stdout or "").split() else None
        conf = os.path.join(s.session_run_dir(self.name), "squid.conf")
        write_private(conf, self.squid_conf(dns=dns))
        pm.run(self.proxy_run_args(conf))
        self._wait_proxy()
        self.env.update(egress.proxy_env(self.proxy_name))
        if self.podman_live or self.podman_dev:
            self._start_podman_access()
        for var in self.adapter.proxy_url_env:
            self.env[var] = self.env["HTTPS_PROXY"]

    @property
    def sockguard_name(self):
        return f"{self.name}-sockguard"

    @property
    def sockguard_socket(self):
        return os.path.join(self.settings.session_run_dir(self.name), "sg", "sockguard.sock")

    @property
    def sockguard_dev_name(self):
        return f"{self.name}-sockguard-dev"

    @property
    def sockguard_dev_socket(self):
        return os.path.join(self.settings.session_run_dir(self.name), "sgdev", "sockguard.sock")

    @property
    def holder_name(self):
        return f"{self.name}-holder"

    def sockguard_run_args(self, service_sock, conf_path, *, name=None, socket_path=None, role="sockguard"):
        s = self.settings
        return sockguard_run_argv(s, labels_for(s, self.project.name, self.name, role), name or self.sockguard_name,
                                  service_sock=service_sock, socket_path=socket_path or self.sockguard_socket,
                                  conf_path=conf_path, cgroup_parent=s.cgroup_parent)

    def sockguard_config(self):
        return sockguard.config(self.podman_live, logs=self.project.observe_logs)

    @property
    def bind_roots(self):
        p = self.project
        return [p.root] + list(p.data_rw) + list(p.data_ro)

    def pin_args(self):
        """Every directory between a grant root and a cover, bound onto itself in the agent's container, so it
        is a mount point and cannot be renamed (EBUSY): renaming a cover's parent would otherwise move the real
        covered file to a path the gate's covered list does not name (audit 5 Oct, H1). Read-only under a
        read-only grant. A rename across one of these boundaries now fails with EXDEV (`git mv` between such
        directories; `mv` falls back to copy and delete)."""
        if not self.podman_dev:
            return []
        roots = [os.path.realpath(r) for r in self.bind_roots]
        ro_roots = {os.path.realpath(r) for r in self.ro_roots()}
        pins = {}
        covered = self.covered_paths()
        for c in covered:
            holders = [r for r in roots if c.startswith(r.rstrip("/") + "/")]
            if not holders:
                continue
            root = max(holders, key=len)
            d = os.path.dirname(c)
            while d != root and d.startswith(root + "/"):
                if not any(inside(d, x) for x in covered):   # N2
                    pins[d] = root in ro_roots
                d = os.path.dirname(d)
        return [f"--mount=type=bind,src={d},dst={d}" + (",ro" if ro else "")
                for d, ro in sorted(pins.items(), key=lambda kv: kv[0].count("/"))]

    def masked_paths(self):
        """The covers that read as empty in the session (secret masks, mask dirs, the adapter's project
        covers): a build context packed in the session carries them empty."""
        out = {os.path.realpath(m.dst) for m in (self.plan.mounts if self.plan else []) if m.kind in ("mask", "tmpfs")}
        out |= {os.path.realpath(p) for p in self.setup.project_covers}
        return sorted(out)

    def cover_ids(self):
        """{covered path: [st_dev, st_ino]} at session start; the gate refuses binds if one changes (a cover
        moved or was replaced).

        A cover left out of this map is one the gate's rename detection no longer applies to, so an identity
        that cannot be read refuses the session rather than being skipped (audit F-2/F-10 sweep). A path that
        does not exist has no identity to pin and nothing to rename."""
        out = {}
        for c in self.covered_paths():
            try:
                st = os.lstat(c)
            except OSError as e:
                if e.errno == errno.ENOENT:
                    continue
                raise AgentboxError(f"{c}: cannot read this cover's identity ({e}), so the gate could not "
                                    "tell if it were renamed or replaced during the session")
            out[c] = [st.st_dev, st.st_ino]
        return out

    def covered_paths(self):
        """Host paths the agent's own container masks or covers read-only, as real paths (crit 5 Oct): secret
        masks and mask dirs, the dev-swap `.env` (the real file behind the swap), `.git/hooks`/`config`/
        `config.worktree`, compose-file covers and the adapter's project covers. The gate refuses a spawned
        container's bind that resolves onto, under, or over any of these, since those in-container mounts do
        not exist in a container the agent starts. A read-only bind whose destination is itself a grant (a
        data_ro grant) is not a cover: the agent may read it, and the gate keeps it read-only in spawned
        containers (`ro_roots`, audit 5 Oct H3)."""
        covered = self._plan_covered()
        for p in self.setup.project_covers:
            covered.add(os.path.realpath(p))
        under = self._dir_covers()
        return sorted(c for c in covered if not any(c.startswith(d.rstrip("/") + "/") for d in under))

    def _plan_covered(self):
        """The cover/mask paths the session PLAN creates (`mounts.covered`: masks, mask dirs, the `.env`
        dev-swap target, and the read-only git hooks/config and compose covers). The single source both
        `covered_paths()` (what the gate enforces) and the pre-session backup's exclude list are built from,
        so a cover added in one place cannot be missed by the other. `covered_paths()` adds the adapter's
        project covers on top (known only after setup).

        NOT what the grant scan may treat as covered: a read-only cover is readable (audit F-6). The scan
        uses `mounts.hidden`, through `grantcheck.preflight`."""
        if not self.plan:
            return set()
        return set(mounts.covered(self.plan, self.bind_roots))

    def sockguard_dev_config(self):
        return sockguard.dev_config(self.name, bind_roots=self.bind_roots)

    def _start_sockguard(self, service, *, name, socket_path, text, role):
        s, run = self.settings, self.settings.session_run_dir(self.name)
        os.makedirs(os.path.dirname(socket_path), mode=0o700, exist_ok=True)
        conf = os.path.join(run, f"{role}.yaml")
        write_private(conf, text)
        self.podman.run(self.sockguard_run_args(service, conf, name=name, socket_path=socket_path, role=role))

        def running():
            st = self.podman.run(["inspect", "-f", "{{.State.Status}}", name], check=False)
            return (st.stdout or "").strip() not in ("exited", "stopped", "dead")
        try:
            sockguard.wait_socket(socket_path, float(s.ready_timeout), role, alive=running)
        except AgentboxError:
            logs = self.podman.run(["logs", name], check=False)
            raise AgentboxError(f"{role} did not start: "
                                f"{' '.join(((logs.stderr or '') + (logs.stdout or '')).strip().splitlines()[-3:])}")

    def holder_run_args(self):
        """A container that only sleeps on the session network: builds join its network namespace."""
        s = self.settings
        return holder_run_argv(s, labels_for(s, self.project.name, self.name, "holder"), self.holder_name,
                               network=self.int_net, cgroup_parent=s.cgroup_parent)

    def dev_proxy_env(self):
        """Proxy variables for the agent and every dev container: the session's own subnet and podman's DNS
        names go direct (they are on the same internal network), everything else through squid."""
        p = self.podman.run(["network", "inspect", self.int_net, "--format",
                             "{{range .Subnets}}{{.Subnet}} {{end}}"], check=False)
        subnets = self.int_subnets = (p.stdout or "").split()
        env = egress.proxy_env(self.proxy_name)
        no_proxy = ",".join(["localhost", "127.0.0.1", "::1"] + subnets + [".dns.podman"])
        env["NO_PROXY"] = env["no_proxy"] = no_proxy
        return env

    def build_network(self):
        """(proxy env by the proxy's IP, [DNS servers]) for builds: their RUN steps join the holder's network
        namespace but get their own resolv.conf, where container names do not resolve (trial, 2 Oct). podman's
        DNS for the session network answers on the network's gateway."""
        p = self.podman.run(["inspect", self.proxy_name, "--format", "{{json .NetworkSettings.Networks}}"],
                            check=False)
        try:
            ip = (json.loads(p.stdout or "{}").get(self.int_net) or {}).get("IPAddress") or ""
        except ValueError:
            ip = ""
        g = self.podman.run(["network", "inspect", self.int_net, "--format", "{{range .Subnets}}{{.Gateway}} {{end}}"],
                            check=False)
        dns = (g.stdout or "").split()
        if not ip:
            return None, dns
        url = f"http://{ip}:{egress.PROXY_PORT}"
        no_proxy = ",".join(["localhost", "127.0.0.1"] + list(self.int_subnets))
        return {"http_proxy": url, "https_proxy": url, "HTTP_PROXY": url, "HTTPS_PROXY": url,
                "no_proxy": no_proxy, "NO_PROXY": no_proxy}, dns

    def gate_config(self, service, proxy_env):
        s = self.settings
        svc = s.service_dir(self.name)
        build_env, build_dns = self.build_network()
        return {
            "listen": self.gate_socket, "upstream": self.sockguard_dev_socket, "raw": service,
            "session": self.name, "network": self.int_net,
            "labels": labels_for(s, self.project.name, self.name, "dev"),
            "proxy_env": proxy_env, "bind_roots": self.bind_roots, "covered": self.covered_paths(),
            "covered_ids": self.cover_ids(), "ro_roots": [os.path.realpath(r) for r in self.ro_roots()],
            "masked": self.masked_paths(),
            "mask_names": [m for m in self.project.mask if not mounts.mask_is_path(m)],
            # data_env: env files under these are shown to the agent on purpose, so a late one is no finding
            "env_visible": [os.path.realpath(self.project.root)] if self.project.data_env == "on" else [],
            "subnets": self.int_subnets,
            "cgroup_parent": s.cgroup_parent or "", "slice": s.cgroup_parent or "",
            "memory": 1 << 30, "pids": 1024, "memory_max": 4 << 30, "pids_max": 4096,
            "build_memory": int(self.project.build_memory),
            "holder": self.holder_name, "registries": list(sockguard.REGISTRIES),
            "images": list(self.project.dev_images), "spool": os.path.join(service_tmp(s, self.name), "gate"),
            "built_record": s.built_record(self.project.name),
            "build_proxy_env": build_env, "build_dns": build_dns,
            "log": os.path.join(svc, "gate.log"),
        }

    def _start_podman_access(self):
        s = self.settings
        stamp = int(self.started)
        service, self._service_proc = sockguard.start_service(
            s.podman, s.service_dir(self.name), float(s.ready_timeout), slice_name=s.cgroup_parent,
            unit=f"{self.name}-podman-{stamp}", tmpdir=service_tmp(s, self.name))
        if self.podman_live:
            self._start_sockguard(service, name=self.sockguard_name, socket_path=self.sockguard_socket,
                                  text=self.sockguard_config(), role="sockguard")
        if not self.podman_dev:
            url = f"unix://{sockguard.AGENT_SOCKET}"
            self.env.update({"CONTAINER_HOST": url, "DOCKER_HOST": url})
            return
        self._start_sockguard(service, name=self.sockguard_dev_name, socket_path=self.sockguard_dev_socket,
                              text=self.sockguard_dev_config(), role="sockguard-dev")
        self.podman.run(self.holder_run_args())
        proxy_env = self.dev_proxy_env()
        self.env.update(proxy_env)
        # One spelling per variable for dev containers: sockguard refuses case-variant duplicate keys in a
        # create body. Lower case is the one curl, wget, Python and Go all read.
        container_proxy_env = {k: v for k, v in proxy_env.items() if k.islower()}
        cfg = os.path.join(s.service_dir(self.name), "gate.json")
        write_private(cfg, json.dumps(self.gate_config(service, container_proxy_env), indent=2))
        os.makedirs(os.path.dirname(self.gate_socket), mode=0o700, exist_ok=True)
        self._gate_proc = sockguard.start_gate(cfg, s.service_dir(self.name), self.gate_socket,
                                               float(s.ready_timeout), slice_name=s.cgroup_parent,
                                               unit=f"{self.name}-gate-{stamp}")
        url = f"unix://{sockguard.AGENT_SOCKET}"
        self.env.update({"CONTAINER_HOST": url, "DOCKER_HOST": url})
        if self.podman_live:
            self.env["AB_PODMAN_LIVE"] = f"unix://{LIVE_SOCKET}"

    def _wait_proxy(self):
        deadline = time.time() + float(self.settings.ready_timeout)
        while time.time() < deadline:
            logs = self.podman.run(["logs", self.proxy_name], check=False)
            text = (logs.stdout or "") + (logs.stderr or "")
            if SQUID_READY in text:
                return
            state = self.podman.run(["inspect", "-f", "{{.State.Status}}", self.proxy_name], check=False)
            if (state.stdout or "").strip() in ("exited", "stopped", "dead"):
                raise AgentboxError(f"egress proxy exited: {' '.join(text.strip().splitlines()[-3:])}")
            time.sleep(0.5)
        raise AgentboxError(f"egress proxy not ready after {self.settings.ready_timeout}s")

    def add_network_cases(self, builder):
        from .selftest import runner
        s = self.settings
        runner.network_cases(builder, self.egress, model=(s.model_host, s.model_port),
                             lan_targets=list(s.lan_targets), model_domains=self.model_domains,
                             extra_domains=self.project.extra_domains)

    def teardown(self, exit_reason="error"):
        if not self.name:
            return
        teardown(self.settings, self.podman, self.name, warn=self.warn, exit_reason=exit_reason)
        # the module teardown -> sockguard.stop_service reaps our service and gate Popen handles as well as
        # killing by PID; this poll is a harmless belt-and-braces.
        for proc in (self._service_proc, self._gate_proc, self._serve_proc):
            if proc:
                proc.poll()


def make_session(settings, agent, project, **kw):
    """The one way a session is made. Kept as a function because every caller (cli, selftest, the tests)
    goes through it, not because there is a choice of class to make."""
    return Session(settings, agent, project, **kw)


def plugin_log_line(settings, agent):
    """The session log's record of the installed plugin tree (item 41): which install it ran with, so a
    behaviour change can be matched to an `ab profile install`. Never raises: it only reports."""
    import hashlib
    from . import installer                  # installer imports this module
    try:
        with open(installer.lock_path(settings, agent), "rb") as f:
            state = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return f"plugins agent={agent} none\n"
    lock = installer.read_lock(settings, agent) or {}
    return f"plugins agent={agent} installed_at={lock.get('installed_at', '?')} state={state}\n"

