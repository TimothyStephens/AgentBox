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
