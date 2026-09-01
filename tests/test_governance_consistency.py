"""The enforcement specification must not contradict the canonical laws.

The earlier law rewrite left docs/LAW_ENFORCEMENT.md defining gates for nineteen
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

    def checksum_greptile(self) -> None:
        """Re-sign the Greptile files so the checksum does not mask the test."""
        import hashlib

        lines = []
        for relative_path in (
            ".greptile/config.json",
            ".greptile/files.json",
            ".greptile/rules.md",
        ):
            digest = hashlib.sha256((self.root / relative_path).read_bytes()).hexdigest()
            lines.append(f"{digest}  {relative_path}")
        (self.root / "governance/GREPTILE.sha256").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

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

    def test_law_zero_cannot_lose_its_enforcement_row(self) -> None:
        text = (self.root / "docs/LAW_ENFORCEMENT.md").read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| 0 — "))
        self.rewrite("docs/LAW_ENFORCEMENT.md", row + "\n", "")
        self.assertTrue(
            any("no gate is defined for law(s) 0" in item for item in self.violations()),
            "one-path governance must not be reduced to canonical prose alone",
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

    def test_greptiles_mandate_cannot_name_a_law_count_that_is_wrong(self) -> None:
        """Review finding: Greptile was still told to review "all 19 Laws".

        It reviews against the laws its mandate names, so an invalid mandate
        would have produced an invalid constitutional review, and no gate
        noticed. The checksum alone does not help: it only proves the file was
        not changed, not that what it says is true.
        """
        self.rewrite(
            ".greptile/rules.md",
            "Review the whole change against every law in that file",
            "Review the whole change against all 19 Laws",
        )
        self.checksum_greptile()
        self.assertTrue(
            any("claims 19 laws exist" in item for item in self.violations()),
            "a mandate naming a law count that does not exist must fail",
        )

    def test_greptile_config_cannot_name_a_law_count_that_is_wrong(self) -> None:
        self.rewrite(
            ".greptile/config.json",
            "currently holds four laws",
            "currently holds all 19 Laws",
        )
        self.checksum_greptile()
        self.assertTrue(
            any("claims 19 laws exist" in item for item in self.violations()),
            "Greptile's structured configuration must reject an obsolete count",
        )

    def test_greptile_context_cannot_name_a_law_count_that_is_wrong(self) -> None:
        self.rewrite(
            ".greptile/files.json",
            "Sole canonical statement of the approved Laws of AL/X.",
            "Sole canonical statement of all 19 approved Laws of AL/X.",
        )
        self.checksum_greptile()
        self.assertTrue(
            any("claims 19 laws exist" in item for item in self.violations()),
            "Greptile's context descriptions must reject an obsolete count",
        )

    def test_greptile_cannot_soften_deletion_into_preference(self) -> None:
        self.rewrite(
            ".greptile/config.json",
            "Tests must prove the competing path is absent",
            "Tests may prefer the new path while retaining the old one",
        )
        self.checksum_greptile()
        self.assertTrue(
            any(
                "alx-one-production-path missing required marker" in item
                for item in self.violations()
            ),
            "Greptile must require deletion rather than preferred-path usage",
        )

    def test_entry_instructions_cannot_retain_replaced_code(self) -> None:
        self.rewrite(
            "AGENTS.md",
            "Git history is the archive for removed implementations.",
            "Deprecated code may remain as an implementation archive.",
        )
        self.assertTrue(
            any("AGENTS.md: missing required marker" in item for item in self.violations()),
            "implementers must be told to delete competing production code",
        )

    def test_pull_request_must_record_superseded_path_deletion(self) -> None:
        self.rewrite(
            ".github/pull_request_template.md",
            "Superseded production entry points searched and deleted",
            "Preferred implementation entry points",
        )
        self.assertTrue(
            any(
                ".github/pull_request_template.md: missing required marker" in item
                for item in self.violations()
            ),
            "review evidence must name the paths that were removed",
        )

    def test_a_live_document_naming_a_deleted_law_is_rejected(self) -> None:
        for relative_path, anchor, replacement in (
            (
                "TODO.md",
                "A candidate for AL/X's own sandbox capability invention",
                "A candidate for AL/X's own sandbox work under Law 19",
            ),
            (
                "docs/PERSISTENT_RESEARCH_NOTEBOOK_BRIEF.md",
                "but it remains a separately governed capability whose deployment needs",
                "but it remains a separately governed capability under Law 19 needs",
            ),
        ):
            with self.subTest(document=relative_path):
                self.setUp()
                self.rewrite(relative_path, anchor, replacement)
                self.assertTrue(
                    any("do not exist in" in item for item in self.violations()),
                    f"{relative_path} naming a deleted law must fail",
                )

    def test_naming_a_law_that_does_exist_still_passes(self) -> None:
        """The check must not simply forbid mentioning laws."""
        self.rewrite("TODO.md", "## Retention", "## Retention\n\nLaw 3 applies here.")
        self.assertEqual(self.violations(), [])

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
