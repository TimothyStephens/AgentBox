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
           # inspect through the Docker API (libpod inspect returns the create command with -e values)
           "curl -s --unix-socket /run/agentbox/podman.sock 'http://d/v5.0.0/containers/WEB/json' | "
           "python3 -c 'import json,sys; c=json.load(sys.stdin)[\"Config\"]; print(\"INSPECT env=%d\" % len(c.get(\"Env\") or []))'; "
           "podman --remote inspect WEB >/dev/null 2>&1; echo PODMAN_INSPECT_RC=$?; "
           "podman --remote logs WEB | head -3; echo LOGS_RC=${PIPESTATUS[0]}; "
           "podman --remote top WEB >/dev/null 2>&1; echo TOP_RC=$?; "
           # podman stats uses GET /libpod/containers/stats, host-wide and unscopable: sockguard refuses it
           "podman --remote stats --no-stream WEB >/dev/null 2>&1; echo PODMAN_STATS_RC=$?; "
           "curl -s --unix-socket /run/agentbox/podman.sock -o /dev/null -w 'STATS=%{http_code}\\n' "
           "'http://d/v5.0.0/containers/WEB/stats?stream=false'; "
           "podman --remote pod ps >/dev/null 2>&1; echo PODPS_RC=$?; "
           # copying files out of (or into) the live stack: refused (review step 5B, G5)
           "curl -s --unix-socket /run/agentbox/podman.sock -o /dev/null -w 'ARCHIVE_GET=%{http_code}\\n' "
           "'http://d/v5.0.0/containers/WEB/archive?path=/etc/hostname'; "
           "curl -s --unix-socket /run/agentbox/podman.sock -X PUT -o /dev/null -w 'ARCHIVE_PUT=%{http_code}\\n' "
           "-H 'Content-Type: application/x-tar' --data-binary '' 'http://d/v5.0.0/containers/WEB/archive?path=/tmp'; "
           "curl -s --unix-socket /run/agentbox/podman.sock -o /dev/null -w 'LIBPOD_INSPECT=%{http_code}\\n' "
           "'http://d/v5.0.0/libpod/containers/WEB/json'; "
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
        # review 1 #1: a value given as `environment:` reaches podman as `-e KEY=VALUE` on the create command
        with open(os.path.join(d, "compose.yml")) as f:
            text = f.read()
        text = text.replace("    env_file: .env\n", f"    env_file: .env\n    environment:\n      - LIVE_SECRET=envsecret-of-{project}\n")
        with open(os.path.join(d, "compose.yml"), "w") as f:
            f.write(text)
        p = compose(project, "up", "-d")
        if p.returncode != 0:
            raise AssertionError(f"compose up {project}: {p.stdout}\n{p.stderr}")
    # The installed plugin tree, where `ab profile install omp` puts it: there is no
    # setting to point anywhere else, and nothing reads the host's ~/.omp (5 Nov).
    tree = ompfake.build(ENV.cfg)
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
        for rc in ("PS_RC", "VERSION_RC"):
            self.assertIn(f"{rc}=0", out, rc)
        for rc in ("PODMAN_INSPECT_RC", "TOP_RC", "PODPS_RC"):
            self.assertRegex(out, rf"{rc}=[1-9]", f"{rc}: refused under observe (review 2026-10-01)")
        self.assertRegex(out, r"LOGS_RC=[1-9]", "logs are off by default (observe_logs)")
        p2, _s2 = run_in_session("sglogs", f"podman --remote logs {web} | head -3; echo LOGS_RC=${{PIPESTATUS[0]}}")
        print(f"\nwith observe_logs: {p2.stdout[-400:]}")
        self.assertIn("LOGS_RC=0", p2.stdout, "observe_logs = true allows the live stack's logs")
        self.assertIn("STATS=200", out, "per-container stats through the Docker API")
        self.assertRegex(out, r"PODMAN_STATS_RC=[1-9]", "podman stats (host-wide endpoint) is refused")
        listed = out.partition("PS_RC")[0].split()
        self.assertEqual(sorted(listed), names(LIVE), "only the live stack is listed")
        self.assertIn("INSPECT env=0", out, "Docker-API inspect works, with the environment redacted")
        for probe in ("ARCHIVE_GET", "ARCHIVE_PUT", "LIBPOD_INSPECT"):
            self.assertIn(f"{probe}=403", out, f"{probe}: refused under observe (review step 5B, G5)")
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
            # and its service dir: only the /proc scan can find the service now
            shutil.rmtree(os.path.join(ENV.run_dir, "services", session_name), ignore_errors=True)
            self.assertTrue(os.path.exists(f"/proc/{pid}"), "the service outlived the launcher")
            g = ENV.ab("session", "gc")
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

    def test_g_client_versions_match_the_server(self):
        """Both agent images carry podman-remote (first on PATH) of the server's major version, the compose
        tool at the host's version, curl and a working python3 -m venv."""
        import re
        host = subprocess.run(["podman-compose", "--version"], capture_output=True, text=True).stdout
        host_compose = re.search(r"podman-compose version (\S+)", host).group(1)
        script = ("command -v podman; podman --remote version --format 'CLIENT={{.Client.Version}} "
                  "SERVER={{.Server.Version}}'; podman-compose version 2>/dev/null | grep -i 'podman-compose version'; "
                  # the trial (2 Oct): agents need an HTTP client and a venv
                  "curl --version >/dev/null && echo CURL_OK; python3 -m venv /tmp/abvenv >/dev/null && echo VENV_OK")
        for agent in ("claude", "omp"):
            p, _sess = run_in_session("sgobs", script, agent=agent)
            print(f"\n{agent}: {p.stdout.strip()}\n{p.stderr[-300:]}")
            self.assertIn("/usr/local/bin/podman\n", p.stdout, f"{agent}: podman-remote is first on PATH")
            m = re.search(r"CLIENT=(\d+)\.\S* SERVER=(\d+)\.", p.stdout)
            self.assertTrue(m, f"{agent}: podman --remote version answered")
            self.assertEqual(m.group(1), m.group(2), f"{agent}: client major version differs from the server's")
            self.assertIn(f"podman-compose version {host_compose}", p.stdout, f"{agent}: compose tool version")
            self.assertIn("CURL_OK", p.stdout, f"{agent}: curl")
            self.assertIn("VENV_OK", p.stdout, f"{agent}: python3 -m venv")

    def test_h_live_stack_secrets_do_not_leak(self):
        """Review 2026-10-01 #1 and #4: the live stack's secrets (env_file and environment:) must not appear in
        any observe response, including top with ps arguments and libpod inspect's CreateCommand; another
        project's pod must not be listed."""
        web = names(LIVE)[0]
        pod = f"abt-{harness.RUN}-otherpod"
        harness.podman("pod", "create", "--name", pod, "--label", "agentbox-test=1", "--label", harness.LABEL,
                       "--label", f"io.podman.compose.project={OTHER}")
        try:
            paths = [f"/libpod/containers/{web}/json", f"/containers/{web}/json",
                     f"/libpod/containers/{web}/top?ps_args=eww", f"/containers/{web}/top?ps_args=eww",
                     "/libpod/containers/json?all=true", "/containers/json?all=true",
                     "/libpod/pods/json", f"/libpod/pods/{pod}/json"]
            script = "; ".join(
                f"echo '=== {p}'; curl -s --unix-socket /run/agentbox/podman.sock -w '\\nHTTP=%{{http_code}}\\n' "
                f"'http://d/v5.0.0{p}'" for p in paths)
            script += f"; echo '=== podman inspect'; podman --remote inspect {web}"
            p, _sess = run_in_session("sgobs", script)
            out = p.stdout + p.stderr
            leaks = []
            for block in out.split("=== ")[1:]:
                head = block.splitlines()[0]
                for secret in (f"secret-of-{LIVE}", f"envsecret-of-{LIVE}"):
                    if secret in block:
                        leaks.append(f"{head}: {secret}")
            print("\n" + "\n".join(f"{b.splitlines()[0]} -> " + (b.strip().splitlines()[-1] if b.strip() else "")
                                    for b in out.split("=== ")[1:]))
            self.assertEqual(leaks, [], "the live stack's secrets leaked through observe")
            self.assertNotIn(pod, out.replace(f"/libpod/pods/{pod}/json", ""), "another project's pod is visible")
        finally:
            harness.podman("pod", "rm", "-f", pod)

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
