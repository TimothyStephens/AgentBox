"""Harness adapters: the only per-agent code.

An adapter declares what its agent needs; the launcher builds and hardens the container.
"""
from dataclasses import dataclass, field


@dataclass
class Adapter:
    name: str
    image: str
    command: list                     # argv of the agent itself
    model_domains: list               # squid allowlist in every egress mode
    env: dict = field(default_factory=dict)
    token_env: str = ""               # env var the token file is exported as, if any
    state_mount: str = ""             # container path of the per-project state dir ("" = none)
    required_tools: list = field(default_factory=list)
    approvals_off_args: list = field(default_factory=list)
    approvals_on_args: list = field(default_factory=list)
    proxy_url_env: list = field(default_factory=list)  # extra variables set to the session proxy's URL
    # The host-owned profile (profile.py): {profile item -> container path it is mounted READ-ONLY at}, and
    # the items that cannot be read-only and are copied into the per-project state instead.
    profile_mounts: dict = field(default_factory=dict)
    profile_copies: tuple = ()
    instructions_name: str = ""        # the generated per-session instruction file, under state_mount
    migrate_to_inbox: tuple = ()       # per-project state names moved to the inbox once (now read-only)
    inbox_mount: str = ""              # container path of the project's inbox (read-write, never loaded)

    def agent_argv(self, approvals):
        return list(self.command) + list(self.approvals_off_args if approvals == "off" else self.approvals_on_args)

    def seed_state(self, settings, state_dir, sandbox_notes):
        """Prepare the per-project state dir at session start. Default: nothing."""

    def prepare_session(self, sess, sandbox_notes):
        """Host-side work at session start that depends on the session (generated files, keys). Returns a
        SessionSetup. Default: nothing extra."""
        return SessionSetup()

    def redundant_state_copy(self, settings, name, path):
        """Is this per-project state file a copy of what the profile already holds, with nothing of its own?

        Such a file is deleted by the migration instead of being queued in the inbox: an inbox full of
        copies of the seed is noise, and noise is what stops a review queue being read. Default: no -- only
        an adapter that knows how it wrote the file can say. (`session.migrate_state_to_inbox`.)"""
        return False

    def session_instructions(self, settings, sandbox_notes):
        """The instruction file to generate for this session and mount read-only, or None for an agent that
        builds its own (OMP generates AGENTS.md in prepare_session). Default: none."""
        return None


@dataclass
class SessionSetup:
    """What an adapter adds to one session. The launcher audits all of it like its own mounts."""
    mounts: list = field(default_factory=list)          # (host src, container dst, read_only)
    mount_root: str = ""                                # every adapter mount dst must sit under this
    tmpfs: list = field(default_factory=list)           # container dirs (under mount_root) made user tmpfs
    ro_empty_dirs: list = field(default_factory=list)   # container dirs (under mount_root) covered empty, read-only
    env: dict = field(default_factory=dict)             # plain values only; never a secret
    model_domains: list = field(default_factory=list)   # added to the proxy's model allowlist
    keys: dict = field(default_factory=dict)            # provider -> host key file, exported at start
    verify: list = field(default_factory=list)          # callables run just before the container starts
    project_covers: list = field(default_factory=list)  # paths inside the project hidden (empty, read-only)


def get(name):
    from . import claude, omp
    adapters = {"claude": claude.ADAPTER, "omp": omp.ADAPTER}
    if name not in adapters:
        from ..errors import AgentboxError
        raise AgentboxError(f"unknown agent {name!r} (known: {', '.join(sorted(adapters))})")
    return adapters[name]
