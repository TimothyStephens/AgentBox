"""The project settings table: one row per registry key, every property of that key decided in one place.

Five parallel hand-maintained lists used to hold those properties -- `cli.SECURITY_OVERRIDES`,
`cli.UNSETTABLE`, `registry.PROJECT_KEYS`, the display gating in `cli._print_effective`, and the ceiling's
looseness orders. Five of the second audit's findings (F4, F6, F8, F10, F11) were the same bug in different
clothes: a key missing from one of them. When one property of a key lives in five places, adding a key gets
four of them right.

So `KEYS` below is the single source, and `presets.SETTINGS`/`ALLOWED`/`LOOSENESS`,
`registry.PROJECT_KEYS`, `cli.SETTING_FLAGS`/`SECURITY_OVERRIDES`/`UNSETTABLE`/`CLEAR_FLAGS`, the ceiling
comparison and `ab project show`'s display all derive from it. `Key` has no defaults for the decision
fields, so a new key is a TypeError until every column is answered:

  kind        preset (resolvable from a preset) | project (registry-only) | structural | ack
  arity       scalar | list | bool | map            -- drives --clear-<list> and "was it given?"
  flags/dest  the CLI flag(s) and argparse dest; () for preset-only or hand-edit-only keys
  validate    which validator in VALIDATORS checks a stored value
  values      allowed values, TIGHTEST FIRST -- this is the looseness order a ceiling preset compares on
  security    is this key security-relevant?
  clears      does a preset change drop it? (an override must not outlive the preset it was set against)
  unsettable  does `ab project set --unset KEY` take it?
  display     always (it has a value even when unstored) | stored (shown only when the entry holds it)
  inert       when this key has no effect under the current posture, and the reason to print
"""
import fnmatch
import re
import sys
from dataclasses import dataclass

from . import cgroup, mounts
from .egress import check_domains
from .errors import AgentboxError
from .fsutil import inside

IMAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._/:@*?-]*$")
SWAP_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
COMPOSE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# A git branch name, by git's own rules as far as they matter here: no leading '-' or '.', no '..', no
# space or shell metacharacter, no trailing '/' or '.lock'. The name reaches `git log <ref> ^<ref>` in the
# end-of-session scan, so it has to be a ref and nothing else.
GIT_BRANCH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")
PODMAN_TIERS = ("none", "observe", "exec", "dev")
PODMAN_TIGHTEST = {"tier": "none"}

# agentbox's own image names, which no `dev_images` pattern may match (audit F-9). The gate already refuses
# them as build tags; an allowlist entry broad enough to cover one of these (`*`, `localhost/*`) is broad
# enough to cover every other local image too, which is the harm dev_images exists to bound. Each agent's
# image is checked against this list by test_projectkeys, so adding an agent cannot quietly leave it out.
RESERVED_IMAGES = ("localhost/agentbox-claude:latest", "localhost/agentbox-omp:latest",
                   "localhost/agentbox-lockdown:latest", "agentbox-claude:latest", "agentbox")


@dataclass(frozen=True)
class Key:
    name: str
    kind: str
    arity: str
    flags: tuple
    dest: str
    validate: str
    values: tuple
    security: bool
    clears: bool
    unsettable: bool
    display: str
    inert: str
    inert_reason: str

    @property
    def tightest(self):
        """The tightest value on this key's looseness order, or None when it has no order. `podman` is a
        map, so its order is component-wise (see is_looser) and its floor is spelled out."""
        if self.values:
            return self.values[0]
        return PODMAN_TIGHTEST if self.name == "podman" else None


def _key(name, **kw):
    return Key(name=name, **kw)


KEYS = (
    # --- structural: what the project IS; neither is cleared, neither can be unset ---
    _key("root", kind="structural", arity="scalar", flags=(), dest="", validate="grant_rw", values=(),
         security=True, clears=False, unsettable=False, display="always", inert="", inert_reason=""),
    _key("preset", kind="structural", arity="scalar", flags=("--preset",), dest="preset",
         validate="preset_name", values=(), security=True, clears=False, unsettable=False,
         display="always", inert="", inert_reason=""),

    # --- preset-resolvable settings: always have a value, always cleared by a preset change ---
    _key("compose", kind="preset", arity="scalar", flags=(), dest="", validate="enum", values=("ro", "rw"),
         security=True, clears=True, unsettable=True, display="always", inert="", inert_reason=""),
    _key("egress", kind="preset", arity="scalar", flags=("--egress",), dest="egress", validate="enum",
         values=("locked", "packages", "open"), security=True, clears=True, unsettable=True,
         display="always", inert="", inert_reason=""),
    _key("podman", kind="preset", arity="map", flags=("--podman",), dest="podman", validate="podman",
         values=(), security=True, clears=True, unsettable=True, display="always", inert="",
         inert_reason=""),
    # One value: the gate refuses cap_add, devices and security options under every preset. "opt-in" was
    # never built; a stored one is read as "refused" with a note (decision 19 Nov 2026, see _caps).
    _key("caps", kind="preset", arity="scalar", flags=(), dest="", validate="caps",
         values=("refused",), security=True, clears=True, unsettable=True, display="always",
         inert="", inert_reason=""),
    _key("secrets", kind="preset", arity="scalar", flags=(), dest="", validate="enum",
         values=("locked", "masked"), security=True, clears=True, unsettable=True, display="always",
         inert="always", inert_reason="secret masks always apply; only the data_env setting unmasks the "
                                               "root's env files"),
    _key("approvals", kind="preset", arity="scalar", flags=(), dest="", validate="enum",
         values=("on", "off"), security=True, clears=True, unsettable=True, display="always", inert="",
         inert_reason=""),
    _key("data", kind="preset", arity="scalar", flags=(), dest="", validate="enum",
         values=("ro-only", "declared"), security=True, clears=True, unsettable=True, display="always",
         inert="", inert_reason=""),
    # The project root itself: `ro` mounts it read-only (the `planner` preset; docs/readonly-root-proposal.md,
    # decided 18 Nov 2026). Writes go to the data_rw grants, which may not lie inside a read-only root.
    _key("code", kind="preset", arity="scalar", flags=("--code",), dest="code", validate="enum",
         values=("ro", "rw"), security=True, clears=True, unsettable=True, display="always",
         inert="", inert_reason=""),

    # --- grants and the live/dev-stack settings ---
    _key("data_ro", kind="project", arity="list", flags=("--data-ro",), dest="data_ro",
         validate="grant_ro", values=(), security=True, clears=True, unsettable=True, display="stored",
         inert="", inert_reason=""),
    _key("data_rw", kind="project", arity="list", flags=("--data-rw",), dest="data_rw",
         validate="grant_rw", values=(), security=True, clears=True, unsettable=True, display="stored",
         inert="", inert_reason=""),
    _key("extra_domains", kind="project", arity="list", flags=("--extra-domain",), dest="extra_domain",
         validate="domains", values=(), security=True, clears=True, unsettable=True, display="stored",
         inert="locked_egress", inert_reason="locked egress has no extra-domain allowlist"),
    _key("compose_project", kind="project", arity="scalar", flags=("--compose-project",),
         dest="compose_project", validate="compose_name", values=(), security=True, clears=True,
         unsettable=True, display="stored", inert="no_live_access",
         inert_reason="no live-stack access at this podman tier"),
    _key("observe_logs", kind="project", arity="bool", flags=("--observe-logs",), dest="observe_logs",
         validate="bool", values=(False, True), security=True, clears=True, unsettable=True,
         display="stored", inert="no_live_access",
         inert_reason="no live-stack access at this podman tier"),
    _key("dev_images", kind="project", arity="list", flags=("--dev-image",), dest="dev_image",
         validate="image_patterns", values=(), security=True, clears=True, unsettable=True,
         display="stored", inert="not_dev_tier", inert_reason="only the dev tier runs or builds images"),

    # --- properties of the project's own files, not of the preset's posture: not cleared ---
    _key("mask_skip", kind="project", arity="list", flags=("--mask-skip",), dest="mask_skip",
         validate="rel_names", values=(), security=True, clears=False, unsettable=True, display="stored",
         inert="", inert_reason=""),
    _key("swap", kind="project", arity="map", flags=("--swap",), dest="swap", validate="swap", values=(),
         security=True, clears=False, unsettable=True, display="stored", inert="", inert_reason=""),
    # The project's own masks, on top of the secret masks (decisions.md, 10 Nov 2026): a bare name (`data`)
    # covers every file or directory of that name anywhere in the grants, as `.env` is; `./data` or
    # `deploy/state` covers that one path under the root. A directory reads empty and read-only in the
    # session and in every container it starts, and is left out of the pre-session backup. A property of the
    # project's files, like mask_skip: a preset change does not clear it.
    _key("mask", kind="project", arity="list", flags=("--mask",), dest="mask", validate="mask_paths",
         values=(), security=True, clears=False, unsettable=True, display="stored", inert="",
         inert_reason=""),
    # Toulmin integration (docs/agentbox_toulmin_integration.md): the session asks `tm` for a socket
    # directory and a bridge, mounts both, and gives the harness an MCP entry. Security-relevant (a socket
    # the agent talks through, a binary it runs) and posture: a preset change clears it, and False is the
    # tightest value, so a ceiling preset (lockdown) refuses it.
    _key("toulmin", kind="project", arity="bool", flags=("--toulmin",), dest="toulmin", validate="bool",
         values=(False, True), security=True, clears=True, unsettable=True, display="stored", inert="",
         inert_reason=""),
    # The two trusted opt-outs (decisions.md, 19 Nov 2026), for an agent the user trusts with real secrets or
    # live data. `data_env` shows the real env files (.env, .env.*, *.env) of the project root instead of the
    # masks and the dev swap; `data_bind` leaves `<root>/data` visible, writable, out of the backup and out of
    # grantcheck's live-data refusals. Posture, so a preset change clears them; off is the tightest value, so a
    # ceiling preset (lockdown) refuses them. The other refusals are project.check_trust.
    _key("data_env", kind="project", arity="scalar", flags=("--data-env",), dest="data_env", validate="enum",
         values=("off", "on"), security=True, clears=True, unsettable=True, display="always", inert="",
         inert_reason=""),
    _key("data_bind", kind="project", arity="scalar", flags=("--data-bind",), dest="data_bind", validate="enum",
         values=("off", "on"), security=True, clears=True, unsettable=True, display="always", inert="",
         inert_reason=""),
    # How the root reaches the session: `checkout` binds the owner's working tree (the default), `clone`
    # binds a fresh clone on branch agentbox/<session> at the owner's path and never mounts the checkout
    # (the default when toulmin is on and the root is a git repository). No looseness order: a hand-set
    # `checkout` under a ceiling preset is not a loosening, it is the mode every project had.
    _key("mount", kind="project", arity="scalar", flags=("--mount",), dest="mount", validate="mount_mode",
         values=(), security=True, clears=False, unsettable=True, display="stored", inert="",
         inert_reason=""),
    # A reviewer project (review.py; decisions.md, 11 Nov 2026): this project's root is a clone of the named
    # builder's repository, which `ab project sync` brings up to date and hands the reviews back from. NOT
    # security-relevant: it grants nothing inside a session (the clone is an ordinary root that overlaps
    # nothing); it only tells the host-side sync where to fetch from and copy to.
    _key("reviews", kind="project", arity="scalar", flags=("--reviews",), dest="reviews",
         validate="project_name", values=(), security=False, clears=False, unsettable=True,
         display="stored", inert="", inert_reason=""),
    # The branch the project's work lands on, so the end-of-session scan can name that branch's new
    # commits one by one instead of reporting a count. NOT security-relevant: it changes what the scan
    # REPORTS and grants nothing, so a ceiling preset must not refuse it.
    #
    # agentbox does not own the value. MAGS-CodeDev has its own `settings.base_branch` (its
    # config.template.yaml), and a project using it sets this to the same name -- one value in two places,
    # both of them the user's. Reading MAGS's config to find it would couple agentbox to another tool's
    # file format for a string the user can simply state.
    _key("base_branch", kind="project", arity="scalar", flags=("--base-branch",), dest="base_branch",
         validate="git_branch", values=(), security=False, clears=False, unsettable=True,
         display="stored", inert="", inert_reason=""),
    _key("build_memory", kind="project", arity="scalar", flags=("--build-memory",), dest="build_memory",
         validate="size", values=(), security=False, clears=False, unsettable=True, display="always",
         inert="not_dev_tier", inert_reason="only the dev tier builds"),
    # Whether the host-owned profile is loaded at all (`ab session start --no-profile` turns it off for one run).
    # NOT security-relevant: the profile is host-owned and trusted by design, and "off" loads strictly less
    # -- so the ceiling must not refuse `profile = "on"` under lockdown, where the profile has always
    # loaded. `values` is spelled out for the enum check; `security=False` is what keeps it out of the
    # ceiling comparison (presets.ORDERED_NON_SETTINGS).
    _key("profile", kind="project", arity="scalar", flags=("--profile",), dest="profile", validate="enum",
         values=("off", "on"), security=False, clears=False, unsettable=True, display="always", inert="",
         inert_reason=""),

    # --- `ab deploy` (deployrun.py; design.md, deploy settings): properties of the LIVE service the user
    # deploys, not of a session's posture, so a preset change does not clear them. Security-relevant: they
    # decide what the live stack may publish and bind. Nothing in a session reads them.
    _key("lan_addr", kind="project", arity="scalar", flags=("--lan-addr",), dest="lan_addr", validate="ipv4",
         values=(), security=True, clears=False, unsettable=True, display="stored", inert="", inert_reason=""),
    _key("deploy_ports", kind="project", arity="list", flags=("--deploy-port",), dest="deploy_port",
         validate="ports", values=(), security=True, clears=False, unsettable=True, display="stored", inert="",
         inert_reason=""),
    _key("deploy_binds", kind="project", arity="list", flags=("--deploy-bind",), dest="deploy_bind",
         validate="grant_deploy", values=(), security=True, clears=False, unsettable=True, display="stored",
         inert="", inert_reason=""),
    _key("deploy_files", kind="project", arity="list", flags=("--deploy-file",), dest="deploy_file",
         validate="rel_files", values=(), security=True, clears=False, unsettable=True, display="stored",
         inert="", inert_reason=""),
    # The compose file, root-relative, when it is not at the root (Tim's stacks keep it in deploy/, 18 Nov).
    _key("deploy_compose", kind="project", arity="scalar", flags=("--deploy-compose",), dest="deploy_compose",
         validate="rel_file", values=(), security=True, clears=False, unsettable=True, display="stored",
         inert="", inert_reason=""),
    _key("deploy_external", kind="project", arity="list", flags=("--deploy-external",), dest="deploy_external",
         validate="external_names", values=(), security=True, clears=False, unsettable=True, display="stored",
         inert="", inert_reason=""),

    # --- acknowledgements: written by their own commands, never cleared by a preset change ---
    # Volumes of a stack run by hand before `ab deploy` (no deploy labels, which podman cannot add to an
    # existing volume), adopted by name with `ab deploy <p> --adopt-volumes` after the user confirms; the deploy
    # gate then accepts exactly these, as it accepts deploy_external (Tim, 17 Nov).
    _key("deploy_adopted", kind="ack", arity="list", flags=(), dest="", validate="external_names",
         values=(), security=True, clears=False, unsettable=True, display="stored", inert="",
         inert_reason=""),
    _key("history_secrets_accepted", kind="ack", arity="list", flags=(), dest="", validate="name_list",
         values=(), security=True, clears=False, unsettable=False, display="stored", inert="",
         inert_reason=""),
    _key("grant_exposure_accepted", kind="ack", arity="scalar", flags=(), dest="",
         validate="fingerprint", values=(), security=True, clears=False, unsettable=False,
         display="stored", inert="", inert_reason=""),
)

BY_NAME = {k.name: k for k in KEYS}

# --- the views that used to be hand-maintained lists ---
PROJECT_KEYS = frozenset(BY_NAME)
PRESET_SETTINGS = tuple(k.name for k in KEYS if k.kind == "preset")
ALLOWED = {k.name: k.values for k in KEYS if k.kind == "preset" and k.values}
LOOSENESS = {k.name: {v: i for i, v in enumerate(k.values)} for k in KEYS if k.values}
CLEARED_ON_PRESET_CHANGE = tuple(k.name for k in KEYS if k.clears)
UNSETTABLE = tuple(k.name for k in KEYS if k.unsettable)
SETTING_FLAGS = {k.name: (k.dest, k.flags[0]) for k in KEYS if k.flags}
CLEAR_FLAGS = {f"clear_{k.name}": k.name for k in KEYS if k.arity == "list" and k.unsettable}


def podman_capabilities(value):
    """What a `podman` setting actually grants, as a set (audit F-3).

    This was ranked `none < observe < dev < exec`, so a ceiling of `{tier = "dev"}` ALLOWED an override of
    `{tier = "observe"}`. But the two are not degrees of one thing: `observe` reads the LIVE host stack
    (another project's running containers), while `dev` runs a stack of the agent's OWN inside the sandbox.
    Neither contains the other, so they are compared as sets and "looser" means "not a subset".

    `live-exec` has no spelling that validates (`_podman` refuses exec), but it is named here so the
    comparison stays right if it is ever built."""
    value = value or {}
    out = set()
    for spelling in (value.get("tier", "none"), value.get("live")):
        out |= {"observe": {"live-observe"}, "exec": {"live-exec"}, "dev": {"dev-stack"}}.get(spelling, set())
    return frozenset(out)


def is_looser(name, value, than):
    """Is `value` looser than `than` on this key's order? False for a key with no order, so an unordered
    key is never treated as a loosening (it may still be *inert*, which is a display matter)."""
    if name == "podman":
        return not podman_capabilities(value) <= podman_capabilities(than)
    order = LOOSENESS.get(name)
    if not order or value not in order or than not in order:
        return False
    return order[value] > order[than]


GRANT_KEYS = ("root", "data_ro", "data_rw")


def _covers(parent, child):
    return inside(child, parent)


def _grant_loosenings(before, after):
    """Grants in `after` that are not already inside a grant `before` had with the same or wider access."""
    from . import paths
    old = paths.grants_of(before)
    out = []
    for path, rw in paths.grants_of(after):
        if any(_covers(o, path) and (writable or not rw) for o, writable in old):
            continue
        out.append(f"grant {path} ({'read-write' if rw else 'read-only'})")
    return out


def loosenings(before_eff, after_eff, before_entry, after_entry):
    """Every security-relevant thing this edit would grant that the project does not already have, as short
    descriptions. Empty means the edit grants nothing new — a tightening or a no-op — which is what lets
    `ab project set` proceed while a grant-exposure acknowledgement is stale: making a project safer must
    never be gated on an acknowledgement (audit F-5).

    Conservative by construction, so "empty" can be trusted:
      * a key with a looseness order is compared on it (`is_looser`);
      * a list setting is a loosening unless the new value is a subset of the old one;
      * any OTHER security-relevant setting that changed at all counts, since we cannot order it;
      * a grant counts unless it is already inside a grant with the same or wider access.
    `preset` and `root` are not compared as values: a preset change shows up in the settings it resolves to,
    and `root` is compared as a grant."""
    out = []
    for k in KEYS:
        if not k.security or k.kind in ("ack", "structural") or k.name in GRANT_KEYS:
            continue
        if k.name in before_eff and k.name in after_eff:
            old, new = before_eff[k.name][0], after_eff[k.name][0]
        else:
            # observe_logs, compose_project, dev_images, mask_skip and swap are not resolvable from a preset,
            # so they are absent from the effective dict: their stored value IS the whole truth, and an
            # absent one means the key's tightest (False for observe_logs). Comparing only the effective
            # dict skipped them, which let `--observe-logs` count as granting nothing new.
            old = before_entry.get(k.name, k.tightest)
            new = after_entry.get(k.name, k.tightest)
        if old == new:
            continue
        if is_looser(k.name, new, old):
            out.append(f"{k.name}: {old} -> {new}")
        elif is_looser(k.name, old, new):
            continue                                        # a tightening on this key's own order
        elif k.arity == "list" and set(new or ()) <= set(old or ()):
            continue                                        # a shorter list grants nothing new
        else:
            out.append(f"{k.name}: changed ({old} -> {new})")
    return out + _grant_loosenings(before_entry, after_entry)


def change_label(name, old, new):
    """"loosened" / "tightened" / "" for a value replaced by a preset change (audit F7)."""
    if is_looser(name, new, old):
        return "loosened"
    if is_looser(name, old, new):
        return "tightened"
    return ""


# When a stored value has no effect under the current posture. The context comes from posture().
INERT_WHEN = {
    "always": lambda ctx: True,
    "no_live_access": lambda ctx: not ctx["live_access"],
    "not_dev_tier": lambda ctx: ctx["tier"] != "dev",
    "locked_egress": lambda ctx: ctx["egress"] == "locked",
}


def posture(project):
    """The resolved posture `ab project show` judges inertness against (takes a session.Project)."""
    podman = project.value("podman") or {}
    tier, live = podman.get("tier", "none"), podman.get("live")
    return {"tier": tier, "live": live, "egress": project.value("egress"),
            "live_access": "observe" in (tier, live)}


def inert_reason(name, ctx):
    """Why this key has no effect right now, or "" when it does have an effect. A stored setting is shown
    either way (audit F8): hiding it made a waived check or a dead allowlist invisible."""
    key = BY_NAME[name]
    if key.inert and INERT_WHEN[key.inert](ctx):
        return key.inert_reason
    return ""


# --- validators, one per `validate` name; each returns the normalised value or raises ---

def _enum(name, where, value, **_kw):
    values = BY_NAME[name].values
    if value not in values:
        raise AgentboxError(f"{where}: {name} = {value!r}, expected one of "
                            f"{', '.join(str(v) for v in values)}")
    return value


_CAPS_NOTED = set()


def _caps(name, where, value, **_kw):
    """`caps` has one value, "refused". The old presets build, pods and build-open said "opt-in", which was
    never built (the gate refused capabilities, devices and security options under every preset), so a
    registry entry or preset file may still hold it: read it as "refused", with a one-line note, rather than
    stop that project or every preset from loading. There is no CLI flag for caps, so no new one is written."""
    if value == "refused":
        return value
    if value == "opt-in":
        if where not in _CAPS_NOTED:
            _CAPS_NOTED.add(where)
            print(f'note: {where}: caps = "opt-in" is read as "refused" (it was never built: capabilities, '
                  'devices and security options are always refused); delete the line', file=sys.stderr)
        return "refused"
    raise AgentboxError(f'{where}: caps = {value!r}, expected "refused" (capabilities, devices and security '
                        'options are always refused; there is no opt-in)')


def _podman(name, where, value, **_kw):
    tier = value.get("tier") if isinstance(value, dict) else None
    if tier not in PODMAN_TIERS:
        raise AgentboxError(f"{where}: podman tier must be one of {', '.join(PODMAN_TIERS)}")
    live = value.get("live") if isinstance(value, dict) else None
    if live not in (None, "observe", "exec"):
        raise AgentboxError(f"{where}: podman live must be observe, exec, or unset")
    if "exec" in (tier, live):
        raise AgentboxError(f"{where}: exec on the live stack is not built; use observe for the "
                            "live stack, and the dev tier for a stack of the agent's own")
    return dict(value)


def _bool(name, where, value, **_kw):
    if not isinstance(value, bool):
        raise AgentboxError(f"{where}: {name} must be true or false")
    return value


def _domains(_name, _where, value, **_kw):
    return check_domains(list(value), "--extra-domain")


def _size(_name, where, value, *, settings=None, **_kw):
    """A build's memory limit: positive, and within the slice's cap (a build over it could never finish)."""
    size = cgroup.parse_size(value)
    if size <= 0:
        raise AgentboxError(f"{where}: build_memory must be positive")
    cap = cgroup.slice_memory_max(settings.cgroup_parent) if settings else None
    if cap is not None and size > cap:
        raise AgentboxError(f"{where}: build_memory {value} is over the "
                            f"{settings.cgroup_parent} cap ({cap >> 20} MB)")
    return value


def _compose_name(_name, where, value, **_kw):
    if not isinstance(value, str) or not COMPOSE_NAME.match(value):
        raise AgentboxError(f"{where}: compose_project {value!r}: lower-case letters, digits, "
                            "'-', '_'")
    return value


def _git_branch(_name, where, value, **_kw):
    """A branch name the scan can hand to git as a ref, and nothing that is not one."""
    if not isinstance(value, str) or not GIT_BRANCH.match(value):
        raise AgentboxError(f"{where}: base_branch {value!r}: a git branch name (letters, digits, "
                            "'.', '_', '-', '/'), not starting with '-' or '.'")
    if ".." in value or value.endswith((".lock", "/", ".")) or "//" in value:
        raise AgentboxError(f"{where}: base_branch {value!r} is not a valid git ref")
    return value


def _image_patterns(_name, where, value, **_kw):
    if not isinstance(value, list) or not all(isinstance(p, str) and IMAGE_PATTERN.match(p) for p in value):
        raise AgentboxError(f"{where}: dev_images must be a list of image name patterns "
                            "(e.g. localhost/myapp:*)")
    for pattern in value:
        hit = next((n for n in RESERVED_IMAGES if fnmatch.fnmatchcase(n, pattern)), None)
        if hit:
            raise AgentboxError(
                f"{where}: dev_images pattern {pattern!r} matches {hit}, one of agentbox's own image names. "
                "Those are reserved (the gate refuses them as build tags too). A pattern this broad would "
                "also cover every other local image, including other projects' builds and live stacks: name "
                "the project's own images instead (e.g. localhost/myapp:*)")
    return list(value)


def _rel_names(name, where, value, **_kw):
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise AgentboxError(f"{where}: {name} must be a list of folder names")
    return list(value)


def _mask_paths(name, where, value, **_kw):
    """`mask` entries. A bare name masks every file or directory of that name in every grant; `./x` or a path
    with a '/' masks exactly that path under the project root (`./` is kept: it is what says "root only").
    Absolute paths and '..' are refused here, at store time, not at mount time (docs/agentbox-masked-paths.md
    §4); so are the characters a mount option cannot carry."""
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise AgentboxError(f"{where}: {name} must be a list of names or root-relative paths")
    out = []
    for raw in value:
        p = raw.strip()
        while len(p) > 1 and p.endswith("/"):
            p = p[:-1]
        parts = p.split("/")
        if (not p or p in (".", "./", "..") or p.startswith("/") or ".." in parts or "" in parts[1:]
                or "\0" in p or "," in p or "\n" in p or (p.startswith("./") and len(parts) < 2)):
            raise AgentboxError(f"{where}: mask {raw!r}: use a plain name (masked wherever it appears in the "
                                "grants, like .env) or a path under the root such as ./data or deploy/state "
                                "(no leading '/', no '..')")
        if p not in out:
            out.append(p)
    return out


MOUNT_MODES = ("checkout", "clone")


def _mount_mode(name, where, value, **_kw):
    if value not in MOUNT_MODES:
        raise AgentboxError(f"{where}: {name} must be one of {', '.join(MOUNT_MODES)}, got {value!r}")
    return value


PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")    # registry.NAME (registry imports this module)


def _project_name(name, where, value, **_kw):
    """A project name; that it is a registered builder is review.check's job, with the registry in hand."""
    if not isinstance(value, str) or not PROJECT_NAME.match(value):
        raise AgentboxError(f"{where}: {name} {value!r}: a project name (lower-case letters, digits, '-', '_')")
    return value


def _name_list(name, where, value, **_kw):
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise AgentboxError(f"{where}: {name} must be a list of names")
    return list(value)


def _fingerprint(name, where, value, **_kw):
    """A grant-exposure acknowledgement: the accepted findings' signatures, or (from before those were
    recorded) the bare fingerprint string. grantcheck.acceptance_fingerprint reads either."""
    if isinstance(value, list):
        if not all(isinstance(v, str) and v for v in value):
            raise AgentboxError(f"{where}: {name} must be a list of finding signatures")
        return list(value)
    if not isinstance(value, str):
        raise AgentboxError(f"{where}: {name} must be a string or a list of finding signatures")
    return value


IPV4 = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
EXTERNAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _ipv4(name, where, value, **_kw):
    """One IPv4 address, not 0.0.0.0 and not a loopback: the address a live stack publishes on. That it is an
    address of THIS host is checked by `ab deploy` itself (the host's addresses can change)."""
    m = IPV4.match(value) if isinstance(value, str) else None
    if not m or not all(int(o) <= 255 and (o == "0" or not o.startswith("0")) for o in m.groups()) \
            or value == "0.0.0.0" or m.group(1) == "127":
        raise AgentboxError(f"{where}: {name} {value!r}: one IPv4 address of this host (not 0.0.0.0 or loopback)")
    return value


def _ports(name, where, value, **_kw):
    if not isinstance(value, list) or not all(isinstance(p, int) and not isinstance(p, bool) and 0 < p < 65536
                                              for p in value):
        raise AgentboxError(f"{where}: {name} must be a list of port numbers (1-65535)")
    return sorted(set(value))


def _rel_file(name, where, value, **kw):
    """One root-relative file path, under the same rules as _rel_files."""
    if not isinstance(value, str):
        raise AgentboxError(f"{where}: {name} must be a file path under the project root")
    return _rel_files(name, where, [value], **kw)[0]


def _rel_files(name, where, value, **_kw):
    """Untracked files copied into the deploy copy: paths under the root, no '..', no absolute path."""
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise AgentboxError(f"{where}: {name} must be a list of root-relative file paths")
    out = []
    for raw in value:
        parts = raw.split("/")
        if (not raw or raw.startswith("/") or ".." in parts or "." in parts or "" in parts or "\0" in raw
                or "\n" in raw or parts[0] == ".git"):
            raise AgentboxError(f"{where}: {name} {raw!r}: a file path under the project root (no leading '/', "
                                "no '.' or '..' segments, nothing under .git)")
        if raw not in out:
            out.append(raw)
    return out


def _external_names(name, where, value, **_kw):
    if not isinstance(value, list) or not all(isinstance(p, str) and EXTERNAL_NAME.match(p) for p in value):
        raise AgentboxError(f"{where}: {name} must be a list of podman volume or network names")
    return sorted(set(value))


def masked_name(fn):
    """Would the secret masks hide a file with this name? (mounts' own patterns, exemptions included.)"""
    return fn not in mounts.MASK_EXEMPT and any(fnmatch.fnmatchcase(fn, p) for p in mounts.MASK_FILES)


def _swap(_name, where, value, **_kw):
    """swap maps a masked file name to the dev stand-in mounted in its place, in the same directory.

    Two rules keep a swap from becoming a way to READ a secret (audit F6):

      * the stand-in -- whose contents the agent sees -- may not itself be a masked name.
        `swap = { "notes.txt" = ".env" }` otherwise mounted the real .env at an unmasked path, in plain
        sight, while the mask dutifully covered the original;
      * the destination must be a masked name, so a swap only ever stands in for a file that is hidden
        anyway, rather than binding a dev file over arbitrary project files (and making them masked by
        virtue of being a swap target).
    """
    for target, dev in dict(value).items():
        for x in (target, dev):
            if not isinstance(x, str) or not SWAP_NAME.match(x) or x in (".", ".."):
                raise AgentboxError(f"{where}: swap entry {target!r} = {dev!r}: use plain file "
                                    "names (no '/', no '..')")
        if masked_name(dev):
            raise AgentboxError(f"{where}: swap {target!r} = {dev!r}: the stand-in {dev!r} is itself a "
                                "masked name, so mounting it would show the agent a real secret; point it "
                                "at a dev file (e.g. .env.dev)")
        if not masked_name(target):
            raise AgentboxError(f"{where}: swap {target!r} = {dev!r}: {target!r} is not a masked name "
                                f"({', '.join(mounts.MASK_FILES)}), so there is no secret for it to stand in "
                                "for; a swap only replaces a file the masks hide")
    return dict(value)


def _preset_name(_name, where, value, **_kw):
    if not isinstance(value, str) or not value:
        raise AgentboxError(f"{where}: preset must be a name")
    return value


def _grant(_name, _where, value, **_kw):
    """Grants are resolved and checked by paths.check_grant with the caller's protected-path context; the
    table only records which kind each key is (see cli/session)."""
    return value


VALIDATORS = {
    "enum": _enum,
    "caps": _caps,
    "podman": _podman,
    "bool": _bool,
    "domains": _domains,
    "size": _size,
    "compose_name": _compose_name,
    "git_branch": _git_branch,
    "image_patterns": _image_patterns,
    "rel_names": _rel_names,
    "mask_paths": _mask_paths,
    "mount_mode": _mount_mode,
    "project_name": _project_name,
    "name_list": _name_list,
    "fingerprint": _fingerprint,
    "swap": _swap,
    "preset_name": _preset_name,
    "grant_rw": _grant,
    "grant_ro": _grant,
    "grant_deploy": _grant,
    "ipv4": _ipv4,
    "ports": _ports,
    "rel_files": _rel_files,
    "rel_file": _rel_file,
    "external_names": _external_names,
}


def validate_value(name, where, value, *, settings=None):
    """Validate one stored value against its key's own validator, returning the normalised value."""
    return VALIDATORS[BY_NAME[name].validate](name, where, value, settings=settings)


def validate_entry(name, entry, *, settings=None):
    """Validate a WHOLE registry entry: no unknown keys, and every stored value through its own validator.

    Run before saving (`ab project add`/`set`) and when loading one (session start), so an entry can never
    be stored that the next `ab session start` would refuse, and a hand-written key or value is caught where it is
    written (audit F11). `set` used to validate only the keys the command touched, so a bad stored `swap`
    or `extra_domains` survived the save and failed later. Returns the normalised entry; grants are
    resolved by the caller, which holds the protected-path context (paths.check_grant/check_overlaps)."""
    where = f"project {name}"
    unknown = sorted(set(entry) - PROJECT_KEYS)
    if unknown:
        raise AgentboxError(f"{where}: unknown settings {', '.join(unknown)}; known settings are "
                            f"{', '.join(sorted(PROJECT_KEYS))}")
    for required in ("root", "preset"):
        if not entry.get(required):
            raise AgentboxError(f"{where}: no {required} (every project needs a code grant and a preset)")
    return {key: validate_value(key, where, value, settings=settings) for key, value in entry.items()}
