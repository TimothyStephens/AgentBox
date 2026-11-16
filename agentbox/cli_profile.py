"""`ab profile` and `ab image`: the host-owned profile, the inbox it feeds, and the personal image layer (split out
of cli.py; the parser scaffolding stays there)."""
import os

from . import adapters, registry
from .cli_common import warn
from .errors import AgentboxError
from .podman import Podman


# --- the host-owned profile and the inbox ------------------------------------------------------------

def _link_in(path):
    """The first symlink (or other non-regular entry) in an inbox item, the item itself included, or None.

    An inbox item is agent-written. A copy into the profile follows links, and the profile is loaded by every
    session of every project, so a planted `key -> ~/.ssh/id_ed25519` would be published by `promote`
    (item 4). Walked without following links, as profile._walk does for the profile itself."""
    if os.path.islink(path):
        return path
    if os.path.isfile(path):
        return None
    if not os.path.isdir(path):
        return path
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for n in sorted(dirnames) + sorted(filenames):
            full = os.path.join(dirpath, n)
            if os.path.islink(full) or not (os.path.isdir(full) or os.path.isfile(full)):
                return full
    return None


def _inbox_rows(s, agent):
    """Every inbox item of one agent, across all projects, newest first, with its provenance."""
    from . import profile
    reg = registry.load(s.registry_path)
    rows = []
    for project, inbox in profile.all_inboxes(s, agent):
        for name in profile.items(inbox):
            doc = profile.read_sidecar(inbox, name)
            rows.append({"project": project, "name": name, "path": os.path.join(inbox, name),
                         "registered": project in reg, "reviewed": bool(doc.get("reviewed")),
                         "added": doc.get("added", ""), "egress": doc.get("egress", ""),
                         "preset": doc.get("preset", ""), "session": doc.get("session", ""),
                         "content": profile.content_id(os.path.join(inbox, name)),
                         "link": _link_in(os.path.join(inbox, name))})
    return sorted(rows, key=lambda r: (r["added"], r["project"], r["name"]), reverse=True)


def cmd_image_build(s, a):
    from . import userimage
    from .podman import Podman
    userimage.build(s, Podman(s.podman), a.agent, force=a.force)
    return 0


def cmd_profile_harvest(s, a):
    """Read-only: what sessions have put in their inboxes, and nothing else.

    An inbox item is reviewed out of context, often days later, so each line carries where it came from --
    and `egress open` is marked, because an item written by a session that could reach the whole internet
    deserves a closer read before it becomes something every later session loads."""
    from . import profile
    agents = [a.agent] if a.agent else list(adapters.NAMES)
    total = 0
    for agent in agents:
        rows = [r for r in _inbox_rows(s, agent) if a.all or not r["reviewed"]]
        src = s.profile_src(agent)
        print(f"profile {agent}: {src}" + (f"  (commit {profile.commit_id(src)})"
                                           if profile.commit_id(src) else "  (no git repo yet)"))
        if not rows:
            print("  inbox: nothing unreviewed\n")
            continue
        total += len(rows)
        groups = {}
        for r in rows:
            groups.setdefault((r["name"], r["content"]), []).append(r)
        by_name = {}
        for r in rows:
            by_name.setdefault(r["name"], set()).add(r["content"])
        print(f"  {len(rows)} unreviewed item(s) in {len({r['project'] for r in rows})} project(s):")
        for (name, content), same in sorted(groups.items()):
            where = ", ".join(f"{r['project']}{'' if r['registered'] else ' (project removed)'}"
                              for r in same)
            flags = []
            if any(r["egress"] == "open" for r in same):
                flags.append("FROM AN OPEN-EGRESS SESSION")
            if len(same) > 1:
                flags.append(f"identical in {len(same)} projects")
            if len(by_name.get(name, ())) > 1:
                flags.append("same name, DIFFERENT content elsewhere")
            if any(r["reviewed"] for r in same):
                flags.append("reviewed")
            if any(r["link"] for r in same):
                flags.append("HOLDS A SYMLINK or special file (promote refuses it): "
                             + next(r["link"] for r in same if r["link"]))
            print(f"    {name}  [{content}]  {where}")
            print(f"      added {same[0]['added'] or '?'}  preset {same[0]['preset'] or '?'}  "
                  f"egress {same[0]['egress'] or '?'}  session {same[0]['session'] or '?'}")
            if flags:
                print(f"      ** {'; '.join(flags)}")
            print(f"      promote: ab profile promote {same[0]['project']}/{name} --as skills/{name}")
        print()
    if total:
        print("Nothing above is loaded by any session. `promote` copies an item into the profile and commits "
              "it; `dismiss` marks it reviewed and leaves it where it is.")
    return 0


def _find_item(s, agent, spec):
    rows = [r for r in _inbox_rows(s, agent)
            if spec in (f"{r['project']}/{r['name']}", r["name"])]
    if not rows:
        raise AgentboxError(f"no inbox item {spec!r} for {agent}; `ab profile harvest` lists them")
    if len({(r["project"], r["name"]) for r in rows}) > 1:
        where = ", ".join(f"{r['project']}/{r['name']}" for r in rows)
        raise AgentboxError(f"{spec!r} is ambiguous: {where}. Name it as <project>/<item>.")
    return rows[0]


def cmd_profile_promote(s, a):
    """Copy an inbox item into the host-owned profile and commit it, with where it came from in the message.

    The destination is explicit (`--as`) unless the item is plainly a skill directory: promoting writes into
    the one thing every session loads, in every project, so guessing where it belongs is not good enough."""
    import shutil
    from . import profile
    row = _find_item(s, a.agent, a.item)
    src, prof = row["path"], s.profile_src(a.agent)
    dest = a.as_path
    if not dest:
        if os.path.isdir(src) and os.path.isfile(os.path.join(src, "SKILL.md")):
            dest = f"skills/{row['name']}"
        else:
            raise AgentboxError(f"say where it goes in the profile: --as skills/<name> (one of "
                                f"{', '.join(profile.ITEMS)}). {src} is not plainly a skill directory.")
    if dest.startswith("/") or ".." in dest.split("/"):
        raise AgentboxError(f"--as {dest!r} must be a relative path inside the profile")
    if row["link"]:                   # refused before anything is copied: the copy would follow it (item 4)
        raise profile._link_refused(row["link"])
    dst = os.path.join(prof, dest)
    if os.path.lexists(dst):
        raise AgentboxError(f"{dst} already exists; remove it or choose another --as")
    # The profile directory first: ensure_repo makes a repository only of a directory that exists, so on a
    # first promotion the copy used to land in a plain directory and the commit then failed (item 47).
    os.makedirs(prof, mode=0o700, exist_ok=True)
    profile.ensure_repo(prof, out=print)
    os.makedirs(os.path.dirname(dst) or prof, mode=0o700, exist_ok=True)
    doc = profile.read_sidecar(s.inbox_dir(row["project"], a.agent), row["name"])
    msg = (f"profile: add {dest} (promoted from {row['project']}/{row['name']})\n\n"
           f"project: {row['project']}\nagent: {a.agent}\nsession: {doc.get('session', '?')}\n"
           f"added: {doc.get('added', '?')}\npreset: {doc.get('preset', '?')}\n"
           f"egress: {doc.get('egress', '?')}\n")
    try:
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=False,
                            ignore=shutil.ignore_patterns("*" + profile.SIDECAR))
        else:
            shutil.copyfile(src, dst)
        profile._git(prof, "add", "-A")
        profile._git(prof, "-c", "user.name=agentbox", "-c", "user.email=agentbox@localhost",
                     "commit", "-qm", msg)
    except BaseException:
        # An uncommitted copy would be loaded by every session with no history behind it (the profile reads
        # as dirty). Take it back out, and unstage it, so the profile is as it was.
        if os.path.isdir(dst) and not os.path.islink(dst):
            shutil.rmtree(dst, ignore_errors=True)
        elif os.path.lexists(dst):
            os.unlink(dst)
        profile._git(prof, "reset", "-q", "--", dest, check=False)
        raise
    doc.update({"reviewed": True, "promoted_to": dest, "promoted_commit": profile.commit_id(prof)})
    profile.write_sidecar(s.inbox_dir(row["project"], a.agent), row["name"], doc)
    print(f"promoted {row['project']}/{row['name']} -> {dst}")
    print(f"  profile commit {profile.commit_id(prof)}; the next session of every project loads it")
    print(f"  the inbox copy is kept and marked reviewed ({row['path']})")
    return 0


def cmd_profile_install(s, a):
    """`ab profile install <agent>`. The declaration is the profile's; what is installed is agentbox's.

    Not a session: the profile is not a grant, so there is no project, no lock, no scan and no gate --
    just one hardened container behind the same sealed networks and squid a session gets, with the tree
    mounted at the path a session will see it at (installer.py says why that matters)."""
    from . import installer
    doc = installer.install(s, a.agent, podman=Podman(s.podman), warn=warn, dry_run=a.dry_run)
    if a.dry_run:
        print(f"would install {len(doc['entries'])} entry(ies); the container would run:\n")
        print(doc["script"])
        return 0
    tree = installer.tree_dir(s, a.agent)
    print(f"installed into {tree}")
    print(f"  against {doc['image']['ref']} ({doc['image'].get('cli_version') or 'version unknown'}, "
          f"node {doc['image'].get('node_major') or '?'})")
    for kind in ("npm", "marketplace"):
        for item in doc["state"].get(kind, []) or []:
            detail = item.get("version") or (item.get("commit") or "")[:12]
            if item.get("plugins"):
                detail += f" ({', '.join(item['plugins'])})"
            print(f"  {kind:<12} {item.get('name') or item.get('id')} {detail}".rstrip())
    print(f"  lockfile {os.path.join(tree, installer.LOCKFILE)}")
    print("Sessions mount this read-only. `ab profile install` again after editing plugins.toml.")
    return 0


def cmd_profile_dismiss(s, a):
    from . import profile
    row = _find_item(s, a.agent, a.item)
    inbox = s.inbox_dir(row["project"], a.agent)
    doc = profile.read_sidecar(inbox, row["name"])
    doc.update({"reviewed": True, "dismissed": True})
    profile.write_sidecar(inbox, row["name"], doc)
    print(f"dismissed {row['project']}/{row['name']} (kept at {row['path']}; nothing was deleted)")
    return 0


def add_verbs(prof_p, add, ex):
    """The `ab profile` subparsers, built with the CLI's own `add` and `ex` helpers (as screen.add_verbs is)."""
    x = add(prof_p, "harvest", "list what sessions have put in their inboxes, unreviewed first (read-only)",
            ex("ab profile harvest", "ab profile harvest --agent claude --all"))
    x.add_argument("--agent", choices=adapters.NAMES, help="only this agent's profile (default: both)")
    x.add_argument("--all", action="store_true", help="include items already reviewed")
    x.set_defaults(fn=cmd_profile_harvest)
    x = add(prof_p, "promote", "copy an inbox item into the profile and commit it, with its provenance in "
                               "the commit message",
            ex("ab profile promote web/skills-deploy --as skills/deploy"))
    x.add_argument("item", help="the item, as <project>/<name> (or just <name> when unambiguous)")
    x.add_argument("--agent", default="claude", choices=adapters.NAMES, help="whose profile (default: claude)")
    x.add_argument("--as", dest="as_path", metavar="PATH",
                   help="where it goes inside the profile, e.g. skills/deploy (required unless the item is "
                        "plainly a skill directory)")
    x.set_defaults(fn=cmd_profile_promote)
    x = add(prof_p, "install", "install the plugins the profile declares, in a sandbox, into a tree "
                               "sessions mount read-only",
            ex("ab profile install omp", "ab profile install claude", "ab profile install omp --dry-run"))
    x.add_argument("agent", choices=adapters.NAMES, help="whose plugins.toml to install")
    x.add_argument("--dry-run", action="store_true",
                   help="print what would be installed and the script that would run; change nothing")
    x.set_defaults(fn=cmd_profile_install)
    x = add(prof_p, "dismiss", "mark an inbox item reviewed and leave it where it is (deletes nothing)",
            ex("ab profile dismiss web/notes.md"))
    x.add_argument("item", help="the item, as <project>/<name> (or just <name> when unambiguous)")
    x.add_argument("--agent", default="claude", choices=adapters.NAMES, help="whose profile (default: claude)")
    x.set_defaults(fn=cmd_profile_dismiss)


def add_image_verbs(img_p, add, ex):
    """The `ab image` subparsers, built the same way."""
    x = add(img_p, "build", "build your profile's Containerfile on top of the agent's harness image",
            ex("ab image build claude", "ab image build omp --force"))
    x.add_argument("agent", choices=adapters.NAMES, help="whose harness image to build on")
    x.add_argument("--force", action="store_true",
                   help="rebuild even when an image for this harness and Containerfile already exists")
    x.set_defaults(fn=cmd_image_build)
