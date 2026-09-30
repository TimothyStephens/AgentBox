"""Step 4 gate: sockguard observe tier. Audit mode first (what do the clients call?), then enforce.

Two dummy compose stacks (tests/fixtures/dummy) run under -p abt-<run>-live and -p abt-<run>-other. Project
`sgobs` may observe abt-<run>-live. Checked:
  * discovery: a deny-all sockguard in audit mode logs every call `podman --remote` makes for the observe
    commands (printed; it is how OBSERVE_RULES was written);
  * enforce: the same commands work, and see only the live stack;
  * label scoping: another project's containers are invisible; their logs, inspect and exec are refused,
    and so are exec, stop and create on the live stack itself (observe has no writes);
  * selftest passes for Claude (node client) and OMP (podman client), including those cases;
  * the per-session podman service is gone after teardown.
Audit mode forwards what it would deny, so it runs only here, against the test's own containers.
"""
import os
import shutil
import subprocess
import unittest

from agentbox import config, session, sockguard
from tests import ompfake

from . import harness

ENV = None
LIVE = f"abt-{harness.RUN}-live"
OTHER = f"abt-{harness.RUN}-other"
STACKS = os.path.join(harness.BASE, "stacks")
# WEB is replaced by a container name (str.format would eat the Go templates' braces).
OBSERVE = ("podman --remote ps -a --format '{{.Names}}'; echo PS_RC=$?; "
           "podman --remote inspect --format '{{.Name}} env={{len .Config.Env}}' WEB; echo INSPECT_RC=$?; "
           "podman --remote logs WEB | head -3; echo LOGS_RC=${PIPESTATUS[0]}; "
           "podman --remote top WEB | head -3; echo TOP_RC=${PIPESTATUS[0]}; "
           # podman stats uses GET /libpod/containers/stats, host-wide and unscopable: sockguard refuses it
           "podman --remote stats --no-stream WEB >/dev/null 2>&1; echo PODMAN_STATS_RC=$?; "
           "curl -s --unix-socket /run/agentbox/podman.sock -o /dev/null -w 'STATS=%{http_code}\\n' "
           "'http://d/v5.0.0/containers/WEB/stats?stream=false'; "
           "podman --remote pod ps; echo PODPS_RC=$?; "
           "podman --remote version --format '{{.Server.Version}}'; echo VERSION_RC=$?")


def compose(project, *args, timeout=300):
    d = os.path.join(STACKS, project)
    return subprocess.run(["podman-compose", "-p", project, "-f", os.path.join(d, "compose.yml"), *args],
                          cwd=d, capture_output=True, text=True, timeout=timeout)


def names(project):
    p = harness.podman("ps", "-a", "--format", "{{.Names}}", "--filter", f"label=io.podman.compose.project={project}")
    return sorted(p.stdout.split())


def setUpModule():
    global ENV
    if harness.podman("image", "exists", config.DEFAULTS["sockguard_image"]).returncode != 0:
        raise unittest.SkipTest("sockguard image not present")
    ENV = harness.Env()
    for project in (LIVE, OTHER):
        d = os.path.join(STACKS, project)
        shutil.copytree(harness.FIXTURE, d)
        with open(os.path.join(d, ".env"), "w") as f:
            f.write(f"GREETING=secret-of-{project}\n")
        p = compose(project, "up", "-d")
        if p.returncode != 0:
            raise AssertionError(f"compose up {project}: {p.stdout}\n{p.stderr}")
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    ENV.make_project("sgobs", extra=["--podman", "observe", "--compose-project", LIVE])
    ENV.make_project("sglogs", extra=["--podman", "observe", "--compose-project", LIVE, "--observe-logs"])
    ENV.make_project("sgnone")
    print(f"\n[itest run {harness.RUN}: step 4, live {names(LIVE)}, other {names(OTHER)}]")


def tearDownModule():
    for project in (LIVE, OTHER):
        if os.path.isdir(os.path.join(STACKS, project)):
            compose(project, "down", "-t", "2")
    harness.cleanup()


def run_in_session(project, script, *, agent="omp", discovery=False):
    """A session run in-process, so the discovery test can swap in the audit-mode config."""
    env_keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    old = {k: os.environ.get(k) for k in env_keys}
    os.environ.update({k: ENV.env[k] for k in env_keys})
    try:
        s = config.load()
        sess = session.make_session(s, agent, session.load_project(s, project), warn=lambda m: None)
        if discovery:
            sess.sockguard_config = lambda: sockguard.discovery_config(LIVE)
        sess.prepare()
        p = sess.run_captured(["bash", "-c", script], timeout=600)
        return p, sess
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def sockguard_log(sess):
    with open(sess.log_path("sockguard")) as f:
        return f.read()


class Step4(unittest.TestCase):
    maxDiff = None

    def test_a_discovery_in_audit_mode(self):
        import json
        web = names(LIVE)[0]
        p, sess = run_in_session("sgobs", OBSERVE.replace("WEB", web), discovery=True)
        print(f"\n{p.stdout[-2500:]}\n{p.stderr[-800:]}")
        calls = set()
        for line in sockguard_log(sess).splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("decision") == "would_deny":
                calls.add(f"{ev['method']} {ev['normalized_path']}")
        print("calls made by the observe commands (audit mode, would_deny):\n  " + "\n  ".join(sorted(calls)))
        self.assertTrue(calls, "the audit log recorded the client's calls")

    def test_b_observe_commands_work_under_enforce(self):
        web = names(LIVE)[0]
        p, sess = run_in_session("sgobs", OBSERVE.replace("WEB", web))
        out = p.stdout
        print(f"\n{out[-2500:]}\n{p.stderr[-800:]}")
        for rc in ("PS_RC", "INSPECT_RC", "TOP_RC", "PODPS_RC", "VERSION_RC"):
            self.assertIn(f"{rc}=0", out, rc)
        self.assertRegex(out, r"LOGS_RC=[1-9]", "logs are off by default (observe_logs)")
        p2, _s2 = run_in_session("sglogs", f"podman --remote logs {web} | head -3; echo LOGS_RC=${{PIPESTATUS[0]}}")
        print(f"\nwith observe_logs: {p2.stdout[-400:]}")
        self.assertIn("LOGS_RC=0", p2.stdout, "observe_logs = true allows the live stack's logs")
        self.assertIn("STATS=200", out, "per-container stats through the Docker API")
        self.assertRegex(out, r"PODMAN_STATS_RC=[1-9]", "podman stats (host-wide endpoint) is refused")
        listed = out.partition("PS_RC")[0].split()
        self.assertEqual(sorted(listed), names(LIVE), "only the live stack is listed")
        self.assertIn(f"{web} env=0", out, "the environment is redacted")
        denied = [l for l in sockguard_log(sess).splitlines() if '"decision":"deny"' in l]
        print("denied under enforce:", len(denied))

    def test_c_label_scoping_and_no_writes(self):
        live_web, other_web = names(LIVE)[0], names(OTHER)[0]
        script = (f"podman --remote exec {other_web} true; echo EXEC_OTHER_RC=$?; "
                  f"podman --remote logs {other_web}; echo LOGS_OTHER_RC=$?; "
                  f"podman --remote inspect {other_web}; echo INSPECT_OTHER_RC=$?; "
                  f"podman --remote exec {live_web} true; echo EXEC_LIVE_RC=$?; "
                  f"podman --remote stop -t 1 {live_web}; echo STOP_LIVE_RC=$?; "
                  f"podman --remote rm -f {other_web}; echo RM_OTHER_RC=$?; "
                  "podman --remote run --rm -v $HOME:/h docker.io/library/busybox:1.36 true; echo RUN_RC=$?; "
                  "podman --remote pull docker.io/library/alpine:3.20; echo PULL_RC=$?; "
                  f"curl -s --unix-socket /run/agentbox/podman.sock -X POST -H 'Content-Type: application/json' "
                  f"-d '{{\"Cmd\":[\"true\"]}}' -o /dev/null -w 'RAWEXEC=%{{http_code}}\\n' "
                  f"http://d/v5.0.0/libpod/containers/{other_web}/exec")
        p, _sess = run_in_session("sgobs", script)
        out = p.stdout
        print(f"\n{out[-3000:]}\n{p.stderr[-1500:]}")
        for rc in ("EXEC_OTHER_RC", "LOGS_OTHER_RC", "INSPECT_OTHER_RC", "EXEC_LIVE_RC", "STOP_LIVE_RC",
                   "RM_OTHER_RC", "RUN_RC", "PULL_RC"):
            self.assertRegex(out, rf"{rc}=[1-9]", rc)
        self.assertIn("RAWEXEC=403", out)
        self.assertNotIn(f"secret-of-{OTHER}", out + p.stderr)
        # nothing changed on the host
        running = harness.podman("ps", "--format", "{{.Names}}", "--filter",
                                 f"label=io.podman.compose.project={LIVE}").stdout.split()
        self.assertIn(live_web, running)
        self.assertIn(other_web, names(OTHER))

    def test_d_selftest_claude_and_omp(self):
        for agent in ("claude", "omp"):
            results, p = ENV.selftest(agent, "sgobs")
            print(f"\nselftest {agent}/sgobs:\n{harness.report(results)}")
            fails = [r for r in results if r["verdict"] == "FAIL"]
            self.assertFalse(fails, harness.report(fails) + "\n" + p.stderr[-1500:])
            labels = " | ".join(r["label"] for r in results)
            for want in ("podman socket present", "exec on another project's container refused",
                         "refused (observe_logs off)",
                         "logs of another project's container refused", f"shows only {LIVE}'s containers",
                         "environment redacted", "create a container refused"):
                self.assertIn(want, labels)
            self.assertNotIn("SKIP", [r["verdict"] for r in results if "canary" in r["label"]])
        results, _p = ENV.selftest("claude", "sgnone")
        self.assertIn(("no podman socket in this session", "PASS"),
                      [(r["label"], r["verdict"]) for r in results])

    def test_f_gc_stops_what_a_crashed_session_left(self):
        import signal
        proc = ENV.ab_bg("run", "--agent", "claude", "--project", "sgobs", "--shell", "--no-tty", "--",
                         "-c", "sleep 300")
        try:
            sg = harness.wait_for(lambda: harness.podman("ps", "--format", "{{.Names}}", "--filter",
                                                         f"label={harness.LABEL}", "--filter",
                                                         "label=agentbox.role=sockguard").stdout.split(), timeout=180)
            self.assertTrue(sg, "the session's sockguard did not start")
            session_name = sg[0][:-len("-sockguard")]
            services = {pid: s for pid, s in sockguard.services_under(ENV.run_dir).items() if s == session_name}
            self.assertEqual(len(services), 1, services)
            pid = next(iter(services))
            with open(f"/proc/{pid}/cgroup") as f:
                cg = f.read()
            print(f"\nservice pid {pid}: {cg.strip()}")
            if ENV.slice:
                self.assertIn("/agentbox.slice/", cg, "the podman service runs in agentbox.slice")
            # the crash: launcher killed outright, its agent gone, its run dir lost
            proc.send_signal(signal.SIGKILL)
            proc.wait(timeout=30)
            harness.podman("rm", "-f", "-t", "1", session_name)
            shutil.rmtree(os.path.join(ENV.run_dir, "sessions", session_name), ignore_errors=True)
            self.assertTrue(os.path.exists(f"/proc/{pid}"), "the service outlived the launcher")
            g = ENV.ab("gc")
            print(g.stdout, g.stderr)
            self.assertEqual(g.returncode, 0, g.stderr)
            self.assertIn(f"{session_name} (podman service)", g.stdout)
            self.assertTrue(harness.wait_for(lambda: not os.path.exists(f"/proc/{pid}"), timeout=30),
                            "gc stopped the orphaned service")
            self.assertEqual([x for x in harness.leftovers() if x.startswith(session_name)], [])
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.communicate()

    def test_e_service_and_sockguard_gone_after_teardown(self):
        p = subprocess.run(["pgrep", "-af", "system service"], capture_output=True, text=True)
        mine = [l for l in p.stdout.splitlines() if harness.BASE in l]
        self.assertEqual(mine, [], "no per-session podman service is left running")
        self.assertEqual([x for x in harness.leftovers() if "sockguard" in x or "canary" in x], [])

    def test_zz_no_leftovers(self):
        left = [x for x in harness.leftovers() if not x.startswith((LIVE, OTHER))]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
