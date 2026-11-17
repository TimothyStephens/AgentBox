"""`ab` command line."""
import argparse
import os
import sys

from . import config, layout, presets, session
from .errors import AgentboxError
from .podman import Podman
from . import cli_deploy, cli_keys, cli_profile, cli_project, cli_session
from .cli_project import (CLEAR_FLAGS, SECURITY_OVERRIDES, SETTING_FLAGS, UNSETTABLE,  # noqa: F401
                          _add_identity_args, _add_project_setting_args, _add_project_unset_args, _add_registered,
                          _apply_project_settings, _check, _given, _grant_kw, _grants_more, _history_secrets,
                          _parse_swap, _print_cleared, _print_effective, _print_granted, _refuse_if_live, _render,
                          _settle_grant_exposure, _settle_history_secrets, _state_summary, _unset_keys,
                          _validate_and_save, cmd_init, cmd_project_accept_grant_exposure,
                          cmd_project_accept_history_secrets, cmd_project_add, cmd_project_check, cmd_project_clean,
                          cmd_project_ls, cmd_project_purge, cmd_project_rm, cmd_project_reset_state, cmd_project_set,
                          cmd_project_show, cmd_project_sync, cmd_selftest, precheck_project_settings)
from .cli_common import warn
from .cli_session import (_dur, _require_registered, _resolve_session, cmd_attach, cmd_gc, cmd_kill,  # noqa: F401
                          cmd_ls, cmd_preview, cmd_run)
from .cli_profile import (_find_item, _inbox_rows, _link_in, cmd_image_build, cmd_profile_dismiss,  # noqa: F401
                          cmd_profile_harvest, cmd_profile_install, cmd_profile_promote)
from .cli_keys import cmd_keys_ls, cmd_keys_rm, cmd_keys_rotate, cmd_keys_set  # noqa: F401


def cmd_screen(s, a):
    """`ab screen`: the tmux workspace (agentbox/screen.py), every agent pane entering the sandbox through
    `ab session start`."""
    from . import screen
    return screen.VERBS[a.sub](s, a, out=print, err=warn)


def parser(settings=None):
    raw = argparse.RawDescriptionHelpFormatter
    # `--preset`'s choices and help come from the presets that actually exist, so an unknown name is
    # refused at parse time with the full list, user presets included.
    presets_map = presets.load_presets(settings.presets_dir) if settings else dict(presets.BUILTIN)
    preset_choices = presets.names_in_order(presets_map)

    def ex(*lines):
        """Format an examples epilog block."""
        return "examples:\n" + "\n".join("  " + line for line in lines)

    def add(subparsers, name, summary, epilog=None):
        """A subparser whose one-line help also serves as its description, with an examples epilog that
        keeps its own line breaks."""
        return subparsers.add_parser(name, help=summary, description=summary, epilog=epilog, formatter_class=raw)

    def demote(subparsers, names):
        """Advanced verbs (or nouns): they still parse, but leave the listed block of `--help` for an
        `advanced:` section underneath, so the everyday ones read first. Returns [(name, summary)] in the
        order given, for `finish`."""
        rows = {}
        keep = []
        for act in subparsers._choices_actions:        # argparse's own list of what `--help` shows
            if act.dest in names:
                rows[act.dest] = act.help
            else:
                keep.append(act)
        subparsers._choices_actions[:] = keep           # the choice itself stays: it still parses
        return [(n, rows[n]) for n in names if n in rows]

    def finish(parser_obj, rows, what):
        """Put the demoted entries into the parser's epilog, above its examples."""
        if not rows:
            return
        import textwrap
        lines = [f"advanced {what} (rarely needed; `--help` on each):"]
        for n, h in rows:
            lines.append(textwrap.fill(h, width=100, initial_indent=f"  {n:<26} ", subsequent_indent=" " * 29))
        parser_obj.epilog = "\n".join(lines) + "\n\n" + (parser_obj.epilog or "")

    p = argparse.ArgumentParser(
        prog="ab", formatter_class=raw,
        description="agentbox: coding agents (Claude Code, OMP) in hardened rootless podman sandboxes, one "
                    "project per session.",
        epilog="Two nouns: `ab project` is the saved configuration (init, add, set, ...), `ab session` is the "
               "live run (start, attach, kill, ...). `ab screen` opens the tmux workspace. The advanced "
               "commands below are host setup and rare repairs. Full reference: docs/USAGE.md.\n\n"
               + ex("ab project init pod web              scaffold and register a new project",
                    "ab session start claude web          start a session",
                    "ab session ls                     list running sessions",
                    "ab project show web               a project's effective settings"))
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    proj_noun = sub.add_parser(
        "project", help="manage registered projects (the saved configuration)", formatter_class=raw,
        description="manage registered projects (the saved configuration). A project names a code grant, a "
                    "preset and any extra data grants.",
        epilog=ex("ab project add web ~/repos/web --preset build",
                  "ab project set web --egress open", "ab project show web", "ab project ls",
                  "ab project rm web"))
    proj = proj_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    cli_project.add_verbs(proj, add, ex, preset_choices, presets_map)
    finish(proj_noun, demote(proj, ['check', 'selftest', 'accept-grant-exposure', 'accept-history-secrets', 'reset-state', 'purge', 'clean']), "verbs")

    sess_noun = sub.add_parser(
        "session", help="the live runs: start, attach, list, preview, kill, gc", formatter_class=raw,
        description="the live runs. `start` creates one in the foreground (one per project); every other "
                    "verb names a project or a session id.",
        epilog=ex("ab session start claude web", "ab session attach web", "ab session ls",
                  "ab session preview web web:8000", "ab session kill web", "ab session gc"))
    sess = sess_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    cli_session.add_verbs(sess, add, ex)
    finish(sess_noun, demote(sess, ['preview', 'gc']), "verbs")

    scr_noun = sub.add_parser(
        "screen", help="tmux workspaces: one per project, agent panes and host shells in a layout",
        formatter_class=raw,
        description="tmux workspaces, one per project (`ab-<project>`): agent panes that enter the sandbox "
                    "through `ab session start` and host shells, laid out from ~/.config/agentbox/screen.toml. "
                    "Both projects must be registered; every pane opens in the project's root.",
        epilog=ex("ab screen start web", "ab screen start web web-review --layout wide", "ab screen ls",
                  "ab screen attach web", "ab screen kill web", "ab screen layouts",
                  "ab screen config > ~/.config/agentbox/screen.toml"))
    scr = scr_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    from . import screen as _screen
    _screen.add_verbs(scr, add, ex)
    for verb in _screen.VERBS:
        scr.choices[verb].set_defaults(fn=cmd_screen)
    finish(scr_noun, demote(scr, ["layouts", "config"]), "verbs")

    prof_p_noun = sub.add_parser(
        "profile", help="the host-owned agent profile: what every session loads, and the inbox it feeds",
        formatter_class=raw,
        description="The host-owned profile (~/.config/agentbox/<agent>/profile/) holds the skills, "
                    "sub-agents, commands and instructions every session loads, read-only, in every "
                    "project and under every preset. Sessions cannot write it; what they want to keep goes "
                    "to the project's inbox, which nothing loads. These commands review that queue.",
        epilog=ex("ab profile harvest", "ab profile harvest --agent claude --all",
                  "ab profile promote web/skills-deploy --as skills/deploy",
                  "ab profile dismiss web/notes.md"))
    prof_p = prof_p_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    cli_profile.add_verbs(prof_p, add, ex)

    img_p_noun = sub.add_parser(
        "image", help="your own image layer on top of an agent's harness image", formatter_class=raw,
        description="A personal layer for what you want in every session -- a language toolchain, a "
                    "linter, a private CLI -- without it going into agentbox's own images, and without a "
                    "session being able to put it there. Write a Containerfile in your profile "
                    "(~/.config/agentbox/<agent>/profile/Containerfile); it must begin `ARG BASE` then "
                    "`FROM ${BASE}`, and `ab image build` supplies BASE as the current harness image.\n\n"
                    "The built image is checked before it is tagged: USER, ENTRYPOINT, CMD and PATH must "
                    "match the harness image, and the image must still report the pinned agent version and "
                    "have git, rg, python3 and the podman client. A build that fails any of those is "
                    "discarded, so what you were using stays in place.\n\n"
                    "The tag covers the harness image ID and the Containerfile. When the harness image is "
                    "rebuilt your layer no longer matches it, and `ab session start` says `personal layer out of "
                    "date` and runs the plain harness image -- it never runs a layer built on an older "
                    "harness. Build it again and the next session picks it up.",
        epilog=ex("ab image build claude", "ab image build omp --force"))
    img_p = img_p_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    cli_profile.add_image_verbs(img_p, add, ex)

    ks_noun = sub.add_parser(
        "keys", help="model tokens and provider API keys", formatter_class=raw,
        description="manage model tokens and provider API keys. Keys live in ~/.config/agentbox/keys/ (0600), "
                    "outside every grant, and reach a session read-only as AB_KEY_<PROVIDER>.",
        epilog=ex("ab keys rotate claude", "ab keys set openai", "ab keys ls", "ab keys rm openai"))
    ks = ks_noun.add_subparsers(dest="sub", required=True, metavar="<verb>")
    cli_keys.add_verbs(ks, add, ex)

    cli_deploy.add_command(sub, add, ex)

    # Everyday nouns first; the host-setup and repair nouns still parse but list under `advanced`.
    finish(p, demote(sub, ["profile", "image", "keys"]), "commands")
    return p


GC_ON_FIRST_USE = {"cmd_run", "cmd_selftest", "cmd_ls"}


def gc_once_per_boot(s):
    """The design's "gc runs automatically on the launcher's first use after boot": a marker named after the
    kernel's boot id, in the run dir (review 2026-10-01, doc claims)."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot = f.read().strip()
    except OSError:
        return False
    marker = os.path.join(s.run_dir, f".gc-{boot}")
    if os.path.exists(marker):
        return False
    # The sweep is maintenance, and the command it rides on is not: a `podman images` that takes longer
    # than the podman timeout (a 1465-image store, 12 Nov: 3.5 min cold) must not fail the session start
    # that happened to come first. Report it, name the by-hand command, and mark the boot as done anyway,
    # so the next command does not pay the same wait again.
    try:
        removed = session.gc(s, Podman(s.podman), warn=warn)
    except AgentboxError as e:
        warn(f"ab: first use since boot: the storage sweep did not finish ({e}); this session starts "
             f"without it. `ab session gc` runs it by hand, with time.")
        removed = []
    if removed:
        warn(f"ab: first use since boot: removed what dead sessions left: {' '.join(removed)}")
    os.makedirs(s.run_dir, mode=0o700, exist_ok=True)
    with open(marker, "w"):
        pass
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `ab session start ... -- <args>` passes everything after the first `--` to the agent/shell. Split it off
    # before argparse so the positionals and flags on the left parse normally (a REMAINDER would swallow --shell).
    passthrough = []
    if argv[:2] == ["session", "start"] and "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    try:
        s = config.load()
        built = parser(s)          # the parser reads the presets, so a broken preset file reports cleanly
    except AgentboxError as e:
        print(f"ab: {e}", file=sys.stderr)
        return 2
    try:
        layout.migrate(s, warn)    # a flat data dir moves into projects/ once (layout.py)
    except OSError as e:
        print(f"ab: could not move the data directory's project state into {s.projects_dir}: {e}",
              file=sys.stderr)
        return 2
    a = built.parse_args(argv)
    if getattr(a, "fn", None) is cmd_run:
        a.passthrough = passthrough
    try:
        if a.fn.__name__ in GC_ON_FIRST_USE:
            gc_once_per_boot(s)
        rc = a.fn(s, a)
        return rc or 0
    except AgentboxError as e:
        print(f"ab: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
