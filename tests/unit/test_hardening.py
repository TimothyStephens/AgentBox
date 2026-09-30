import unittest

from agentbox import config, hardening
from agentbox.errors import AgentboxError

S = config.Settings(config_dir="/c", data_dir="/d", run_dir="/r", podman="podman", values=dict(config.DEFAULTS))


def base(extra=()):
    return hardening.fixed_flags(S, cgroup_parent="agentbox.slice") + list(extra) + ["img"]


def audit(argv, **kw):
    kw.setdefault("allowed_mount_srcs", ["/proj"])
    kw.setdefault("networks", ["sess-int"])
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
                                    "--mount=type=bind,src=/dev/null,dst=/proj/.env,ro",
                                    "--mount=type=tmpfs,dst=/proj/secrets,notmpcopyup"])))

    def test_forbidden(self):
        for bad in ("--privileged", "--privileged=true", "--cap-add=NET_ADMIN", "--cap-add", "--network=host",
                    "--network=container:x", "--net=bridge", "--pid=host", "--ipc=host", "--uts=host",
                    "--security-opt=seccomp=unconfined", "--security-opt=apparmor=unconfined",
                    "--security-opt=label=disable", "--device=/dev/fuse", "-v", "--volume=/:/h",
                    "--publish=8080:80", "-p", "-P", "--pod=live", "--volumes-from=x", "--userns=host",
                    "--env-host", "--env-file=/x", "--group-add=keep-groups",
                    "--mount=type=bind,src=/home,dst=/h", "--mount=type=volume,src=v,dst=/v",
                    "--mount=type=bind,src=/dev/null,dst=/proj/.env",
                    "--mount=type=bind,src=/proj,dst=/p,bind-propagation=shared",
                    "--tmpfs=/etc:rw,notmpcopyup", "--mount=type=tmpfs,dst=/proj/secrets", "--tmpfs=/tmp:rw", "--mount=type=bind,src=/proj/../etc,dst=/e"):
            with self.assertRaises(AgentboxError, msg=bad):
                audit(base([bad]))

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


if __name__ == "__main__":
    unittest.main()
