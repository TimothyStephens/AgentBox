"""Squid configuration and the networked session, against the fake podman."""
import os

from agentbox import egress, session
from agentbox.errors import AgentboxError

from .helpers import SettingsCase


def conf(mode, **kw):
    kw.setdefault("model_domains", ["api.anthropic.com"])
    return egress.squid_conf(mode, model_host="10.0.0.2", model_port=8080, **kw)


def access_lines(text):
    return [line for line in text.splitlines() if line.startswith("http_access")]


class SquidConfTest(SettingsCase):
    def test_rule_order_every_mode(self):
        for mode in egress.MODES:
            rules = access_lines(conf(mode, host_addrs=["10.0.0.103"]))
            self.assertEqual(rules[0], "http_access allow localmodel modelport", mode)
            self.assertEqual(rules[1], "http_access deny private", mode)
            self.assertEqual(rules[2], "http_access deny hostaddr", mode)
            self.assertEqual(rules[3], "http_access deny CONNECT !sslport", mode)
            self.assertEqual(rules[4], "http_access deny !webports", mode)
            self.assertEqual(rules[5], "http_access allow models", mode)
            self.assertEqual(rules[-1], "http_access deny all", mode)

    def test_modes(self):
        locked, packages, opened = (access_lines(conf(m, extra_domains=["huggingface.co"])) for m in egress.MODES)
        self.assertNotIn("http_access allow packages", locked)
        self.assertNotIn("http_access allow extra", locked)
        self.assertNotIn("http_access allow all", locked)
        self.assertIn("http_access allow packages", packages)
        self.assertIn("http_access allow extra", packages)
        self.assertNotIn("http_access allow all", packages)
        self.assertIn("http_access allow all", opened)

    def test_private_list(self):
        text = conf("open")
        acl = next(line for line in text.splitlines() if line.startswith("acl private dst"))
        for net in ("0.0.0.0/8", "10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
                    "192.168.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10", "::1"):
            self.assertIn(net, acl.split())

    def test_packages_list(self):
        acl = next(line for line in conf("packages").splitlines() if line.startswith("acl packages"))
        names = acl.split()[3:]
        for d in ("pypi.org", ".pythonhosted.org", ".github.com", ".githubusercontent.com", "conda.anaconda.org",
                  "dl-cdn.alpinelinux.org", ".bioconductor.org", "cloud.r-project.org"):
            self.assertIn(d, names)
        for registry in ("docker.io", ".docker.io", "ghcr.io", "quay.io", ".quay.io", "lscr.io"):
            self.assertNotIn(registry, names)
        self.assertFalse({"github.com", "crates.io", "bioconductor.org"} & set(names),
                         "plain names covered by a dot form are dropped (squid rejects both)")

    def test_bad_domains_refused(self):
        for bad in ("exa mple.com", "http://x.com", "x", "*.x.com", "x.com\nhttp_access allow all"):
            with self.assertRaises(AgentboxError, msg=bad):
                conf("packages", extra_domains=[bad])

    def test_proxy_env(self):
        env = egress.proxy_env("s-proxy")
        self.assertEqual(env["HTTPS_PROXY"], "http://s-proxy:3128")
        self.assertEqual(env["http_proxy"], "http://s-proxy:3128")
        self.assertNotIn("10.0.0.2", env["NO_PROXY"])


class NetSessionTest(SettingsCase):
    def setUp(self):
        super().setUp()
        self.root = self.project("p1", extra_domains=["huggingface.co"])
        self.token()

    def start(self, name="p1"):
        p = session.load_project(self.settings, name)
        s = session.make_session(self.settings, "claude", p, warn=lambda m: None)
        s.prepare()
        return s

    def test_infra_and_agent_env(self):
        s = self.start()
        s.run_captured(["true"])
        calls = self.podman_calls()
        creates = [c for c in calls if c[:2] == ["network", "create"]]
        self.assertEqual([c[-1] for c in creates], [s.int_net, s.egr_net])
        self.assertIn("--internal", creates[0])
        self.assertNotIn("--internal", creates[1])
        for c in creates:
            self.assertIn("isolate=strict", c)
            self.assertIn("agentbox-test=1", c)
        proxy = next(c for c in calls if c[:2] == ["run", "-d"])
        for want in ("--cap-drop=ALL", "--read-only", f"--network={s.egr_net}", f"--network={s.int_net}",
                     "--label=agentbox.role=proxy"):
            self.assertIn(want, proxy)
        self.assertTrue(any(a.startswith("docker.io/ubuntu/squid@sha256:") for a in proxy))
        agent = next(c for c in calls if c[:2] == ["run", "--rm"])
        self.assertIn(f"--network={s.int_net}", agent)
        self.assertIn(f"--env=HTTPS_PROXY=http://{s.proxy_name}:3128", agent)
        self.assertLess(calls.index(proxy), calls.index(agent), "proxy starts before the agent")

    def test_egress_mode_and_conf(self):
        s = self.start()
        self.assertEqual(s.egress, "packages")
        text = s.squid_conf(dns="10.89.9.1")
        self.assertIn("dns_nameservers 10.89.9.1", text)
        self.assertIn("huggingface.co", text)

    def test_open_only_when_project_says(self):
        self.project("p2", preset="pods")
        self.assertEqual(self.start("p2").egress, "packages")
        self.project("p3", preset="pods", egress="open")
        self.assertEqual(self.start("p3").egress, "open")

    def test_proxy_that_exits_fails_and_tears_down(self):
        os.environ["FAKE_PODMAN_LOGS"] = "FATAL: bad config"
        os.environ["FAKE_PODMAN_STATE"] = "exited"
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_LOGS", None)
        self.addCleanup(os.environ.pop, "FAKE_PODMAN_STATE", None)
        s = self.start()
        with self.assertRaises(AgentboxError) as e:
            s.run_captured(["true"])
        self.assertIn("proxy exited", str(e.exception))
        self.assertFalse([c for c in self.podman_calls() if c[:2] == ["run", "--rm"]], "agent never started")
        self.assertFalse(os.path.exists(self.settings.session_run_dir(s.name)))
