"""Where agentbox keeps things, and the launcher-wide settings.

Every path can be moved with an environment variable so tests never touch the real
~/.config/agentbox or ~/.local/share/agentbox:

  AB_CONFIG_DIR   default ~/.config/agentbox        (projects.toml, agentbox.toml, presets/, <agent>/ seeds, tokens, keys/)
  AB_DATA_DIR     default ~/.local/share/agentbox   (per-project directories, logs, profile snapshots)
  AB_RUN_DIR      default $XDG_RUNTIME_DIR/agentbox (live session metadata, generated configs; tmpfs)
  AB_PODMAN       default podman
  AB_REPOS_DIR    default ~/repos                   (where `ab init` scaffolds new projects)

The data directory (decisions.md, 9 Nov 2026):

  projects/<name>/            everything one project owns -- `ab project purge` removes this and nothing else
      project.json            the project's identity: the root it was registered with (layout.py)
      <agent>/                the agent's state (memory, settings, transcripts) and its inbox/
      backups/                pre-session snapshots of the grants, and agent-state-<stamp>/ from reset-state
      built-images.json       the gate's record of the images this project built (builtrec.py)
      session.lock            the one-live-session lock (projectlock.py)
  logs/                       kept session logs, ab.log            (keyed by session)
  tmp/<session>/              a session's build cache              (keyed by session)
  ephemeral/<session>/        a lockdown session's agent state     (keyed by session)
  profiles/<agent>/<digest>/  read-only snapshots of the profiles  (keyed by agent)

Project state used to sit directly in the data dir, beside `logs/` and `backups/`, so a project named `logs`
would have put its transcripts inside the kept logs; `layout.migrate` moves a flat layout into projects/ once.
"""
import os
import sys
import tomllib
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")

PROJECTS = "projects"
# The data dir's own entries, old and new: never a project. `layout.migrate` leaves these where they are.
OWN_DIRS = frozenset({PROJECTS, "logs", "tmp", "ephemeral", "profiles", "backups", "built-images",
                      "trial"})   # trial: tests/trial/check.py's snapshots
# A project directory's own entries: everything else in it is an agent's state dir.
PROJECT_FILE = "project.json"
BUILT_RECORD = "built-images.json"
BACKUPS = "backups"
LOCK_FILE = "session.lock"
PROJECT_ENTRIES = frozenset({PROJECT_FILE, BUILT_RECORD, BACKUPS, LOCK_FILE, LOCK_FILE + ".tmp"})


def project_dir(data_dir, project):
    """One project's directory. Module-level so builtrec, which is handed a data dir, agrees with Settings."""
    return os.path.join(data_dir, PROJECTS, project)

DEFAULTS = {
    "name_prefix": "ab-",
    "labels": {"agentbox": "1"},
    "cgroup_parent": "agentbox.slice",
    "agent_memory": "4g",
    "agent_cpus": "3",
    "agent_pids": 4096,
    "oom_score_adj": 500,          # agent and proxy; containers the gate creates get 1000 (decisions.md: OOM order)
    "preview_image": "docker.io/library/busybox:1.36",   # ab session preview's forwarder (nc -lk -e)
    "squid_image": "docker.io/ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029",  # 6.13
    "squid_memory": "256m",
    "sockguard_image": "docker.io/codeswhat/sockguard@sha256:3ae41b2fd646132de4fe212326d72fe3786ffd89f65e4500eff1124ac34587ef",  # 2.2.1
    "sockguard_memory": "64m",
    "model_host": "10.0.0.2",
    "model_port": 8080,
    "build_memory": "2g",   # per-build memory limit the gate sets (step 5); a project may override it
    "host_addrs": ["10.0.0.103"],   # the VM's own addresses, always refused by the proxy (plus any found)
    "lan_targets": ["10.0.0.103:8088", "10.0.0.103:8003"],   # selftest: must be refused, proxied and direct
    "backup_keep": 5,
    "backup_daily_days": 7,
    "backup_warn_mb": 2048,
    "slice_warn_fraction": 0.75,
    "ready_timeout": 60,
    "token_dir": "",       # empty: the config dir. Where the <agent>-oauth-token files live.
    "git_name": "",        # empty: taken from the host's global git config
    "git_email": "",
    # `ab session gc`'s keep window, for everything it sweeps: session-built images (the newest per tag
    # stays), superseded personal image layers (the current key always stays; the window is the rollback
    # one, since rollback is `podman tag` onto the previous layer) and unused profile snapshots.
    "image_keep_days": 7,
    "keys_dir": "",        # empty: <config dir>/keys. Provider API key files, one per provider.
    "repos_dir": "~/repos",  # where `ab init` scaffolds new projects (AB_REPOS_DIR overrides)
    "session_idle_warn_hours": 6,  # ab session ls flags, and warns about, a running session idle longer than this
}


@dataclass
class Settings:
    config_dir: str
    data_dir: str
    run_dir: str
    podman: str
    values: dict = field(default_factory=dict)

    def __getattr__(self, name):
        values = self.__dict__.get("values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    @property
    def registry_path(self):
        return os.path.join(self.config_dir, "projects.toml")

    @property
    def presets_dir(self):
        return os.path.join(self.config_dir, "presets")

    @property
    def projects_dir(self):
        """One directory per project (registered or not: state outlives `ab project rm`)."""
        return os.path.join(self.data_dir, PROJECTS)

    def project_dir(self, project):
        return project_dir(self.data_dir, project)

    def project_file(self, project):
        """The project's identity (layout.py): which root its state belongs to."""
        return os.path.join(self.project_dir(project), PROJECT_FILE)

    def project_backups_dir(self, project):
        """Pre-session snapshots of the project's grants, and `agent-state-<stamp>/` from reset-state."""
        return os.path.join(self.project_dir(project), BACKUPS)

    def built_record(self, project):
        return os.path.join(self.project_dir(project), BUILT_RECORD)

    def projects_on_disk(self):
        """Names of every project directory, registered or not, sorted."""
        try:
            names = os.listdir(self.projects_dir)
        except FileNotFoundError:
            return []
        return sorted(n for n in names if os.path.isdir(os.path.join(self.projects_dir, n)))

    def agent_state_dirs(self, project):
        """[(agent, state dir)] for every agent that has state under the project: the project directory's
        subdirectories that are not its own entries (PROJECT_ENTRIES)."""
        base = self.project_dir(project)
        try:
            names = os.listdir(base)
        except FileNotFoundError:
            return []
        return [(n, os.path.join(base, n)) for n in sorted(names)
                if n not in PROJECT_ENTRIES and os.path.isdir(os.path.join(base, n))]

    @property
    def profiles_dir(self):
        """Snapshots of the host-owned agent profiles, one directory per content hash (profile.py)."""
        return os.path.join(self.data_dir, "profiles")

    def profile_src(self, agent):
        """The host-owned profile for one agent: skills, sub-agents, commands, instructions. Only the host
        writes here; sessions get a read-only snapshot of it."""
        return os.path.join(self.config_dir, agent, "profile")

    def inbox_dir(self, project, agent):
        """Where a session puts what it wants to keep (a skill it wrote, a note). Read-write, never loaded
        by the agent, reviewed with `ab profile harvest`."""
        return os.path.join(self.state_dir(project, agent), "inbox")

    def token_file(self, agent):
        base = os.path.expanduser(self.values.get("token_dir") or "") or self.config_dir
        return os.path.join(base, f"{agent}-oauth-token")

    def seed_dir(self, agent):
        return os.path.join(self.config_dir, agent)

    def state_dir(self, project, agent):
        return os.path.join(self.project_dir(project), agent)

    def service_dir(self, session):
        """A session's podman API service: socket, PID, log. Kept apart from the session run dir, whose
        files may be mounted into the sandbox."""
        return os.path.join(self.run_dir, "services", session)

    def session_run_dir(self, session):
        return os.path.join(self.run_dir, "sessions", session)


# Settings that existed and do not any more, with what replaced them. Read by `load` so an old
# agentbox.toml reports instead of refusing to start (or worse, being silently ignored).
RETIRED = {
    "omp_home": "OMP plugins come from the tree `ab profile install omp` builds, not from the host's "
                "~/.omp (5 Nov 2026)",
}


def load():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    config_dir = os.environ.get("AB_CONFIG_DIR") or os.path.join(HOME, ".config", "agentbox")
    data_dir = os.environ.get("AB_DATA_DIR") or os.path.join(HOME, ".local", "share", "agentbox")
    run_dir = os.environ.get("AB_RUN_DIR") or os.path.join(runtime, "agentbox")
    values = dict(DEFAULTS)
    values["labels"] = dict(DEFAULTS["labels"])
    path = os.path.join(config_dir, "agentbox.toml")
    if os.path.exists(path):
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if k in RETIRED:
                # A setting that used to exist is not an unknown setting. Refusing to start over one would
                # punish whoever was using the feature we removed; ignoring it silently would leave them
                # believing it still does something. So: say what replaced it, and carry on.
                print(f"ab: {path}: {k} is no longer used -- {RETIRED[k]}. Remove the line.",
                      file=sys.stderr)
                continue
            if k not in DEFAULTS:
                raise ValueError(f"{path}: unknown setting {k!r}")
            values[k] = v
    if os.environ.get("AB_REPOS_DIR"):
        values["repos_dir"] = os.environ["AB_REPOS_DIR"]
    return Settings(config_dir=os.path.abspath(config_dir), data_dir=os.path.abspath(data_dir),
                    run_dir=os.path.abspath(run_dir),
                    podman=os.environ.get("AB_PODMAN") or "podman", values=values)
