"""A real podman-compose workflow through the real gate (Tim, 2 Oct: the itests passed with hand-built requests
while real compose failed in the trial).

One dev-tier session (OMP image: podman-remote and podman-compose, as the agent has them) runs, in order:
  * `podman-compose build`: a Dockerfile whose RUN does `pip install` through the proxy;
  * `podman-compose up -d`: the built image (web) depends_on a service with a healthcheck (db); a named
    volume and a ./app bind on web; both NO_PROXY and no_proxy set in its environment;
  * web serves ./app and reaches db by name; the agent reaches web by name;
  * `podman stop` / `podman start` of db, `podman-compose up -d --force-recreate web`;
  * `podman-compose down`, `podman-compose up -d` (the named volume's data is still there), `down` again.
Every step must succeed. Afterwards nothing of the session is left (containers, pods, volumes, networks).
"""
import os
import unittest

from agentbox import config, session

from . import harness

ENV = None
COMPOSE = f"abt-{harness.RUN}-cw"
BASE_IMAGE = "docker.io/library/python:3.12-slim"
FILES = {
    "web/Dockerfile": f"""FROM {BASE_IMAGE}
RUN pip install --no-cache-dir six==1.16.0 && python -c "import six; print('PIP_INSTALL_OK')"
WORKDIR /srv
""",
    "app/index.html": "compose-web-ok\n",
    "compose.yml": f"""services:
  web:
    build: ./web
    image: localhost/{COMPOSE}-web:latest
    command: ["python", "-m", "http.server", "8000", "--directory", "/app"]
    depends_on:
      - db
    volumes:
      - ./app:/app
      - data:/data
    environment:
      NO_PROXY: "db,web,localhost"
      no_proxy: "db,web,localhost"
    labels:
      - agentbox-test=1
  db:
    image: docker.io/library/busybox:1.36
    command: ["sh", "-c", "mkdir -p /www && echo db-ok > /www/index.html && exec httpd -f -p 8080 -h /www"]
    healthcheck:
      test: ["CMD", "wget", "-Y", "off", "-q", "-O-", "http://localhost:8080/"]
      interval: 2s
      retries: 10
    labels:
      - agentbox-test=1
volumes:
  data: {{}}
""",
}

SCRIPT = f"""
cd {{root}}
c={COMPOSE}
step() {{ name=$1; shift; out=$("$@" 2>&1); rc=$?; echo "$name=$rc"; [ $rc -ne 0 ] && echo "$out" | tail -15 | sed "s/^/  $name| /"; return 0; }}
podman-compose -p $c build > /tmp/build.out 2>&1; echo "BUILD=$?"; tail -15 /tmp/build.out | sed "s/^/  BUILD| /"
grep -x PIP_INSTALL_OK /tmp/build.out | head -1
step UP podman-compose -p $c up -d
for i in $(seq 1 30); do curl -s --noproxy '*' -m 3 http://web:8000/index.html | grep -q compose-web-ok && break; sleep 2; done
echo "WEB_BY_NAME=$(curl -s --noproxy '*' -m 3 http://web:8000/index.html)"
step WEB_TO_DB podman exec ${{c}}_web_1 python -c "import urllib.request,sys; sys.exit(0 if b'db-ok' in urllib.request.urlopen('http://db:8080/', timeout=5).read() else 1)"
podman exec ${{c}}_web_1 sh -c 'echo "ENV_UPPER=$NO_PROXY"; echo "ENV_LOWER=$no_proxy"'
step DATA_WRITE podman exec ${{c}}_web_1 sh -c 'echo kept > /data/marker'
step HEALTH podman healthcheck run ${{c}}_db_1
step STOP podman stop -t 2 ${{c}}_db_1
step START podman start ${{c}}_db_1
step RECREATE podman-compose -p $c up -d --force-recreate web
step DOWN podman-compose -p $c down
step UP2 podman-compose -p $c up -d
step DATA_KEPT podman exec ${{c}}_web_1 sh -c 'grep -q kept /data/marker'
step DOWN2 podman-compose -p $c down
echo "PS_AFTER=$(podman ps -a --format '{{{{.Names}}}}' | tr '\\n' ' ')"
echo "PODS_AFTER=$(podman pod ps --format '{{{{.Name}}}}' | tr '\\n' ' ')"
step POD_CREATE podman pod create --infra=false --name ${{c}}-pod
step POD_RM podman pod rm ${{c}}-pod
step POD_LEFT podman pod create --infra=false --name ${{c}}-left
# ${{c}}-left is left on purpose: the session's teardown (what ab kill runs) must remove it
"""

STEPS = ("BUILD", "UP", "WEB_TO_DB", "DATA_WRITE", "HEALTH", "STOP", "START", "RECREATE", "DOWN", "UP2", "DATA_KEPT",
         "DOWN2", "POD_CREATE", "POD_RM", "POD_LEFT")


def setUpModule():
    global ENV
    for img in ("localhost/agentbox-omp:latest", "docker.io/library/busybox:1.36", BASE_IMAGE):
        if harness.podman("image", "exists", img).returncode != 0:
            p = harness.podman("pull", "-q", img, timeout=600) if not img.startswith("localhost/") else None
            if p is None or p.returncode != 0:
                raise unittest.SkipTest(f"{img} not present")
    ENV = harness.Env()
    from tests import ompfake
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    root = ENV.make_project("cw", preset="python", extra=["--podman", "dev"], secrets_files=False)
    for rel, text in FILES.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
        with open(os.path.join(root, rel), "w") as f:
            f.write(text)
    print(f"\n[itest run {harness.RUN}: step 5 compose workflow]")


def tearDownModule():
    for img in harness.podman("images", "-q", "--filter", f"label={harness.LABEL}").stdout.split():
        harness.podman("rmi", "-f", img)
    harness.cleanup()


class ComposeWorkflow(unittest.TestCase):
    maxDiff = None

    def test_a_compose_workflow(self):
        keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
        old = {k: os.environ.get(k) for k in keys}
        os.environ.update({k: ENV.env[k] for k in keys})
        try:
            s = config.load()
            sess = session.make_session(s, "omp", session.load_project(s, "cw"), warn=lambda m: None)
            sess.prepare()
            root = os.path.join(ENV.projects, "cw")
            p = sess.run_captured(["bash", "-c", SCRIPT.replace("{root}", root)], timeout=1800)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        out = p.stdout
        print(f"\n{out[-6000:]}\n{p.stderr[-1500:]}")
        gate_log = [fn for fn in os.listdir(os.path.join(ENV.data, "logs")) if fn.endswith("-gate.log")]
        for fn in gate_log:
            denies = [l for l in open(os.path.join(ENV.data, "logs", fn)) if '"deny"' in l or '"error"' in l]
            if denies:
                print("gate refusals:\n  " + "  ".join(denies[-25:]))
        for fn in os.listdir(os.path.join(ENV.data, "logs")):
            if fn.endswith("-sockguard-dev.log"):
                sg = [l for l in open(os.path.join(ENV.data, "logs", fn)) if '"decision":"deny"' in l]
                if sg:
                    print("sockguard refusals:\n  " + "  ".join(l[:400] for l in sg[-15:]))
        for fn in os.listdir(os.path.join(ENV.data, "logs")):
            if fn.endswith("-proxy.log"):
                lines = [l.rstrip() for l in open(os.path.join(ENV.data, "logs", fn), errors="replace")
                         if "pypi" in l or "pythonhosted" in l or " 503 " in l or "/503" in l]
                print("proxy log (pypi, 503):\n  " + "\n  ".join(lines[-20:]))
        failed = [s for s in STEPS if f"{s}=0" not in out]
        self.assertEqual(failed, [], "compose workflow steps that failed")
        self.assertIn("PIP_INSTALL_OK", out, "pip install in a build, through the proxy")
        self.assertIn("WEB_BY_NAME=compose-web-ok", out, "the built image runs, serving the ./app bind")
        self.assertRegex(out, r"ENV_UPPER=db,web,localhost\n")
        self.assertRegex(out, r"ENV_LOWER=db,web,localhost\n")
        self.assertRegex(out, r"PS_AFTER=\s*\n", "compose down removed the containers")
        self.assertRegex(out, r"PODS_AFTER=\s*\n", "compose down removed its pod")

    def test_z_nothing_left(self):
        """Including the pod left on purpose: teardown (what ab kill runs) removes session pods (trial, 2 Oct)."""
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
