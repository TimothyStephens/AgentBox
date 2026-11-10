"""The Toulmin integration (agentbox/toulmin.py; docs/agentbox_toulmin_integration.md) against the stub
`tm` in tests/toulmin/tm-stub.sh, with the fake podman: clone mode, the session mounts and the token, the
MCP entry, the must-not-mount list, confirm before `podman run`, `serve` beside the session, and `end`
from every exit path. With AB_TOULMIN_ARTIFACTS=<dir> set, the artifacts the contract asks for (3.1, 3.2,
3.4) are written there as they are produced."""
import json
import os
import signal
import stat
import subprocess
import sys
import time

from agentbox import config, projectlock, toulmin

from .helpers import SettingsCase, read_json

STUB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                    "tests", "toulmin", "tm-stub.sh")
ARTIFACTS = os.environ.get("AB_TOULMIN_ARTIFACTS")


def artifact(name, text):
    if ARTIFACTS:
        os.makedirs(ARTIFACTS, exist_ok=True)
        with open(os.path.join(ARTIFACTS, name), "a") as f:
            f.write(text if text.endswith("\n") else text + "\n")


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True, text=True)


class ToulminCase(SettingsCase):
    """A registered git project with toulmin on, a stub tm, and the start message pointing at real paths."""

    def setUp(self):
        super().setUp()
        self.token()
        self.root = self.project("p1", preset="script", toulmin=True)
        self.write("projects/p1/app.py", "print('code')\n")
        self.write("projects/p1/.gitignore", "scratch/\n")
        self.write("projects/p1/scratch/untracked.txt", "not in the clone\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = subprocess.run(["git", "-C", self.root, "rev-parse", "HEAD"], capture_output=True,
                                   text=True, check=True).stdout.strip()
        self.stub = self.mkdir("tm-stub")
        self.sock_dir = self.mkdir("toulmin-1000", "S4")
        self.bridge = os.path.join(self.mkdir("bin"), "tm-bridge")
        with open(self.bridge, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(self.bridge, 0o755)
        self.start_msg = {
            "handshake": 1, "session": "S4", "token": "t0ken-" + "x" * 40, "bridge": self.bridge,
            "socket": os.path.join(self.sock_dir, "sock"), "mount_socket_dir": self.sock_dir,
            "agent_denied": ["data/patients.tsv"],
            "must_not_mount": [os.path.join(self.tmp, "shared", "records"), os.path.join(self.tmp, "toulmin-1000", "*")],
            "project_root": self.root, "role": "writer"}
        self.write_start()
        with open(os.path.join(self.cfg, "agentbox.toml"), "a") as f:
            f.write(f'toulmin_tm_path = "{STUB}"\n')
        os.environ["TM_STUB_DIR"] = self.stub
        self.addCleanup(os.environ.pop, "TM_STUB_DIR", None)
        self.settings = config.load()

    def write_start(self):
        with open(os.path.join(self.stub, "start.json"), "w") as f:
            json.dump(self.start_msg, f)

    def received(self, name):
        return read_json(os.path.join(self.stub, name))

    def calls(self):
        try:
            with open(os.path.join(self.stub, "calls")) as f:
                return [l.split()[:2] + l.split()[2:3] for l in f.read().splitlines()]
        except FileNotFoundError:
            return []

    def run_argv(self):
        """The fake podman's recorded `podman run` argv for the agent container."""
        for c in self.podman_calls():
            if c[:2] == ["run", "--rm"]:
                return c
        return None


class CloneModeSession(ToulminCase):
    def test_clone_mode_mounts_the_clone_at_the_owners_path_and_never_the_checkout(self):
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        sid = "abt-unit-claude-p1-1"
        clone = os.path.join(self.settings.clones_dir("p1"), sid)
        self.assertTrue(os.path.isdir(os.path.join(clone, ".git")), "the clone exists and is kept")
        branch = subprocess.run(["git", "-C", clone, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
        self.assertEqual(branch, f"agentbox/{sid}")
        self.assertFalse(os.path.exists(os.path.join(clone, "scratch")), "nothing untracked or ignored")
        argv = self.run_argv()
        self.assertIsNotNone(argv)
        binds = [o for o in argv if o.startswith("--mount=type=bind")]
        root_binds = [o for o in binds if f",dst={self.root}" in o and f",dst={self.root}/" not in o]
        self.assertEqual(len(root_binds), 1, binds)
        self.assertIn(f"src={clone},dst={self.root}", root_binds[0], "the clone, at the owner's path")
        self.assertFalse(any(f"src={self.root}," in o or f"src={self.root}/" in o for o in binds),
                         "the owner's checkout is bound nowhere")
        self.assertIn(f"--workdir={self.root}", argv)
        self.assertIn("--env=AGENTBOX_AGENT=claude", argv)
        self.assertIn(f"--mount=type=bind,src={self.sock_dir},dst={toulmin.SOCKET_DIR_DST}", argv)
        self.assertIn(f"--mount=type=bind,src={self.bridge},dst={toulmin.BRIDGE_DST},ro", argv)
        token_mounts = [o for o in argv if o.endswith(f",dst={toulmin.SOCKET_DIR_DST}/token,ro")]
        self.assertEqual(len(token_mounts), 1, argv)
        self.assertNotIn(self.start_msg["token"], " ".join(argv), "the token is in no argv, so in no inspect")
        self.assertFalse(any("--env=" in o and "token" in o.lower() for o in argv))
        self.assertFalse(any(self.sock_dir in o and o.endswith("/sock") for o in argv), "the dir, not the socket")
        self.assertFalse(os.path.exists(os.path.join(self.settings.project_backups_dir("p1"))),
                         "no rsync snapshot in clone mode")
        # the three files tm received
        confirm = self.received("confirm.json")
        self.assertEqual((confirm["handshake"], confirm["session"], confirm["isolation"]), (1, "S4", "container"))
        self.assertEqual((confirm["preset"], confirm["network"], confirm["agent"]), ("script", "packages", "claude"))
        self.assertTrue(confirm["lists_applied"])
        self.assertEqual(confirm["container_paths"],
                         {"socket_dir": toulmin.SOCKET_DIR_DST, "bridge": toulmin.BRIDGE_DST, "project_root": self.root})
        self.assertEqual((confirm["clone"], confirm["branch"], confirm["base"]), (clone, f"agentbox/{sid}", self.base))
        self.assertTrue(confirm["launcher"].startswith("agentbox "))
        entries = toulmin.mount_list(argv)
        self.assertEqual(confirm["mounts_hash"], toulmin.mounts_hash(entries))
        self.assertEqual(entries, sorted(entries, key=lambda e: e["target"]))
        targets = [e["target"] for e in entries]
        self.assertIn(toulmin.SOCKET_DIR_DST, targets)
        self.assertIn(toulmin.BRIDGE_DST, targets)
        self.assertEqual(next(e for e in entries if e["target"] == self.root)["source"], clone)
        end = self.received("end.json")
        self.assertEqual((end["handshake"], end["session"], end["exit"]), (1, "S4", "agent_exit"))
        self.assertEqual((end["clone"], end["base"], end["head"]), (clone, self.base, self.base))
        self.assertRegex(end["ended_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual([c[:2] for c in self.calls()],
                         [["session", "start"], ["session", "confirm"], ["session", "serve"], ["session", "end"]])
        self.assertFalse(os.path.exists(os.path.join(self.stub, "serving")), "serve was stopped after end")
        artifact("1-mount-list.json", json.dumps({"mounts_hash": confirm["mounts_hash"], "mounts": entries}, indent=1))
        artifact("2-confirm.json", json.dumps(confirm, indent=1, sort_keys=True))
        artifact("2-end-agent_exit.json", json.dumps(end, indent=1, sort_keys=True))
        artifact("2-podman-run-argv.txt", "\n".join(argv))

    def test_the_token_file_is_0600_in_the_run_dir_and_mounted_read_only(self):
        seen = {}
        real_write = toulmin.write_token

        def spy(run_dir, token):
            path = real_write(run_dir, token)
            seen["path"], seen["mode"] = path, stat.S_IMODE(os.stat(path).st_mode)
            with open(path) as f:
                seen["content"] = f.read()
            return path
        from unittest import mock
        with mock.patch.object(toulmin, "write_token", spy):
            code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertEqual(seen["mode"], 0o600)
        self.assertEqual(seen["content"], self.start_msg["token"])
        self.assertTrue(seen["path"].startswith(self.settings.run_dir + "/sessions/"))
        self.assertIn(f"src={seen['path']},dst={toulmin.SOCKET_DIR_DST}/token,ro", " ".join(self.run_argv()))
        artifact("2-token-file.txt", f"{seen['path']}  mode {oct(seen['mode'])}  (removed with the run dir at teardown)")

    def test_the_claude_mcp_entry_is_in_the_state_during_the_session_and_gone_after(self):
        state = os.path.join(self.settings.state_dir("p1", "claude"), ".claude.json")
        seen = {}
        from agentbox.podman import Podman
        real = Podman.run

        def spy(self_, argv, *a, **kw):
            if argv[:2] == ["run", "--rm"]:
                seen["mcp"] = read_json(state).get("mcpServers")
            return real(self_, argv, *a, **kw)
        from unittest import mock
        with mock.patch.object(Podman, "run", spy):
            code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertEqual(seen["mcp"], {"toulmin": {"type": "stdio", "command": toulmin.BRIDGE_DST,
                                                   "args": [f"{toulmin.SOCKET_DIR_DST}/sock", "--token-file",
                                                            f"{toulmin.SOCKET_DIR_DST}/token"]}})
        self.assertNotIn("toulmin", read_json(state).get("mcpServers") or {}, "removed at teardown")
        artifact("3-mcp-config-claude.json", json.dumps({"mcpServers": seen["mcp"]}, indent=1))

    def test_the_session_notes_say_it_is_a_clone(self):
        seen = {}
        from agentbox.podman import Podman
        real = Podman.run

        def spy(self_, argv, *a, **kw):
            if argv[:2] == ["run", "--rm"]:
                m = next(o for o in argv if o.endswith("dst=/run/agentbox/claude/CLAUDE.md,ro"))
                with open(m.split("src=", 1)[1].split(",", 1)[0]) as f:
                    seen["notes"] = f.read()
            return real(self_, argv, *a, **kw)
        from unittest import mock
        with mock.patch.object(Podman, "run", spy):
            code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertIn("clone mode:", out)
        self.assertIn("no pre-session snapshot", out)
        self.assertIn("fresh clone on branch `agentbox/abt-unit-claude-p1-1`", seen["notes"])
        self.assertIn("nothing you do here can push to it", seen["notes"])


class CheckoutModeWithToulmin(ToulminCase):
    def test_checkout_mode_confirm_has_no_clone_and_end_reports_the_roots_head(self):
        from agentbox import registry
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["mount"] = "checkout"
        registry.save(self.settings.registry_path, reg)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        confirm = self.received("confirm.json")
        for k in ("clone", "branch", "base"):
            self.assertNotIn(k, confirm)
        end = self.received("end.json")
        self.assertNotIn("clone", end)
        self.assertEqual((end["base"], end["head"]), (self.base, self.base))
        self.assertIn(f"src={self.root},dst={self.root}", " ".join(self.run_argv()), "the checkout, as always")
        self.assertFalse(os.path.exists(self.settings.clones_dir("p1")))
        artifact("2-confirm-checkout-mode.json", json.dumps(confirm, indent=1, sort_keys=True))

    def test_a_non_git_root_defaults_to_checkout_and_clone_is_refused_for_it(self):
        import shutil
        shutil.rmtree(os.path.join(self.root, ".git"))
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertNotIn("clone", self.received("confirm.json"))
        self.assertEqual(self.ab("project", "set", "p1", "--mount", "clone")[0], 0)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2, out)
        self.assertIn("needs a git repository", out)

    def test_clone_mode_with_the_dev_tier_is_refused_before_tm_is_called(self):
        from agentbox import registry
        reg = registry.load(self.settings.registry_path)
        reg["p1"]["preset"] = "build"
        registry.save(self.settings.registry_path, reg)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2, out)
        self.assertIn("dev podman tier is not built", out)
        self.assertEqual(self.calls(), [], "tm was not called")


class Refusals(ToulminCase):
    def test_a_must_not_mount_hit_refuses_before_confirm(self):
        """A read-only data grant inside a listed directory: the list wins, before confirm. (The owner's root
        is on no list in the examples, and in clone mode it is not a mount source at all.)"""
        records = self.mkdir("shared", "records", "proj")
        self.assertEqual(self.ab("project", "set", "p1", "--data-ro", records)[0], 0)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2, out)
        self.assertIn("must-not-mount list refuses", out)
        self.assertIn(records, out)
        self.assertIn(os.path.join(self.tmp, "shared", "records"), out)
        self.assertEqual([c[:2] for c in self.calls()], [["session", "start"], ["session", "end"]],
                         "no confirm, no serve; end still reports the error")
        self.assertEqual(self.received("end.json")["exit"], "error")

    def test_the_socket_dir_glob_does_not_refuse_the_socket_dir_itself(self):
        self.assertEqual(toulmin.violations([os.path.join(self.tmp, "toulmin-1000", "*")],
                                            [self.sock_dir, self.root], allowed=[self.sock_dir]), [])
        self.assertEqual(toulmin.violations([os.path.join(self.tmp, "toulmin-1000", "*")], [self.tmp]),
                         [(self.tmp, os.path.join(self.tmp, "toulmin-1000", "*"))], "a parent exposes the glob")
        lit = os.path.join(self.tmp, "shared", "records")
        self.assertEqual(toulmin.violations([lit], [os.path.join(lit, "proj")]), [(os.path.join(lit, "proj"), lit)])

    def test_a_refused_confirm_aborts_before_the_container(self):
        os.environ["TM_STUB_REFUSE_CONFIRM"] = "1"
        self.addCleanup(os.environ.pop, "TM_STUB_REFUSE_CONFIRM", None)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2, out)
        self.assertIn("refused", out)
        self.assertIsNone(self.run_argv(), "podman run never happened")
        self.assertEqual(self.received("end.json")["exit"], "error")

    def test_tm_absent_means_no_session(self):
        path = os.path.join(self.cfg, "agentbox.toml")
        with open(path) as f:
            text = f.read().replace(f'toulmin_tm_path = "{STUB}"', 'toulmin_tm_path = "/nonexistent/tm"')
        with open(path, "w") as f:
            f.write(text)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2, out)
        self.assertIn("not an executable", out)

    def test_a_bad_start_message_is_refused(self):
        for bad in ({"handshake": 2}, {"token": "short"}, {"bridge": "relative/path"}, {"must_not_mount": "x"}):
            with self.subTest(bad=bad):
                msg = dict(self.start_msg, **bad)
                with open(os.path.join(self.stub, "start.json"), "w") as f:
                    json.dump(msg, f)
                code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
                self.assertEqual(code, 2, out)
                self.assertIn("tm session start", out)

    def test_lockdown_refuses_toulmin(self):
        code, out = self.ab("project", "set", "p1", "--preset", "lockdown", "--toulmin")
        self.assertEqual(code, 2, out)


class ExitReasons(ToulminCase):
    """`tm session end` from each of the five exit values, with the right head each time."""

    REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def launcher(self):
        env = dict(os.environ, FAKE_PODMAN_PSJSON="[]", FAKE_PODMAN_RUN_SLEEP="10")
        p = subprocess.Popen([sys.executable, "-c",
                              "from agentbox import cli; cli.main(['session', 'start', 'claude', 'p1', '--no-tty'])"],
                             cwd=self.REPO, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self._close, p)
        for _ in range(600):
            doc = projectlock.read(self.settings, "p1")
            if doc and doc.get("session") and os.path.exists(os.path.join(self.stub, "serving")):
                return p
            if p.poll() is not None:
                out, err = p.communicate()
                raise AssertionError(f"the launcher exited early ({p.returncode}):\n{out}\n{err}")
            time.sleep(0.05)
        p.kill()
        raise AssertionError("the launcher never reached serve")

    @staticmethod
    def _close(p):
        if p.poll() is None:
            p.kill()
        p.wait(timeout=30)
        for f in (p.stdout, p.stderr):
            if f:
                f.close()

    def commit_in_clone(self, sid):
        clone = os.path.join(self.settings.clones_dir("p1"), sid)
        with open(os.path.join(clone, "new.py"), "w") as f:
            f.write("x = 1\n")
        git(clone, "add", "new.py")
        git(clone, "commit", "-q", "-m", "agent work")
        return subprocess.run(["git", "-C", clone, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

    def ends(self):
        with open(os.path.join(self.stub, "ends.jsonl")) as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_agent_exit_after_a_commit_reports_the_new_head(self):
        from agentbox.podman import Podman
        real = Podman.run
        heads = {}

        def spy(self_, argv, *a, **kw):
            if argv[:2] == ["run", "--rm"]:
                heads["head"] = self.commit_in_clone("abt-unit-claude-p1-1")
            return real(self_, argv, *a, **kw)
        from unittest import mock
        with mock.patch.object(Podman, "run", spy):
            code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        end = self.received("end.json")
        self.assertEqual((end["exit"], end["base"], end["head"]), ("agent_exit", self.base, heads["head"]))
        self.assertNotEqual(end["head"], end["base"])
        artifact("4-trace-agent_exit.json", json.dumps(end, indent=1, sort_keys=True))

    def test_signal_kill_and_gc(self):
        sid = "abt-unit-claude-p1-1"
        for sig, reason in ((signal.SIGTERM, "signal"), (signal.SIGHUP, "signal")):
            with self.subTest(signal=sig.name):
                p = self.launcher()
                try:
                    head = self.commit_in_clone(sid)
                    p.send_signal(sig)
                    p.wait(timeout=120)
                finally:
                    if p.poll() is None:
                        p.kill()
                end = self.ends()[-1]
                self.assertEqual((end["exit"], end["head"], end["base"]), (reason, head, self.base))
                self.assertFalse(os.path.exists(os.path.join(self.stub, "serving")))
                artifact(f"4-trace-signal-{sig.name}.json", json.dumps(end, indent=1, sort_keys=True))
                import shutil
                shutil.rmtree(self.settings.clones_dir("p1"), ignore_errors=True)
        # `ab session kill` from another process while the launcher waits
        p = self.launcher()
        try:
            head = self.commit_in_clone(sid)
            code, out = self.ab("session", "kill", "p1")
            self.assertEqual(code, 0, out)
        finally:
            p.kill()
            p.wait(timeout=30)
        end = self.ends()[-1]
        self.assertEqual((end["exit"], end["head"]), ("kill", head))
        artifact("4-trace-kill.json", json.dumps(end, indent=1, sort_keys=True))
        import shutil
        shutil.rmtree(self.settings.clones_dir("p1"), ignore_errors=True)
        # SIGKILL of the launcher: nothing ends the session until gc finds the husk
        p = self.launcher()
        head = self.commit_in_clone(sid)
        before = len(self.ends())
        p.kill()
        p.wait(timeout=30)
        self.assertEqual(len(self.ends()), before, "a dead launcher reports nothing")
        self.assertTrue(os.path.exists(os.path.join(self.stub, "serving")), "serve outlives a SIGKILLed launcher")
        code, out = self.ab("session", "gc")
        self.assertEqual(code, 0, out)
        end = self.ends()[-1]
        self.assertEqual((end["exit"], end["head"], end["session"]), ("gc", head, "S4"))
        self.assertFalse(os.path.exists(os.path.join(self.stub, "serving")), "gc stopped the orphaned serve")
        artifact("4-trace-gc-after-sigkill.json", json.dumps(end, indent=1, sort_keys=True))

    def test_error_before_the_container_reports_error_with_head_equal_to_base(self):
        """`serve` dying at once is an error after start and confirm, before the container: the launcher
        tears down with exit = error and no agent ever ran."""
        os.environ["TM_STUB_SERVE_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "TM_STUB_SERVE_EXIT", None)
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 2, out)
        self.assertIn("exited at once", out)
        self.assertIsNone(self.run_argv(), "podman run never happened")
        end = self.received("end.json")
        self.assertEqual((end["exit"], end["head"]), ("error", end["base"]))
        artifact("4-trace-error.json", json.dumps(end, indent=1, sort_keys=True))


class OmpMcp(ToulminCase):
    def test_omp_gets_a_generated_mcp_json_mounted_in_place_of_the_seed(self):
        seed = os.path.join(self.settings.seed_dir("omp"), "mcp.json")
        os.makedirs(os.path.dirname(seed), exist_ok=True)
        with open(seed, "w") as f:
            json.dump({"mcpServers": {"mine": {"command": "x"}}}, f)
        with open(os.path.join(self.settings.seed_dir("omp"), "models.yml"), "w") as f:
            f.write("providers: {}\n")
        from agentbox import keys
        os.makedirs(keys.keys_dir(self.settings), exist_ok=True)
        seen = {}
        from agentbox.podman import Podman
        real = Podman.run

        def spy(self_, argv, *a, **kw):
            if argv[:2] == ["run", "--rm"]:
                m = [o for o in argv if o.endswith("/.omp/agent/.mcp.json,ro")]
                seen["mount"] = m
                if m:
                    src = m[0].split("src=", 1)[1].split(",", 1)[0]
                    seen["doc"] = read_json(src)
            return real(self_, argv, *a, **kw)
        from unittest import mock
        with mock.patch.object(Podman, "run", spy):
            code, out = self.ab("session", "start", "omp", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(seen["mount"]), 1, seen)
        self.assertIn("omp-mcp.json", seen["mount"][0])
        self.assertEqual(set(seen["doc"]["mcpServers"]), {"mine", "toulmin"})
        self.assertEqual(seen["doc"]["mcpServers"]["toulmin"]["command"], toulmin.BRIDGE_DST)
        artifact("3-mcp-config-omp.json", json.dumps(seen["doc"], indent=1))


class ProjectClean(ToulminCase):
    def test_clean_removes_clones_of_ended_sessions(self):
        code, out = self.ab("session", "start", "claude", "p1", "--no-tty")
        self.assertEqual(code, 0, out)
        clones = self.settings.clones_dir("p1")
        self.assertEqual(len(os.listdir(clones)), 1)
        code, out = self.ab("project", "clean", "p1")
        self.assertEqual(code, 1, out)
        self.assertIn("would remove", out)
        self.assertEqual(len(os.listdir(clones)), 1)
        code, out = self.ab("project", "clean", "p1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(clones), [])
        self.assertEqual(self.settings.agent_state_dirs("p1"), [("claude", self.settings.state_dir("p1", "claude"))],
                         "clones/ is the project's own entry, not an agent")


class MountListAndHash(SettingsCase):
    def test_hash_is_over_canonical_json_of_the_sorted_list(self):
        argv = ["run", "--rm", "--mount=type=bind,src=/b,dst=/x/b,ro", "--tmpfs=/tmp:rw,nosuid,notmpcopyup",
                "--mount=type=tmpfs,dst=/x/a,tmpfs-mode=0700,U=true,notmpcopyup,ro",
                "--mount=type=bind,src=/c,dst=/x/c", "img"]
        entries = toulmin.mount_list(argv)
        self.assertEqual(entries, [{"source": "tmpfs", "target": "/tmp", "mode": "rw"},
                                   {"source": "tmpfs", "target": "/x/a", "mode": "ro"},
                                   {"source": "/b", "target": "/x/b", "mode": "ro"},
                                   {"source": "/c", "target": "/x/c", "mode": "rw"}])
        canon = toulmin.canonical_json(entries)
        self.assertEqual(canon, '[{"mode":"rw","source":"tmpfs","target":"/tmp"},'
                                '{"mode":"ro","source":"tmpfs","target":"/x/a"},'
                                '{"mode":"ro","source":"/b","target":"/x/b"},'
                                '{"mode":"rw","source":"/c","target":"/x/c"}]')
        import hashlib
        self.assertEqual(toulmin.mounts_hash(entries), "sha256:" + hashlib.sha256(canon.encode()).hexdigest())
