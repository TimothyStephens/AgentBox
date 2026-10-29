"""The host-owned agent profile, against real podman: what a session can read, and what it cannot write.

The unit tests pin the argv and the snapshot logic. These are the cases only the kernel can answer -- a
read-only bind really does refuse a write, a second project really does get the same bytes, and a host-side
edit really does not reach a container that is already running.

OMP's cases are present and skipped, deliberately: its profile load paths are not wired yet, because
autolearn writes into the very directories a profile would occupy read-only (see docs/TODO.md, "OMP profile
load paths"). A skip that names the reason is better than a gap.
"""
import json
import os
import shutil
import signal
import subprocess
import unittest

from . import harness
from .harness import LABEL, podman

ENV = None
SKILL = "abt-probe"
SKILL_TEXT = "---\nname: abt-probe\ndescription: an agentbox itest probe\n---\nSay PROBE-OK.\n"


def setUpModule():
    global ENV
    ENV = harness.Env()
    ENV.make_project("prof1")
    ENV.make_project("prof2")
    ENV.make_project("proflock", preset="lockdown")
    profile_src = os.path.join(ENV.cfg, "claude", "profile")
    os.makedirs(os.path.join(profile_src, "skills", SKILL), mode=0o700, exist_ok=True)
    with open(os.path.join(profile_src, "skills", SKILL, "SKILL.md"), "w") as f:
        f.write(SKILL_TEXT)
    with open(os.path.join(profile_src, "CLAUDE.md"), "w") as f:
        f.write("PROFILE-INSTRUCTIONS-OK\n")
    print(f"\n[itest run {harness.RUN}: profile at {profile_src}]")


def tearDownModule():
    harness.cleanup()


CONFIG = "/run/agentbox/claude"


def shell(project, script, timeout=300):
    return ENV.ab("run", "--agent", "claude", "--project", project, "--shell", "--no-tty", "--",
                  "-c", script, timeout=timeout)


class Profile(unittest.TestCase):
    maxDiff = None

    def test_a_profile_skill_is_readable_in_the_session(self):
        """What the agent loads from. Whether Claude Code then USES the skill is a model call, so that half
        belongs to `make claude-live`; this is the half the kernel decides."""
        p = shell("prof1", f"cat {CONFIG}/skills/{SKILL}/SKILL.md; echo RC=$?")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertIn("an agentbox itest probe", p.stdout)
        self.assertIn("RC=0", p.stdout)

    def test_the_profile_paths_are_read_only(self):
        """Each write is tested on its own exit status, not a pipeline's: `cmd | head` reports HEAD's status,
        which is 0 whatever the command did (that is what made the first version of this test vacuous)."""
        p = shell("prof1", f"""
            if touch {CONFIG}/skills/evil.md 2>&1; then echo TOUCH_WROTE; else echo TOUCH_REFUSED; fi
            if rm -f {CONFIG}/skills/{SKILL}/SKILL.md 2>&1; then echo RM_WROTE; else echo RM_REFUSED; fi
            if (echo x > {CONFIG}/CLAUDE.md) 2>&1; then echo MD_WROTE; else echo MD_REFUSED; fi
            if (echo x > {CONFIG}/commands/evil.md) 2>&1; then echo CMD_WROTE; else echo CMD_REFUSED; fi
            test -f {CONFIG}/skills/{SKILL}/SKILL.md && echo SKILL_STILL_THERE
            echo "MD_BYTES=$(wc -c < {CONFIG}/CLAUDE.md)"
            grep -c 'agentbox sandbox' {CONFIG}/CLAUDE.md
        """)
        for what in ("TOUCH", "RM", "MD", "CMD"):
            self.assertIn(f"{what}_REFUSED", p.stdout, f"{what}: a write to a profile path must fail")
            self.assertNotIn(f"{what}_WROTE", p.stdout)
        self.assertIn("Read-only file system", p.stdout)
        self.assertIn("SKILL_STILL_THERE", p.stdout, "and the skill is untouched")
        self.assertNotIn("MD_BYTES=2", p.stdout, "CLAUDE.md was not replaced by `x`")
        self.assertIn("1", p.stdout.strip().splitlines()[-1], "it is still the generated file")

    def test_the_generated_instructions_lead_with_the_notes_then_the_profile(self):
        p = shell("prof1", f"cat {CONFIG}/CLAUDE.md")
        self.assertIn("agentbox sandbox", p.stdout)
        self.assertIn("PROFILE-INSTRUCTIONS-OK", p.stdout)
        self.assertLess(p.stdout.index("agentbox sandbox"), p.stdout.index("PROFILE-INSTRUCTIONS-OK"))

    def test_a_second_project_sees_the_same_profile(self):
        a = shell("prof1", f"sha256sum {CONFIG}/skills/{SKILL}/SKILL.md | cut -d' ' -f1")
        b = shell("prof2", f"sha256sum {CONFIG}/skills/{SKILL}/SKILL.md | cut -d' ' -f1")
        digest = a.stdout.strip().splitlines()[-1]
        self.assertEqual(len(digest), 64, a.stdout)
        self.assertEqual(digest, b.stdout.strip().splitlines()[-1])

    def test_lockdown_loads_the_profile_too(self):
        """A repo's own .claude is covered empty under lockdown; the host-owned profile is not repo-shipped
        code, so it is exactly what must still load there."""
        p = shell("proflock", f"cat {CONFIG}/skills/{SKILL}/SKILL.md")
        self.assertIn("an agentbox itest probe", p.stdout)

    def test_a_lockdown_inbox_write_does_not_survive_the_session(self):
        """Lockdown keeps nothing, the inbox included: it lives in the session's ephemeral state."""
        p = shell("proflock", f"""
            mkdir -p {CONFIG}/inbox/gone && echo 'not kept' > {CONFIG}/inbox/gone/SKILL.md
            echo WROTE=$?
            grep -c 'keeps nothing' {CONFIG}/CLAUDE.md
        """)
        self.assertIn("WROTE=0", p.stdout, p.stderr[-1500:])
        self.assertIn("1", p.stdout.strip().splitlines()[-1], "the agent was told")
        self.assertFalse(os.path.exists(os.path.join(ENV.data, "proflock", "claude", "inbox", "gone")))
        self.assertFalse([n for n in os.listdir(os.path.join(ENV.data, "ephemeral"))
                          if os.path.isdir(os.path.join(ENV.data, "ephemeral", n))]
                         if os.path.isdir(os.path.join(ENV.data, "ephemeral")) else [],
                         "the ephemeral state is gone with the session")
        out = ENV.ab("profile", "harvest", "--agent", "claude", check=True).stdout
        self.assertNotIn("gone", out, "nothing was queued for review")

    def test_an_inbox_write_persists_and_is_not_a_load_path(self):
        first = shell("prof1", f"""
            mkdir -p {CONFIG}/inbox/learned && echo 'kept across sessions' > {CONFIG}/inbox/learned/SKILL.md
            echo WROTE=$?
        """)
        self.assertIn("WROTE=0", first.stdout, first.stderr[-1500:])
        second = shell("prof1", f"""
            cat {CONFIG}/inbox/learned/SKILL.md
            ls {CONFIG}/skills/ | tr '\\n' ' '
        """)
        self.assertIn("kept across sessions", second.stdout, "the inbox survived the session")
        self.assertNotIn("learned", second.stdout.split("\n")[-2], "and is not under a load path")
        host = os.path.join(ENV.data, "prof1", "claude", "inbox", "learned")
        self.assertTrue(os.path.isdir(host), host)
        with open(os.path.join(ENV.data, "prof1", "claude", "inbox", "learned.provenance.json")) as f:
            doc = json.load(f)
        self.assertEqual((doc["project"], doc["agent"], doc["reviewed"]), ("prof1", "claude", False))
        self.assertTrue(doc["session"].startswith(f"abt-{harness.RUN}-claude-prof1-"), doc)
        out = ENV.ab("profile", "harvest", "--agent", "claude", check=True).stdout
        self.assertIn("learned", out)

    def test_a_host_profile_edit_does_not_reach_a_running_session(self):
        """The snapshot is taken at start, so what a session loaded cannot change under it."""
        proc = ENV.ab_bg("run", "--agent", "claude", "--project", "prof2", "--shell", "--no-tty", "--",
                         "-c", "sleep 120")
        try:
            name = harness.wait_for(lambda: podman("ps", "--format", "{{.Names}}", "--filter",
                                                   f"label={LABEL}", "--filter",
                                                   "label=agentbox.project=prof2", "--filter",
                                                   "label=agentbox.role=agent").stdout.split(), timeout=180)
            self.assertTrue(name, "the session did not start")
            live = os.path.join(ENV.cfg, "claude", "profile", "skills", SKILL, "SKILL.md")
            with open(live, "w") as f:
                f.write("---\nname: abt-probe\n---\nEDITED-MID-SESSION\n")
            inside = podman("exec", name[0], "cat", f"{CONFIG}/skills/{SKILL}/SKILL.md")
            self.assertEqual(inside.returncode, 0, inside.stderr)
            self.assertNotIn("EDITED-MID-SESSION", inside.stdout)
            self.assertIn("an agentbox itest probe", inside.stdout)
        finally:
            with open(os.path.join(ENV.cfg, "claude", "profile", "skills", SKILL, "SKILL.md"), "w") as f:
                f.write(SKILL_TEXT)
            # SIGTERM, not SIGKILL: the launcher tears the session down on it. Killing it outright leaves
            # the proxy and the networks behind -- a husk for `ab session gc`, and a leftover here.
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.communicate()
            harness.wait_for(lambda: not [x for x in harness.leftovers() if x.startswith("abt-")
                                          and "prof2" in x] or None, timeout=60)

    def test_the_session_log_records_the_hash_and_the_commit(self):
        shell("prof1", "true")
        logs = sorted(os.listdir(os.path.join(ENV.data, "logs")))
        # Name the AGENT too: an OMP session of the same project writes a profile log as well, and it sorts
        # last -- which is the log the first version of this test read (hash=(none), no OMP load paths yet).
        profile_logs = [n for n in logs if n.endswith("-profile.log") and "-claude-prof1-" in n]
        self.assertTrue(profile_logs, logs)
        with open(os.path.join(ENV.data, "logs", profile_logs[-1])) as f:
            line = f.read()
        self.assertIn("agent=claude", line)
        self.assertRegex(line, r"hash=[0-9a-f]{16} commit=[0-9a-f]{7,} dirty=(yes|no)")
        self.assertIn("loaded=skills", line)

    def test_promote_puts_an_inbox_item_in_front_of_the_next_session(self):
        shell("prof2", f"mkdir -p {CONFIG}/inbox/abt-promoted && "
                       f"printf '%s' '---\\nname: abt-promoted\\n---\\nPROMOTED-OK\\n' "
                       f"> {CONFIG}/inbox/abt-promoted/SKILL.md")
        p = ENV.ab("profile", "promote", "prof2/abt-promoted", "--as", "skills/abt-promoted", check=True)
        self.assertIn("promoted", p.stdout)
        after = shell("prof1", f"cat {CONFIG}/skills/abt-promoted/SKILL.md")
        self.assertIn("PROMOTED-OK", after.stdout, "every project's next session loads it")
        shutil.rmtree(os.path.join(ENV.cfg, "claude", "profile", "skills", "abt-promoted"))

    @unittest.skip("OMP profile load paths are not wired: autolearn writes into the same directories "
                   "(docs/TODO.md). The inbox, harvest and snapshot do work for OMP.")
    def test_omp_reads_a_profile_skill(self):
        pass

    @unittest.skip("OMP profile load paths are not wired (docs/TODO.md)")
    def test_omp_profile_paths_are_read_only(self):
        pass

    def test_omp_gets_an_inbox_and_its_provenance(self):
        """The half of the design that is wired for OMP today."""
        if harness.podman("image", "exists", "localhost/agentbox-omp:latest").returncode != 0:
            self.skipTest("no OMP image on this host (make omp-image)")
        p = ENV.ab("run", "--agent", "omp", "--project", "prof1", "--shell", "--no-tty", "--",
                   "-c", "mkdir -p ~/.omp/agent/inbox/omp-learned && echo note > "
                         "~/.omp/agent/inbox/omp-learned/note.md && echo WROTE=$?", timeout=600)
        self.assertIn("WROTE=0", p.stdout, p.stderr[-2000:])
        host = os.path.join(ENV.data, "prof1", "omp", "inbox", "omp-learned")
        self.assertTrue(os.path.isdir(host), host)
        out = ENV.ab("profile", "harvest", "--agent", "omp", check=True).stdout
        self.assertIn("omp-learned", out)

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
