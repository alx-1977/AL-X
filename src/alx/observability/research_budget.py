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
                    settled_at TEXT,
                    kind TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_spend_day
                    ON research_spend(day);
                """
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
        self, tier: str, provider: str, model: str, kind: str = "research"
    ) -> Reservation:
        """Withdraw one request's maximum, or refuse the call.

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
                required = self._budget.per_request_max_usd
                if remaining < required:
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

    def settle(self, reservation: Reservation, actual_usd: float) -> float:
        """Replace the reservation with the measured cost.

        The true cost is recorded even when it exceeds what was reserved.
        Clamping it to the reservation would hide the overrun and let the day
        keep spending against money already gone; the ledger must show what was
        actually spent so the ceiling is enforced against reality. An overrun is
        a bound or pricing fault, and it is surfaced rather than absorbed.
        """
        if actual_usd < 0:
            raise ValueError("actual_usd must not be negative")
        settled = actual_usd
        with self._lock:
            database = self._db()
            try:
                database.execute(
                    "UPDATE research_spend SET actual_usd = ?, settled_at = ? "
                    "WHERE reservation_id = ? AND actual_usd IS NULL",
                    (
                        settled,
                        datetime.now(UTC).isoformat(),
                        reservation.reservation_id,
                    ),
                )
                database.commit()
            finally:
                database.close()
        return settled

    def abandon(self, reservation: Reservation) -> float:
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
        return self.settle(reservation, reservation.reserved_usd)

    def day(self, day: str | None = None) -> dict[str, object]:
        """Inspectable rollup so Friedl can see where research spend went."""
        day = day or self._today()
        database = self._db()
        try:
            rows = database.execute(
                "SELECT kind, tier, provider, model, reserved_usd, actual_usd "
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
