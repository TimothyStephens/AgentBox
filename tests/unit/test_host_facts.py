"""The user's network is not agentbox's: no committed default for the model server, the host's addresses or the
selftest's LAN targets (18 Nov). They come from ~/.config/agentbox/agentbox.toml."""
import unittest

from agentbox import config


class NoCommittedHostFacts(unittest.TestCase):
    def test_the_defaults_name_no_host(self):
        self.assertEqual(config.DEFAULTS["model_host"], "")
        self.assertEqual(config.DEFAULTS["host_addrs"], [])
        self.assertEqual(config.DEFAULTS["lan_targets"], [])
