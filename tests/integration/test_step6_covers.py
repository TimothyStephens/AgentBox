"""Critical (5 Oct, life-updates spec 6.2) and its independent audit (docs/reviews/2026-10-05-covers.md): the
agent's in-container masks and read-only covers are absent in a container it starts through the gate, so the
gate refuses any bind that reaches a covered path. Run from inside a session, through the gate, as the agent
would: the spec's 5.2 bypass test plus the audit's cases.

  * direct binds: the root, `secrets/`, a nested mask `sub/.env`, the masked `.env`, the dev-swap `.env`
    (a directory with `.env.dev`), `.git`, `.git/config`, `.git/hooks`, a compose cover, a relative and a
    `..` spelling -- all refused, no sentinel ever shown; `app/` (not covered) binds;
  * H1: the agent cannot rename a cover's parent (`dev`, `.git`, `tests/fx`): each is pinned;
  * H2: a container created with `app/` bound, `app` then swapped for a symlink to `secrets/`, is refused at
    start (mounts are checked after init, before the entrypoint runs) and never prints the sentinel;
  * H3: a read-only grant stays read-only in a spawned container;
  * builds: a Containerfile that COPYs `secrets/` and `.env` gets them empty (the context is packed in the
    session, through the masks); a context that carries content at a masked path is refused.
"""
import os
import unittest

from agentbox import config, session

from . import harness

ENV = None
SESS = None
ROOT = None
RO = None
IMG = "docker.io/library/busybox:1.36"


def setUpModule():
    global ENV, SESS, ROOT, RO
    for img in ("localhost/agentbox-omp:latest", IMG):
        if harness.podman("image", "exists", img).returncode != 0:
            raise unittest.SkipTest(f"{img} not present")
    ENV = harness.Env()
    from tests import ompfake
    omp_home = ompfake.build(os.path.join(harness.BASE, "omp"))
    with open(os.path.join(ENV.cfg, "agentbox.toml"), "a") as f:
        f.write(f'omp_home = "{omp_home}"\n')
    RO = os.path.join(ENV.projects, "covro")
    harness._write(os.path.join(RO, "data.txt"), "read-only grant\n")
    ROOT = ENV.make_project("cov", preset="python", extra=["--podman", "dev", "--data-ro", RO],
                            secrets_files=False)
    w = harness._write
    w(os.path.join(ROOT, "secrets", "db-password.txt"), "SENTINEL-SECRET\n")
    vault = os.path.join(ROOT, "secrets", "vault")          # N2: a git repo nested inside the secrets/ mask
    __import__("subprocess").run(["git", "init", "-q", vault], check=True)
    w(os.path.join(vault, ".git", "config"), '[remote "o"]\n  url = https://x:SENTINEL-GITCRED@h/r\n')
    w(os.path.join(ROOT, ".env"), "SENTINEL-ROOTENV\n")                 # no .env.dev here: a mask
    w(os.path.join(ROOT, "sub", ".env"), "SENTINEL-SUBENV\n")           # a nested mask
    w(os.path.join(ROOT, "dev", ".env"), "SENTINEL-SWAPENV\n")          # .env.dev beside it: the swap
    w(os.path.join(ROOT, "dev", ".env.dev"), "DEV=1\n")
    w(os.path.join(ROOT, "tests", "fx", "server.pem"), "SENTINEL-PEM\n")
    w(os.path.join(ROOT, "svc", "compose.yaml"), "services: {}\n")      # python preset: compose files ro
    w(os.path.join(ROOT, "app", "main.py"), "print('ok')\n")
    os.link(os.path.join(ROOT, "secrets", "db-password.txt"), os.path.join(ROOT, "link-to-secret"))
    w(os.path.join(ROOT, "Containerfile"), f"FROM {IMG}\n")
    keys = ("AB_CONFIG_DIR", "AB_DATA_DIR", "AB_RUN_DIR")
    os.environ.update({k: ENV.env[k] for k in keys})
    s = config.load()
    SESS = session.make_session(s, "omp", session.load_project(s, "cov"), warn=lambda m: None)
    SESS.prepare()
    print(f"\n[itest run {harness.RUN}: step 6 covers, session {SESS.name}]")


def tearDownModule():
    try:
        if SESS is not None:
            session.teardown(SESS.settings, SESS.podman, SESS.name, warn=lambda m: None)
    finally:
        for img in harness.podman("images", "-q", "--filter", f"label={harness.LABEL}").stdout.split():
            harness.podman("rmi", "-f", img)
        harness.cleanup()


SCRIPT = f"""
cd {{root}}
try() {{ name=$1; shift; out=$("$@" 2>&1); echo "$name=$? $(echo "$out" | tr '\\n' ' ' | tail -c 200)"; }}
R="podman run --rm"
try ROOT        $R -v {{root}}:/src {IMG} cat /src/secrets/db-password.txt
try SECRETS     $R -v {{root}}/secrets:/s {IMG} cat /s/db-password.txt
try SUBENV      $R -v {{root}}/sub:/s {IMG} cat /s/.env
try ROOTENV     $R -v {{root}}/.env:/e {IMG} cat /e
try SWAPENV     $R -v {{root}}/dev:/d {IMG} cat /d/.env
try GITDIR      $R -v {{root}}/.git:/g {IMG} cat /g/config
try GITCONFIG   $R -v {{root}}/.git/config:/c {IMG} true
try GITHOOKS    $R -v {{root}}/.git/hooks:/h {IMG} true
try COMPOSE     $R -v {{root}}/svc:/s {IMG} sh -c 'echo x >> /s/compose.yaml'
try REL         $R -v ./secrets:/s {IMG} cat /s/db-password.txt
try DOTDOT      $R -v {{root}}/app/../secrets:/s {IMG} true
try APP         $R -v {{root}}/app:/a {IMG} cat /a/main.py
# N2: nothing is mounted inside the secrets/ cover, so the nested .git is not exposed in the session
echo "N2_LS=$(ls -A {{root}}/secrets | tr '\\n' ',')"
echo "N2_GITCFG=$(cat {{root}}/secrets/vault/.git/config 2>&1 | tr '\\n' ' ' | tail -c 70)"
# N4: a hardlink to a covered file, made before the session, reads empty
echo "N4_HARDLINK=bytes=$(wc -c < {{root}}/link-to-secret 2>/dev/null)"
# N9: a symlink created at runtime inside a bound dir cannot reach a cover
try N9ABS       $R -v {{root}}/app:/a {IMG} sh -c 'ln -s {{root}}/secrets/db-password.txt /a/l; cat /a/l'
try N9REL       $R -v {{root}}/app:/a {IMG} sh -c 'ln -s ../secrets/db-password.txt /a/l; cat /a/l'
# N3: a RUN --mount with an absolute or .. source is refused by the gate itself
printf 'FROM {IMG}\\nRUN --mount=type=bind,source=/etc,target=/e cat /e/hostname\\n' > Containerfile.n3a
podman build -t abt-{{run}}-n3a -f Containerfile.n3a . 2>&1 | grep -oiE 'RUN --mount[^"]*not allowed' | head -1 | sed 's/^/N3A=/'
printf 'FROM {IMG}\\nRUN --mount=type=cache,target=/c,source=../../.. true\\n' > Containerfile.n3b
podman build -t abt-{{run}}-n3b -f Containerfile.n3b . 2>&1 | grep -oiE 'RUN --mount[^"]*not allowed' | head -1 | sed 's/^/N3B=/'
# H1: a cover's parent cannot be renamed in the session
try MVDEV       mv {{root}}/dev {{root}}/dev2
try MVGIT       mv {{root}}/.git {{root}}/.g
try MVFX        mv {{root}}/tests/fx {{root}}/app/fx
try AFTERMV     $R -v {{root}}/app:/a {IMG} sh -c 'cat /a/fx/server.pem /a/fx/*.pem 2>/dev/null; ls /a'
# H2: create with app bound, swap app for a link to secrets, then start
try CREATE      podman create --name {{swapname}} -v {{root}}/app:/a {IMG} cat /a/db-password.txt
mv {{root}}/app {{root}}/app.real && ln -s secrets {{root}}/app
try SWAPSTART   podman start -a {{swapname}}
echo "SWAPLOGS=$(podman logs {{swapname}} 2>&1 | tr '\\n' ' ')"
rm {{root}}/app && mv {{root}}/app.real {{root}}/app
# H3: a read-only grant stays read-only in a spawned container
try ROWRITE     $R -v {{ro}}:/r {IMG} sh -c 'echo x > /r/new'
try RODATA      $R -v {{ro}}:/r {IMG} cat /r/data.txt
# builds: COPY of masked paths arrives empty; a context carrying masked content is refused
printf 'FROM {IMG}\\nCOPY secrets/ /s/\\nCOPY .env /e\\nRUN echo "SECRETS_BYTES=$(cat /s/* | wc -c) ENV_BYTES=$(wc -c < /e)"\\n' > Containerfile.copy
podman build --no-cache -t abt-{{run}}-copy -f Containerfile.copy . 2>&1 | grep -oE 'SECRETS_BYTES=[0-9]+ ENV_BYTES=[0-9]+' | head -1
python3 - <<'PY'
import io, json, tarfile, urllib.parse, http.client, socket
buf = io.BytesIO()
with tarfile.open(fileobj=buf, mode="w") as t:
    for name, data in (("Containerfile", b"FROM {IMG}\\nCOPY secrets/ /s/\\n"), ("secrets/db-password.txt", b"carried\\n")):
        info = tarfile.TarInfo(name); info.size = len(data); t.addfile(info, io.BytesIO(data))
class C(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX); self.sock.connect("/run/agentbox/podman.sock")
c = C("d"); q = urllib.parse.urlencode({{"dockerfile": json.dumps(["Containerfile"]), "t": "abt-{{run}}-crafted"}})
c.request("POST", "/v5.0.0/libpod/build?" + q, body=buf.getvalue(), headers={{"Content-Type": "application/x-tar"}})
r = c.getresponse(); print("CRAFTED=%d %s" % (r.status, r.read()[:160].decode(errors="replace")))
PY
# N1: a stop between the gate's init-check and the forwarded start would re-initialise and re-resolve a
# sub-path. A start loop that flips the bound path between a real dir and a symlink to secrets/, with a
# concurrent stop loop, must never mount the real secrets/ (150 iterations).
mkdir -p race-d; ln -sfn race-d race
podman create --name {{racename}} -v {{root}}/race:/a {IMG} sh -c 'cat /a/db-password.txt 2>/dev/null; sleep 0.2' >/dev/null
( for i in $(seq 1 150); do podman stop -t 0 {{racename}} >/dev/null 2>&1; done ) &
STOP=$!
seen=none
for i in $(seq 1 150); do
  if [ $((i % 2)) -eq 0 ]; then ln -sfn secrets race; else ln -sfn race-d race; fi
  case "$(podman start -a {{racename}} 2>&1)" in *SENTINEL-SECRET*) seen=LEAKED;; esac
done
kill $STOP 2>/dev/null; wait $STOP 2>/dev/null
echo "RACE=$seen"
echo "RACELOGS=$(podman logs {{racename}} 2>&1 | grep -c SENTINEL-SECRET || echo 0)"
podman rm -f -t 0 {{racename}} >/dev/null 2>&1; ln -sfn race-d race
"""


class Covers(unittest.TestCase):
    maxDiff = None

    def test_a_spawned_container_cannot_reach_a_covered_path(self):
        swapname = f"abt-{harness.RUN}-swap"
        script = (SCRIPT.replace("{root}", ROOT).replace("{ro}", RO).replace("{swapname}", swapname)
                  .replace("{racename}", f"abt-{harness.RUN}-race").replace("{run}", harness.RUN))
        p = SESS.run_captured(["bash", "-c", script], timeout=2400)
        out = p.stdout
        print(f"\n{out[-5000:]}\n{p.stderr[-800:]}")
        self.assertNotIn("SENTINEL", out, "no covered content reached any spawned container or build")
        lines = {l.split("=", 1)[0]: l for l in out.splitlines() if "=" in l}
        for key in ("ROOT", "SECRETS", "SUBENV", "ROOTENV", "SWAPENV", "GITDIR", "GITCONFIG", "GITHOOKS", "COMPOSE",
                    "REL", "DOTDOT", "MVDEV", "MVGIT", "MVFX", "SWAPSTART", "ROWRITE"):
            self.assertIn(key, lines, key)
            self.assertNotRegex(lines[key], rf"^{key}=0 ", f"{key} should be refused: {lines[key]}")
        for key in ("MVDEV", "MVGIT", "MVFX"):
            self.assertIn("busy", lines[key].lower(), f"{key}: pinned, so the rename fails: {lines[key]}")
        self.assertRegex(lines["APP"], r"^APP=0 .*ok", f"a non-covered sub-dir binds: {lines['APP']}")
        self.assertRegex(lines["CREATE"], r"^CREATE=0 ", lines["CREATE"])
        self.assertIn("before it ran", lines["SWAPSTART"], "refused at start, after init, before the entrypoint")
        self.assertRegex(lines["RODATA"], r"^RODATA=0 .*read-only grant", "the read-only grant is readable")
        self.assertIn("SECRETS_BYTES=0 ENV_BYTES=0", out, "COPY of masked paths gets them empty")
        self.assertRegex(out, r"CRAFTED=403 .*masked", "a context carrying masked content is refused")
        self.assertNotEqual(harness.podman("image", "exists", f"abt-{harness.RUN}-crafted").returncode, 0)
        self.assertNotEqual(harness.podman("container", "exists", swapname).returncode, 0, "removed before it ran")
        self.assertNotIn("SENTINEL-GITCRED", out, "the nested .git under secrets/ is not exposed (N2)")
        self.assertRegex(lines.get("N2_LS", "N2_LS=x"), r"^N2_LS=\s*$", "secrets/ lists empty (N2)")
        self.assertIn("bytes=0", lines.get("N4_HARDLINK", ""), "a hardlink to a covered file reads empty (N4)")
        for key in ("N9ABS", "N9REL"):
            self.assertNotRegex(lines.get(key, f"{key}=0 X"), rf"^{key}=0 .*SENTINEL",
                                f"a runtime symlink cannot reach a cover ({key})")
        self.assertRegex(out, r"N3A=.*not allowed", "RUN --mount source=/etc refused by the gate (N3)")
        self.assertRegex(out, r"N3B=.*not allowed", "RUN --mount cache source=.. refused by the gate (N3)")
        self.assertNotEqual(harness.podman("image", "exists", f"abt-{harness.RUN}-n3a").returncode, 0)
        # N1: the stop/start race (150 iterations) never mounted the real secrets/
        self.assertIn("RACE=none", out, "the stop/start race never exposed the secret (N1)")
        self.assertRegex(out, r"RACELOGS=0", "no run of the race container ever read the secret (N1)")

    def test_z_nothing_left(self):
        session.teardown(SESS.settings, SESS.podman, SESS.name, warn=lambda m: None)
        self.assertEqual(harness.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
