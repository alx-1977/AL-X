"""Report what is in AL/X's durable stores and what retention would remove.

Governance decision D-013. Read-only by construction: every database is opened
in SQLite read-only mode, and this script has no code path that writes, deletes
or vacuums. Run it before authorising a purge, to see what the first purge
would do.

It reports counts, identifiers, ages and classifications. It never prints
record content: an inventory that quoted what it measured would be one more
copy of the thing being retained.

Usage:
    python3 scripts/inventory_retention.py [--root .] [--at ISO8601]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.provenance import (  # noqa: E402
    ContentOrigin,
    ContentProvenance,
    RetentionPolicy,
)
from alx.safety.retention import (  # noqa: E402
    Classification,
    classify,
    RecordSurvey,
    preview_purge,
)

# store file -> (table, identifier column, retention column)
SURVEYED_TABLES = {
    "goals.sqlite3": ("goals", "goal_id", "retention_until"),
    "conversations.sqlite3": ("conversations", "conversation_id", "retention_until"),
    "memories.sqlite3": ("memories", "memory_id", "retention_until"),
    "mail-observations.sqlite3": ("mail_observations", "uid", None),
}


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _surveyed(store: str, row: tuple, stamped: bool) -> RecordSurvey:
    """Build one survey row, reading provenance only where it exists."""
    identifier = str(row[0])
    if not stamped:
        return RecordSurvey(store, identifier, Classification.UNCLASSIFIED)
    origin, expires_at = row[1], row[2]
    if origin is None or expires_at is None:
        return RecordSurvey(store, identifier, Classification.UNCLASSIFIED)
    provenance = ContentProvenance(
        origin=ContentOrigin(origin),
        content_expires_at=datetime.fromisoformat(expires_at),
        recorded_at=datetime.fromisoformat(expires_at) - RetentionPolicy().mail_content_lifetime,
        source_reference={"store": store, "record_id": identifier},
    )
    return RecordSurvey(store, identifier, classify(provenance), provenance)


def survey(runtime: Path) -> list[RecordSurvey]:
    """Read every store without writing to any of them."""
    records: list[RecordSurvey] = []
    for filename, (table, identifier, _retention) in SURVEYED_TABLES.items():
        path = runtime / filename
        if not path.exists():
            continue
        connection = _read_only(path)
        try:
            names = {
                row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            if identifier not in names:
                continue
            # No store carries provenance columns yet, so every existing
            # record is unclassified. That is the honest answer, not a
            # placeholder: nothing can tell by inspection whether a sentence
            # AL/X wrote came from a mail body or from Friedl's own words.
            # When provenance is stamped, this reads it instead of assuming.
            stamped = "content_expires_at" in names and "content_origin" in names
            columns = f'"{identifier}"'
            if stamped:
                columns += ', "content_origin", "content_expires_at"'
            for row in connection.execute(
                f'SELECT {columns} FROM "{table}" ORDER BY "{identifier}"'
            ):
                records.append(_surveyed(filename, row, stamped))
        finally:
            connection.close()
    return records


def retention_horizon(runtime: Path) -> list[tuple[str, int, int]]:
    """How far in the future each store's existing deadlines sit, in days."""
    horizons: list[tuple[str, int, int]] = []
    now = datetime.now(UTC)
    for filename, (table, _identifier, retention) in SURVEYED_TABLES.items():
        if retention is None:
            continue
        path = runtime / filename
        if not path.exists():
            continue
        connection = _read_only(path)
        try:
            days = []
            for (value,) in connection.execute(f'SELECT "{retention}" FROM "{table}"'):
                try:
                    days.append((datetime.fromisoformat(value) - now).days)
                except (TypeError, ValueError):
                    continue
            if days:
                horizons.append((filename, min(days), max(days)))
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

    records = survey(runtime)
    preview = preview_purge(tuple(records), at, RetentionPolicy())

    print(preview.render())
    print()
    print("Existing deadlines, days from now:")
    for filename, low, high in retention_horizon(runtime):
        print(f"  {filename:28} min={low:6} max={high:6}")
    print()
    print("Records per store:")
    counts: dict[str, int] = {}
    for record in records:
        counts[record.store] = counts.get(record.store, 0) + 1
    for store, count in sorted(counts.items()):
        print(f"  {store:28} {count:6}")
    print()
    print(
        "Nothing was modified. Every database was opened read-only.\n"
        "Purging requires a separate authorisation from Friedl (D-013)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
