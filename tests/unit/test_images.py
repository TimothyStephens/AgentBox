"""Invariants of the image Containerfiles (`images/*/Containerfile`), read as text.

The images are built by Tim, not here, so these are the checks that do not need a build -- and they cover
the two ways an image has actually gone wrong in this project:

  * **a download with no checksum.** Everything an image pulls in is verified: apt checks its own packages,
    pip runs `--require-hashes`, and a direct download uses `ADD --checksum=`, which podman verifies BEFORE
    the file is used. A `curl | tar` in a RUN step would bypass all of it, and nothing about the resulting
    image would look different.
  * **an ARG declared before FROM and used after it.** `FROM` ends the pre-build stage: an ARG above it is
    not in scope below, so a reference reads as empty with no error at all. omp-env's Containerfile pinned
    the OMP version that way, which is why it asked for 18.1.3 and shipped 18.1.11 -- a pin that silently
    was not one. An ARG used *in* the FROM line is the one case that must come first.
"""
import os
import re
import unittest

IMAGES = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "images")
# A RUN step that fetches something itself, instead of letting ADD --checksum or a package manager do it.
UNVERIFIED_FETCH = re.compile(r"\b(?:curl|wget)\b[^\n]*\bhttps?://", re.I)
ARG_DECL = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
ARG_USE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def makefile():
    with open(os.path.join(os.path.dirname(IMAGES), "Makefile")) as f:
        return f.read()


def containerfiles():
    for name in sorted(os.listdir(IMAGES)):
        path = os.path.join(IMAGES, name, "Containerfile")
        if os.path.isfile(path):
            with open(path) as f:
                yield name, f.read()


def logical_lines(text):
    """Containerfile lines with backslash continuations joined and comments dropped."""
    out, buf = [], ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        buf += raw[:-1] + " " if raw.rstrip().endswith("\\") else raw
        if not raw.rstrip().endswith("\\"):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


class EveryDownloadIsVerified(unittest.TestCase):
    def test_there_is_at_least_one_containerfile(self):
        """So that an empty images/ directory cannot make every test below pass vacuously."""
        self.assertTrue(list(containerfiles()))

    def test_every_add_from_a_url_has_a_checksum(self):
        for name, text in containerfiles():
            for line in logical_lines(text):
                if not line.upper().startswith("ADD "):
                    continue
                if "://" not in line:
                    continue                      # a local path: COPY semantics, nothing to verify
                with self.subTest(image=name, line=line[:60]):
                    self.assertIn("--checksum=", line,
                                  "a URL in ADD must be pinned with --checksum, which podman verifies "
                                  "before the file is used")

    def test_no_run_step_fetches_over_http_itself(self):
        """`curl ... | tar` or `wget && install` would download whatever the URL serves that day."""
        for name, text in containerfiles():
            for line in logical_lines(text):
                if not line.upper().startswith("RUN "):
                    continue
                with self.subTest(image=name, line=line[:60]):
                    self.assertIsNone(UNVERIFIED_FETCH.search(line),
                                      "fetch it with ADD --checksum, or install it from a package manager "
                                      "that verifies its own packages")

    def test_pip_installs_require_hashes(self):
        for name, text in containerfiles():
            for line in logical_lines(text):
                if re.search(r"\bpip3?\s+install\b", line):
                    with self.subTest(image=name, line=line[:60]):
                        self.assertIn("--require-hashes", line)

    def test_every_requirements_file_pins_every_entry_with_a_hash(self):
        for name in sorted(os.listdir(IMAGES)):
            path = os.path.join(IMAGES, name, "requirements.txt")
            if not os.path.isfile(path):
                continue
            with open(path) as f:
                text = f.read()
            for line in logical_lines(text):
                with self.subTest(image=name, line=line[:40]):
                    self.assertIn("--hash=sha256:", line, "pinned with hashes, including dependencies")
                    self.assertRegex(line, r"^[A-Za-z0-9._-]+==", "and to an exact version")


class ArgsAreInScopeWhereTheyAreUsed(unittest.TestCase):
    def test_an_arg_used_after_from_is_declared_after_from(self):
        """The omp-env bug: declared above FROM, referenced below it, silently empty."""
        for name, text in containerfiles():
            lines = logical_lines(text)
            from_at = [i for i, l in enumerate(lines) if l.upper().startswith("FROM ")]
            self.assertTrue(from_at, f"{name}: no FROM")
            last_from = from_at[-1]
            before = {m.group(1) for i, l in enumerate(lines) if i < last_from for m in [ARG_DECL.match(l)] if m}
            after = {m.group(1) for i, l in enumerate(lines) if i > last_from for m in [ARG_DECL.match(l)] if m}
            for i, line in enumerate(lines):
                if i <= last_from or ARG_DECL.match(line):
                    continue
                for used in ARG_USE.findall(line):
                    if used in before and used not in after:
                        self.fail(f"{name}: ${used} is used after FROM but only declared before it, so it "
                                  f"reads as empty: {line[:70]}")

    def test_an_arg_used_in_the_from_line_is_declared_before_it(self):
        for name, text in containerfiles():
            lines = logical_lines(text)
            declared = []
            for line in lines:
                m = ARG_DECL.match(line)
                if m:
                    declared.append(m.group(1))
                if line.upper().startswith("FROM "):
                    for used in ARG_USE.findall(line):
                        with self.subTest(image=name):
                            self.assertIn(used, declared,
                                          f"{name}: FROM uses ${used}, which must be declared above it")


class TheBaseImage(unittest.TestCase):
    """What the agent layers are entitled to assume is already there."""

    TOOLS = ("git", "curl", "ca-certificates", "unzip", "jq", "rsync",
             "python3", "python3-pip", "python3-venv", "ripgrep", "fd-find", "gcc", "g++", "make")

    def setUp(self):
        with open(os.path.join(IMAGES, "base", "Containerfile")) as f:
            self.text = f.read()

    def test_it_installs_the_shared_toolchain(self):
        """Checked against the apt install step, not the file: the comment above it names these tools too,
        and matching the whole text passed with `ripgrep` deleted from the packages (found by mutation)."""
        installs = [l for l in logical_lines(self.text) if re.search(r"\bapt-get\s+install\b", l)]
        self.assertTrue(installs, "no apt-get install step")
        packages = set(" ".join(installs).split())
        for tool in self.TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, packages)

    def test_fd_is_reachable_as_fd(self):
        """Debian installs fd-find's binary as `fdfind`; agents reach for `fd`."""
        self.assertIn("/usr/local/bin/fd", self.text)

    def test_the_podman_client_comes_from_the_makefile(self):
        """One place decides which client every image carries, so it cannot drift from the host service."""
        self.assertIn("ARG PODMAN_REMOTE_VERSION", self.text)
        self.assertIn("ARG PODMAN_REMOTE_SHA256", self.text)
        with open(os.path.join(os.path.dirname(IMAGES), "Makefile")) as f:
            mk = f.read()
        self.assertIn("PODMAN_REMOTE_VERSION :=", mk)
        self.assertIn("--build-arg PODMAN_REMOTE_VERSION=", mk)

    def test_the_os_base_can_be_pinned_by_digest(self):
        self.assertIn("OS_BASE_DIGEST", makefile())
        self.assertIn("ARG OS_BASE", self.text)
        self.assertIn("FROM ${OS_BASE}", self.text)
        self.assertIn("--build-arg OS_BASE=", makefile(),
                      "the build must pass it, since the post-FROM ARG has no default of its own")

    def test_what_it_was_built_from_is_recorded_on_the_image(self):
        """So `podman image inspect` answers "which base?" without anyone remembering."""
        self.assertIn('agentbox.base="${OS_BASE}"', self.text)

    def test_node_is_in_the_base_not_in_each_agent_layer(self):
        """Both agents need node 22 or newer -- Claude Code declares engines.node >= 22, and OMP installs
        with npm -- so it is shared. A pinned node download in two layers is the duplication the base
        exists to remove, and the official node image is maintained and needs no checksum of ours."""
        self.assertRegex(self.text, r"FROM \$\{OS_BASE\}")
        self.assertRegex(makefile(), r"(?m)^NODE_MAJOR := \d+$", "the major lives in one place")
        for agent in ("claude", "omp"):
            with open(os.path.join(IMAGES, agent, "Containerfile")) as f:
                layer = f.read()
            with self.subTest(agent=agent):
                self.assertNotIn("nodejs.org", layer, "node comes from the base")
                self.assertIn("FROM ${BASE}", layer, "and the layer builds on agentbox-base")

    def test_the_os_base_default_matches_the_makefile(self):
        """The Makefile always passes OS_BASE, so the Containerfile's default only matters to a hand-build
        -- which makes a stale default a trap rather than an error. They have to name the same image."""
        mk = makefile()
        major = re.search(r"(?m)^NODE_MAJOR := (\d+)$", mk)
        self.assertTrue(major, "NODE_MAJOR not found in the Makefile")
        tag = re.search(r"(?m)^OS_BASE_TAG := (\S+)$", mk)
        self.assertTrue(tag, "OS_BASE_TAG not found in the Makefile")
        expected = tag.group(1).replace("$(NODE_MAJOR)", major.group(1))
        default = re.search(r"(?m)^ARG OS_BASE=(\S+)$", self.text)
        self.assertTrue(default, "the Containerfile declares no OS_BASE default")
        self.assertEqual(default.group(1), expected)

    def test_the_build_proves_which_node_it_got(self):
        """The one that was missing, and it cost a wrong image. `OS_BASE` can carry a TAG AND A DIGEST, and
        podman resolves the digest and ignores the tag -- so `node:26-bookworm-slim@sha256:<a node 22
        digest>` builds node 22 while every name in the Makefile says 26. The build printed
        `node --version` and nothing compared it, so a session ran node v22.23.4 from an image whose tag
        said 26 (5 Nov). A printed version is not a check."""
        mk = makefile()
        major = re.search(r"(?m)^NODE_MAJOR := (\d+)$", mk).group(1)
        self.assertRegex(self.text, r"(?m)^ARG NODE_MAJOR$",
                         "NODE_MAJOR is not declared after the FROM, so ${NODE_MAJOR} below is empty")
        self.assertIn("--build-arg NODE_MAJOR=$(NODE_MAJOR)", mk,
                      "make base-image does not pass NODE_MAJOR, so the assertion cannot fire")
        # The assertion itself, not the comment that explains it: a RUN line that compares node's version
        # with the ARG, and a refusal when the ARG is empty (an unpassed ARG must not read as "fine").
        steps = [l for l in self.text.splitlines() if not l.lstrip().startswith("#")]
        body = "\n".join(steps)
        # The prefix only: the Containerfile needs `\\.` so the parser hands grep a `\.`, and asserting
        # that escape level here would just encode one fiddly detail in two places.
        self.assertIn('node --version | grep -Eq "^v${NODE_MAJOR}', body,
                      "nothing compares the node in the image with NODE_MAJOR")
        self.assertIn('test -n "${NODE_MAJOR}"', body,
                      "an unpassed NODE_MAJOR would make the comparison vacuous")
        self.assertIn('agentbox.node-major="${NODE_MAJOR}"', body,
                      "the major is not recorded on the image, so a stale image cannot be told apart")
        self.assertNotIn(f"v{major}", body, "the major belongs in the Makefile, not written out here")


class TheAgentLayers(unittest.TestCase):
    """Each agent image installs its own CLI at a pin, and proves the pin rather than claiming it."""

    AGENTS = {"claude": ("CLAUDE_VERSION", "claude"), "omp": ("OMP_VERSION", "omp")}

    def layer(self, agent):
        with open(os.path.join(IMAGES, agent, "Containerfile")) as f:
            return f.read()

    def test_each_builds_on_agentbox_base_and_nothing_else(self):
        """They used to be FROM localhost/{claudecode,omp-env}, built outside this repo -- so the image
        shipped whatever CLI those happened to have (56 and 59 days old at the last check).

        Checked on the DIRECTIVES, not the file: both files explain in prose what they no longer build on,
        and matching the whole text failed on its own comments (the same trap as the ripgrep check)."""
        for agent in self.AGENTS:
            with self.subTest(agent=agent):
                lines = logical_lines(self.layer(agent))
                froms = [l for l in lines if l.upper().startswith("FROM ")]
                self.assertEqual(froms, ["FROM ${BASE}"])
                self.assertIn("ARG BASE=localhost/agentbox-base:latest", lines)
                for old_base in ("localhost/claudecode", "localhost/omp-env"):
                    self.assertFalse([l for l in lines if old_base in l],
                                     f"{old_base} is still referenced by a directive")

    def test_the_installed_cli_is_asserted_against_the_pin(self):
        """A pin that is only written down is not a pin. Each build runs the binary and greps for its own
        version, so the image cannot ship a different one -- which is exactly what omp-env did by declaring
        its ARG above the FROM (asks 18.1.3, ships 18.1.11)."""
        for agent, (arg, binary) in self.AGENTS.items():
            with self.subTest(agent=agent):
                text = self.layer(agent)
                self.assertRegex(text, rf"ARG {arg}=\d", "pinned to a version, after the FROM")
                self.assertIn(f'{binary} --version | grep -F "${{{arg}}}"', text)

    def test_the_pin_is_on_the_label_ab_selftest_reads(self):
        for agent, (arg, _bin) in self.AGENTS.items():
            with self.subTest(agent=agent):
                self.assertIn(f'agentbox.cli-version="${{{arg}}}"', self.layer(agent))

    def test_claude_has_its_auto_updater_off_in_the_image(self):
        """Not only in the session environment: an updater that replaced the binary would make the label a
        lie and `ab selftest`'s version check wrong."""
        self.assertIn("DISABLE_AUTOUPDATER=1", self.layer("claude"))

    def test_omp_gets_bun_because_it_cannot_load_extensions_without_it(self):
        """`@oh-my-pi/pi-coding-agent@18.13.3` declares `engines.bun >= 1.4.4`."""
        text = self.layer("omp")
        self.assertIn("ARG BUN_VERSION", text)
        self.assertIn("bun-linux-x64.zip", text)
        self.assertIn("bun.sh/install", makefile(),
                      "and the Makefile says why it is not piped from bun.sh/install")

    def test_omp_keeps_the_root_guard_and_the_omp_symlink(self):
        """Bun lives in /root/.bun and the session runs as the host user, so /root is opened -- after a
        step that fails the build on anything credential-like. The symlink is for the absolute
        /root/.omp/... paths `omp-env install` writes into the plugin links on the host."""
        text = self.layer("omp")
        self.assertIn("chmod 0755 /root", text)
        self.assertIn('ln -sfn "${AB_HOME}/.omp" /root/.omp', text)
        self.assertIn("credential-like files in /root", text)

    def test_neither_layer_carries_a_container_client_of_its_own(self):
        """agentbox-base has the pinned podman-remote that must shadow Debian's; omp-env's `podman` and
        `docker.io` packages were used by no agentbox path (checked 4 Nov)."""
        for agent in self.AGENTS:
            with self.subTest(agent=agent):
                installs = [l for l in logical_lines(self.layer(agent))
                            if re.search(r"\bapt-get\s+install\b", l)]
                packages = set(" ".join(installs).split())
                self.assertNotIn("podman", packages)
                self.assertNotIn("docker.io", packages)


def make_vars():
    """{name: value} for the Makefile's `NAME := value` assignments, continuations joined."""
    out, buf = {}, ""
    for raw in makefile().splitlines():
        if raw.lstrip().startswith("#"):
            continue
        buf += raw[:-1].rstrip() + " " if raw.rstrip().endswith("\\") else raw
        if not raw.rstrip().endswith("\\"):
            m = re.match(r"^([A-Z][A-Z0-9_]*)\s*[:?]?=\s*(.*)$", buf.strip())
            if m:
                out[m.group(1)] = m.group(2)
            buf = ""
    return out


def build_args_for(image):
    """The --build-arg names the Makefile passes when building this image, with $(VAR) expanded."""
    mv, mk = make_vars(), makefile()
    if image == "base":
        line = [l for l in mk.splitlines() if "-t localhost/agentbox-base:latest" in l
                or ("podman build" in l and "OS_BASE" in l)]
        text = " ".join(line)
    else:
        text = mv.get(f"{image.upper()}_BUILD_ARGS", "")
    for _ in range(5):                                  # expand $(VAR), a couple of levels is plenty
        new = re.sub(r"\$\(([A-Z][A-Z0-9_]*)\)", lambda m: mv.get(m.group(1), ""), text)
        if new == text:
            break
        text = new
    return set(re.findall(r"--build-arg\s+([A-Za-z_][A-Za-z0-9_]*)=", text))


def declared_args(text, after_from_only=False):
    lines = logical_lines(text)
    froms = [i for i, l in enumerate(lines) if l.upper().startswith("FROM ")]
    pick = lines[froms[-1] + 1:] if (after_from_only and froms) else lines
    return {m.group(1): l for l in pick for m in [ARG_DECL.match(l)] if m}


class ArgsAndBuildArgsMatch(unittest.TestCase):
    """Both directions, because each failure mode is silent.

    An ARG the build does not pass is EMPTY -- no error, just a step that did something other than what the
    file says (the same shape as declaring one above the FROM). And a --build-arg nothing consumes is a
    no-op podman only mentions in passing: `make omp-image` printed "one or more build args were not
    consumed: [PODMAN_REMOTE_SHA256 PODMAN_REMOTE_VERSION]" after the client moved to the base (4 Nov).
    Harmless there, but indistinguishable from a pin that is not being applied where you think it is."""

    IMAGES = ("base", "claude", "omp")

    def test_the_makefile_passes_every_defaultless_arg(self):
        for name, text in containerfiles():
            passed = build_args_for(name)
            for arg, line in declared_args(text, after_from_only=True).items():
                if "=" in line.partition(arg)[2]:
                    continue                            # has a default of its own
                with self.subTest(image=name, arg=arg):
                    self.assertIn(arg, passed,
                                  f"{name}: ARG {arg} has no default, so the build must pass it")

    def test_every_build_arg_is_consumed_by_the_image_it_is_passed_to(self):
        for image in self.IMAGES:
            with open(os.path.join(IMAGES, image, "Containerfile")) as f:
                declared = declared_args(f.read())
            for arg in build_args_for(image):
                with self.subTest(image=image, arg=arg):
                    self.assertIn(arg, declared,
                                  f"{image}: --build-arg {arg} is passed but the Containerfile declares no "
                                  f"such ARG, so podman ignores it")

    def test_the_collector_actually_finds_something(self):
        """Both tests above pass vacuously if the Makefile parse returns nothing."""
        for image in self.IMAGES:
            with self.subTest(image=image):
                self.assertTrue(build_args_for(image), f"no --build-arg found for {image}")


if __name__ == "__main__":
    unittest.main()
