"""`ab image build <agent>`: the user's own image layer, on top of the harness image.

The chain is `agentbox-base -> agentbox-<agent> -> this`. A user layer is for what only this user wants in
every session -- a language toolchain, a linter, a private CLI -- without that going into agentbox's own
images, and without a session being able to put it there.

Three things make it safe to run an image the user built:

  * **it is a layer, not a replacement.** The Containerfile must be `ARG BASE` then `FROM ${BASE}`, and
    `ab image build` supplies BASE. A Containerfile that names its own base is refused, because then
    nothing of the harness is underneath it.
  * **it cannot change how the agent is started.** USER, ENTRYPOINT, CMD and PATH are compared against the
    harness image and must be identical. Those four are how the launcher's own contract with the image is
    expressed; a layer that moves any of them is a different image wearing the tag.
  * **it must still be a working session image.** The harness image and the built image are each run once,
    offline, and asked for the agent's version and for `git`, `rg`, `python3` and the podman client. The
    answers are COMPARED: a layer must not lose a tool the harness image has, and must not change the
    agent. Comparing rather than checking against a fixed list is the point -- the first real
    `ab image build claude` was rejected for "missing rg" when `rg` is not in that harness image either,
    so the layer was being held to a standard its own base fails.

A rejected build is never tagged: it is built under a candidate tag, checked, and only then given the real
one. So the last good image stays exactly where it was, and the failure is a message rather than a session
that will not start.

The tag is `localhost/agentbox-<agent>-user:<key>`, where the key covers the harness image ID and the
Containerfile (`profile.user_image_key`). A new harness image therefore gives a different key, nothing is
built under it, and `ab session start` says "personal layer out of date" and runs the plain harness image -- it never
runs a layer built on an older harness.
"""
import os
import shlex

from . import adapters, profile
from .errors import AgentboxError

CHECK_TOOLS = ("git", "rg", "python3", "podman")
CANDIDATE_SUFFIX = "-candidate"
# Compared between the harness image and the candidate. Everything here is part of how the launcher starts
# the agent: `wrapper()` runs through the entrypoint it sets, the agent binary is found on PATH, and the
# container runs as the host user by way of USER plus --userns=keep-id.
PINNED_CONFIG = ("User", "Entrypoint", "Cmd")


def _config(doc):
    return (doc or {}).get("Config") or {}


def image_labels(doc):
    """An image's labels. podman reports them both at the top level and under Config; a label set by
    `podman build --label` shows up in both, but reading one place only has no reason to be the gamble."""
    return {**((doc or {}).get("Labels") or {}), **(_config(doc).get("Labels") or {})}


def _path_of(doc):
    for entry in _config(doc).get("Env") or []:
        name, sep, value = entry.partition("=")
        if sep and name == "PATH":
            return value
    return ""


def _check_script(agent_bin):
    """A shell script that reports the agent's version and any missing tool, and nothing else.

    Run with no network and no mounts: it answers "is this still a session image", which needs neither."""
    tools = " ".join(shlex.quote(t) for t in CHECK_TOOLS)
    return (f'v=$({shlex.quote(agent_bin)} --version 2>/dev/null | head -1 | tr -d "\\r"); '
            f'printf "VERSION=%s\\n" "$v"; '
            f'for t in {tools}; do command -v "$t" >/dev/null 2>&1 || printf "MISSING=%s\\n" "$t"; done; '
            f'printf "DONE\\n"')


def _parse_check(text):
    """(version, [missing tools], complete?) from the script's output."""
    version, missing, done = "", [], False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith("VERSION="):
            version = line[len("VERSION="):].strip()
        elif line.startswith("MISSING="):
            missing.append(line[len("MISSING="):].strip())
        elif line == "DONE":
            done = True
    return version, missing, done


def harness_pin(podman, image):
    """(image id, the agent CLI version `make <agent>-image` recorded on it)."""
    doc = podman.image_json(image)
    return doc.get("Id") or "", (image_labels(doc).get("agentbox.cli-version") or "").strip()


USER_LAYER_LABEL = "agentbox.user-layer"


def any_built(podman, agent):
    """Is there a personal layer for this agent at all, under any key?

    This is what tells "out of date" from "never built". `ab image build` labels what it builds, so the
    question is answered by the label rather than by guessing from tag names."""
    return bool(podman.images_json({USER_LAYER_LABEL: agent}))


def probe(podman, ref, agent_bin):
    """(version, tools of CHECK_TOOLS missing, did the check finish, podman's complaint) for one image.

    Run once per image, offline: it answers "is this a working session image", which needs no network and
    no mounts. `input=""` because the check reads nothing and a container that inherited a terminal's stdin
    could block."""
    p = podman.run(["run", "--rm", "--network=none", "--entrypoint=", ref,
                    "sh", "-c", _check_script(agent_bin)], check=False, input="")
    version, missing, done = _parse_check(p.stdout)
    if p.returncode != 0 or not done:
        err = " ".join((p.stderr or "").strip().splitlines()[-2:])
        return "", [], False, f"({p.returncode}) {err}".strip()
    return version, missing, True, ""


def verify(podman, harness, candidate, agent_bin, pinned_version, *, run_check=True):
    """[] when the candidate is a usable layer on `harness`, else the reasons it is not.

    Every check is RELATIVE to the harness image. The first real `ab image build claude` was rejected for
    "missing from the image: rg" -- when `rg` is not in that harness image either, so the layer was being
    held to a standard its own base fails (ripgrep arrives with agentbox-base). A layer can only be asked
    not to have BROKEN anything: what it must not do is lose a tool, move the agent, or change how the
    launcher starts it."""
    base, built = podman.image_json(harness), podman.image_json(candidate)
    bad = []
    for key in PINNED_CONFIG:
        if _config(built).get(key) != _config(base).get(key):
            bad.append(f"{key} differs from {harness} "
                       f"({_config(base).get(key)!r} -> {_config(built).get(key)!r})")
    if _path_of(built) != _path_of(base):
        bad.append(f"PATH differs from {harness}, so the agent binary may not be the one the launcher "
                   f"expects ({_path_of(base)!r} -> {_path_of(built)!r})")
    if not run_check:
        return bad
    base_version, base_missing, base_ok, base_why = probe(podman, harness, agent_bin)
    if not base_ok:
        # Not the layer's fault, and saying so matters: the thing to go and fix is the harness image.
        bad.append(f"the image check could not run in {harness} itself, so this layer cannot be compared "
                   f"against it: {base_why}. Rebuild it (`make <agent>-image`)")
        return bad
    version, missing, done, why = probe(podman, candidate, agent_bin)
    if not done:
        bad.append(f"the image check did not run in it: {why}")
        return bad
    lost = [t for t in missing if t not in base_missing]
    if lost:
        bad.append(f"{', '.join(lost)} present in {harness} but not in this layer")
    if not version:
        bad.append(f"{agent_bin} did not report a version in it")
    elif version != base_version:
        bad.append(f"{agent_bin} reports {version!r}, but {harness} reports {base_version!r}: a layer may "
                   f"not change the agent")
    elif pinned_version and pinned_version not in version:
        # Both agree, and neither carries the pin the label names: the harness image is off its own pin,
        # which is a `make <agent>-image` problem. Reported here because this is where it becomes visible.
        #
        # Containment, not equality: the label holds the pinned VERSION (`2.1.318`) while the binary
        # reports its own banner (`2.1.318 (Claude Code)`, `omp/18.13.3`). The image build already greps
        # the binary's output for the pin, so this is the backstop for a harness image that has gone stale
        # since, not the primary gate.
        bad.append(f"{agent_bin} reports {version!r} in both this layer and {harness}, but {harness} is "
                   f"labelled {pinned_version!r} -- the harness image is off its own pin; rebuild it")
    return bad


def build(settings, podman, agent, *, force=False, run_check=True, out=print):
    """Build the profile's Containerfile on top of the agent's harness image. Returns the image reference.

    Raises with the reason when there is nothing to build or when the result is not a usable layer; in the
    latter case the candidate is removed, so whatever was built before is still what `ab session start` would use."""
    adapter = adapters.get(agent)
    src = settings.profile_src(agent)
    recipe = profile.containerfile(src)
    if recipe is None:
        raise AgentboxError(f"no {profile.CONTAINERFILE} in {src}; there is no personal image layer to "
                            f"build. Write one there: {profile.CONTRACT}")
    profile.check_user_containerfile(recipe)
    if not podman.image_exists(adapter.image):
        raise AgentboxError(f"the harness image {adapter.image} is missing; build it first "
                            f"(`make {agent}-image`), then build your layer on it")
    harness_id, pinned = harness_pin(podman, adapter.image)
    if not harness_id:
        raise AgentboxError(f"podman did not report an image ID for {adapter.image}")
    ref = profile.user_image_ref(agent, harness_id, recipe)
    if podman.image_exists(ref) and not force:
        out(f"ab: {ref} is already built from this {profile.CONTAINERFILE} on {adapter.image} "
            f"({harness_id[:12]}); nothing to do (--force rebuilds).")
        return ref
    candidate = ref + CANDIDATE_SUFFIX
    out(f"ab: building {ref}\n    from {os.path.join(src, profile.CONTAINERFILE)}\n"
        f"    on  {adapter.image} ({harness_id[:12]})")
    # --pull=never: the base is the local harness image, and a layer must never reach for a registry one.
    podman.run(["build", "--pull=never", "--build-arg", f"BASE={adapter.image}",
                "--label", f"{USER_LAYER_LABEL}={agent}",
                "--label", f"agentbox.harness-id={harness_id}",
                "-t", candidate, src], capture=False, timeout=3600)
    try:
        bad = verify(podman, adapter.image, candidate, adapter.command[0], pinned, run_check=run_check)
    except AgentboxError as e:
        _discard(podman, candidate)
        raise AgentboxError(f"the image built, but it could not be checked ({e}); it was discarded")
    if bad:
        _discard(podman, candidate)
        kept = "the image you were using is untouched" if podman.image_exists(ref) else \
               f"no personal layer is tagged, so `ab session start` uses {adapter.image}"
        raise AgentboxError("the image built, but it is not a usable layer, so it was discarded:\n  - "
                            + "\n  - ".join(bad) + f"\n{kept}.")
    podman.run(["tag", candidate, ref])
    _discard(podman, candidate)
    out(f"ab: built {ref}. Every session of every project with the profile on now runs it; "
        f"`ab session start --no-profile` runs {adapter.image} instead.")
    return ref


def _discard(podman, ref):
    podman.run(["image", "rm", "-f", ref], check=False)
