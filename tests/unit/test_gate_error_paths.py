"""TODO.md §4.3 rows E5, E6, E11, E12: gate error paths that had no test. Each test pins the current fail-closed
behaviour (refused, an error status, nothing created or verified). Upstream lookups are faked."""
import json
import os
import shutil
import socket
import tempfile
import unittest

from agentbox import gate
from tests.unit.test_review_step5 import Fake, container, decide, run_handler

GRANT = {gate.DEV_OWNER: "abt-s-1", "agentbox.role": "grant"}


class Failing(Fake):
    """The step-5 fake whose raw podman requests answer `status` for paths matching a predicate (or raise it, if
    it is an exception). Every other request behaves as in `Fake`. Log lines are kept in `lines`."""

    def __init__(self, objects=None, fail=lambda method, path: False, status=500):
        super().__init__(objects)
        self.fail, self.status, self.lines, self.attempts = fail, status, [], []

    def raw_request(self, method, path, body=None):
        self.attempts.append((method, path))
        if self.fail(method, path):
            if isinstance(self.status, BaseException):
                raise self.status
            return self.status
        return super().raw_request(method, path, body)

    def log(self, verdict, method, path, why=""):
        self.lines.append((verdict, method, path, why))


def deletes(method, path):
    return method == "DELETE"


def volume_creates(method, path):
    return method == "POST" and path.endswith("/volumes/create")


def reply(status, doc):
    body = json.dumps(doc).encode()
    return f"HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body


def read_all(sock):
    out = b""
    while True:
        data = sock.recv(65536)
        if not data:
            return out
        out += data


# ---- E5: the remove failure path ------------------------------------------------------------------------------
class RemoveFails(unittest.TestCase):
    def test_a_delete_podman_refuses_twice_reports_failure_and_the_object_stays_unusable(self):
        g = Failing({"containers/c1": container("c1")}, fail=deletes, status=500)
        g.verified.add("c1")
        self.assertFalse(g.remove("containers", "c1", "refused"))
        self.assertEqual([m for m, _p in g.attempts], ["DELETE", "DELETE"], "tried twice, then gave up")
        self.assertNotIn("c1", g.verified)
        self.assertEqual(g.lines[-1][0], "remove-failed")
        self.assertIn("last status 500", g.lines[-1][3])
        with self.assertRaises(gate.Deny):          # its labels still say dev; it is refused all the same
            g.require_dev("containers", "c1")

    def test_a_delete_that_raises_oserror_reports_failure_and_the_object_stays_unusable(self):
        g = Failing({"volumes/v1": {"Name": "v1", "Labels": dict(GRANT, **{"agentbox.role": "dev"})}},
                    fail=deletes, status=ConnectionRefusedError("raw socket gone"))
        g.verified.add("v1")
        self.assertFalse(g.remove("volumes", "v1", "refused"))
        self.assertEqual(len(g.attempts), 2)
        self.assertNotIn("v1", g.verified)
        self.assertEqual(g.lines[-1][0], "remove-failed")
        self.assertIn("ConnectionRefusedError", g.lines[-1][3])
        with self.assertRaises(gate.Deny):
            g.require_dev("volumes", "v1")

    def test_a_delete_that_fails_once_is_retried(self):
        g = Failing(fail=lambda m, p: m == "DELETE" and len(g.attempts) == 1, status=500)
        self.assertTrue(g.remove("pods", "p1", "refused"))
        self.assertEqual(len(g.attempts), 2)
        self.assertEqual(g.lines[-1][0], "removed")

    def test_a_refused_create_whose_removal_fails_is_still_refused_and_never_verified(self):
        bad = container("c9", labels={})                    # no dev labels: the post-create check fails
        g = Failing({"containers/c9": bad}, fail=deletes, status=500)
        with self.assertRaises(gate.Deny):
            g.verify_created("libpod_create", reply(201, {"Id": "c9"}), "web")
        self.assertNotIn("c9", g.verified)
        self.assertEqual(g.lines[-1][0], "remove-failed")

    def test_a_start_whose_mounts_fail_and_whose_removal_fails_is_still_refused(self):
        doc = container("c1", Mounts=[{"Type": "bind", "Source": "/proj", "Destination": "/d"}],
                        State={"Pid": 4242, "Status": "initialized"})
        g = Failing({"containers/c1": doc}, fail=deletes, status=500)
        g.verified.add("c1")
        g.host_mountinfo = lambda: "38 1 0:40 / /proj rw - tmpfs tmpfs rw\n"
        g.read_mountinfo = lambda pid: "1 1 0:40 /elsewhere /d rw - tmpfs tmpfs rw\n"
        try:
            with self.assertRaises(gate.Deny):
                g.prepare_start("c1")
        finally:
            g.release_held()
        self.assertNotIn("c1", g.verified)
        self.assertEqual(g.lines[-1][0], "remove-failed")


# ---- E6: grant-volume and named-volume creates ----------------------------------------------------------------
SUB_BIND = {"image": "x", "mounts": [{"type": "bind", "source": "/proj/data", "destination": "/d"}]}


class VolumeCreatesFail(unittest.TestCase):
    def test_a_failed_grant_volume_create_refuses_the_container(self):
        g = Failing(fail=volume_creates, status=500)
        with self.assertRaises(gate.Deny) as cm:
            decide(g, "POST", "/v5/libpod/containers/create", SUB_BIND)
        self.assertIn("could not create the grant volume for /proj (status 500)", str(cm.exception))
        self.assertNotIn("volumes/abt-s-1-grant0", g.objects)
        self.assertNotIn("abt-s-1-grant0", g.verified)

    def test_a_grant_volume_create_that_raises_is_answered_with_a_refusal_not_a_traceback(self):
        g = Failing(fail=volume_creates, status=ConnectionResetError("raw socket reset"))
        a, t = run_handler(self, g)
        body = json.dumps(SUB_BIND).encode()
        a.sendall(b"POST /v5/libpod/containers/create HTTP/1.1\r\nContent-Length: "
                  + str(len(body)).encode() + b"\r\n\r\n" + body)
        a.shutdown(socket.SHUT_WR)
        t.join(10)
        out = read_all(a)
        self.assertTrue(out.startswith(b"HTTP/1.1 403 Forbidden"), out[:120])
        self.assertIn(b"request refused", out)
        self.assertNotIn("abt-s-1-grant0", g.verified)

    def test_a_grant_volume_reported_created_but_not_found_refuses_the_container(self):
        g = Failing(fail=volume_creates, status=201)        # "created", but nothing is stored
        with self.assertRaises(gate.Deny) as cm:
            decide(g, "POST", "/v5/libpod/containers/create", SUB_BIND)
        self.assertIn("is not this session's grant volume", str(cm.exception))

    def test_a_name_collision_with_a_volume_that_is_not_the_grant_volume_refuses_the_container(self):
        grant_opts = {"type": "none", "o": "bind", "device": "/proj"}
        for why, doc in (
                ("another stack's volume", {"Labels": {"io.podman.compose.project": "media"}}),
                ("this session's ordinary dev volume", {"Labels": dict(GRANT, **{"agentbox.role": "dev"}),
                                                         "Options": grant_opts}),
                ("another session's grant volume", {"Labels": dict(GRANT, **{gate.DEV_OWNER: "abt-other"}),
                                                     "Options": grant_opts}),
                ("a grant volume over another device", {"Labels": GRANT, "Options": dict(grant_opts, device="/etc")}),
                ("a grant volume with extra options", {"Labels": GRANT, "Options": dict(grant_opts, uid="0")}),
                ("a grant volume on another driver", {"Labels": GRANT, "Options": grant_opts, "Driver": "nfs"})):
            g = Failing({"volumes/abt-s-1-grant0": dict(doc, Name="abt-s-1-grant0")})
            with self.subTest(why), self.assertRaises(gate.Deny) as cm:
                decide(g, "POST", "/v5/libpod/containers/create", SUB_BIND)
            self.assertIn("exists but is not this session's grant volume", str(cm.exception))
            self.assertFalse([p for _m, p in g.attempts if p.endswith("/volumes/create")], "nothing created")

    def test_a_failed_named_volume_create_refuses_the_container(self):
        for path, body in (
                ("/v5/libpod/containers/create", {"image": "x", "volumes": [{"Name": "data", "Dest": "/d"}]}),
                ("/v5/containers/create", {"Image": "x", "HostConfig": {"Binds": ["data:/d"]}}),
                ("/v5/containers/create",
                 {"Image": "x", "HostConfig": {"Mounts": [{"Type": "volume", "Source": "data", "Target": "/d"}]}})):
            g = Failing(fail=volume_creates, status=409)
            with self.subTest(path=path, body=body), self.assertRaises(gate.Deny) as cm:
                decide(g, "POST", path, body)
            self.assertIn("could not create volume data (status 409)", str(cm.exception))
            self.assertNotIn("data", g.verified)
            self.assertNotIn("volumes/data", g.objects)

    def test_a_named_volume_create_that_raises_is_answered_with_a_refusal_not_a_traceback(self):
        g = Failing(fail=volume_creates, status=ConnectionRefusedError("raw socket gone"))
        a, t = run_handler(self, g)
        body = json.dumps({"image": "x", "volumes": [{"Name": "data", "Dest": "/d"}]}).encode()
        a.sendall(b"POST /v5/libpod/containers/create HTTP/1.1\r\nContent-Length: "
                  + str(len(body)).encode() + b"\r\n\r\n" + body)
        a.shutdown(socket.SHUT_WR)
        t.join(10)
        out = read_all(a)
        self.assertTrue(out.startswith(b"HTTP/1.1 403 Forbidden"), out[:120])
        self.assertNotIn("data", g.verified)


# ---- E11: network ignoreIfExists; unrecognised /create endpoints ------------------------------------------------
class NetworkIgnoreIfExists(unittest.TestCase):
    def test_network_create_with_ignore_if_exists_is_refused(self):
        for path in ("/v5/libpod/networks/create?ignoreIfExists=true", "/v5/libpod/networks/create?ignoreIfExists=1",
                     "/v5/libpod/networks/create?ignoreIfExists=TRUE", "/v5/libpod/networks/create?IgnoreIfExists=True",
                     "/v5/libpod/networks/create?ignoreifexists=true",
                     "/v5/libpod/networks/create?ignoreIfExists=false&ignoreIfExists=true",
                     "/v5/networks/create?ignoreIfExists=true", "/libpod/networks/create?ignoreIfExists=1"):
            with self.subTest(path=path), self.assertRaises(gate.Deny) as cm:
                decide(Fake(), "POST", path, {"name": "n"})
            self.assertIn("ignoreIfExists is not allowed", str(cm.exception))

    def test_network_create_with_ignore_if_exists_false_is_rewritten_not_refused(self):
        _t, body, kind = decide(Fake(), "POST", "/v5/libpod/networks/create?ignoreIfExists=false", {"name": "n"})
        self.assertEqual(kind, "net_create")
        self.assertTrue(json.loads(body)["internal"])

    # Found while writing these tests (17 Nov): podman decodes this bool with gorilla/schema's convertBool, which
    # takes "on" and every strconv.ParseBool true; the gate refused only "1" and "true". It now refuses anything
    # that is not a plain false.
    def test_network_create_with_other_true_spellings_of_ignore_if_exists_is_refused(self):
        for value in ("t", "T", "on", "TRUE", "yes"):
            with self.subTest(value=value), self.assertRaises(gate.Deny):
                decide(Fake(), "POST", f"/v5/libpod/networks/create?ignoreIfExists={value}", {"name": "n"})

    def test_an_existing_foreign_network_reported_as_created_is_refused_and_not_deleted(self):
        foreign = {"Id": "n1", "name": "media_default", "driver": "bridge", "internal": False,
                   "labels": {"com.docker.compose.project": "media"}}
        g = Failing({"networks/n1": foreign})
        with self.assertRaises(gate.Deny):
            g.verify_created("net_create", reply(200, {"Id": "n1", "name": "media_default"}), "media_default")
        self.assertEqual(g.deleted, [], "another stack's network must not be removed")
        self.assertNotIn("n1", g.verified)
        self.assertNotIn("media_default", g.verified)


class UnrecognisedCreate(unittest.TestCase):
    def test_a_post_to_an_unrecognised_create_endpoint_is_refused(self):
        for path in ("/v5/libpod/widgets/create", "/v5/widgets/create", "/libpod/widgets/create",
                     "/v5/libpod/images/create", "/v5/libpod/containers/c1/create", "/v5/libpod/exec/create",
                     "/v4.9.3/libpod/foo/bar/create?x=1"):
            with self.subTest(path=path), self.assertRaises(gate.Deny) as cm:
                decide(Fake(), "POST", path, {})
            self.assertIn("an unrecognised create endpoint", str(cm.exception))


# ---- E12: mount_violations' "cannot confirm what is mounted" -----------------------------------------------------
class CannotConfirmMounts(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="abt-unit-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.proj = os.path.join(self.tmp, "proj")
        os.makedirs(os.path.join(self.proj, "data"))
        self.rel = self.proj[len(self.tmp):]
        self.host = f"38 1 0:40 / {self.tmp} rw - tmpfs tmpfs rw\n"

    def gate(self, mounts, grant_doc=None, pid=4242):
        doc = container("c1", Mounts=mounts, State={"Pid": pid, "Status": "initialized"})
        objects = {"containers/c1": doc}
        objects["volumes/abt-s-1-grant0"] = grant_doc if grant_doc is not None else {
            "Name": "abt-s-1-grant0", "Driver": "local", "Labels": GRANT,
            "Options": {"type": "none", "o": "bind", "device": self.proj}}
        g = Failing(objects)
        g.cfg["bind_roots"] = [self.proj]
        g.verified.add("c1")
        g.host_mountinfo = lambda: self.host
        return g, doc

    BIND = [{"Type": "bind", "Source": "/x", "Destination": "/d"}]
    GRANT_VOL = [{"Type": "volume", "Name": "abt-s-1-grant0", "Destination": "/m"}]

    def test_a_bind_missing_from_the_mount_table_is_a_violation(self):
        g, doc = self.gate(self.BIND)
        g.read_mountinfo = lambda pid: f"1 1 0:40 {self.rel} /other ro - tmpfs tmpfs rw\n"
        for strict in (False, True):
            with self.subTest(strict=strict):
                self.assertEqual(g.mount_violations(doc, strict=strict), ["cannot confirm what is mounted at /d"])

    def test_a_grant_volume_missing_from_the_mount_table_is_a_violation(self):
        g, doc = self.gate(self.GRANT_VOL)
        g.read_mountinfo = lambda pid: f"1 1 0:40 {self.rel} /other ro - tmpfs tmpfs rw\n"
        self.assertEqual(g.mount_violations(doc, strict=True), ["cannot confirm what is mounted at /m"])

    def test_a_grant_named_volume_that_is_not_a_grant_volume_is_a_violation(self):
        """Mounted where the grant should be, but the volume under that name is not the gate's grant volume."""
        for why, vol in (("missing", {}),
                         ("not grant-labelled", {"Name": "abt-s-1-grant0", "Labels": {}, "Options": {}}),
                         ("over another device", {"Name": "abt-s-1-grant0", "Labels": GRANT,
                                                  "Options": {"type": "none", "o": "bind", "device": "/etc"}})):
            g, doc = self.gate(self.GRANT_VOL, grant_doc=vol)
            g.read_mountinfo = lambda pid: f"1 1 0:40 {self.rel}/data /m ro - tmpfs tmpfs rw\n"
            with self.subTest(why):
                self.assertEqual(g.mount_violations(doc, strict=True), ["cannot confirm what is mounted at /m"])

    def test_a_grant_root_the_host_mount_table_does_not_place_is_a_violation(self):
        g, doc = self.gate(self.GRANT_VOL)
        g.host_mountinfo = lambda: ""                       # the grant root's filesystem cannot be located
        g.read_mountinfo = lambda pid: f"1 1 0:40 {self.rel}/data /m ro - tmpfs tmpfs rw\n"
        self.assertEqual(g.mount_violations(doc, strict=True), ["cannot confirm what is mounted at /m"])

    def test_no_process_or_an_unreadable_mount_table_is_a_violation_when_strict(self):
        g, doc = self.gate(self.BIND, pid=0)
        self.assertEqual(g.mount_violations(doc, strict=True), ["cannot read the container's mounts (no process)"])
        self.assertEqual(g.mount_violations(doc), [], "not strict: pinned as is (every caller passes strict)")
        g, doc = self.gate(self.BIND)
        g.read_mountinfo = lambda pid: (_ for _ in ()).throw(PermissionError("denied"))
        self.assertEqual(g.mount_violations(doc, strict=True), ["cannot read the container's mounts"])
        self.assertEqual(g.mount_violations(doc), [])

    def test_a_start_whose_mounts_cannot_be_confirmed_removes_the_container_and_is_refused(self):
        g, _doc = self.gate([{"Type": "bind", "Source": self.proj, "Destination": "/d"}])
        g.read_mountinfo = lambda pid: f"1 1 0:40 {self.rel} /other ro - tmpfs tmpfs rw\n"
        try:
            with self.assertRaises(gate.Deny) as cm:
                g.prepare_start("c1")
        finally:
            g.release_held()
        self.assertIn("cannot confirm what is mounted at /d", str(cm.exception))
        self.assertTrue(any("/containers/c1" in d for d in g.deleted), g.deleted)
        self.assertNotIn("c1", g.verified)


if __name__ == "__main__":
    unittest.main()
