"""D-013 provenance, tombstone, and non-destructive inventory evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.mail import MailReference  # noqa: E402
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
    preview_purge,
    tombstone_for,
)
from scripts.inventory_retention import (  # noqa: E402
    InventorySchemaError,
    _read_only,
    retention_horizon,
    survey,
)


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
REFERENCE = MailReference("INBOX", "777", "2")
SECOND_REFERENCE = MailReference("INBOX", "777", "9")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SECRET = "ARTIFICIAL-MAIL-CONTENT-6ec4"


def _mail_record(record_id: str, written_days_ago: int) -> RecordSurvey:
    provenance = RetentionPolicy().direct_mail(
        NOW - timedelta(days=written_days_ago), (REFERENCE,)
    )
    return RecordSurvey("goals.sqlite3:goal_state", record_id, provenance)


class RetentionPolicyTests(unittest.TestCase):
    def test_the_deadline_is_exactly_thirty_days(self) -> None:
        policy = RetentionPolicy()
        self.assertEqual(policy.expires_at(NOW), NOW + timedelta(days=30))
        for lifetime in (timedelta(seconds=1), timedelta(days=29), timedelta(days=31)):
            with self.subTest(lifetime=lifetime), self.assertRaises(ValueError):
                RetentionPolicy(lifetime)

    def test_direct_mail_requires_typed_content_free_references(self) -> None:
        with self.assertRaises(TypeError):
            RetentionPolicy().direct_mail(NOW, ({"body": SECRET},))  # type: ignore[arg-type]

    def test_non_mail_content_cannot_claim_a_mail_reference(self) -> None:
        with self.assertRaises(ValueError):
            ContentProvenance(
                origins=frozenset({ContentOrigin.ALX}),
                recorded_at=NOW,
                mail_references=(REFERENCE,),
            )

    def test_transitive_derivation_unions_every_origin_and_mail_reference(self) -> None:
        policy = RetentionPolicy()
        first = policy.direct_mail(NOW, (REFERENCE,))
        second = policy.direct_mail(NOW, (SECOND_REFERENCE, REFERENCE))
        person = policy.non_mail(ContentOrigin.PERSON, NOW)
        derived = policy.derive(
            ContentOrigin.ALX,
            NOW + timedelta(hours=1),
            (first, person, second),
        )
        self.assertEqual(
            derived.origins,
            frozenset(
                {ContentOrigin.MAIL_MESSAGE, ContentOrigin.PERSON, ContentOrigin.ALX}
            ),
        )
        self.assertEqual(derived.mail_references, (REFERENCE, SECOND_REFERENCE))
        self.assertTrue(derived.governed_by_retention())
        # The inherited deadline, not a fresh thirty days from the derivation.
        self.assertEqual(derived.content_expires_at, NOW + timedelta(days=30))

    def test_alx_authorship_does_not_erase_mail_derivation(self) -> None:
        source = RetentionPolicy().direct_mail(NOW, (REFERENCE,))
        derived = RetentionPolicy().derive(
            ContentOrigin.ALX, NOW + timedelta(minutes=1), (source,)
        )
        record = RecordSurvey("goals.sqlite3:goal_state", "goal-1", derived)
        self.assertEqual(record.classification, Classification.MAIL_DERIVED)

    def test_rereading_starts_a_new_clock_without_mutating_the_old_record(self) -> None:
        policy = RetentionPolicy()
        first = policy.direct_mail(NOW, (REFERENCE,))
        later = NOW + timedelta(days=20)
        second = policy.direct_mail(later, (REFERENCE,))
        self.assertEqual(first.content_expires_at, NOW + timedelta(days=30))
        self.assertEqual(second.content_expires_at, later + timedelta(days=30))


class SurveyAndTombstoneTests(unittest.TestCase):
    def test_classification_is_derived_and_cannot_be_supplied_separately(self) -> None:
        mail = RetentionPolicy().direct_mail(NOW, (REFERENCE,))
        self.assertEqual(
            RecordSurvey("store", "record", mail).classification,
            Classification.MAIL_DERIVED,
        )
        with self.assertRaises(TypeError):
            RecordSurvey(  # type: ignore[call-arg]
                "store", "record", Classification.NOT_MAIL_DERIVED, mail
            )

    def test_a_tombstone_has_no_arbitrary_content_field(self) -> None:
        stone = tombstone_for(_mail_record("goal-1", 31), NOW)
        self.assertEqual(
            {item.name for item in fields(stone)},
            {
                "record_id",
                "origins",
                "recorded_at",
                "expired_at",
                "reason",
                "mail_references",
            },
        )
        self.assertEqual(stone.mail_references, (REFERENCE,))
        self.assertNotIn(SECRET, repr(stone))

    def test_a_tombstone_rejects_untyped_or_content_bearing_references(self) -> None:
        with self.assertRaises(TypeError):
            ContentTombstone(
                "record",
                frozenset({ContentOrigin.MAIL_MESSAGE}),
                NOW,
                NOW + timedelta(days=31),
                ExpiryReason.RETENTION_ELAPSED,
                ({"body": SECRET},),  # type: ignore[arg-type]
            )

    def test_live_or_non_mail_content_cannot_be_tombstoned(self) -> None:
        with self.assertRaises(ValueError):
            tombstone_for(_mail_record("recent", 1), NOW)
        person = RetentionPolicy().non_mail(ContentOrigin.PERSON, NOW)
        with self.assertRaises(ValueError):
            tombstone_for(RecordSurvey("store", "person", person), NOW)

    def test_a_tombstone_is_not_evidence(self) -> None:
        stone = tombstone_for(_mail_record("goal-1", 31), NOW)
        self.assertFalse(stone.is_evidence())
        self.assertFalse(hasattr(stone, "supports"))

    def test_preview_never_expires_unclassified_records(self) -> None:
        legacy = RecordSurvey("store", "legacy")
        preview = preview_purge((legacy, _mail_record("old", 31)), NOW)
        self.assertEqual(preview.unclassified, (legacy,))
        self.assertEqual([item.record_id for item in preview.would_expire], ["old"])
        self.assertFalse(preview.is_destructive())


def _execute(path: Path, statements: tuple[tuple[str, tuple[object, ...]], ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        with connection:
            for statement, values in statements:
                connection.execute(statement, values)
    finally:
        connection.close()


def _legacy_runtime(root: Path) -> Path:
    runtime = root / ".alx/runtime"
    backup = runtime / "backup"
    backup.mkdir(parents=True)
    _execute(
        runtime / "conversations.sqlite3",
        (
            (
                "CREATE TABLE conversations (conversation_id TEXT, retention_until TEXT)",
                (),
            ),
            (
                "CREATE TABLE conversation_turns (conversation_id TEXT, turn_id TEXT, turn_json TEXT)",
                (),
            ),
            ("INSERT INTO conversations VALUES (?, ?)", ("c1", "2036-01-01T00:00:00+00:00")),
            ("INSERT INTO conversation_turns VALUES (?, ?, ?)", ("c1", "t1", SECRET)),
            ("INSERT INTO conversation_turns VALUES (?, ?, ?)", ("c1", "t2", SECRET)),
        ),
    )
    goal_statements = (
        ("CREATE TABLE goals (goal_id TEXT, state_json TEXT, retention_until TEXT)", ()),
        (
            "CREATE TABLE conversation_turns (goal_id TEXT, turn_id TEXT, turn_json TEXT)",
            (),
        ),
        (
            "CREATE TABLE pending_memory_batches (goal_id TEXT, goal_revision INTEGER, ordinal INTEGER, proposal_json TEXT)",
            (),
        ),
        ("INSERT INTO goals VALUES (?, ?, ?)", ("g1", SECRET, "2036-01-01T00:00:00+00:00")),
        ("INSERT INTO conversation_turns VALUES (?, ?, ?)", ("g1", "t1", SECRET)),
        ("INSERT INTO pending_memory_batches VALUES (?, ?, ?, ?)", ("g1", 1, 0, SECRET)),
    )
    _execute(runtime / "goals.sqlite3", goal_statements)
    _execute(backup / "goals.legacy.bak", goal_statements)
    _execute(
        runtime / "memories.sqlite3",
        (
            ("CREATE TABLE memories (memory_id TEXT, retention_until TEXT)", ()),
            (
                "CREATE TABLE memory_revisions (memory_id TEXT, revision INTEGER, revision_json TEXT)",
                (),
            ),
            ("INSERT INTO memories VALUES (?, ?)", ("m1", "2036-01-01T00:00:00+00:00")),
            ("INSERT INTO memory_revisions VALUES (?, ?, ?)", ("m1", 1, SECRET)),
        ),
    )
    _execute(
        runtime / "mail-observations.sqlite3",
        (
            (
                "CREATE TABLE mail_observations (mailbox_id TEXT, uid_validity TEXT, uid INTEGER, event_json TEXT)",
                (),
            ),
            ("INSERT INTO mail_observations VALUES (?, ?, ?, ?)", ("INBOX", "777", 2, SECRET)),
        ),
    )
    return runtime


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InventoryTests(unittest.TestCase):
    def test_every_content_bearing_table_and_backup_is_surveyed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _legacy_runtime(Path(directory))
            records = survey(runtime)
        counts: dict[str, int] = {}
        for record in records:
            kind = record.store.rsplit(":", 1)[1]
            counts[kind] = counts.get(kind, 0) + 1
        self.assertEqual(
            counts,
            {
                "conversation_turn": 2,
                "goal_state": 2,
                "legacy_goal_turn": 2,
                "pending_memory": 2,
                "memory_revision": 1,
                "mail_observation": 1,
            },
        )
        self.assertEqual(len(records), 10)
        self.assertTrue(all(item.classification is Classification.UNCLASSIFIED for item in records))

    def test_inventory_reads_real_stamped_metadata_without_fabricating_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / ".alx/runtime"
            runtime.mkdir(parents=True)
            path = runtime / "goals.sqlite3"
            recorded = NOW.isoformat()
            expires = (NOW + timedelta(days=30)).isoformat()
            references = json.dumps(
                [
                    {
                        "mailbox_id": REFERENCE.mailbox_id,
                        "uid_validity": REFERENCE.uid_validity,
                        "uid": REFERENCE.uid,
                    }
                ]
            )
            _execute(
                path,
                (
                    (
                        "CREATE TABLE goals (goal_id TEXT, state_json TEXT, content_origins TEXT, content_recorded_at TEXT, content_expires_at TEXT, mail_references TEXT)",
                        (),
                    ),
                    (
                        "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            "g1",
                            SECRET,
                            json.dumps(["mail_message", "alx"]),
                            recorded,
                            expires,
                            references,
                        ),
                    ),
                ),
            )
            record = survey(runtime)[0]
        assert record.provenance is not None
        self.assertEqual(record.provenance.recorded_at, NOW)
        self.assertEqual(record.provenance.mail_references, (REFERENCE,))
        self.assertEqual(record.classification, Classification.MAIL_DERIVED)

    def test_inventory_rejects_coerced_mail_reference_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / ".alx/runtime"
            runtime.mkdir(parents=True)
            _execute(
                runtime / "goals.sqlite3",
                (
                    (
                        "CREATE TABLE goals (goal_id TEXT, state_json TEXT, content_origins TEXT, content_recorded_at TEXT, content_expires_at TEXT, mail_references TEXT)",
                        (),
                    ),
                    (
                        "INSERT INTO goals VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            "g1",
                            SECRET,
                            json.dumps(["mail_message"]),
                            NOW.isoformat(),
                            (NOW + timedelta(days=30)).isoformat(),
                            json.dumps(
                                [
                                    {
                                        "mailbox_id": "INBOX",
                                        "uid_validity": "777",
                                        "uid": 2,
                                    }
                                ]
                            ),
                        ),
                    ),
                ),
            )
            with self.assertRaises(InventorySchemaError):
                survey(runtime)

    def test_partial_provenance_schema_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / ".alx/runtime"
            runtime.mkdir(parents=True)
            _execute(
                runtime / "goals.sqlite3",
                (
                    (
                        "CREATE TABLE goals (goal_id TEXT, state_json TEXT, content_origins TEXT)",
                        (),
                    ),
                ),
            )
            with self.assertRaises(InventorySchemaError):
                survey(runtime)

    def test_an_unregistered_json_content_surface_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / ".alx/runtime"
            runtime.mkdir(parents=True)
            _execute(
                runtime / "future.sqlite3",
                (
                    (
                        "CREATE TABLE future_records (record_id TEXT, payload_json TEXT)",
                        (),
                    ),
                ),
            )
            with self.assertRaises(InventorySchemaError):
                survey(runtime)

    def test_read_only_connection_rejects_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _legacy_runtime(Path(directory))
            connection = _read_only(runtime / "goals.sqlite3")
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM goals")
            finally:
                connection.close()

    def test_subprocess_leaves_all_live_and_backup_files_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _legacy_runtime(root)
            paths = sorted(path for path in runtime.rglob("*") if path.is_file())
            before = {path.relative_to(runtime): _digest(path) for path in paths}
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/inventory_retention.py",
                    "--root",
                    str(root),
                    "--at",
                    NOW.isoformat(),
                ],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
                check=True,
            )
            after = {path.relative_to(runtime): _digest(path) for path in paths}
        self.assertEqual(before, after)
        self.assertIn("records surveyed:     10", result.stdout)
        self.assertNotIn(SECRET, result.stdout)
        self.assertIn("Every database and backup was opened read-only", result.stdout)

    def test_horizon_uses_the_requested_evaluation_time_and_includes_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = _legacy_runtime(Path(directory))
            horizons = retention_horizon(runtime, NOW)
        labels = {label for label, _low, _high in horizons}
        self.assertIn("goals.sqlite3:goals", labels)
        self.assertIn("backup/goals.legacy.bak:goals", labels)


class DerivationCannotExtendAMessagesLifeTests(unittest.TestCase):
    """D-013: re-reading must not renew an existing record's deadline.

    Provenance is transitive, so a summary of a mail-derived record is itself
    mail-derived. If each summary took a fresh thirty days, AL/X revisiting a
    thread every few weeks would keep one message's content alive forever,
    with every record honestly stamped and the policy quietly defeated.
    """

    def test_a_summary_inherits_the_deadline_it_was_derived_from(self) -> None:
        policy = RetentionPolicy()
        source = policy.direct_mail(NOW, (REFERENCE,))
        summary = policy.derive(
            ContentOrigin.ALX, NOW + timedelta(days=29), (source,)
        )
        self.assertEqual(summary.content_expires_at, source.content_expires_at)

    def test_repeated_derivation_never_extends_the_original_deadline(self) -> None:
        policy = RetentionPolicy()
        record = policy.direct_mail(NOW, (REFERENCE,))
        deadline = record.content_expires_at
        for step in range(1, 19):
            record = policy.derive(
                ContentOrigin.ALX, NOW + timedelta(days=20 * step), (record,)
            )
        self.assertEqual(record.content_expires_at, deadline)
        self.assertTrue(record.governed_by_retention())

    def test_the_earliest_deadline_wins_across_several_sources(self) -> None:
        """Merging an older message with a newer one cannot revive the older."""
        policy = RetentionPolicy()
        older = policy.direct_mail(NOW, (REFERENCE,))
        newer = policy.direct_mail(NOW + timedelta(days=10), (SECOND_REFERENCE,))
        merged = policy.derive(
            ContentOrigin.ALX, NOW + timedelta(days=10), (older, newer)
        )
        self.assertEqual(merged.content_expires_at, older.content_expires_at)

    def test_a_fresh_read_still_starts_its_own_thirty_days(self) -> None:
        """Re-reading the message itself creates a new record, which is allowed."""
        policy = RetentionPolicy()
        first = policy.direct_mail(NOW, (REFERENCE,))
        later = policy.direct_mail(NOW + timedelta(days=20), (REFERENCE,))
        self.assertEqual(later.content_expires_at, NOW + timedelta(days=50))
        self.assertEqual(first.content_expires_at, NOW + timedelta(days=30))


class FailingClosedIsLegibleTests(unittest.TestCase):
    """Refusing to run must say so plainly, not crash.

    This is a script Friedl runs. A raw traceback would bury the one fact that
    matters: the inventory stopped rather than report a partial count, because
    an uncounted content column looks exactly like an empty one.
    """

    def test_an_uninventoried_content_column_exits_with_a_clear_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".alx/runtime"
            runtime.mkdir(parents=True)
            connection = sqlite3.connect(runtime / "goals.sqlite3")
            try:
                connection.execute(
                    "CREATE TABLE future_notes (id TEXT PRIMARY KEY, note_json TEXT)"
                )
                connection.commit()
            finally:
                connection.close()

            result = subprocess.run(
                [sys.executable, "scripts/inventory_retention.py", "--root", str(root)],
                cwd=REPOSITORY_ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("Inventory refused to run", result.stdout)
        self.assertIn("future_notes.note_json", result.stdout)
        self.assertIn("No store was modified", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
