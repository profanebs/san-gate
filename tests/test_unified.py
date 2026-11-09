import contextlib
import io
import unittest
from pathlib import Path

from sangate.cli import main

ROOT = Path(__file__).resolve().parent.parent


class UnifiedCliTests(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = main(argv)
        return code, out.getvalue()

    def test_version(self):
        code, out = self.run_cli(["version"])
        self.assertEqual(code, 0)
        self.assertIn("artifact 1.0.0", out)
        self.assertIn("document 2.0.0", out)

    def test_artifact_check(self):
        code, out = self.run_cli(
            ["check", str(ROOT / "sangate1" / "tests" / "fixtures" / "evil-readme.md"), "--quiet"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "RED")

    def test_document_ingest(self):
        code, out = self.run_cli(
            ["ingest", str(ROOT / "fixtures" / "canary" / "visible" / "canary_01.txt")]
        )
        self.assertEqual(code, 1)
        self.assertIn("RED", out)

    def test_artifact_verify(self):
        code, out = self.run_cli(["verify"])
        self.assertEqual(code, 0)
        self.assertIn("all fixtures matched", out)

    def test_audit_runs_both_engines(self):
        code, out = self.run_cli(
            ["audit", str(ROOT / "sangate1" / "tests" / "fixtures" / "clean-notes.md")]
        )
        self.assertEqual(code, 0)
        self.assertIn("san-gate preflight", out)
        self.assertIn("document scan", out)


if __name__ == "__main__":
    unittest.main()
