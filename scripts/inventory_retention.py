#!/usr/bin/env python3
"""Preview D-013 retention across every durable content-bearing SQLite row.

The inventory opens live stores and backups in SQLite read-only mode. It reads
identities and provenance metadata only, never the content columns themselves.
Legacy rows predate provenance and are reported as unclassified; they are never
silently assigned an origin or selected for purge.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.mail import MailReference  # noqa: E402
from alx.contracts.provenance import (  # noqa: E402
    ContentOrigin,
    ContentProvenance,
)
from alx.safety.retention import RecordSurvey, preview_purge  # noqa: E402


PROVENANCE_COLUMNS = (
    "content_origins",
    "content_recorded_at",
    "content_expires_at",
    "mail_references",
)


@dataclass(frozen=True, slots=True)
class TableSpec:
    """One logical content record stored in one SQLite row."""

    kind: str
    table: str
    identity_columns: tuple[str, ...]
    content_column: str

    @property
    def required_columns(self) -> frozenset[str]:
        return frozenset((*self.identity_columns, self.content_column))


CONTENT_TABLES = (
    TableSpec(
        "conversation_turn",
        "conversation_turns",
        ("conversation_id", "turn_id"),
        "turn_json",
    ),
    TableSpec("goal_state", "goals", ("goal_id",), "state_json"),
    TableSpec(
        "legacy_goal_turn",
        "conversation_turns",
        ("goal_id", "turn_id"),
        "turn_json",
    ),
    TableSpec(
        "pending_memory",
        "pending_memory_batches",
        ("goal_id", "goal_revision", "ordinal"),
        "proposal_json",
    ),
    TableSpec(
        "memory_revision",
        "memory_revisions",
        ("memory_id", "revision"),
        "revision_json",
    ),
    TableSpec(
        "mail_observation",
        "mail_observations",
        ("mailbox_id", "uid_validity", "uid"),
        "event_json",
    ),
)

RETENTION_CONTAINERS = (
    ("goals", "retention_until"),
    ("conversations", "retention_until"),
    ("memories", "retention_until"),
)


class InventorySchemaError(RuntimeError):
    """A partially migrated schema cannot be classified safely."""


def _read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _database_paths(runtime: Path) -> tuple[Path, ...]:
    primary = sorted(runtime.glob("*.sqlite3"))
    backups = sorted((runtime / "backup").glob("*.bak"))
    return tuple((*primary, *backups))


def _store_name(runtime: Path, path: Path, kind: str) -> str:
    return f"{path.relative_to(runtime)}:{kind}"


def _columns(connection: sqlite3.Connection, table: str) -> frozenset[str]:
    return frozenset(
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    )


def _assert_content_schema_covered(
    connection: sqlite3.Connection, path: Path
) -> None:
    """Fail closed when a new JSON content surface is not inventoried."""
    covered = {(spec.table, spec.content_column) for spec in CONTENT_TABLES}
    tables = (
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    missing = sorted(
        (table, column)
        for table in tables
        for column in _columns(connection, table)
        if column.endswith("_json") and (table, column) not in covered
    )
    if missing:
        fields = ", ".join(f"{table}.{column}" for table, column in missing)
        raise InventorySchemaError(
            f"{path} has content columns absent from the inventory: {fields}"
        )


def _provenance_state(columns: frozenset[str], store: str) -> bool:
    present = frozenset(PROVENANCE_COLUMNS).intersection(columns)
    if present and present != frozenset(PROVENANCE_COLUMNS):
        missing = sorted(frozenset(PROVENANCE_COLUMNS) - present)
        raise InventorySchemaError(
            f"{store} has partial provenance metadata; missing {', '.join(missing)}"
        )
    return bool(present)


def _mail_reference(value: object) -> MailReference:
    if not isinstance(value, dict) or set(value) != {
        "mailbox_id",
        "uid_validity",
        "uid",
    }:
        raise InventorySchemaError("mail provenance contains an invalid reference")
    if any(not isinstance(value[field], str) for field in value):
        raise InventorySchemaError("mail provenance reference fields must be strings")
    try:
        return MailReference(
            mailbox_id=value["mailbox_id"],
            uid_validity=value["uid_validity"],
            uid=value["uid"],
        )
    except (TypeError, ValueError) as error:
        raise InventorySchemaError("mail provenance contains an invalid reference") from error


def _decode_provenance(row: tuple[object, ...], offset: int) -> ContentProvenance:
    origins_value, recorded_value, expires_value, references_value = row[offset:]
    if origins_value is None or recorded_value is None or references_value is None:
        raise InventorySchemaError("stamped provenance contains null required metadata")
    try:
        raw_origins = json.loads(str(origins_value))
        raw_references = json.loads(str(references_value))
        if not isinstance(raw_origins, list) or not isinstance(raw_references, list):
            raise TypeError
        origins = frozenset(ContentOrigin(str(item)) for item in raw_origins)
        references = tuple(_mail_reference(item) for item in raw_references)
        return ContentProvenance(
            origins=origins,
            recorded_at=datetime.fromisoformat(str(recorded_value)),
            mail_references=references,
            content_expires_at=(
                None
                if expires_value is None
                else datetime.fromisoformat(str(expires_value))
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InventorySchemaError("stamped provenance is malformed") from error


def _record_id(kind: str, values: tuple[object, ...]) -> str:
    encoded = "/".join(str(value) for value in values)
    return f"{kind}:{encoded}"


def _survey_database(runtime: Path, path: Path) -> list[RecordSurvey]:
    records: list[RecordSurvey] = []
    connection = _read_only(path)
    try:
        _assert_content_schema_covered(connection, path)
        for spec in CONTENT_TABLES:
            columns = _columns(connection, spec.table)
            if not spec.required_columns.issubset(columns):
                continue
            store = _store_name(runtime, path, spec.kind)
            stamped = _provenance_state(columns, store)
            selected = [f'"{item}"' for item in spec.identity_columns]
            if stamped:
                selected.extend(f'"{item}"' for item in PROVENANCE_COLUMNS)
            order = ", ".join(f'"{item}"' for item in spec.identity_columns)
            query = (
                f'SELECT {", ".join(selected)} FROM "{spec.table}" '
                f"ORDER BY {order}"
            )
            for row in connection.execute(query):
                identity_count = len(spec.identity_columns)
                provenance = (
                    _decode_provenance(row, identity_count) if stamped else None
                )
                records.append(
                    RecordSurvey(
                        store,
                        _record_id(spec.kind, row[:identity_count]),
                        provenance,
                    )
                )
    except sqlite3.DatabaseError as error:
        raise InventorySchemaError(f"cannot inventory {path}") from error
    finally:
        connection.close()
    return records


def survey(runtime: Path) -> list[RecordSurvey]:
    """Read every logical content row in live stores and backups."""
    records: list[RecordSurvey] = []
    for path in _database_paths(runtime):
        records.extend(_survey_database(runtime, path))
    return records


def retention_horizon(
    runtime: Path, as_of: datetime
) -> list[tuple[str, int, int]]:
    """Report existing container deadlines without treating them as content."""
    horizons: list[tuple[str, int, int]] = []
    for path in _database_paths(runtime):
        connection = _read_only(path)
        try:
            for table, column in RETENTION_CONTAINERS:
                columns = _columns(connection, table)
                if column not in columns:
                    continue
                days: list[int] = []
                for (value,) in connection.execute(
                    f'SELECT "{column}" FROM "{table}"'
                ):
                    try:
                        days.append((datetime.fromisoformat(str(value)) - as_of).days)
                    except (TypeError, ValueError):
                        continue
                if days:
                    label = f"{path.relative_to(runtime)}:{table}"
                    horizons.append((label, min(days), max(days)))
        finally:
            connection.close()
    return horizons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--at",
        type=datetime.fromisoformat,
        default=None,
        help="evaluate the preview at this instant instead of now",
    )
    arguments = parser.parse_args(argv)
    runtime = arguments.root / ".alx/runtime"
    if not runtime.exists():
        print(f"No runtime stores found at {runtime}")
        return 0

    at = arguments.at or datetime.now(UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)

    try:
        records = survey(runtime)
    except InventorySchemaError as error:
        # Fail closed, but legibly: this is a script Friedl runs, and a raw
        # traceback would obscure the one thing that matters, which is that
        # the inventory refused to report on a store it does not fully cover.
        print(f"Inventory refused to run: {error}")
        print(
            "\nNo store was modified. The inventory stops rather than report a "
            "partial count, because an uncounted content column would look "
            "exactly like an empty one."
        )
        return 1

    preview = preview_purge(tuple(records), at)
    print(preview.render())
    print("\nExisting container deadlines, days from evaluation time:")
    for label, low, high in retention_horizon(runtime, at):
        print(f"  {label:52} min={low:6} max={high:6}")
    print("\nLogical content records per store:")
    counts: dict[str, int] = {}
    for record in records:
        counts[record.store] = counts.get(record.store, 0) + 1
    for store, count in sorted(counts.items()):
        print(f"  {store:52} {count:6}")
    print(
        "\nNothing was modified. Every database and backup was opened read-only.\n"
        "Purging requires a separate authorisation from Friedl (D-013)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
