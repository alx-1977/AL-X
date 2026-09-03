"""A durable record of every cognition opportunity and what it cost.

This is the evidence that answers the question nobody can currently answer:
how often does AL/X actually want to think, and what does it cost when she
does. It exists so behavioural limits, if any are ever warranted, follow
evidence rather than guesswork.

It records identities, outcomes and money. It never records what she thought,
what she concluded, or the note she wrote to herself: an audit trail that
quietly became a transcript of her private reflection would be a different
thing than an audit trail.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class SQLiteOpportunityLedger:
    """One durable row per cognition opportunity."""

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(
            str(database_path), check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def _migrate(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cognition_opportunities (
                    opportunity_id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    arose_at TEXT NOT NULL,
                    refs TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'created',
                    reasoning_calls INTEGER NOT NULL DEFAULT 0,
                    reserved_usd REAL,
                    settled_usd REAL,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    recorded_at TEXT NOT NULL,
                    -- RESPONDED with no live transport to deliver it. A fact
                    -- about the occasion, not a stored message: the prose is
                    -- deliberately not kept, so nothing can replay it.
                    response_undelivered INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            columns = {
                row[1]
                for row in self._connection.execute(
                    "PRAGMA table_info(cognition_opportunities)"
                ).fetchall()
            }
            if "response_undelivered" not in columns:
                self._connection.execute(
                    "ALTER TABLE cognition_opportunities "
                    "ADD COLUMN response_undelivered INTEGER NOT NULL DEFAULT 0"
                )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS commissioning (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    dispatches_attempted INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def record_created(self, opportunity: Any) -> bool:
        """Record that an opportunity exists. False if it already did.

        Idempotent by identity, which is what stops a restart replaying a
        matured request into a second paid turn.
        """
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO cognition_opportunities(opportunity_id, origin, "
                    "arose_at, refs, outcome, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        opportunity.opportunity_id,
                        opportunity.origin.value,
                        opportunity.arose_at.isoformat(),
                        "\x1f".join(opportunity.references),
                        "created",
                        datetime.now(UTC).isoformat(),
                    ),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def exists(self, opportunity_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM cognition_opportunities WHERE opportunity_id = ?",
            (opportunity_id,),
        ).fetchone()
        return row is not None

    def record_reserved(
        self, opportunity_id: str, provider: str, model: str, reserved_usd: float
    ) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE cognition_opportunities SET outcome = ?, provider = ?, "
                "model = ?, reserved_usd = ? WHERE opportunity_id = ?",
                ("reserved", provider, model, reserved_usd, opportunity_id),
            )

    def release(self, opportunity_id: str) -> None:
        """Give a claimed occasion back, so its request can mature again.

        A turn that never happened is not a thought AL/X had. Keeping the row
        would leave her request pending for ever while every later scan skipped
        it: she asked to come back to something and silently never would.

        Only an occasion that produced nothing is released. One that reached
        the Core keeps its row, because that thought did happen and repeating
        it would be a second paid turn for a request made once.
        """
        with self._connection:
            self._connection.execute(
                "DELETE FROM cognition_opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            )

    def record_outcome(
        self,
        opportunity_id: str,
        outcome: str,
        reasoning_calls: int = 0,
        settled_usd: float | None = None,
        usage: Any = None,
    ) -> None:
        """Close one opportunity. Token counts only; never reasoning content."""
        counts = usage if isinstance(usage, dict) else {}
        with self._connection:
            self._connection.execute(
                "UPDATE cognition_opportunities SET outcome = ?, "
                "reasoning_calls = ?, settled_usd = ?, input_tokens = ?, "
                "cached_tokens = ?, output_tokens = ?, reasoning_tokens = ?, "
                "cache_write_tokens = ? WHERE opportunity_id = ?",
                (
                    outcome,
                    reasoning_calls,
                    settled_usd,
                    int(counts.get("input_tokens") or 0),
                    int(counts.get("cached_tokens") or 0),
                    int(counts.get("output_tokens") or 0),
                    int(counts.get("reasoning_tokens") or 0),
                    int(counts.get("cache_write_tokens") or 0),
                    opportunity_id,
                ),
            )

    def unfinished(self) -> tuple[dict[str, Any], ...]:
        """Rows whose occasion never reached a terminal outcome.

        These are the only candidates for recovery. A row with any other
        outcome records a turn that happened or was refused, and is never
        reclaimed.
        """
        return tuple(
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM cognition_opportunities WHERE outcome IN (?, ?)",
                ("created", "reserved"),
            ).fetchall()
        )

    def mark_unreconciled(self, opportunity_id: str) -> None:
        """Retain an occasion whose replay cannot be proven safe.

        Terminal, so it is never offered again, and distinct from a completed
        turn so Friedl can see that a paid call may have happened without a
        recorded result. Retained for inspection rather than silently replayed
        or silently dropped.
        """
        with self._connection:
            self._connection.execute(
                "UPDATE cognition_opportunities SET outcome = ? "
                "WHERE opportunity_id = ?",
                ("unreconciled", opportunity_id),
            )


    def mark_response_undelivered(self, opportunity_id: str) -> None:
        """Record that an autonomous response had nowhere to go.

        The fact, never the prose. Keeping the words would be a delivery queue
        by another name, and D-024 is explicit that an undeliverable response is
        not replayed: AL/X is shown that it happened and decides on a later turn
        whether she still wants to say anything, composing it fresh then.

        Deterministic code records this and draws no conclusion from it. There
        is no expiry, no priority and no notification semantics.
        """
        with self._connection:
            self._connection.execute(
                "UPDATE cognition_opportunities SET response_undelivered = 1 "
                "WHERE opportunity_id = ?",
                (opportunity_id,),
            )

    def resolve_undelivered(self, opportunity_id: str) -> bool:
        """Close one undelivered occasion because AL/X decided what to do.

        Only she may close it. Nothing expires it, nothing infers resolution
        because similar words were later spoken, and there is no priority or
        ordering beyond when it happened.
        """
        with self._connection:
            changed = self._connection.execute(
                "UPDATE cognition_opportunities SET response_undelivered = 0 "
                "WHERE opportunity_id = ? AND response_undelivered = 1",
                (opportunity_id,),
            ).rowcount
        return bool(changed)

    def undelivered(self) -> tuple[dict[str, Any], ...]:
        """Occasions whose response could not be delivered, newest last."""
        return tuple(
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM cognition_opportunities "
                "WHERE response_undelivered = 1 ORDER BY recorded_at"
            ).fetchall()
        )

    # --- commissioning latch ------------------------------------------------
    #
    # Temporary safety for the first supervised activation, and nothing more.
    # A financial fuse cannot limit turns: a reservation reconciles to actual
    # spend, so a cheap turn returns most of its withdrawal and the next one
    # fits. At $0.1632 that permits 79 dispatches, not two.
    #
    # This counts dispatch attempts, so it is independent of what anything
    # cost. It is durable, so a crash cannot reset it. It is deliberately not a
    # daily quota, a chain-depth limit or a cadence rule, and it is removed
    # once the supervised turn is validated.

    def commissioning_dispatches(self) -> int:
        row = self._connection.execute(
            "SELECT dispatches_attempted FROM commissioning WHERE id = 1"
        ).fetchone()
        return 0 if row is None else int(row[0])

    def record_commissioning_dispatch(self) -> int:
        """Count one attempt, before it is made. Returns the new total.

        Recorded before dispatch rather than after, so a process that dies
        mid-call cannot come back and spend a second time on the strength of
        having no record of the first.
        """
        with self._connection:
            self._connection.execute(
                "INSERT INTO commissioning(id, dispatches_attempted) VALUES (1, 0) "
                "ON CONFLICT(id) DO NOTHING"
            )
            self._connection.execute(
                "UPDATE commissioning SET dispatches_attempted = "
                "dispatches_attempted + 1 WHERE id = 1"
            )
        return self.commissioning_dispatches()

    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM cognition_opportunities ORDER BY recorded_at"
            ).fetchall()
        )
