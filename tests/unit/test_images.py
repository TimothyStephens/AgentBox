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

    def test_the_debian_base_can_be_pinned_by_digest(self):
        with open(os.path.join(os.path.dirname(IMAGES), "Makefile")) as f:
            mk = f.read()
        self.assertIn("BOOKWORM_DIGEST", mk)
        self.assertIn("ARG DEBIAN_BASE", self.text)
        self.assertIn("FROM ${DEBIAN_BASE}", self.text)
        self.assertIn("--build-arg DEBIAN_BASE=", mk,
                      "the build must pass it, since the post-FROM ARG has no default of its own")

    def test_what_it_was_built_from_is_recorded_on_the_image(self):
        """So `podman image inspect` answers "which debian?" without anyone remembering."""
        self.assertIn('agentbox.base="${DEBIAN_BASE}"', self.text)


if __name__ == "__main__":
    unittest.main()
