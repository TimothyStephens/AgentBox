"""Claude Code adapter.

State: the whole CLAUDE_CONFIG_DIR is per project and persistent. settings.json, CLAUDE.md and
commands/ are copied in from ~/.config/agentbox/claude/ at every start; nothing else is touched.
The host's ~/.claude is never used.
"""
import json
import os
import re
import shutil

from . import Adapter, SessionSetup

SEEDED = ("settings.json", "CLAUDE.md", "commands")
MANAGED_DIR = "/etc/claude-code"
# The injected sandbox description is wrapped in these markers and the old block is stripped before the new
# one is written, so it is refreshed each session instead of stacking up (audit 2026-10-15 #2).
NOTES_BEGIN = "<!-- agentbox:sandbox-notes — regenerated each session, do not edit -->"
NOTES_END = "<!-- /agentbox:sandbox-notes -->"
_NOTES_BLOCK = re.compile(re.escape(NOTES_BEGIN) + r".*?" + re.escape(NOTES_END) + r"\n*", re.DOTALL)


def _unlink_if_link(path):
    """Drop a symlink (or any non-regular file) a previous session may have left at a state path, so the read
    or write below neither follows it to a host file the user owns (copying it into the next session, or
    truncating it) nor is fooled by it. A real regular file is kept — it is the agent's own persisted state.
    Session prepare runs before the agent container starts, so nothing races this (audit 2026-10-15 #1)."""
    if os.path.islink(path) or (os.path.lexists(path) and not os.path.isfile(path)):
        os.unlink(path)


def _read_text(path):
    _unlink_if_link(path)
    if os.path.isfile(path):
        with open(path) as f:
            return f.read()
    return ""


def _read_json(path):
    text = _read_text(path)
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _write_atomic(path, text):
    """Write `text` to `path` via a .tmp sibling, following a symlink at neither (O_EXCL|O_NOFOLLOW)."""
    _unlink_if_link(path)
    tmp = path + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
# With approvals on (lockdown), Claude Code's managed policy: bypass mode cannot be entered, and only managed
# hooks and permission rules apply, so a repo's own .claude/settings.json hooks and allow rules do nothing
# (review 2026-10-01 #2). Key names checked against the image's Claude Code build.
MANAGED_APPROVALS_ON = {
    "permissions": {"disableBypassPermissionsMode": "disable"},
    "allowManagedHooksOnly": True,
    "allowManagedPermissionRulesOnly": True,
}


class ClaudeAdapter(Adapter):
    def seed_state(self, settings, state_dir, sandbox_notes):
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        seed = settings.seed_dir(self.name)
        for item in SEEDED:
            src, dst = os.path.join(seed, item), os.path.join(state_dir, item)
            if not os.path.lexists(src):
                continue            # no seed for this item: keep the agent's persisted file, do not wipe it
            if os.path.isdir(dst) and not os.path.islink(dst):
                shutil.rmtree(dst)
            elif os.path.lexists(dst):
                os.unlink(dst)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False)
            elif os.path.isfile(src):
                shutil.copyfile(src, dst)
        # The sandbox description leads, so the agent reads its environment before the user's own CLAUDE.md
        # (PERSONALIZATION.md §4). Strip any previous injected block first so it is refreshed, not stacked.
        md = os.path.join(state_dir, "CLAUDE.md")
        user_md = _NOTES_BLOCK.sub("", _read_text(md)).lstrip()
        block = f"{NOTES_BEGIN}\n{sandbox_notes.rstrip()}\n{NOTES_END}\n"
        _write_atomic(md, block + ("\n" + user_md if user_md.strip() else ""))

    def prepare_session(self, sess, sandbox_notes):
        approvals_off = sess.project.value("approvals") == "off"
        _mark_onboarded(os.path.join(sess.state_dir, ".claude.json"), sess.project.root, trusted=approvals_off)
        if approvals_off:
            # Running with --dangerously-skip-permissions makes Claude Code write
            # skipDangerousModePermissionPrompt:true into settings.json on first start. Establish it now (before
            # the end-of-session scan's baseline is taken) so that write is part of the template's initial
            # settings, not reported as a change the user made on a fresh project (7 Oct).
            _merge_settings(os.path.join(sess.state_dir, "settings.json"), {"skipDangerousModePermissionPrompt": True})
        setup = SessionSetup()
        if not approvals_off:
            path = os.path.join(sess.settings.session_run_dir(sess.name), "claude-managed-settings.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(MANAGED_APPROVALS_ON, f, indent=2)
            setup.mount_root = MANAGED_DIR
            setup.mounts.append((path, f"{MANAGED_DIR}/managed-settings.json", True))
        return setup


def _merge_settings(path, defaults):
    """Ensure settings.json has `defaults` (merged over whatever a user seed or a prior session left), written
    the way Claude Code writes it (2-space JSON, no trailing newline), so Claude finds the keys already set and
    does not rewrite the file during the session."""
    existed = os.path.isfile(path) and not os.path.islink(path)
    doc = _read_json(path)
    if not isinstance(doc, dict):
        return
    if existed and all(doc.get(k) == v for k, v in defaults.items()):
        return
    doc.update(defaults)
    _write_atomic(path, json.dumps(doc, indent=2))


def _mark_onboarded(path, project_root, trusted):
    """Skip first-run onboarding. Folder trust and bypass mode are pre-accepted only when approvals are off;
    with approvals on Claude asks, and a repo's settings and hooks wait for that answer."""
    doc = _read_json(path)
    if not isinstance(doc, dict):
        doc = {}
    doc["hasCompletedOnboarding"] = True
    doc["bypassPermissionsModeAccepted"] = trusted
    projects = doc.setdefault("projects", {})
    proj = projects.setdefault(project_root, {})
    proj["hasTrustDialogAccepted"] = trusted
    proj.setdefault("hasCompletedProjectOnboarding", True)
    _write_atomic(path, json.dumps(doc, indent=2))


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
