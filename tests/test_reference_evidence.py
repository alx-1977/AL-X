"""Reads refused only for a reason, and said (27 Nov 2026).

Every read of an edit's full_diff_path was refused live: the evidence lives under AL/X's own
.alx folder, which the secret-file check refused as a whole, and the refusal gave no reason, so
the board agent tried again and again (one job: 25 times).
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers.kicad import KiCadError, bounded  # noqa: E402
from alx.providers.reference_file import ReferenceError, read_reference  # noqa: E402


class EvidenceUnderAlxTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.evidence = Path(folder.name).resolve() / ".alx" / "runtime" / "kicad.nosync" / "w" / "evidence"
        self.evidence.mkdir(parents=True)
        (self.evidence / "changes.diff").write_text("--- a\n+++ b\n")
        (self.evidence / "token.txt").write_text("x")

    def test_evidence_below_a_trusted_root_is_read_though_the_path_has_alx_in_it(self):
        got = read_reference({"path": str(self.evidence / "changes.diff")}, roots=(self.evidence,), protected=False)
        self.assertIn("+++ b", got["text"])

    def test_a_secret_name_below_the_trusted_root_is_still_refused(self):
        with self.assertRaises(ReferenceError):
            read_reference({"path": str(self.evidence / "token.txt")}, roots=(self.evidence,), protected=False)

    def test_without_a_trusted_root_alx_stays_refused(self):
        with self.assertRaises(ReferenceError):
            read_reference({"path": str(self.evidence / "changes.diff")}, roots=(self.evidence.parents[4],))


class RefusalsSayWhyTests(unittest.TestCase):
    def test_a_place_outside_the_design_folders_is_refused_with_the_reason(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            with self.assertRaises(KiCadError) as refused:
                bounded(elsewhere, (Path(root).resolve(),))
            self.assertIn("outside the design folders", refused.exception.recovery["detail"])


if __name__ == "__main__":
    unittest.main()
