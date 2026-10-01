"""Claude Code adapter.

State: the whole CLAUDE_CONFIG_DIR is per project and persistent. settings.json, CLAUDE.md and
commands/ are copied in from ~/.config/agentbox/claude/ at every start; nothing else is touched.
The host's ~/.claude is never used.
"""
import json
import os
import shutil

from . import Adapter, SessionSetup

SEEDED = ("settings.json", "CLAUDE.md", "commands")
MANAGED_DIR = "/etc/claude-code"
# With approvals on (lockdown), Claude Code's managed policy: bypass mode cannot be entered, and only managed
# hooks and permission rules apply, so a repo's own .claude/settings.json hooks and allow rules do nothing
# (review 2026-10-01 #2). Key names checked against the image's Claude Code build.
MANAGED_APPROVALS_ON = {
    "permissions": {"disableBypassPermissionsMode": "disable"},
    "allowManagedHooksOnly": True,
    "allowManagedPermissionRulesOnly": True,
}


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

    def prepare_session(self, sess, sandbox_notes):
        approvals_off = sess.project.value("approvals") == "off"
        _mark_onboarded(os.path.join(sess.state_dir, ".claude.json"), sess.project.root, trusted=approvals_off)
        setup = SessionSetup()
        if not approvals_off:
            path = os.path.join(sess.settings.session_run_dir(sess.name), "claude-managed-settings.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(MANAGED_APPROVALS_ON, f, indent=2)
            setup.mount_root = MANAGED_DIR
            setup.mounts.append((path, f"{MANAGED_DIR}/managed-settings.json", True))
        return setup


def _mark_onboarded(path, project_root, trusted):
    """Skip first-run onboarding. Folder trust and bypass mode are pre-accepted only when approvals are off;
    with approvals on Claude asks, and a repo's settings and hooks wait for that answer."""
    doc = {}
    if os.path.isfile(path):
        try:
            with open(path) as f:
                doc = json.load(f)
        except ValueError:
            doc = {}
    doc["hasCompletedOnboarding"] = True
    doc["bypassPermissionsModeAccepted"] = trusted
    projects = doc.setdefault("projects", {})
    proj = projects.setdefault(project_root, {})
    proj["hasTrustDialogAccepted"] = trusted
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
