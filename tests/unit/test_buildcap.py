import os
import tempfile
import unittest

from agentbox import buildcap
from agentbox.errors import AgentboxError

GIB = 1 << 30


def active(name):
    return True, "active"


def idle(name):
    return False, "agentbox.slice is inactive, not active"


class BuildCapTest(unittest.TestCase):
    def test_rewrite_when_active(self):
        q = buildcap.rewrite_build_query([("t", "img"), ("memory", "0"), ("networkmode", "1")],
                                         slice_name="agentbox.slice", session="s1", memory_bytes=GIB, state=active)
        d = dict(q)
        self.assertEqual(d["t"], "img")
        self.assertEqual(d["networkmode"], "1")
        self.assertTrue(d["cgroupparent"].endswith("/agentbox.slice/s1-build"))
        self.assertTrue(d["cgroupparent"].startswith(f"/user.slice/user-{os.getuid()}.slice/"))
        self.assertEqual((d["memory"], d["memswap"]), (str(GIB), str(GIB)))
        self.assertEqual([k for k, _ in q].count("memory"), 1, "the client's memory value is dropped")

    def test_refused_when_idle(self):
        with self.assertRaises(AgentboxError) as e:
            buildcap.rewrite_build_query([("t", "img")], slice_name="agentbox.slice", session="s1",
                                         memory_bytes=GIB, state=idle)
        self.assertIn("uncapped build is not allowed", str(e.exception))

    def test_client_cgroup_parent_refused(self):
        for key in ("cgroupparent", "CgroupParent"):
            with self.assertRaises(AgentboxError):
                buildcap.rewrite_build_query([(key, "/")], slice_name="agentbox.slice", session="s1",
                                             memory_bytes=GIB, state=active)

    def test_slice_state_reads_memory_max(self):
        import shutil
        root = tempfile.mkdtemp(prefix="abt-unit-cg-")
        self.addCleanup(shutil.rmtree, root, True)
        d = root + buildcap.slice_cgroup_path("x.slice", uid=5)
        os.makedirs(d)
        self.assertFalse(buildcap.slice_state("x.slice", root=root, uid=5, systemctl=False)[0], "no memory.max")
        with open(os.path.join(d, "memory.max"), "w") as f:
            f.write("max\n")
        self.assertEqual(buildcap.slice_state("x.slice", root=root, uid=5, systemctl=False)[0], False)
        with open(os.path.join(d, "memory.max"), "w") as f:
            f.write("4613734400\n")
        self.assertEqual(buildcap.slice_state("x.slice", root=root, uid=5, systemctl=False)[0], True)
        self.assertFalse(buildcap.slice_state("y.slice", root=root, uid=5, systemctl=False)[0])


class BuildMemoryTest(unittest.TestCase):
    def setUp(self):
        from agentbox import config
        self.s = config.Settings(config_dir="/c", data_dir="/d", run_dir="/r", podman="podman",
                                 values=dict(config.DEFAULTS))

    def check(self, value, cap):
        from unittest import mock
        from agentbox import session
        with mock.patch("agentbox.cgroup.slice_memory_max", return_value=cap):
            return session.check_build_memory(self.s, "p", value)

    def test_default_is_2g(self):
        self.assertEqual(self.s.build_memory, "2g")
        self.assertEqual(self.check(self.s.build_memory, 4400 << 20), 2 << 30)

    def test_sizes(self):
        from agentbox.cgroup import parse_size
        self.assertEqual([parse_size(x) for x in ("2g", "512m", "1024k", "1.5g", "100")],
                         [2 << 30, 512 << 20, 1 << 20, 3 << 29, 100])
        for bad in ("lots", "", "2x"):
            with self.assertRaises(AgentboxError, msg=bad):
                parse_size(bad)

    def test_within_the_slice_cap(self):
        self.assertEqual(self.check("4g", 4400 << 20), 4 << 30)
        with self.assertRaises(AgentboxError) as e:
            self.check("5g", 4400 << 20)
        self.assertIn("over the agentbox.slice cap", str(e.exception))
        with self.assertRaises(AgentboxError):
            self.check("0", 4400 << 20)
        self.assertEqual(self.check("8g", None), 8 << 30, "no slice, no cap to check (builds are refused anyway)")
