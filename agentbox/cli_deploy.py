"""`ab deploy <project>`: the live stack from a reviewed commit (deploy.py, deployrun.py; design.md).

Order: host only; one deploy per project (flock); every check (the commit, the clean tree, the `.env` names, the
LAN address, the deploy binds, objects of the compose project no deploy made); the review; the confirmation;
then the run (deployrun.Deploy), its record, what is running, and the lingering warnings. `--check` stops after
the review and starts or records nothing.
"""
import os
import sys

from . import deploy, deployrun, egress, session
from .cli_common import warn
from .errors import AgentboxError
from .podman import Podman


def confirm(a, c, project, lan_addr, stdin=None):
    """`Deploy <commit> of <project> to <lan>? [y/N]` on a terminal. `--yes` only for a script (stdin not a
    terminal) redeploying the commit deployed last; anything else is refused, saying why."""
    stdin = stdin or sys.stdin
    tty = bool(getattr(stdin, "isatty", lambda: False)())
    same = bool(c["last"]) and c["last"] == c["commit"]
    if a.yes:
        if tty:
            raise AgentboxError("--yes is for scripts (stdin not a terminal); on a terminal, answer the prompt")
        if not same:
            raise AgentboxError(f"--yes only redeploys the commit deployed last ({(c['last'] or 'none')[:12]}); "
                                f"{c['commit'][:12]} has changes to review: run `ab deploy {project}` on a "
                                "terminal and confirm")
        return True
    if not tty:
        raise AgentboxError("no terminal to confirm on: run `ab deploy` on a terminal (--yes is only for "
                            "redeploying the commit deployed last, from a script)")
    print(f"Deploy {c['commit'][:12]} of {project} to {lan_addr}? [y/N] ", end="", flush=True)
    answer = stdin.readline()
    return answer.strip().lower() in ("y", "yes")


def cmd_deploy(s, a):
    if os.environ.get("AGENTBOX_SESSION"):
        raise AgentboxError("`ab deploy` runs on the host, not inside a session: it is the user's review of "
                            "what the agent wrote, and it starts the live stack")
    p = session.load_project(s, a.project)
    host = set(s.host_addrs) | set(egress.host_addresses())
    lan = deploy.parse_lan(a.lan, host) if a.lan else None
    if a.volumes and not a.down:
        raise AgentboxError("--volumes goes with --down (it removes the stack's own volumes as it comes down)")
    if a.down:
        if a.lan or a.commit or a.check or a.adopt_volumes:
            raise AgentboxError("--down takes no deploy options (--lan, --commit, --check, --adopt-volumes)")
        with deployrun.lock(s, p.name):
            return _down(s, a, p)
    if a.adopt_volumes:
        return _adopt(s, p)
    if a.check:
        return _deploy(s, a, p, lan, host)
    with deployrun.lock(s, p.name):
        return _deploy(s, a, p, lan, host)


def _down(s, a, p, stdin=None):
    """`--down`: stop and remove the live stack; its own volumes only with --volumes, which always asks on a
    terminal (data loss is never a script's decision)."""
    stdin = stdin or sys.stdin
    podman = Podman(s.podman)
    objs = deployrun.stack_objects(podman, p)
    adopted = sorted(set(p.deploy_adopted) | set(p.deploy_external) | set(deployrun.unlabelled_volumes(podman, p)))
    if not objs["containers"] and not objs["networks"] and not (a.volumes and objs["volumes"]):
        print(f"nothing of {p.name}'s live stack is running (label agentbox.deploy={p.name})"
              + (f"; its volumes are kept: {', '.join(objs['volumes'])}" if objs["volumes"] else ""))
        return 0
    print(f"ab deploy {p.name} --down: stops and removes")
    for kind in ("containers", "networks"):
        if objs[kind]:
            print(f"  {kind}: {', '.join(objs[kind])}")
    if a.volumes and objs["volumes"]:
        print(f"  volumes, AND THEIR DATA: {', '.join(objs['volumes'])}")
    tty = bool(getattr(stdin, "isatty", lambda: False)())
    if a.volumes and not tty:
        raise AgentboxError("--volumes deletes the stack's data: run it on a terminal and confirm (--yes does not "
                            "apply)")
    if a.yes and tty:
        raise AgentboxError("--yes is for scripts (stdin not a terminal); on a terminal, answer the prompt")
    if not a.yes:
        if not tty:
            raise AgentboxError("no terminal to confirm on: run `ab deploy --down` on a terminal, or add --yes "
                                "from a script (never with --volumes)")
        print(f"Take down the live {p.name} stack{' and delete its volumes' if a.volumes else ''}? [y/N] ",
              end="", flush=True)
        if stdin.readline().strip().lower() not in ("y", "yes"):
            print("nothing removed.")
            return 1
    failed = deployrun.down(s, podman, p, objs, volumes=a.volumes)
    kept = [] if a.volumes else objs["volumes"]
    if kept:
        print(f"kept volumes (the stack's data): {', '.join(kept)}. `ab deploy {p.name} --down --volumes` removes "
              f"them; the next deploy reuses them")
    if adopted:
        print(f"never removed by agentbox (adopted, external, or not made by a deploy): {', '.join(adopted)}")
    if failed:
        warn(f"ab deploy: could not remove: {', '.join(failed)}")
        return 1
    print(f"{p.name}'s live stack is down. Its images and deploy copies stay (a redeploy reuses them).")
    return 0


def _adopt(s, p, stdin=None):
    """`--adopt-volumes`: take over the volumes a hand-run stack left, by name, after the user confirms them on a
    terminal. Their data becomes the live stack's; nothing is copied, moved, started or removed."""
    from .podman import Podman
    stdin = stdin or sys.stdin
    names = [n for n in deployrun.unlabelled_volumes(Podman(s.podman), p) if n not in p.deploy_adopted]
    if not names:
        print(f"no unlabelled {p.compose_project}_* volumes to adopt"
              + (f" (already adopted: {', '.join(p.deploy_adopted)})" if p.deploy_adopted else ""))
        return 0
    print(f"volumes of compose project {p.compose_project} that no `ab deploy` made:\n"
          + "\n".join(f"  {n}" for n in names)
          + f"\nAdopting them makes their data the live {p.name} stack's from the next deploy on.")
    if not bool(getattr(stdin, "isatty", lambda: False)()):
        raise AgentboxError("no terminal to confirm on: run `ab deploy --adopt-volumes` on a terminal")
    print(f"Adopt these {len(names)} volume(s) for {p.name}? [y/N] ", end="", flush=True)
    if stdin.readline().strip().lower() not in ("y", "yes"):
        print("nothing adopted")
        return 1
    deployrun.store_adopted(s, p.name, names)
    print(f"adopted; `ab deploy {p.name}` uses them by name (deploy_adopted in `ab project show {p.name}`)")
    return 0


def _deploy(s, a, p, lan, host):
    root = p.root
    c = deploy.check(s, p.name, root, ref=a.commit or "HEAD", compose=p.deploy_compose)
    problems = []
    if not c["has_compose"]:
        problems.append(f"commit {c['commit'][:12]} has no {p.deploy_compose}" if p.deploy_compose else
                        f"commit {c['commit'][:12]} has no compose file at the root (one elsewhere, such as "
                        f"deploy/compose.yaml: `ab project set {p.name} --deploy-compose deploy/compose.yaml`)")
    if c["dirty"]:
        problems.append("uncommitted changes to tracked files (a deploy is a commit; commit or stash them): "
                        + ", ".join(c["dirty"][:10]) + (" …" if len(c["dirty"]) > 10 else ""))
    if c["missing"]:
        problems.append(f"{os.path.join(root, '.env')} lacks variables .env.dev defines: "
                        + ", ".join(c["missing"]))
    lan_addr, ports = None, list(p.deploy_ports)
    if lan:
        lan_addr, ports = lan[0], sorted(set(ports) | {lan[1]})
    elif p.lan_addr:
        try:
            lan_addr = deployrun.check_lan_addr(p.lan_addr, host)
        except AgentboxError as e:
            problems.append(str(e))
    elif not a.check:
        problems.append(f"no LAN address: give --lan ADDR:PORT (stored for later deploys of {p.name})")
    binds = []
    try:
        binds = deployrun.checked_binds(s, p)
    except AgentboxError as e:
        problems.append(str(e))
    podman = Podman(s.podman)
    try:
        problems += deployrun.foreign_objects(s, podman, p)
    except AgentboxError as e:
        problems.append(f"cannot tell whether compose project {p.compose_project} has objects no deploy made: {e}")
    report, note = (None, "") if not c["has_compose"] else deployrun.contract_report(
        s, podman, p, root, c["commit"], lan_addr, ports, binds)
    if report is not None and report.failed:
        problems.append("the compose file breaks the deploy contract (the FAIL lines under \"contract\" above; "
                        "design.md, \"What `ab deploy` deploys\")")
    print(f"ab deploy {p.name}: commit {c['commit']}"
          + (f", on {lan_addr}" if lan_addr else "") + (f", ports {', '.join(map(str, ports))}" if ports else "")
          + (f", compose project {p.compose_project}" if p.compose_project else ""))
    print(c["review"])
    if c["extra"]:
        print(f"\nnote: .env defines variables .env.dev does not: {', '.join(c['extra'])}")
    if report is not None:
        print("\ncontract:\n" + report.text())
    elif note:
        print(f"\nnote: the contract check did not run ({note}); the deploy gate still enforces it")
    if problems:
        print("\nrefused:\n" + "\n".join(f"  - {x}" for x in problems), file=sys.stderr)
        return 2
    if a.check:
        print("\ncheck passed; nothing was started or recorded.")
        return 0
    if not confirm(a, c, p.name, lan_addr):
        print("not deployed.")
        return 1
    run = deployrun.Deploy(s, p, commit=c["commit"], lan_addr=lan_addr, ports=ports, binds=binds, podman=podman,
                           warn=warn, out=print)
    result = run.run()
    if result == "ok" and lan:
        deployrun.store_lan(s, p.name, lan[0], lan[1])
    if result != "ok":
        warn(f"ab deploy: the deploy of {c['commit'][:12]} failed; what podman-compose started is left as it is "
             f"(no rollback). `ab deploy {p.name} --commit <earlier commit>` redeploys an earlier one.")
    run.report()
    return 0 if result == "ok" else 1


def add_command(sub, add, ex):
    x = add(sub, "deploy", "start a project's live stack from a reviewed commit (host only)",
            ex("ab deploy filelab --check", "ab deploy filelab --lan 192.0.2.10:8095", "ab deploy filelab",
               "ab deploy filelab --commit 3f2a9c1", "ab deploy filelab --down"))
    x.add_argument("project", help="the registered project whose live stack to deploy")
    x.add_argument("--lan", metavar="ADDR:PORT",
                   help="the address of this host the stack publishes on, and a port it may publish; after a "
                        "successful deploy the address is stored as lan_addr and the port added to deploy_ports, "
                        "so later deploys may omit it. Only those ports, only on that address, are published")
    x.add_argument("--commit", metavar="REF",
                   help="deploy this commit instead of HEAD (a redeploy or a rollback); the working tree "
                        "need not be clean then, since nothing is taken from it but the real .env")
    x.add_argument("--yes", action="store_true",
                   help="skip the prompt: only from a script (stdin not a terminal), and only to redeploy the "
                        "commit deployed last")
    x.add_argument("--adopt-volumes", action="store_true",
                   help="take over, by name, the <compose_project>_* volumes a stack run by hand left (after "
                        "you stopped it with `podman-compose down`), so their data carries over; asks first, "
                        "then deploys nothing")
    x.add_argument("--down", action="store_true",
                   help="stop and remove the live stack's containers and networks (what `ab deploy` made, by "
                        "label); its volumes stay unless --volumes. Asks first; --yes from a script")
    x.add_argument("--volumes", action="store_true",
                   help="with --down: also delete the stack's own volumes and their data (off by default; always "
                        "asks on a terminal; adopted and external volumes are never deleted)")
    x.add_argument("--check", action="store_true",
                   help="show the review and run every check (including the contract check, a no-network YAML parse in "
                        "the base image), then stop: start no service or stack, record nothing")
    x.set_defaults(fn=cmd_deploy)
