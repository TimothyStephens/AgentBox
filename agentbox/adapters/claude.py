"""Claude Code adapter.

State: the whole CLAUDE_CONFIG_DIR is per project and persistent. The host's ~/.claude is never used.

What the agent LOADS comes from the host-owned profile (`profile.py`): `skills/`, `agents/` and
`commands/` are mounted read-only from a snapshot of `~/.config/agentbox/claude/profile/`, and CLAUDE.md is
generated per session (the sandbox notes, then the profile's CLAUDE.md) and mounted read-only too. So a
session cannot change what later sessions load, and the same profile loads under every preset -- lockdown
included, where a repo's own `.claude` is covered.

settings.json is the exception: Claude Code writes it itself (it records
`skipDangerousModePermissionPrompt` on first start with approvals off), so a read-only one would fail at
every launch. It is copied from the profile into the state dir at every start instead, which keeps the host
profile authoritative without pretending the file is immutable.

Plugins (9 Nov) come the same way the profile does: `ab profile install claude` clones each marketplace
the profile's `plugins.toml` declares, at its sha, into `~/.config/agentbox/claude/plugins/<marketplace>`;
a session mounts that tree READ-ONLY at `/run/agentbox/claude/marketplaces` and writes two keys into the
copied settings.json -- `extraKnownMarketplaces` (source `directory`, that path) and `enabledPlugins`.
Claude Code registers the marketplace and enables the plugin at startup, reads the plugin from the mounted
directory in place, and keeps its own records (`plugins/known_marketplaces.json`, `installed_plugins.json`)
in the per-project state, where it can write. The probe that settled this is in decisions.md.
"""
import json
import os
import re

from ..errors import AgentboxError
from . import Adapter, SessionSetup

MANAGED_DIR = "/etc/claude-code"
# The installed plugin tree's path in a session, and the state-dir name under it (state_mirrors maps the
# config dir onto the state dir, so the mount point is `<state>/marketplaces`).
MARKETPLACES_DST = "/run/agentbox/claude/marketplaces"
MARKETPLACES_NAME = "marketplaces"
# Per-project state directories that used to be where skills and sub-agents accumulated: whatever a session
# wrote there is moved to the project's inbox once, since those paths are read-only mounts now (profile.py).
MIGRATE_TO_INBOX = ("skills", "agents", "CLAUDE.md")
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
    def seed_state(self, settings, state_dir, sandbox_notes, profile_on=True):
        """Prepare the per-project state dir. Nothing is loaded from here any more except settings.json:
        skills, sub-agents, commands and CLAUDE.md come from the profile as read-only mounts.

        settings.json is a profile item too -- copied rather than mounted only because Claude Code writes to
        it -- so with the profile off it is not copied either, and Claude starts on its own defaults."""
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        if not profile_on:
            return
        from .. import profile
        profile.copy_into_state(settings, self, state_dir)

    def extra_profile_notes(self, inbox, kept=True):
        """Claude's memory shortcuts: one of the two targets still works, and the other now fails."""
        if not kept:
            return []
        return ["- Memory shortcuts: a `#` note aimed at the PROJECT's own `CLAUDE.md` works normally (it "
                "is in the grant, and the end-of-session scan reports it, so say in the commit what you "
                "changed). One aimed at the user-level `CLAUDE.md` fails: that file is generated per "
                f"session and read-only. Put such notes in `{inbox}` instead."]

    def redundant_state_copy(self, settings, name, path):
        """A state `CLAUDE.md` is a copy of the pre-profile seed if, with the injected sandbox block taken
        out, it is byte-identical to the profile's own CLAUDE.md (where that seed was moved).

        `seed_state` used to write exactly that file -- the block plus the seed -- so on most projects it
        holds nothing a session or the user added, and queueing it for review would be asking someone to
        read back their own seed. Anything else, including an empty profile CLAUDE.md against a non-empty
        state one, is content and goes to the inbox."""
        if name != "CLAUDE.md":
            return False
        own = _NOTES_BLOCK.sub("", _read_text(path)).strip()
        seed = _read_text(os.path.join(settings.profile_src(self.name), "CLAUDE.md")).strip()
        return own == seed

    def session_instructions(self, settings, sandbox_notes, profile_on=True):
        """The CLAUDE.md the session gets: the sandbox description first, then the profile's own CLAUDE.md.

        The notes lead so the agent reads its environment before anything else (PERSONALIZATION.md §4), and
        the whole file is generated per session rather than edited in place, so there is no stale copy and
        nothing to strip: the marker block is kept only to say out loud that the text is regenerated.

        With the profile off the notes block is the whole file: the agent still has to be told what sandbox
        it is in, which is agentbox's own text, not the user's."""
        block = f"{NOTES_BEGIN}\n{sandbox_notes.rstrip()}\n{NOTES_END}\n"
        if not profile_on:
            return block
        md = os.path.join(settings.profile_src(self.name), "CLAUDE.md")
        own = _NOTES_BLOCK.sub("", _read_text(md)).lstrip()
        return block + ("\n" + own if own.strip() else "")

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
        # Plugins follow the profile, and are off in lockdown as OMP's are (decisions.md, 15 Oct): lockdown
        # is "nothing runs unasked", and a plugin can carry hooks and MCP servers.
        on = not sess.profile_off[0] and approvals_off
        mounts, keys = plugin_setup(sess.settings, sess.warn, sess.image_labels(), plugins=on)
        if mounts:
            # The mount point is made here, under the state dir, so the runtime does not create it root-owned
            # in the middle of the agent's config dir (session._own_mount_point says why that matters).
            os.makedirs(os.path.join(sess.state_dir, MARKETPLACES_NAME), mode=0o700, exist_ok=True)
            setup.mounts += [(src, dst, True) for src, dst in mounts]
        if keys:
            # Merged INTO the profile's own values for these two keys, not over them: a marketplace the
            # profile's settings.json declares itself stays declared.
            path = os.path.join(sess.state_dir, "settings.json")
            had = _read_json(path)
            had = had if isinstance(had, dict) else {}
            _merge_settings(path, {k: {**(had.get(k) if isinstance(had.get(k), dict) else {}), **v}
                                   for k, v in keys.items()})
        if not approvals_off:
            path = os.path.join(sess.settings.session_run_dir(sess.name), "claude-managed-settings.json")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(MANAGED_APPROVALS_ON, f, indent=2)
            setup.mount_root = MANAGED_DIR
            setup.mounts.append((path, f"{MANAGED_DIR}/managed-settings.json", True))
        return setup


def plugin_setup(settings, warn, image_labels, plugins=True):
    """([(host src, container dst)], {settings keys}) for the installed plugin tree. ([], {}) when there is
    nothing to load, which is a normal state and not an error.

    Both come from the same declaration, read fresh: the tree is mounted whole, and a marketplace is
    declared to Claude Code only when the profile names it AND the tree holds its catalog. A declared
    marketplace the tree lacks is said out loud with the command that fixes it, and nothing is declared for
    it -- an `enabledPlugins` entry pointing at a directory that is not there is a startup error Claude
    Code would report inside the sandbox, to nobody.

    `plugins=False` (`ab session start --no-profile`, `profile = "off"`, or lockdown) returns nothing and reads
    nothing, so the profile's own settings.json is what the session gets, unchanged."""
    from .. import installer, plugins as plugindecl   # local: installer imports session, which imports this package
    if not plugins:
        return [], {}
    try:
        declared = plugindecl.load(os.path.join(settings.profile_src("claude"), "plugins.toml"),
                                   plugindecl.sections_for("claude"))
    except AgentboxError as e:
        warn(f"agentbox: {e}; no plugins are loaded this session")   # a broken file must not stop a session
        return [], {}
    if not declared:
        return [], {}
    tree = installer.tree_dir(settings, "claude")
    if os.path.islink(tree):
        raise AgentboxError(f"{tree} is a symlink; refusing to mount it")
    if not os.path.isdir(tree):
        warn(f"agentbox: no plugins installed ({tree} does not exist); Claude Code runs without "
             f"{', '.join(e.id for e in declared)}. `ab profile install claude` installs what the profile's "
             "plugins.toml declares")
        return [], {}
    for line in installer.staleness(settings, "claude", image_labels, declared=declared):
        warn(f"agentbox: {line}")
    marketplaces, enabled, missing = {}, {}, []
    for e in declared:
        manifest = os.path.join(tree, e.marketplace, installer.CLAUDE_MANIFEST)
        if os.path.islink(manifest) or not os.path.isfile(manifest):
            missing.append(e.id)
            continue
        marketplaces[e.marketplace] = {"source": {"source": "directory",
                                                  "path": f"{MARKETPLACES_DST}/{e.marketplace}"}}
        enabled[e.id] = True
    if missing:
        warn(f"agentbox: the installed tree has no catalog for {', '.join(missing)}; not enabled this "
             "session. `ab profile install claude` rebuilds the tree from plugins.toml")
    if not enabled:
        return [], {}
    return [(tree, MARKETPLACES_DST)], {"extraKnownMarketplaces": marketplaces, "enabledPlugins": enabled}


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
    # The profile's load paths. They are under CLAUDE_CONFIG_DIR, not ~/.claude: the adapter points
    # CLAUDE_CONFIG_DIR at the state mount, and `commands/` seeded there has been the working mechanism
    # since step 1, so this is where Claude Code reads a user-level skill, sub-agent and command from in an
    # agentbox session. CLAUDE.md is generated per session (notes + the profile's own) and mounted here too.
    profile_mounts={"skills": "/run/agentbox/claude/skills",
                    "agents": "/run/agentbox/claude/agents",
                    "commands": "/run/agentbox/claude/commands"},
    profile_copies=("settings.json",),
    instructions_name="CLAUDE.md",
    inbox_mount="/run/agentbox/claude/inbox",
    state_mirrors={"/run/agentbox/claude": ""},     # the config dir IS the per-project state dir
    migrate_to_inbox=MIGRATE_TO_INBOX,
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
