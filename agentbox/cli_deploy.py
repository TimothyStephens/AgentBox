"""`ab deploy <project>`: the live stack from a reviewed commit (deploy.py; docs/deploy-proposal.md).

Built in steps (decisions.md, 17 Nov 2026). What runs today is `--check`: everything decided before podman --
the commit, the clean tree, the `.env` names, the review -- printed, with nothing started or recorded.
"""
import os
import sys

from . import deploy, egress, session
from .cli_common import warn
from .errors import AgentboxError

NOT_BUILT = ("the deploy itself (the deploy-mode gate and the runner) is not built yet; `ab deploy {p} --check` "
             "shows what would be deployed and whether it may be")


def cmd_deploy(s, a):
    if os.environ.get("AGENTBOX_SESSION"):
        raise AgentboxError("`ab deploy` runs on the host, not inside a session: it is the user's review of "
                            "what the agent wrote, and it starts the live stack")
    p = session.load_project(s, a.project)
    root = p.root
    lan = deploy.parse_lan(a.lan, set(s.host_addrs) | set(egress.host_addresses())) if a.lan else None
    c = deploy.check(s, p.name, root, ref=a.commit or "HEAD")
    problems = []
    if not c["has_compose"]:
        problems.append(f"commit {c['commit'][:12]} has no compose file at the root")
    if c["dirty"]:
        problems.append("uncommitted changes to tracked files (a deploy is a commit; commit or stash them): "
                        + ", ".join(c["dirty"][:10]) + (" …" if len(c["dirty"]) > 10 else ""))
    if c["missing"]:
        problems.append(f"{os.path.join(root, '.env')} lacks variables .env.dev defines: "
                        + ", ".join(c["missing"]))
    print(f"ab deploy {p.name}: commit {c['commit']}"
          + (f", on {lan[0]}:{lan[1]}" if lan else "")
          + (f", compose project {p.compose_project}" if p.compose_project else ""))
    print(c["review"])
    if c["extra"]:
        print(f"\nnote: .env defines variables .env.dev does not: {', '.join(c['extra'])}")
    if problems:
        print("\nrefused:\n" + "\n".join(f"  - {x}" for x in problems), file=sys.stderr)
        return 2
    if a.check:
        print("\ncheck passed; nothing was started or recorded.")
        return 0
    raise AgentboxError(NOT_BUILT.format(p=p.name))


def add_command(sub, add, ex):
    x = add(sub, "deploy", "start a project's live stack from a reviewed commit (host only)",
            ex("ab deploy filelab --check", "ab deploy filelab --lan 10.0.0.103:8095",
               "ab deploy filelab --commit 3f2a9c1 --check"))
    x.add_argument("project", help="the registered project whose live stack to deploy")
    x.add_argument("--lan", metavar="ADDR:PORT",
                   help="the address of this host and the port the stack publishes on; nothing else is "
                        "published")
    x.add_argument("--commit", metavar="REF",
                   help="deploy this commit instead of HEAD (a redeploy or a rollback); the working tree "
                        "need not be clean then, since nothing is taken from it but the real .env")
    x.add_argument("--check", action="store_true",
                   help="show the review and run every check, then stop: start nothing, record nothing")
    x.set_defaults(fn=cmd_deploy)
