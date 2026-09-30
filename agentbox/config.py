"""Where agentbox keeps things, and the launcher-wide settings.

Every path can be moved with an environment variable so tests never touch the real
~/.config/agentbox or ~/.local/share/agentbox:

  AB_CONFIG_DIR   default ~/.config/agentbox        (projects.toml, agentbox.toml, presets/, claude/, tokens)
  AB_DATA_DIR     default ~/.local/share/agentbox   (per-project agent state, backups)
  AB_RUN_DIR      default $XDG_RUNTIME_DIR/agentbox (live session metadata, generated configs; tmpfs)
  AB_PODMAN       default podman
"""
import os
import tomllib
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")

DEFAULTS = {
    "name_prefix": "ab-",
    "labels": {"agentbox": "1"},
    "cgroup_parent": "agentbox.slice",
    "agent_memory": "4g",
    "agent_cpus": "3",
    "agent_pids": 4096,
    "oom_score_adj": 500,          # agent and proxy; containers the gate creates get 1000 (decisions.md: OOM order)
    "squid_image": "docker.io/ubuntu/squid@sha256:6a097f68bae708cedbabd6188d68c7e2e7a38cedd05a176e1cc0ba29e3bbe029",  # 6.13
    "squid_memory": "256m",
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
    def backups_dir(self):
        return os.path.join(self.data_dir, "backups")

    def token_file(self, agent):
        base = os.path.expanduser(self.values.get("token_dir") or "") or self.config_dir
        return os.path.join(base, f"{agent}-oauth-token")

    def seed_dir(self, agent):
        return os.path.join(self.config_dir, agent)

    def state_dir(self, project, agent):
        return os.path.join(self.data_dir, project, agent)

    def session_run_dir(self, session):
        return os.path.join(self.run_dir, "sessions", session)


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
            if k not in DEFAULTS:
                raise ValueError(f"{path}: unknown setting {k!r}")
            values[k] = v
    return Settings(config_dir=os.path.abspath(config_dir), data_dir=os.path.abspath(data_dir),
                    run_dir=os.path.abspath(run_dir),
                    podman=os.environ.get("AB_PODMAN") or "podman", values=values)
