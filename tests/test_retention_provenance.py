"""Retention by provenance: the replacement for the removed similarity guard.

Governance decision D-013. Mail-derived content expires thirty days after it
is written, whether or not a goal is still open. What survives is a reference,
not a copy.

This phase is non-destructive by authorisation: it stamps, classifies and
previews, and deletes nothing. Several tests below exist to prove that claim
rather than state it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.provenance import (  # noqa: E402
    ContentOrigin,
    ContentProvenance,
    ContentTombstone,
    ExpiryReason,
    RetentionPolicy,
)
from alx.safety.retention import (  # noqa: E402
    Classification,
    RecordSurvey,
    classify,
    preview_purge,
    tombstone_for,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
REFERENCE = {"mailbox_id": "INBOX", "uid_validity": "777", "uid": "2"}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _mail_record(record_id: str, written_days_ago: int) -> RecordSurvey:
    provenance = RetentionPolicy().stamp(
        ContentOrigin.MAIL_MESSAGE, NOW - timedelta(days=written_days_ago), REFERENCE
    )
    return RecordSurvey("goals.sqlite3", record_id, Classification.MAIL_DERIVED, provenance)


class RetentionPolicyTests(unittest.TestCase):
    def test_the_deadline_is_thirty_days(self) -> None:
        policy = RetentionPolicy()
        self.assertEqual(policy.expires_at(NOW), NOW + timedelta(days=30))

    def test_a_longer_lifetime_than_the_decision_is_refused(self) -> None:
        """A policy may be configured, but not beyond what D-013 authorises."""
        with self.assertRaises(ValueError) as caught:
            RetentionPolicy(timedelta(days=90))
        self.assertIn("D-013", str(caught.exception))

    def test_content_is_live_before_the_deadline_and_expired_after(self) -> None:
        provenance = RetentionPolicy().stamp(ContentOrigin.MAIL_MESSAGE, NOW, REFERENCE)
        self.assertFalse(provenance.is_expired(NOW + timedelta(days=29)))
        self.assertTrue(provenance.is_expired(NOW + timedelta(days=31)))

    def test_mail_content_must_carry_the_reference_it_is_reread_from(self) -> None:
        """Without a bookmark, expiry would lose the message rather than the copy."""
        with self.assertRaises(ValueError):
            ContentProvenance(
                origin=ContentOrigin.MAIL_MESSAGE,
                content_expires_at=NOW + timedelta(days=30),
                recorded_at=NOW,
            )

    def test_rereading_starts_a_new_clock_and_never_renews_the_old_record(self) -> None:
        """Otherwise touching a message would defeat the deadline."""
        policy = RetentionPolicy()
        first = policy.stamp(ContentOrigin.MAIL_MESSAGE, NOW, REFERENCE)
        later = NOW + timedelta(days=20)
        second = policy.stamp(ContentOrigin.MAIL_MESSAGE, later, REFERENCE)
        self.assertEqual(second.content_expires_at, later + timedelta(days=30))
        self.assertEqual(first.content_expires_at, NOW + timedelta(days=30))


class GoalOutlivesItsContentTests(unittest.TestCase):
    """D-013 chose Option B: the deadline is not suspended by an open goal."""

    def test_content_expires_while_its_goal_is_still_open(self) -> None:
        record = _mail_record("goal-1", written_days_ago=31)
        self.assertTrue(record.would_expire_at(NOW))

    def test_content_that_is_not_mail_derived_is_not_governed(self) -> None:
        """Friedl's own words are not on this clock."""
        provenance = ContentProvenance(
            origin=ContentOrigin.PERSON,
            content_expires_at=NOW - timedelta(days=1),
            recorded_at=NOW - timedelta(days=400),
        )
        record = RecordSurvey(
            "goals.sqlite3", "goal-2", classify(provenance), provenance
        )
        self.assertEqual(record.classification, Classification.NOT_MAIL_DERIVED)
        self.assertFalse(record.would_expire_at(NOW))


class TombstoneTests(unittest.TestCase):
    def test_a_tombstone_carries_a_reference_and_no_content(self) -> None:
        """D-013: no subject, no summary, no extracted fact."""
        stone = tombstone_for(_mail_record("goal-1", 31), NOW)
        rendered = repr(stone)
        for content in ("subject", "summary", "body", "price", "quote"):
            self.assertNotIn(content, rendered.lower())
        self.assertEqual(stone.source_reference["uid"], "2")

    def test_a_tombstone_is_not_evidence(self) -> None:
        """It records that support was lost, and cannot supply support."""
        stone = tombstone_for(_mail_record("goal-1", 31), NOW)
        self.assertFalse(stone.is_evidence())
        self.assertFalse(hasattr(stone, "supports"))

    def test_a_tombstone_records_why_the_content_went(self) -> None:
        stone = tombstone_for(_mail_record("goal-1", 31), NOW)
        self.assertEqual(stone.reason, ExpiryReason.RETENTION_ELAPSED)

    def test_a_record_without_provenance_has_no_tombstone(self) -> None:
        record = RecordSurvey("goals.sqlite3", "old", Classification.UNCLASSIFIED)
        with self.assertRaises(ValueError):
            tombstone_for(record, NOW)


class PurgePreviewTests(unittest.TestCase):
    def test_the_preview_separates_expired_from_live(self) -> None:
        preview = preview_purge(
            (_mail_record("old", 31), _mail_record("recent", 5)), NOW
        )
        self.assertEqual([item.record_id for item in preview.would_expire], ["old"])

    def test_the_preview_leaves_one_tombstone_per_expired_record(self) -> None:
        preview = preview_purge((_mail_record("old", 31),), NOW)
        self.assertEqual(len(preview.tombstones), 1)
        self.assertIsInstance(preview.tombstones[0], ContentTombstone)

    def test_unclassified_records_are_reported_and_never_expired(self) -> None:
        """Records predating provenance must not be purged on a guess."""
        record = RecordSurvey("goals.sqlite3", "legacy", Classification.UNCLASSIFIED)
        preview = preview_purge((record,), NOW)
        self.assertEqual(len(preview.unclassified), 1)
        self.assertEqual(preview.would_expire, ())

    def test_the_preview_is_not_destructive(self) -> None:
        self.assertFalse(preview_purge((_mail_record("old", 31),), NOW).is_destructive())

    def test_the_rendered_summary_carries_no_content(self) -> None:
        rendered = preview_purge((_mail_record("old", 31),), NOW).render()
        self.assertNotIn("body", rendered.lower())
        self.assertIn("mail-derived expired: 1", rendered)


class InventoryIsReadOnlyTests(unittest.TestCase):
    """The inventory must be provably non-destructive, not merely intended so."""

    def test_running_the_inventory_leaves_every_database_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".alx/runtime"
            runtime.mkdir(parents=True)
            for name in ("goals.sqlite3", "conversations.sqlite3", "memories.sqlite3"):
                source = REPOSITORY_ROOT / ".alx/runtime" / name
                if source.exists():
                    (runtime / name).write_bytes(source.read_bytes())
            if not any(runtime.iterdir()):
                self.skipTest("no runtime stores to inventory")

            before = {p.name: p.read_bytes() for p in runtime.iterdir()}
            result = subprocess.run(
                [sys.executable, "scripts/inventory_retention.py", "--root", str(root)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            after = {p.name: p.read_bytes() for p in runtime.iterdir()}

        self.assertEqual(before, after, "the inventory modified a database")
        self.assertIn("Nothing was modified", result.stdout)

    def test_the_inventory_issues_no_sql_that_writes(self) -> None:
        """Read-only mode enforces this; the intent is checked too.

        Checked against the SQL string literals rather than the whole file, so
        prose in a docstring is not mistaken for a statement.
        """
        import ast

        source = (REPOSITORY_ROOT / "scripts/inventory_retention.py").read_text()
        writes = ("DELETE", "DROP", "UPDATE", "INSERT", "VACUUM", "ALTER", "REPLACE")
        offenders = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value.upper()
                if "SELECT" not in text and "PRAGMA" not in text:
                    continue
                offenders.extend(word for word in writes if word in text)
            elif isinstance(node, ast.JoinedStr):
                rendered = "".join(
                    part.value.upper()
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
                offenders.extend(word for word in writes if word in rendered)
        self.assertEqual(offenders, [])

    def test_the_inventory_reports_unclassified_records_honestly(self) -> None:
        """Every record predating provenance must be reported, not assumed safe."""
        result = subprocess.run(
            [sys.executable, "scripts/inventory_retention.py"],
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("unclassified", result.stdout)
        self.assertIn("Purging requires a separate authorisation", result.stdout)


if __name__ == "__main__":
    unittest.main()
