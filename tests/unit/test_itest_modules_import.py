"""Every integration module imports under `make test`.

15 Nov: `test_omp_isolation` had an f-string probe script with an unescaped `{dark: titanium}`, so the
module raised NameError at import, `make itest` could not even count it, and the mistake reached Tim's host
twice. py_compile passes such a file; only importing it catches it. A module that needs a host tool the
unit-test machine lacks (step 1 asks systemctl about the slice at import) is skipped by name of the missing
binary, not silently."""
import importlib
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION = os.path.join(os.path.dirname(HERE), "integration")


class IntegrationModulesImport(unittest.TestCase):
    def test_every_integration_module_imports(self):
        names = sorted(f[:-3] for f in os.listdir(INTEGRATION) if f.startswith("test_") and f.endswith(".py"))
        self.assertTrue(names, f"no integration modules under {INTEGRATION}")
        skipped = []
        for name in names:
            with self.subTest(module=name):
                try:
                    importlib.import_module(f"tests.integration.{name}")
                except FileNotFoundError as e:
                    # a host binary this machine lacks, not a broken module
                    skipped.append(f"{name} ({e.filename})")
        if skipped:
            print(f"\n[skipped for a missing host binary: {', '.join(skipped)}]")
