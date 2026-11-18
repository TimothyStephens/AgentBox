"""A read-only project root (`code = ro`, the `planner` preset; docs/readonly-root-proposal.md, decided 18 Nov 2026),
on real podman: what only the host can show -- the mount layering (masks and the swap over a read-only bind),
the agent's shell starting with a read-only working directory, and writes going to the data_rw grant outside the
root and nowhere else."""
import os
import unittest

from . import harness

ENV = None
NAME = "plan"
PLANS = None


def setUpModule():
    global ENV, PLANS
    ENV = harness.Env()
    PLANS = os.path.join(harness.BASE, "plans")
    os.makedirs(PLANS, exist_ok=True)
    ENV.make_project(NAME, preset="planner", extra=["--data-rw", PLANS])
    print(f"\n[itest run {harness.RUN}: planner, root read-only, plans in {PLANS}]")


def tearDownModule():
    harness.cleanup()


def shell(script):
    p = ENV.ab("session", "start", "claude", NAME, "--shell", "--no-tty", "--", "-c", script, timeout=300)
    return p.returncode, p.stdout + p.stderr


class Planner(unittest.TestCase):
    def test_the_root_is_read_only_and_the_plans_grant_is_writable(self):
        root = os.path.join(ENV.projects, NAME)
        script = (
            f"cd {root}; "
            "touch new.txt 2>/dev/null && echo TOUCH=written || echo TOUCH=refused; "
            "echo x >> README.md 2>/dev/null && echo APPEND=written || echo APPEND=refused; "
            "mkdir d 2>/dev/null && echo MKDIR=made || echo MKDIR=refused; "
            "git -c user.name=t -c user.email=t@t commit -q --allow-empty -m x 2>/dev/null "
            "&& echo COMMIT=made || echo COMMIT=refused; "
            "echo ENV=$(cat .env); "
            "echo KEY=$(wc -c < tls/server.key); "
            f"echo plan > {PLANS}/plan.md && echo PLAN=written || echo PLAN=refused; "
            "grep -q 'READ-ONLY' /run/agentbox/claude/CLAUDE.md && echo NOTES=told || echo NOTES=silent")
        code, out = shell(script)
        print(out[-1500:])
        self.assertEqual(code, 0, out)
        for want in ("TOUCH=refused", "APPEND=refused", "MKDIR=refused", "COMMIT=refused",
                     "ENV=GREETING=dev-value", "KEY=0", "PLAN=written", "NOTES=told"):
            with self.subTest(want=want):
                self.assertIn(want, out)
        self.assertFalse(os.path.exists(os.path.join(root, "new.txt")))
        with open(os.path.join(PLANS, "plan.md")) as f:
            self.assertEqual(f.read(), "plan\n")

    def test_zz_no_leftovers(self):
        self.assertEqual(harness.leftovers(), [])
