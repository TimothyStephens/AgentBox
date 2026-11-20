"""Each install run has its own container, proxy and network names (20 Nov: a stale aardvark-dns record for
`ab-install-omp-int`, left by an interrupted install, kept every later OMP install's proxy name from resolving,
because each new network of the same name was matched to the old record)."""
import unittest

from agentbox import installer
from .helpers import SettingsCase


class UniqueInstallNames(SettingsCase):
    def test_two_runs_share_no_name(self):
        a, b = installer.Install(self.settings, "omp"), installer.Install(self.settings, "omp")
        for attr in ("name", "proxy_name", "int_net", "egr_net"):
            with self.subTest(attr=attr):
                self.assertNotEqual(getattr(a, attr), getattr(b, attr))

    def test_the_names_still_say_what_they_are(self):
        j = installer.Install(self.settings, "pi")
        self.assertTrue(j.name.startswith(f"{self.settings.name_prefix}install-pi-"))
        self.assertTrue(j.proxy_name.startswith(j.name) and j.int_net.startswith(j.name))


if __name__ == "__main__":
    unittest.main()
