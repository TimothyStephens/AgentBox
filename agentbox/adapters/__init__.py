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

    def agent_argv(self, approvals):
        return list(self.command) + (list(self.approvals_off_args) if approvals == "off" else [])

    def seed_state(self, settings, state_dir, project_root, sandbox_notes):
        """Prepare the per-project state dir at session start. Default: nothing."""


def get(name):
    from . import claude
    adapters = {"claude": claude.ADAPTER}
    if name not in adapters:
        from ..errors import AgentboxError
        raise AgentboxError(f"unknown agent {name!r} (known: {', '.join(sorted(adapters))})")
    return adapters[name]
