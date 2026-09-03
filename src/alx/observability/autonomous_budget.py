"""A hard daily ceiling on autonomous Core cognition.

This is the emergency fuse for D-024. It is deliberately not a frequency rule:
nothing here counts how often AL/X thinks, and nothing decides that a thought
was not worth having. It bounds money, which is a resource, rather than
initiative, which is hers.

Spend is reserved, not predicted. Cost is unknowable before a call because
output tokens are what make a turn expensive and they exist only once the model
has answered, so the worst case is withdrawn before dispatch and replaced by
the measured cost afterwards. Between the two the full worst case is already
accounted for, so a crash mid-call cannot overspend the day.

This mirrors SQLiteResearchLedger rather than sharing it. Research and
cognition are separate ceilings under D-023 and D-024, and one table serving
both would let either quietly consume the other's headroom.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from threading import Lock
from uuid import uuid4


class AutonomousBudgetExceeded(Exception):
    """Raised before dispatch when the day cannot cover another turn.

    This stops autonomous cognition. It never selects a cheaper model, a
    smaller bound or a shorter prompt to fit what is left: a ceiling that
    quietly buys something lesser is not a ceiling, and the model AL/X reasons
    with is not a runtime reaction to cost. The refusal becomes evidence she
    reasons about on a later turn.
    """

    def __init__(self, remaining_usd: float, required_usd: float) -> None:
        self.remaining_usd = remaining_usd
        self.required_usd = required_usd
        super().__init__(
            f"autonomous cognition budget exhausted: {remaining_usd:.4f} USD "
            f"remaining, {required_usd:.4f} USD required for one turn"
        )


class AutonomousModelUnpriced(Exception):
    """The configured autonomous model has no recorded price.

    Refused before dispatch. A call we cannot price is not a free call, and
    charging it at a neighbouring model's rate would defeat the ceiling it is
    meant to enforce.
    """

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.model = model
        super().__init__(f"no configured price for {provider} model {model}")


class AutonomousBoundMissing(Exception):
    """A finite output bound is required before any autonomous turn.

    Without one there is no worst-case price, so no reservation can be honest.
    """


@dataclass(frozen=True, slots=True)
class AutonomousReservation:
    """A withdrawal held against the day until the real cost is known."""

    reservation_id: str
    reserved_usd: float


class SQLiteAutonomousLedger:
    """Reserve autonomous cognition spend before a call, reconcile it after.

    Entries are durable so a restart cannot hand AL/X a fresh day's budget, and
    an unreconciled reservation from a crashed call stays withdrawn rather than
    silently returning to the pool.
    """

    def __init__(self, path: Path, daily_usd: float, pricing) -> None:
        if not isfinite(daily_usd) or daily_usd < 0:
            raise ValueError("daily_usd must be finite and non-negative")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._daily_usd = daily_usd
        self._pricing = pricing
        self._lock = Lock()
        database = self._db()
        try:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS autonomous_spend (
                    reservation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    reserved_usd REAL NOT NULL,
                    actual_usd REAL,
                    settled_at TEXT,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    opportunity_id TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'reserved'
                );
                CREATE INDEX IF NOT EXISTS autonomous_spend_day
                    ON autonomous_spend(day);
                """
            )
            database.commit()
        finally:
            database.close()

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None)

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _committed(self, database, day: str) -> float:
        row = database.execute(
            "SELECT COALESCE(SUM("
            "  CASE WHEN actual_usd IS NULL THEN reserved_usd ELSE actual_usd END"
            "), 0.0) FROM autonomous_spend WHERE day = ?",
            (day,),
        ).fetchone()
        return float(row[0])

    def remaining_usd(self) -> float:
        database = self._db()
        try:
            return max(0.0, self._daily_usd - self._committed(database, self._today()))
        finally:
            database.close()

    def worst_case_usd(
        self,
        provider: str,
        model: str,
        max_input_tokens: int,
        max_output_tokens: int | None,
    ) -> float:
        """The most one autonomous turn can cost. Fails closed, never guesses."""
        if max_output_tokens is None or max_output_tokens <= 0:
            raise AutonomousBoundMissing(
                "an autonomous turn requires a finite positive output bound"
            )
        worst = self._pricing.worst_case_usd(
            provider, model, max_input_tokens, max_output_tokens
        )
        if worst is None:
            raise AutonomousModelUnpriced(provider, model)
        return worst

    def reserve(
        self,
        provider: str,
        model: str,
        max_input_tokens: int,
        max_output_tokens: int | None,
        opportunity_id: str = "",
    ) -> AutonomousReservation:
        """Withdraw this turn's worst case, or refuse it.

        The check and the withdrawal happen under one lock and one transaction.
        Were they separate, two concurrent autonomous turns could both read the
        same remaining budget and both proceed, putting the day over the
        ceiling by a whole turn.
        """
        required = self.worst_case_usd(
            provider, model, max_input_tokens, max_output_tokens
        )
        day = self._today()
        with self._lock:
            database = self._db()
            try:
                database.execute("BEGIN IMMEDIATE")
                committed = self._committed(database, day)
                remaining = self._daily_usd - committed
                required_micros = round(required * 1_000_000)
                committed_micros = round(committed * 1_000_000)
                daily_micros = round(self._daily_usd * 1_000_000)
                if committed_micros + required_micros > daily_micros:
                    database.rollback()
                    raise AutonomousBudgetExceeded(max(0.0, remaining), required)
                reservation_id = uuid4().hex
                database.execute(
                    "INSERT INTO autonomous_spend(reservation_id, day, opened_at, "
                    "reserved_usd, actual_usd, settled_at, provider, model, "
                    "opportunity_id) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)",
                    (
                        reservation_id,
                        day,
                        datetime.now(UTC).isoformat(),
                        required,
                        provider,
                        model,
                        opportunity_id,
                    ),
                )
                database.commit()
            finally:
                database.close()
        return AutonomousReservation(reservation_id, required)

    def mark_dispatched(self, reservation: AutonomousReservation) -> None:
        """Record, before the call, that the provider is about to be reached.

        Written and committed immediately before `complete()`, never after.
        That ordering is the whole guarantee: a crash at any instant during the
        call leaves this on disk, so a row still at `reserved` proves the
        provider was never reached and the occasion is safe to replay. Writing
        it afterwards would make the two histories indistinguishable and every
        recovery a guess about whether money had already been spent.
        """
        with self._lock:
            database = self._db()
            try:
                database.execute(
                    "UPDATE autonomous_spend SET outcome = ? "
                    "WHERE reservation_id = ? AND outcome = ?",
                    ("dispatched", reservation.reservation_id, "reserved"),
                )
                database.commit()
            finally:
                database.close()

    def dispatch_started(self, opportunity_id: str) -> bool:
        """Whether any reservation for this occasion reached the provider.

        The one question recovery asks of the spend ledger. True means a paid
        call may already have happened, so the occasion must never be replayed.
        """
        database = self._db()
        try:
            row = database.execute(
                "SELECT COUNT(*) FROM autonomous_spend "
                "WHERE opportunity_id = ? AND outcome != ?",
                (opportunity_id, "reserved"),
            ).fetchone()
            return bool(row[0])
        finally:
            database.close()

    def settle(
        self,
        reservation: AutonomousReservation,
        provider: str,
        model: str,
        usage,
    ) -> float:
        """Replace the reservation with the measured cost.

        Usage that cannot be priced keeps the full reservation. A provider that
        reported nothing has not told us the call was free, and pricing that
        silence at zero would let an unlimited number of unmeasured turns run
        inside one day's budget.
        """
        measured = self._pricing.cost_usd(provider, model, usage)
        conservative = measured is None
        actual = reservation.reserved_usd if conservative else measured
        with self._lock:
            database = self._db()
            try:
                database.execute(
                    "UPDATE autonomous_spend SET actual_usd = ?, settled_at = ?, "
                    "outcome = ? WHERE reservation_id = ?",
                    (
                        actual,
                        datetime.now(UTC).isoformat(),
                        "unmeasured" if conservative else "settled",
                        reservation.reservation_id,
                    ),
                )
                database.commit()
            finally:
                database.close()
        return actual

    def spend_today(self) -> float:
        database = self._db()
        try:
            return self._committed(database, self._today())
        finally:
            database.close()
