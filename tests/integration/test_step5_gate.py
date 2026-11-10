"""Step 5 gate: the host-side gate and the dev tier, on the dummy two-container compose project.

Project `dv` has the dev tier (`--podman dev`); `dvp` uses the pods preset (dev tier plus observe on its own
live stack). Checked, in the OMP image (it has the podman client, the compose tool and curl):
  * discovery: a deny-all dev sockguard in audit mode logs every call compose, run, exec, logs and build
    make (printed; it is how sockguard.DEV_RULES was checked);
  * the dummy stack comes up through the gate, its services answer by name, logs and exec work, and it
    comes down again; on the host its containers carry the dev labels, sit only on the session network, in
    the slice, with the default limits and OOM score 1000;
  * refused: published ports, privileged, host network, binds outside the project, an unlisted registry,
    exec/stop/rm/inspect on the session's own proxy, sockguard and holder, on another project's container,
    and on a raw exec session the gate did not create;
  * builds join the session network: a RUN step cannot reach the LAN directly, and a client-supplied network
    namespace is ignored;
  * selftest passes for both projects;
  * afterwards no container, pod, volume or network the sessions made is left (test_zz checks each kind by the
    run's label, plus no new unlabelled volume), and no gate or podman service process. Images the sessions
    built are kept on purpose (decision 3, 1 Oct: `ab session gc` ages them out); test_zz checks they are only the
    ones these tests built (or a session build a rebuild left untagged), and the module teardown removes them.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

from agentbox import config, session, sockguard
from tests import ompfake

from . import harness
from .harness import read_file

ENV = None
COMPOSE = f"abt-{harness.RUN}-dv"
CANARY = f"abt-{harness.RUN}-canary"
CONTAINERFILE = """FROM docker.io/library/busybox:1.36
RUN (wget -q -T 5 -O /dev/null http://10.0.0.103:8088/ && echo BUILD_LAN_REACHED) || echo BUILD_LAN_BLOCKED
RUN (env -u http_proxy -u https_proxy wget -q -T 5 -O /dev/null http://example.com/ && echo BUILD_DIRECT_REACHED) || echo BUILD_DIRECT_BLOCKED
RUN echo BUILD_PROXY=$http_proxy
"""


VOLUMES_BEFORE = set()


def setUpModule():
    global ENV
    VOLUMES_BEFORE.update(harness.all_volumes())
    for img in (config.DEFAULTS["sockguard_image"], "localhost/agentbox-omp:latest", "docker.io/library/busybox:1.36"):
        if harness.podman("image", "exists", img).returncode != 0:
            raise unittest.SkipTest(f"{img} not present")
    ENV = harness.Env()
    # The installed plugin tree, where `ab profile install omp` puts it: there is no
    # setting to point anywhere else, and nothing reads the host's ~/.omp (5 Nov).
    tree = ompfake.build(ENV.cfg)
    for name, preset, extra in (("dv", "python", ["--podman", "dev"]), ("dvp", "pods", [])):
        root = ENV.make_project(name, preset=preset, extra=extra)
        with open(os.path.join(root, "Containerfile"), "w") as f:
            f.write(CONTAINERFILE)
    # another project's container, made on the host: the dev tier must not reach it
    harness.podman("run", "-d", "--name", CANARY, "--label", "agentbox-test=1", "--label", harness.LABEL,
                   "--label", "io.podman.compose.project=abt-other", "--network=none",
                   "docker.io/library/busybox:1.36", "sleep", "600")
    print(f"\n[itest run {harness.RUN}: step 5]")


def tearDownModule():
    harness.podman("rm", "-f", "-t", "1", CANARY)
    for img in harness.podman("images", "-q", "--filter", f"label={harness.LABEL}").stdout.split():
        harness.podman("rmi", "-f", img)
    harness.cleanup()


def in_session(project, script, *, discovery=False, before_teardown=None):
    """An OMP session run in-process (so discovery can swap in the audit config). Returns (proc, session)."""
    keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({k: ENV.env[k] for k in keys})
    try:
        s = config.load()
        sess = session.make_session(s, "omp", session.load_project(s, project), warn=lambda m: None)
        if discovery:
            sess.sockguard_dev_config = lambda: sockguard.dev_discovery_config(sess.name, bind_roots=sess.bind_roots)
        sess.prepare()
        hook = {}
        if before_teardown:
            orig = sess.teardown

            def teardown(exit_reason="error"):      # `_guarded` passes the exit reason (toulmin.EXITS)
                hook["seen"] = before_teardown(sess)
                orig(exit_reason)
            sess.teardown = teardown
        p = sess.run_captured(["bash", "-c", script], timeout=1500)
        return p, sess, hook.get("seen")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


STACK = f"""
cd {{root}}
podman pull docker.io/library/busybox:1.36 >/dev/null; echo PULL_RC=$?
podman-compose -p {COMPOSE} up -d; echo UP_RC=$?
podman ps --format '{{{{.Names}}}}' | sort; echo PS_RC=$?
for i in 1 2 3 4 5 6 7 8 9 10; do curl -s --noproxy '*' -m 3 http://web:8080/ && break; sleep 2; done; echo CURL_RC=$?
podman logs {COMPOSE}_web_1 >/dev/null 2>&1; echo LOGS_RC=$?
podman exec {COMPOSE}_web_1 sh -c 'echo EXEC_OK; env | grep -c _proxy'; echo EXEC_RC=$?
podman build --no-cache -t abt-{harness.RUN}-img -f Containerfile . 2>&1 | grep -E '^BUILD_' ; echo BUILD_RC=${{PIPESTATUS[0]}}
podman exec {COMPOSE}_worker_1 sh -c 'echo "WORKER_NOPROXY=$no_proxy"; wget -q -T 5 -O- http://web:8080/ && echo WORKER_REACHED_WEB; wget -Y off -q -T 5 -O- http://web:8080/ && echo WORKER_DIRECT_OK'
"""


class Step5(unittest.TestCase):
    maxDiff = None

    def test_a_discovery(self):
        root = os.path.join(ENV.projects, "dv")
        p, sess, _ = in_session("dv", STACK.replace("{root}", root) + f"podman-compose -p {COMPOSE} down", discovery=True)
        print(f"\n{p.stdout[-2500:]}\n{p.stderr[-1500:]}")
        calls = set()
        for fn in os.listdir(os.path.join(ENV.data, "logs")):
            if fn.startswith(sess.name) and fn.endswith("-sockguard-dev.log"):
                for line in open(os.path.join(ENV.data, "logs", fn)):
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("decision") == "would_deny":
                        calls.add(f"{ev['method']} {ev['normalized_path']}")
        import re
        generic = sorted({re.sub(r"/(?:containers|exec|images|networks|volumes|pods)/[^/]+",
                                 lambda m: m.group(0).rsplit("/", 1)[0] + "/*", c) for c in calls})
        print("calls (audit mode, would_deny):\n  " + "\n  ".join(generic))
        self.assertTrue(calls)

    def test_b_dev_stack_works_and_is_pinned(self):
        root = os.path.join(ENV.projects, "dv")

        def inspect(sess):
            names = harness.podman("ps", "--format", "{{.Names}}", "--filter", f"label=agentbox.session={sess.name}",
                                   "--filter", "label=agentbox.role=dev").stdout.split()
            docs = json.loads(harness.podman("inspect", *names).stdout or "[]") if names else []
            out = []
            for d in docs:
                pid = d["State"]["Pid"]
                cg = read_file(f"/proc/{pid}/cgroup").strip() if pid else ""
                out.append({"name": d["Name"], "labels": d["Config"]["Labels"],
                            "nets": sorted((d["NetworkSettings"].get("Networks") or {}).keys()),
                            "oom": d["HostConfig"].get("OomScoreAdj"), "mem": d["HostConfig"].get("Memory"),
                            "ports": d["HostConfig"].get("PortBindings"), "cgroup": cg,
                            "restart": (d["HostConfig"].get("RestartPolicy") or {}).get("Name")})
            return out

        p, sess, seen = in_session("dv", STACK.replace("{root}", root), before_teardown=inspect)
        out = p.stdout
        print(f"\n{out[-3000:]}\n{p.stderr[-1500:]}")
        for rc in ("PULL_RC", "UP_RC", "PS_RC", "CURL_RC", "LOGS_RC", "EXEC_RC", "BUILD_RC"):
            self.assertIn(f"{rc}=0", out, rc)
        self.assertIn("dummy-web", out, "the agent reached the web service by name")
        self.assertIn("EXEC_OK", out)
        self.assertIn("BUILD_LAN_BLOCKED", out)
        self.assertIn("BUILD_DIRECT_BLOCKED", out)
        # by the proxy's IP: a build's own resolv.conf cannot resolve the proxy's container name (trial, 2 Oct)
        self.assertRegex(out, r"BUILD_PROXY=http://\d+\.\d+\.\d+\.\d+:3128\n")
        # decision 1 Oct: no_proxy holds the names already on the session network, so services reach each other
        self.assertRegex(out, r"WORKER_NOPROXY=localhost,127\.0\.0\.1,10\.\S*\bweb\b")
        self.assertNotRegex(out, r"WORKER_NOPROXY=.*(-proxy|-holder)", "only the dev stack's names")
        # busybox wget ignores no_proxy (curl, Python and Go honour it); the direct path itself must work
        self.assertIn("WORKER_DIRECT_OK", out)
        labels = json.loads(harness.podman("image", "inspect", f"abt-{harness.RUN}-img").stdout)[0]["Labels"]
        self.assertEqual((labels.get("agentbox.project"), labels.get("agentbox.session")), ("dv", sess.name),
                         "session-built images carry their project and session")
        print("on the host:", json.dumps(seen, indent=1)[:3000])
        self.assertTrue(seen, "dev containers were running before teardown")
        for c in seen:
            self.assertEqual(c["labels"].get(sockguard.DEV_OWNER), sess.name, c["name"])
            self.assertEqual(c["labels"].get("agentbox-test"), "1", c["name"])
            self.assertEqual(c["nets"], [sess.int_net], c["name"])
            self.assertEqual(c["oom"], 1000, c["name"])
            self.assertEqual(c["mem"], 1 << 30, c["name"])
            self.assertFalse(c["ports"], c["name"])
            self.assertIn(c["restart"], ("", "no", None), c["name"])
            if ENV.slice:
                self.assertIn("/agentbox.slice/", c["cgroup"], c["name"])
        left = [x for x in harness.leftovers() if x.startswith(sess.name) or x.startswith(COMPOSE)]
        self.assertEqual(left, [], "the dev stack is gone with the session")

    def test_c_refusals(self):
        root = os.path.join(ENV.projects, "dv")
        img = "docker.io/library/busybox:1.36"

        script = f"""
cd {root}
try() {{ out=$("$@" 2>&1); rc=$?; echo "$rc $(echo "$out" | tail -1)"; }}
echo "PORT $(try podman run --rm -p 18080:80 {img} true)"
echo "PRIV $(try podman run --rm --privileged {img} true)"
echo "HOSTNET $(try podman run --rm --network host {img} true)"
echo "BIND $(try podman run --rm -v $HOME:/h {img} true)"
echo "CAP $(try podman run --rm --cap-add NET_ADMIN {img} true)"
echo "REGISTRY $(try podman pull registry.example.com/x:1)"
echo "CANARY_EXEC $(try podman exec {CANARY} true)"
echo "CANARY_LOGS $(try podman logs {CANARY})"
echo "CANARY_STOP $(try podman stop -t 1 {CANARY})"
for c in $(echo $AB_SELF_NAMES); do
  echo "SELF_EXEC_$c $(try podman exec $c true)"
  echo "SELF_STOP_$c $(try podman stop -t 1 $c)"
done
for body in '"PortMappings": [{{"host_port": 18082, "container_port": 80}}]' '"PRIVILEGED": true' \
            '"privileged": false, "Privileged": true' '"NetNS": {{"NsMode": "host"}}'; do
  code=$(curl -s --unix-socket /run/agentbox/podman.sock -X POST -H 'Content-Type: application/json' -o /dev/null \
    -w '%{{http_code}}' -d "{{\"image\": \"{img}\", \"name\": \"abt-{harness.RUN}-casep\", $body}}" \
    http://d/v5.0.0/libpod/containers/create)
  echo "CASE $code $body"
done
echo "RAWEXEC $(curl -s --unix-socket /run/agentbox/podman.sock -X POST -o /dev/null -w '%{{http_code}}' \
  http://d/v5.0.0/libpod/exec/0123456789abcdef/start -H 'Content-Type: application/json' -d '{{}}')"
podman ps -a --format '{{{{.Names}}}}' | grep -v '^$' > /tmp/ps.txt; echo "PS $(tr '\\n' ' ' < /tmp/ps.txt)"
"""
        keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
        old = {k: os.environ.get(k) for k in keys}
        os.environ.update({k: ENV.env[k] for k in keys})
        try:
            s = config.load()
            sess = session.make_session(s, "omp", session.load_project(s, "dv"), warn=lambda m: None)
            sess.prepare()
            sess.env["AB_SELF_NAMES"] = " ".join([sess.name, sess.proxy_name, sess.sockguard_dev_name,
                                                  sess.holder_name])
            p = sess.run_captured(["bash", "-c", script], timeout=900)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        out = p.stdout
        print(f"\n{out[-4000:]}\n{p.stderr[-800:]}")
        lines = {l.split(" ", 1)[0]: l for l in out.splitlines() if " " in l}
        for key in ("PORT", "PRIV", "HOSTNET", "BIND", "CAP", "REGISTRY", "CANARY_EXEC", "CANARY_LOGS", "CANARY_STOP"):
            self.assertIn(key, lines, key)
            self.assertNotRegex(lines[key], rf"^{key} 0 ", f"{key} was allowed: {lines[key]}")
        self_lines = [l for k, l in lines.items() if k.startswith("SELF_")]
        self.assertEqual(len(self_lines), 8, self_lines)
        for l in self_lines:
            self.assertNotRegex(l, r"^\S+ 0 ", f"allowed: {l}")
        self.assertIn("RAWEXEC 403", out)
        # found 1 Oct: podman matches JSON field names case-insensitively ("PortMappings" published a port)
        case_lines = [l for l in out.splitlines() if l.startswith("CASE ")]
        self.assertEqual(len(case_lines), 4, case_lines)
        for l in case_lines:
            self.assertTrue(l.startswith("CASE 403 "), l)
        self.assertEqual(harness.podman("ps", "-a", "-q", "--filter", f"name=abt-{harness.RUN}-casep").stdout.strip(),
                         "", "nothing was created")
        ps = lines.get("PS", "PS")
        for name in (sess.proxy_name, sess.sockguard_dev_name, sess.holder_name, CANARY):
            self.assertNotIn(name, ps, "only the session's dev containers are listed")
        running = harness.podman("ps", "--format", "{{.Names}}", "--filter", f"name={CANARY}").stdout
        self.assertIn(CANARY, running, "the other project's container is untouched")

    def test_d_build_ignores_a_client_namespace(self):
        """The podman client forwards any namespace path it is given; the gate must overwrite it. The tag is
        unique per run (abt-<run>-nsx) and labelled by the gate, so teardown removes it and a stale image from
        a prior run never collides with this one (a fixed `abt-nsx` tag did: the gate refused the second run's
        build because an image it had not built already held the name)."""
        root = os.path.join(ENV.projects, "dv")
        tag = f"abt-{harness.RUN}-nsx"
        script = (f"cd {root}; tar -cf /tmp/ctx.tar Containerfile; "
                  "curl -s --unix-socket /run/agentbox/podman.sock -X POST -H 'Content-Type: application/x-tar' "
                  f"--data-binary @/tmp/ctx.tar 'http://d/v5.0.0/libpod/build?dockerfile=Containerfile&t={tag}"
                  "&networkmode=2&nsoptions=%5B%7B%22Name%22%3A%22network%22%2C%22Path%22%3A%22%2Fproc%2F1%2Fns%2Fnet%22%7D%5D'"
                  " > /tmp/nsx.json; echo RAW_HEAD: >&2; head -c 400 /tmp/nsx.json >&2; echo >&2; "
                  "cat /tmp/nsx.json | python3 -c 'import json,sys; d=json.JSONDecoder(); t=sys.stdin.read(); i=0\n"
                  "while i < len(t):\n"
                  "    t2=t[i:].lstrip(); i=len(t)-len(t2)\n"
                  "    if not t2: break\n"
                  "    o,n=d.raw_decode(t2); i+=n; print(o.get(\"stream\",\"\"),end=\"\")'"
                  " | grep -xE 'BUILD_[A-Z_]+(=.*)?' | sort -u")
        p, _sess, _ = in_session("dv", script)
        print(f"\n{p.stdout[-1500:]}\n{p.stderr[-800:]}")
        if "BUILD_" not in p.stdout:
            self.fail("build produced no BUILD_ output — the build was refused or did not run (not a leak). "
                      f"Raw gate response in stderr:\n{p.stderr[-800:]}")
        self.assertIn("BUILD_LAN_BLOCKED", p.stdout)
        self.assertNotIn("BUILD_LAN_REACHED", p.stdout)

    def test_e_selftest(self):
        for project in ("dv", "dvp"):
            results, p = ENV.selftest("omp", project)
            print(f"\nselftest omp/{project}:\n{harness.report(results)}")
            fails = [r for r in results if r["verdict"] == "FAIL"]
            self.assertFalse(fails, harness.report(fails) + "\n" + p.stderr[-1500:])
            labels = " | ".join(r["label"] for r in results)
            for want in ("podman socket present (dev tier", "create with privileged refused",
                         "create with a published port refused", "pull from an unlisted registry refused",
                         "exec on abt-"):
                self.assertIn(want, labels)

    def test_f_no_gate_or_service_left(self):
        p = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True)
        mine = [l for l in p.stdout.splitlines() if harness.BASE in l and ("agentbox.gate" in l or "system service" in l)]
        self.assertEqual(mine, [])

    def test_zz_no_leftovers(self):
        left = [x for x in harness.leftovers() if x != CANARY]
        self.assertEqual(left, [], "containers, pods, volumes and networks")
        self.assertEqual(sorted(harness.all_volumes() - VOLUMES_BEFORE), [], "no unlabelled (anonymous) volume")
        images = harness.leftover_images()
        # the tests' own tags, or a session build a rebuild left untagged (labelled; ab session gc ages it out)
        self.assertTrue(all(i.startswith("localhost/abt-") or re.fullmatch(r"[0-9a-f]{12}", i) for i in images), images)


if __name__ == "__main__":
    unittest.main()
