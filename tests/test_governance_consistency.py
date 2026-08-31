"""The enforcement specification must not contradict the canonical laws.

The three-law rewrite left docs/LAW_ENFORCEMENT.md defining gates for nineteen
laws that no longer existed, and both gates passed anyway. AGENTS.md says both
documents bind and that work stops when they conflict, so a contradiction that
no gate detects is exactly the failure the gates exist to prevent.
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.check_governance import check_repository  # noqa: E402


class ConsistencyGateTests(unittest.TestCase):
    """Each mutation must be rejected, or the gate proves nothing."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repository"
        shutil.copytree(
            REPOSITORY_ROOT,
            self.root,
            ignore=shutil.ignore_patterns(
                ".git", "__pycache__", ".pytest_cache", ".alx", "*.pyc"
            ),
        )

    def rewrite(self, relative_path: str, old: str, new: str) -> None:
        path = self.root / relative_path
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"{relative_path} no longer contains the anchor")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def violations(self) -> list[str]:
        return check_repository(self.root)

    def test_the_unmodified_repository_passes(self) -> None:
        self.assertEqual(self.violations(), [])

    def test_a_gate_for_a_law_that_does_not_exist_is_rejected(self) -> None:
        """The exact contradiction the rewrite left behind."""
        self.rewrite(
            "docs/LAW_ENFORCEMENT.md",
            "| 3 — Ambiguity returns to AL/X",
            "| 9 — Every tool result returns to AL/X",
        )
        self.assertTrue(
            any("do not exist" in item for item in self.violations()),
            "an enforcement gate for a deleted law must fail the check",
        )

    def test_a_law_with_no_gate_at_all_is_rejected(self) -> None:
        text = (self.root / "docs/LAW_ENFORCEMENT.md").read_text(encoding="utf-8")
        row = next(
            line for line in text.splitlines() if line.startswith("| 2 — ")
        )
        self.rewrite("docs/LAW_ENFORCEMENT.md", row + "\n", "")
        self.assertTrue(
            any("no gate is defined" in item for item in self.violations()),
            "a law with no enforcement gate must fail the check",
        )

    def test_a_gate_row_carrying_the_wrong_title_is_rejected(self) -> None:
        self.rewrite(
            "docs/LAW_ENFORCEMENT.md",
            "| 2 — Code executes known procedures",
            "| 2 — Something else entirely",
        )
        self.assertTrue(
            any("does not carry that law's title" in item for item in self.violations()),
            "a renamed law must not keep passing on a stale gate row",
        )

    def test_the_superseded_blueprint_example_is_rejected(self) -> None:
        """It contradicted Law 2 and was cited in review as prohibiting capture."""
        self.rewrite(
            "docs/ARCHITECTURE_BLUEPRINT.md",
            "A capability is prohibited when it interprets what Friedl wants",
            "A capability such as `process_DHL_invoice_workflow` would encode a "
            "journey and is prohibited unless Friedl explicitly approves it as "
            "an exception. A capability is prohibited when it interprets what "
            "Friedl wants",
        )
        self.assertTrue(
            any("prohibited-capability example" in item for item in self.violations()),
            "the superseded blueprint example must fail the check",
        )

    def test_every_law_in_the_canonical_text_is_enforced(self) -> None:
        laws = (REPOSITORY_ROOT / "LAWS_OF_ALX.md").read_text(encoding="utf-8")
        enforcement = (
            REPOSITORY_ROOT / "docs/LAW_ENFORCEMENT.md"
        ).read_text(encoding="utf-8")
        for number, title in re.findall(r"^## Law (\d+) — (.+)$", laws, re.MULTILINE):
            with self.subTest(law=number):
                self.assertIn(f"| {number} — {title.strip()} |", enforcement)


if __name__ == "__main__":
    unittest.main()
