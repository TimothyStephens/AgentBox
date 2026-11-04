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
  * **it must still be a working session image.** The built image is run once, offline, and asked for the
    agent's version (which must equal the harness image's pin) and for `git`, `rg`, `python3` and the
    podman client. A layer that breaks the agent is rejected rather than discovered at the next `ab run`.

A rejected build is never tagged: it is built under a candidate tag, checked, and only then given the real
one. So the last good image stays exactly where it was, and the failure is a message rather than a session
that will not start.

The tag is `localhost/agentbox-<agent>-user:<key>`, where the key covers the harness image ID and the
Containerfile (`profile.user_image_key`). A new harness image therefore gives a different key, nothing is
built under it, and `ab run` says "personal layer out of date" and runs the plain harness image -- it never
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


def verify(podman, harness, candidate, agent_bin, pinned_version, *, run_check=True):
    """[] when the candidate is a usable layer on `harness`, else the reasons it is not."""
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
    # input="": the check reads nothing, and a container that inherited a terminal's stdin could block.
    p = podman.run(["run", "--rm", "--network=none", "--entrypoint=", candidate,
                    "sh", "-c", _check_script(agent_bin)], check=False, input="")
    version, missing, done = _parse_check(p.stdout)
    if p.returncode != 0 or not done:
        err = (p.stderr or "").strip().splitlines()[-2:]
        bad.append(f"the image check did not run in it ({p.returncode}): {' '.join(err)}")
        return bad
    if missing:
        bad.append(f"missing from the image: {', '.join(missing)}")
    if not version:
        bad.append(f"{agent_bin} did not report a version in it")
    elif pinned_version and version != pinned_version:
        bad.append(f"{agent_bin} reports {version!r}, but {harness} is pinned at {pinned_version!r}")
    return bad


def build(settings, podman, agent, *, force=False, run_check=True, out=print):
    """Build the profile's Containerfile on top of the agent's harness image. Returns the image reference.

    Raises with the reason when there is nothing to build or when the result is not a usable layer; in the
    latter case the candidate is removed, so whatever was built before is still what `ab run` would use."""
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
               f"no personal layer is tagged, so `ab run` uses {adapter.image}"
        raise AgentboxError("the image built, but it is not a usable layer, so it was discarded:\n  - "
                            + "\n  - ".join(bad) + f"\n{kept}.")
    podman.run(["tag", candidate, ref])
    _discard(podman, candidate)
    out(f"ab: built {ref}. Every session of every project with the profile on now runs it; "
        f"`ab run --no-profile` runs {adapter.image} instead.")
    return ref


def _discard(podman, ref):
    podman.run(["image", "rm", "-f", ref], check=False)
