import unittest

from agentbox import config, hardening
from agentbox.errors import AgentboxError

S = config.Settings(config_dir="/c", data_dir="/d", run_dir="/r", podman="podman", values=dict(config.DEFAULTS))


def base(extra=()):
    return hardening.fixed_flags(S, cgroup_parent="agentbox.slice") + list(extra) + ["img"]


# The session's one empty mask file, as `session.py` emits it. `/dev/null` used to be accepted here too,
# from before step 5 -- a character device in the project tree broke rootless builds -- and these tests were
# the only thing that reached that branch, so they drive the live path now (D-E8).
MASK = "/r/sessions/s/mask-empty"


def audit(argv, **kw):
    kw.setdefault("allowed_mount_srcs", ["/proj", "/r/sessions/s"])
    kw.setdefault("allowed_mount_dsts", ["/proj"])
    kw.setdefault("networks", ["sess-int"])
    kw.setdefault("mask_file", MASK)
    return hardening.audit(argv, **kw)


class HardeningTest(unittest.TestCase):
    def test_fixed_flags(self):
        f = hardening.fixed_flags(S, cgroup_parent="agentbox.slice")
        for want in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id",
                     "--memory=4g", "--memory-swap=4g", "--cpus=3", "--pids-limit=4096",
                     "--oom-score-adj=500", "--cgroup-parent=agentbox.slice"):
            self.assertIn(want, f)
        self.assertNotIn("--cgroup-parent=", " ".join(hardening.fixed_flags(S, cgroup_parent="")))

    def test_clean_argv_passes(self):
        self.assertTrue(audit(base(["--mount=type=bind,src=/proj,dst=/proj", "--network=none",
                                    f"--mount=type=bind,src={MASK},dst=/proj/.env,ro",
                                    "--mount=type=tmpfs,dst=/proj/secrets,notmpcopyup"])))

    def test_forbidden(self):
        for bad in ("--privileged", "--privileged=true", "--cap-add=NET_ADMIN", "--cap-add", "--network=host",
                    "--network=container:x", "--net=bridge", "--pid=host", "--ipc=host", "--uts=host",
                    "--security-opt=seccomp=unconfined", "--security-opt=apparmor=unconfined",
                    "--security-opt=label=disable", "--device=/dev/fuse", "-v", "--volume=/:/h",
                    "--publish=8080:80", "-p", "-P", "--pod=live", "--volumes-from=x", "--userns=host",
                    "--env-host", "--env-file=/x", "--group-add=keep-groups",
                    "--mount=type=bind,src=/home,dst=/h", "--mount=type=volume,src=v,dst=/v",
                    f"--mount=type=bind,src={MASK},dst=/proj/.env",     # a mask must be read-only
                    "--mount=type=bind,src=/proj,dst=/p,bind-propagation=shared",
                    "--tmpfs=/etc:rw,notmpcopyup", "--mount=type=tmpfs,dst=/proj/secrets", "--tmpfs=/tmp:rw", "--mount=type=bind,src=/proj/../etc,dst=/e"):
            with self.assertRaises(AgentboxError, msg=bad):
                audit(base([bad]))

    def test_mount_source_type_and_propagation_refused_with_an_allowed_destination(self):
        """E1/T1: the existing forbidden cases use a destination outside the grants, so the destination
        check refuses first and the source/type/propagation checks are never reached. Here the destination
        is allowed (under /proj), so each of those checks is what bites."""
        # bind source outside the grants
        with self.assertRaises(AgentboxError) as e:
            audit(base(["--mount=type=bind,src=/etc,dst=/proj/p"]))
        self.assertIn("outside the session's grants", str(e.exception))
        # a non-bind, non-tmpfs mount type
        with self.assertRaises(AgentboxError) as e:
            audit(base(["--mount=type=volume,src=v,dst=/proj/v"]))
        self.assertIn("mount type", str(e.exception))
        # a disallowed mount option on an otherwise-allowed bind
        for opt in ("propagation=rshared", "bind-propagation=shared", "relabel=shared"):
            with self.subTest(opt=opt), self.assertRaises(AgentboxError) as e:
                audit(base([f"--mount=type=bind,src=/proj,dst=/proj/p,{opt}"]))
            self.assertIn("not allowed", str(e.exception))
        # the same bind without those options passes (proves it is the option, not the dst, that bit)
        self.assertTrue(audit(base(["--mount=type=bind,src=/proj,dst=/proj/p,ro"])))

    def test_required(self):
        for drop in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id",
                     "--memory=4g", "--pids-limit=4096", "--oom-score-adj=500"):
            argv = [a for a in base() if a != drop]
            with self.assertRaises(AgentboxError, msg=drop):
                audit(argv)

    def test_session_network_allowed(self):
        audit(base(["--network=sess-int"]))
        with self.assertRaises(AgentboxError):
            audit(base(["--network=podman"]))


class MaskFile(unittest.TestCase):
    """Step 5: masks are read-only binds of one empty file (a /dev/null character device in the tree broke
    rootless builds of the project directory). A writable mount of that file is refused."""

    def argv(self, mount):
        return ["--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--userns=keep-id",
                "--pids-limit=1", "--memory=1g", "--memory-swap=1g", "--oom-score-adj=1", mount, "img"]

    def test_mask_must_be_read_only(self):
        from agentbox import hardening
        from agentbox.errors import AgentboxError
        ok = self.argv("--mount=type=bind,src=/run/s/mask-empty,dst=/proj/.env,ro")
        hardening.audit(ok, allowed_mount_srcs=["/run/s"], allowed_mount_dsts=["/proj"], networks=[],
                        mask_file="/run/s/mask-empty")
        bad = self.argv("--mount=type=bind,src=/run/s/mask-empty,dst=/proj/.env")
        with self.assertRaises(AgentboxError):
            hardening.audit(bad, allowed_mount_srcs=["/run/s"], allowed_mount_dsts=["/proj"], networks=[],
                            mask_file="/run/s/mask-empty")


if __name__ == "__main__":
    unittest.main()
