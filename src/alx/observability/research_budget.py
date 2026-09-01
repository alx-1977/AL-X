"""A hard daily ceiling on autonomous research spend.

Cost is not knowable before a call: output and reasoning tokens are what make a
research question expensive, and they exist only once the model has answered.
Checking the remaining budget before dispatch and recording the true cost after
would therefore let one final expensive call land beyond Friedl's boundary.

So spend is reserved, not predicted. Before dispatch the per-request maximum is
withdrawn from the day's remaining budget; after the call the reservation is
replaced by the measured cost and the difference returned. The day can never be
overspent by more than nothing: at every instant between the two, the full
per-request maximum is already accounted for.

A model with no configured price cannot be reconciled honestly, so it cannot
take part in paid research at all. Guessing a price would defeat the ceiling it
is meant to enforce.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from threading import Lock
from uuid import uuid4


# Observability is a leaf: it observes and stores, and depends on no other AL/X
# module. The reservation record and the exhaustion failure are therefore
# defined here, and `alx.contracts.research` states the same shape as the
# promise the bounded-question path is written against. Bootstrap wires the two
# together; neither layer imports the other.


class ResearchBudgetExceeded(Exception):
    """Raised before dispatch when the day cannot cover another request.

    This stops research. It never selects a cheaper tier or another provider:
    a ceiling that quietly buys something less is not a ceiling, and provider
    choice is Friedl's decision rather than a runtime reaction to cost.
    """

    def __init__(self, remaining_usd: float, required_usd: float) -> None:
        self.remaining_usd = remaining_usd
        self.required_usd = required_usd
        super().__init__(
            f"research budget exhausted: {remaining_usd:.4f} USD remaining, "
            f"{required_usd:.4f} USD required for one request"
        )


@dataclass(frozen=True, slots=True)
class Reservation:
    """A withdrawal held against the day until the real cost is known."""

    reservation_id: str
    reserved_usd: float


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    """Friedl's hard spending boundary for autonomous research."""

    daily_usd: float
    per_request_max_usd: float

    def __post_init__(self) -> None:
        if not isfinite(self.daily_usd) or not isfinite(self.per_request_max_usd):
            raise ValueError("research budgets must be finite")
        if self.daily_usd < 0:
            raise ValueError("daily_usd must not be negative")
        if self.per_request_max_usd <= 0:
            raise ValueError("per_request_max_usd must be positive")
        if self.per_request_max_usd > self.daily_usd:
            raise ValueError(
                "per_request_max_usd must not exceed daily_usd; no request "
                "could ever be afforded"
            )


class SQLiteResearchLedger:
    """Reserve research spend before a call and reconcile it afterwards.

    Entries are durable so a restart cannot hand AL/X a fresh day's budget, and
    an unreconciled reservation from a crashed call stays withdrawn rather than
    silently returning to the pool.
    """

    def __init__(self, path: Path, budget: ResearchBudget) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._budget = budget
        self._lock = Lock()
        database = self._db()
        try:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_spend (
                    reservation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    reserved_usd REAL NOT NULL,
                    actual_usd REAL,
                    overrun_usd REAL NOT NULL DEFAULT 0.0,
                    settled_at TEXT,
                    kind TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT NOT NULL DEFAULT 'reserved',
                    failure_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS research_spend_day
                    ON research_spend(day);
                """
            )
            existing = {
                str(row["name"])
                for row in database.execute("PRAGMA table_info(research_spend)")
            }
            if "overrun_usd" not in existing:
                database.execute(
                    "ALTER TABLE research_spend ADD COLUMN overrun_usd REAL "
                    "NOT NULL DEFAULT 0.0"
                )
            for column, definition in (
                ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("cached_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("outcome", "TEXT NOT NULL DEFAULT 'reserved'"),
                ("failure_code", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in existing:
                    database.execute(
                        f"ALTER TABLE research_spend ADD COLUMN {column} {definition}"
                    )
            database.commit()
        finally:
            database.close()

    def _db(self) -> sqlite3.Connection:
        database = sqlite3.connect(str(self._path), timeout=30)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def committed_usd(self, day: str | None = None) -> float:
        """Spend already committed today: settled costs plus open reservations.

        An open reservation counts at its full reserved amount. That is what
        makes the ceiling hold while a call is in flight.
        """
        day = day or self._today()
        database = self._db()
        try:
            row = database.execute(
                "SELECT COALESCE(SUM("
                "  CASE WHEN actual_usd IS NULL THEN reserved_usd "
                "       ELSE actual_usd END"
                "), 0.0) FROM research_spend WHERE day = ?",
                (day,),
            ).fetchone()
        finally:
            database.close()
        return float(row[0])

    def remaining_usd(self, day: str | None = None) -> float:
        return max(0.0, self._budget.daily_usd - self.committed_usd(day))

    def reserve(
        self,
        tier: str,
        provider: str,
        model: str,
        kind: str = "research",
        worst_case_usd: float | None = None,
    ) -> Reservation:
        """Withdraw this request's worst case, or refuse the call.

        The check and the withdrawal happen under one lock and one transaction.
        Were they separate, two concurrent research calls could both read the
        same remaining budget and both proceed, putting the day over the
        ceiling by a whole request.
        """
        day = self._today()
        with self._lock:
            database = self._db()
            try:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT COALESCE(SUM("
                    "  CASE WHEN actual_usd IS NULL THEN reserved_usd "
                    "       ELSE actual_usd END"
                    "), 0.0) FROM research_spend WHERE day = ?",
                    (day,),
                ).fetchone()
                committed = float(row[0])
                remaining = self._budget.daily_usd - committed
                # Withdraw what this request can actually cost at worst, never
                # a nominal figure. A reservation smaller than the worst case
                # lets settlement exceed it, which is how a 0.02 USD ceiling
                # committed a full dollar.
                required = (
                    self._budget.per_request_max_usd
                    if worst_case_usd is None
                    else worst_case_usd
                )
                if not isfinite(required) or required < 0:
                    database.rollback()
                    raise ValueError("research reservation must be finite and non-negative")
                required_micros = round(required * 1_000_000)
                committed_micros = round(committed * 1_000_000)
                request_ceiling_micros = round(
                    self._budget.per_request_max_usd * 1_000_000
                )
                daily_ceiling_micros = round(self._budget.daily_usd * 1_000_000)
                if required_micros > request_ceiling_micros:
                    database.rollback()
                    raise ResearchBudgetExceeded(max(0.0, remaining), required)
                if committed_micros + required_micros > daily_ceiling_micros:
                    database.rollback()
                    raise ResearchBudgetExceeded(max(0.0, remaining), required)
                reservation_id = uuid4().hex
                database.execute(
                    "INSERT INTO research_spend(reservation_id, day, opened_at, "
                    "reserved_usd, actual_usd, settled_at, kind, tier, provider, "
                    "model) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)",
                    (
                        reservation_id,
                        day,
                        datetime.now(UTC).isoformat(),
                        required,
                        kind,
                        tier,
                        provider,
                        model,
                    ),
                )
                database.commit()
            finally:
                database.close()
        return Reservation(reservation_id, required)

    def settle(
        self,
        reservation: Reservation,
        actual_usd: float,
        usage: dict[str, object] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> float:
        """Replace the reservation with the measured cost.

        A measured cost above the reservation means the enforced bound did not
        hold: the provider ignored it, or the configured price is wrong. Both
        are faults, and both are recorded rather than silently absorbed. The
        ledger keeps the true figure so the day reflects real spend, and
        `overrun_usd` reports the excess so research can stop entirely rather
        than continue against a ceiling already known to have failed.
        """
        if not isfinite(actual_usd) or actual_usd < 0:
            raise ValueError("actual_usd must not be negative")
        # Record the provider's measured charge, never an accounting clamp. If
        # it exceeds the mechanically enforced bound, overrun_usd makes the
        # failed guarantee explicit and the caller stops research immediately.
        settled = actual_usd
        overrun = max(0.0, actual_usd - reservation.reserved_usd)
        outcome = "failed" if overrun > 0 else "succeeded"
        failure_code = "cost_overrun" if overrun > 0 else ""
        usage = usage or {}
        with self._lock:
            database = self._db()
            try:
                database.execute(
                    "UPDATE research_spend SET actual_usd = ?, overrun_usd = ?, "
                    "settled_at = ?, input_tokens = ?, cached_tokens = ?, "
                    "output_tokens = ?, reasoning_tokens = ?, provider = COALESCE(?, provider), "
                    "model = COALESCE(?, model), outcome = ?, failure_code = ? "
                    "WHERE reservation_id = ? AND actual_usd IS NULL",
                    (
                        settled,
                        overrun,
                        datetime.now(UTC).isoformat(),
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("cached_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        int(usage.get("reasoning_tokens") or 0),
                        provider,
                        model,
                        outcome,
                        failure_code,
                        reservation.reservation_id,
                    ),
                )
                database.commit()
            finally:
                database.close()
        return settled

    def abandon(
        self,
        reservation: Reservation,
        failure_code: str = "provider_failed",
        usage: dict[str, object] | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> float:
        """Close a failed or unmeasurable call at its full reservation.

        A failed provider call is not a free one. A request can be billed and
        still fail: a timeout after the model generated its answer, a stream cut
        mid-response, a malformed body. Settling those at zero would let an
        unbounded number of failing paid calls run inside one day's budget,
        which is exactly the hole a ceiling exists to close.

        Charging the full reservation is deliberately conservative. It may
        overstate the cost of a call that genuinely never reached the provider,
        and that is the safe direction to be wrong in.
        """
        usage = usage or {}
        with self._lock:
            database = self._db()
            try:
                database.execute(
                    "UPDATE research_spend SET actual_usd = reserved_usd, "
                    "settled_at = ?, input_tokens = ?, cached_tokens = ?, "
                    "output_tokens = ?, reasoning_tokens = ?, "
                    "provider = COALESCE(?, provider), model = COALESCE(?, model), "
                    "outcome = 'failed', failure_code = ? "
                    "WHERE reservation_id = ? AND actual_usd IS NULL",
                    (
                        datetime.now(UTC).isoformat(),
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("cached_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        int(usage.get("reasoning_tokens") or 0),
                        provider,
                        model,
                        failure_code,
                        reservation.reservation_id,
                    ),
                )
                database.commit()
            finally:
                database.close()
        return reservation.reserved_usd

    def overrun_usd(self, day: str | None = None) -> float:
        """How far settled spend has exceeded its reservations today.

        Non-zero means a bound failed. Research must stop rather than continue
        against a ceiling that is already known not to hold.
        """
        day = day or self._today()
        database = self._db()
        try:
            row = database.execute(
                "SELECT COALESCE(SUM(overrun_usd), 0.0) "
                "FROM research_spend WHERE day = ?",
                (day,),
            ).fetchone()
        finally:
            database.close()
        return round(float(row[0]), 6)

    def day(self, day: str | None = None) -> dict[str, object]:
        """Inspectable rollup so Friedl can see where research spend went."""
        day = day or self._today()
        database = self._db()
        try:
            rows = database.execute(
                "SELECT reservation_id, kind, tier, provider, model, reserved_usd, "
                "actual_usd, overrun_usd, input_tokens, cached_tokens, "
                "output_tokens, reasoning_tokens, outcome, failure_code "
                "FROM research_spend WHERE day = ? ORDER BY opened_at",
                (day,),
            ).fetchall()
        finally:
            database.close()
        calls = [dict(row) for row in rows]
        return {
            "day": day,
            "daily_usd": self._budget.daily_usd,
            "per_request_max_usd": self._budget.per_request_max_usd,
            "committed_usd": round(self.committed_usd(day), 6),
            "remaining_usd": round(self.remaining_usd(day), 6),
            "open_reservations": sum(
                1 for item in calls if item["actual_usd"] is None
            ),
            "calls": calls,
        }
