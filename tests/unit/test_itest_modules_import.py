"""Every integration module imports under `make test`.

15 Nov: `test_omp_isolation` had an f-string probe script with an unescaped `{dark: titanium}`, so the
module raised NameError at import, `make itest` could not even count it, and the mistake reached Tim's host
twice. py_compile passes such a file; only importing it catches it. A module that needs a host tool the
unit-test machine lacks (step 1 asks systemctl about the slice at import) is skipped by name of the missing
binary, not silently."""
import importlib
import os
import shutil
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
INTEGRATION = os.path.join(os.path.dirname(HERE), "integration")


def missing_binary(e):
    """The FileNotFoundError is subprocess failing to find a program: a bare name, not on PATH."""
    name = e.filename
    return isinstance(name, str) and bool(name) and os.sep not in name and shutil.which(name) is None


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
                    # Only a host BINARY this machine lacks (a bare command name not on PATH) is a skip. A
                    # missing fixture or any other path is a broken module and fails (review 16 Nov).
                    if not missing_binary(e):
                        raise
                    skipped.append(f"{name} ({e.filename})")
        if skipped:
            print(f"\n[skipped for a missing host binary: {', '.join(skipped)}]")


class OnlyAMissingBinaryIsSkipped(unittest.TestCase):
    def test_the_classifier(self):
        import errno
        gone = FileNotFoundError(errno.ENOENT, "No such file or directory", "abt-no-such-binary-xyz")
        self.assertTrue(missing_binary(gone))
        for e in (FileNotFoundError(errno.ENOENT, "x", "/repo/tests/fixtures/missing.json"),
                  FileNotFoundError(errno.ENOENT, "x", "fixtures/missing.json"),
                  FileNotFoundError(errno.ENOENT, "x", "python3"),           # on PATH: not why it failed
                  FileNotFoundError("no filename at all")):
            with self.subTest(e=e):
                self.assertFalse(missing_binary(e))
