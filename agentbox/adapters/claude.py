"""Claude Code adapter.

State: the whole CLAUDE_CONFIG_DIR is per project and persistent. settings.json, CLAUDE.md and
commands/ are copied in from ~/.config/agentbox/claude/ at every start; nothing else is touched.
The host's ~/.claude is never used.
"""
import json
import os
import shutil

from . import Adapter

SEEDED = ("settings.json", "CLAUDE.md", "commands")


class ClaudeAdapter(Adapter):
    def seed_state(self, settings, state_dir, project_root, sandbox_notes):
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        seed = settings.seed_dir(self.name)
        for item in SEEDED:
            src, dst = os.path.join(seed, item), os.path.join(state_dir, item)
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            elif os.path.lexists(dst):
                os.unlink(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False)
            elif os.path.isfile(src):
                shutil.copyfile(src, dst)
        # The sandbox description goes after the user's own CLAUDE.md.
        md = os.path.join(state_dir, "CLAUDE.md")
        user_md = ""
        if os.path.isfile(md):
            with open(md) as f:
                user_md = f.read()
        with open(md, "w") as f:
            f.write(user_md.rstrip() + ("\n\n" if user_md.strip() else "") + sandbox_notes)
        _mark_onboarded(os.path.join(state_dir, ".claude.json"), project_root)


def _mark_onboarded(path, project_root):
    """Skip first-run onboarding and the folder trust prompt; keep everything else in the file."""
    doc = {}
    if os.path.isfile(path):
        try:
            with open(path) as f:
                doc = json.load(f)
        except ValueError:
            doc = {}
    doc["hasCompletedOnboarding"] = True
    doc["bypassPermissionsModeAccepted"] = True
    projects = doc.setdefault("projects", {})
    proj = projects.setdefault(project_root, {})
    proj["hasTrustDialogAccepted"] = True
    proj.setdefault("hasCompletedProjectOnboarding", True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(doc, f, indent=2)
    os.replace(tmp, path)


ADAPTER = ClaudeAdapter(
    name="claude",
    image="localhost/agentbox-claude:latest",   # derived from localhost/claudecode (make claude-image)
    command=["claude"],
    model_domains=["api.anthropic.com"],
    env={
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
    },
    token_env="CLAUDE_CODE_OAUTH_TOKEN",
    state_mount="/run/agentbox/claude",
    required_tools=["bash", "git", "node", "claude", "podman", "podman-compose"],
    approvals_off_args=["--dangerously-skip-permissions"],
)
ADAPTER.env["CLAUDE_CONFIG_DIR"] = ADAPTER.state_mount
