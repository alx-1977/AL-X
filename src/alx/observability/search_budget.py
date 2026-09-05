"""Durable spend accounting for flat-fee public web search, under D-025.

Search is a different economic resource from model cognition. One request costs
one fixed price; there is no tier, no model, no token count and no variance
between what a request might cost and what it did. The research ledger's schema
would force `tier`, `model` and four token columns onto every row, and a
durable audit record asserting that a web search had a cognition tier is a
false record. So this is a separate store with a separate file, and every
column here has an honest value for the thing it describes.

Two ceilings are enforced, not one, because they fail differently. The USD
ceiling bounds money. The request-count ceiling bounds how much AL/X may lean
on an external provider in a day, and it holds even if the configured price is
wrong — a price recorded too low would let a USD-only ceiling admit hundreds of
requests while appearing to hold.

Accounting is deliberately conservative. Where it cannot be certain whether a
request was billable it records the spend rather than omitting it, so the
recorded figure may exceed reality but never understates it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from threading import Lock
from uuid import uuid4


# Money is compared in whole micro-dollars. Accumulating floats against a hard
# ceiling lets rounding decide whether the last request of the day is allowed.
_MICROS = 1_000_000


class SearchBudgetExceeded(Exception):
    """The day cannot cover another search request."""

    def __init__(self, reason: str, remaining_usd: float, remaining_requests: int) -> None:
        self.reason = reason
        self.remaining_usd = remaining_usd
        self.remaining_requests = remaining_requests
        super().__init__(
            f"search refused: {reason} "
            f"({remaining_requests} requests and {remaining_usd:.4f} USD left today)"
        )


class SearchLedgerCorrupt(Exception):
    """Durable accounting could not be read or is internally inconsistent.

    Raised rather than assuming a clean day. A ledger that cannot be trusted
    must stop search, because the alternative is spending against a ceiling
    nobody is measuring.
    """


@dataclass(frozen=True, slots=True)
class SearchReservation:
    """One request's price, withdrawn until its outcome is known."""

    reservation_id: str
    reserved_usd: float


@dataclass(frozen=True, slots=True)
class SearchBudget:
    """Friedl's hard daily boundary for paid public web search.

    `daily_requests` and `daily_usd` are independent invariants. Neither
    implies the other, and both are checked before every reservation.
    """

    daily_usd: float
    daily_requests: int
    usd_per_request: float

    def __post_init__(self) -> None:
        if not isfinite(self.daily_usd) or not isfinite(self.usd_per_request):
            raise ValueError("search budgets must be finite")
        if self.daily_usd < 0:
            raise ValueError("daily_usd must not be negative")
        if self.usd_per_request <= 0:
            raise ValueError("usd_per_request must be positive")
        if not isinstance(self.daily_requests, int) or isinstance(
            self.daily_requests, bool
        ):
            raise TypeError("daily_requests must be an integer")
        if self.daily_requests < 0:
            raise ValueError("daily_requests must not be negative")


class SQLiteSearchLedger:
    """Reserve a search request's price before dispatch, and reconcile after.

    Entries are durable so a restart cannot hand AL/X a fresh day's allowance,
    and an unreconciled reservation from a crashed request stays withdrawn
    rather than quietly returning to the pool.
    """

    def __init__(self, path: Path, budget: SearchBudget) -> None:
        if not isinstance(budget, SearchBudget):
            raise TypeError("budget must be a SearchBudget")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._budget = budget
        self._lock = Lock()
        try:
            database = self._db()
        except sqlite3.Error as error:
            raise SearchLedgerCorrupt(f"search ledger unavailable: {error}") from None
        try:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS search_spend (
                    reservation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    reserved_usd REAL NOT NULL,
                    actual_usd REAL,
                    settled_at TEXT,
                    provider TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'reserved',
                    failure_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS search_spend_day ON search_spend(day);
                """
            )
            database.commit()
        except sqlite3.Error as error:
            raise SearchLedgerCorrupt(f"search ledger unusable: {error}") from None
        finally:
            database.close()

    def _db(self) -> sqlite3.Connection:
        database = sqlite3.connect(str(self._path), timeout=30)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _day_totals(self, database: sqlite3.Connection, day: str) -> tuple[float, int]:
        """Committed spend and request count for one day.

        An abandoned request costs nothing and counts for nothing: it never
        reached the provider, so charging it against either ceiling would
        penalise AL/X for a connection failure she did not cause.
        """
        row = database.execute(
            "SELECT COALESCE(SUM("
            "  CASE WHEN actual_usd IS NULL THEN reserved_usd ELSE actual_usd END"
            "), 0.0) AS spend, COUNT(*) AS requests "
            "FROM search_spend WHERE day = ? AND outcome != 'abandoned'",
            (day,),
        ).fetchone()
        spend = float(row["spend"])
        requests = int(row["requests"])
        if not isfinite(spend) or spend < 0 or requests < 0:
            raise SearchLedgerCorrupt(
                f"search ledger holds impossible totals for {day}: "
                f"{requests} requests, {spend} USD"
            )
        return spend, requests

    def reserve(self, provider: str) -> SearchReservation:
        """Withdraw one request's price, or refuse the search.

        The check and the withdrawal happen under one lock and one transaction.
        Were they separate, two concurrent searches could read the same
        remaining allowance and both proceed, putting the day over a ceiling by
        a whole request.
        """
        if not provider.strip():
            raise ValueError("search provider must be named")
        price = self._budget.usd_per_request
        day = self._today()
        with self._lock:
            try:
                database = self._db()
            except sqlite3.Error as error:
                raise SearchLedgerCorrupt(f"search ledger unavailable: {error}") from None
            try:
                database.execute("BEGIN IMMEDIATE")
                spend, requests = self._day_totals(database, day)

                # Count first: it holds even when the configured price is wrong.
                if requests + 1 > self._budget.daily_requests:
                    database.rollback()
                    raise SearchBudgetExceeded(
                        "daily request ceiling reached",
                        max(0.0, self._budget.daily_usd - spend),
                        0,
                    )
                price_micros = round(price * _MICROS)
                spend_micros = round(spend * _MICROS)
                ceiling_micros = round(self._budget.daily_usd * _MICROS)
                if spend_micros + price_micros > ceiling_micros:
                    database.rollback()
                    raise SearchBudgetExceeded(
                        "daily spend ceiling reached",
                        max(0.0, self._budget.daily_usd - spend),
                        max(0, self._budget.daily_requests - requests),
                    )

                reservation_id = uuid4().hex
                database.execute(
                    "INSERT INTO search_spend(reservation_id, day, opened_at, "
                    "reserved_usd, actual_usd, settled_at, provider, outcome, "
                    "failure_code) VALUES (?, ?, ?, ?, NULL, NULL, ?, 'reserved', '')",
                    (
                        reservation_id,
                        day,
                        datetime.now(UTC).isoformat(),
                        price,
                        provider,
                    ),
                )
                database.commit()
            except (SearchBudgetExceeded, SearchLedgerCorrupt):
                raise
            except sqlite3.Error as error:
                raise SearchLedgerCorrupt(f"search reservation failed: {error}") from None
            finally:
                database.close()
        return SearchReservation(reservation_id, price)

    def settle(self, reservation: SearchReservation) -> float:
        """Record the request as charged at the flat price.

        The reservation already withdrew exactly the flat price, so settlement
        confirms it rather than adjusting it. A flat fee has no variance, which
        is why there is no measured-cost argument here to disagree with it.
        """
        return self._close(reservation, "settled", "", reservation.reserved_usd)

    def abandon(
        self, reservation: SearchReservation, failure_code: str = "provider_failed"
    ) -> float:
        """Release a reservation for a request that never reached the provider.

        Used only where no HTTP response was received at all. Anything the
        provider answered is settled instead, because this accounting
        deliberately over-records rather than risk understating spend.
        """
        return self._close(reservation, "abandoned", failure_code, 0.0)

    def _close(
        self,
        reservation: SearchReservation,
        outcome: str,
        failure_code: str,
        actual_usd: float,
    ) -> float:
        if not isinstance(reservation, SearchReservation):
            raise TypeError("reservation must be a SearchReservation")
        with self._lock:
            try:
                database = self._db()
            except sqlite3.Error as error:
                raise SearchLedgerCorrupt(f"search ledger unavailable: {error}") from None
            try:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT outcome FROM search_spend WHERE reservation_id = ?",
                    (reservation.reservation_id,),
                ).fetchone()
                if row is None:
                    database.rollback()
                    raise SearchLedgerCorrupt(
                        "settling a search reservation that was never recorded"
                    )
                if str(row["outcome"]) != "reserved":
                    # Closing a reservation twice would either double-count the
                    # spend or silently release money already committed.
                    database.rollback()
                    raise SearchLedgerCorrupt(
                        f"search reservation already {row['outcome']}"
                    )
                database.execute(
                    "UPDATE search_spend SET actual_usd = ?, settled_at = ?, "
                    "outcome = ?, failure_code = ? WHERE reservation_id = ?",
                    (
                        actual_usd,
                        datetime.now(UTC).isoformat(),
                        outcome,
                        failure_code,
                        reservation.reservation_id,
                    ),
                )
                database.commit()
            except SearchLedgerCorrupt:
                raise
            except sqlite3.Error as error:
                raise SearchLedgerCorrupt(f"search settlement failed: {error}") from None
            finally:
                database.close()
        return actual_usd

    def committed_usd(self, day: str | None = None) -> float:
        return self._totals(day)[0]

    def committed_requests(self, day: str | None = None) -> int:
        return self._totals(day)[1]

    def remaining_usd(self, day: str | None = None) -> float:
        return max(0.0, self._budget.daily_usd - self._totals(day)[0])

    def remaining_requests(self, day: str | None = None) -> int:
        return max(0, self._budget.daily_requests - self._totals(day)[1])

    def _totals(self, day: str | None) -> tuple[float, int]:
        with self._lock:
            try:
                database = self._db()
            except sqlite3.Error as error:
                raise SearchLedgerCorrupt(f"search ledger unavailable: {error}") from None
            try:
                return self._day_totals(database, day or self._today())
            except sqlite3.Error as error:
                raise SearchLedgerCorrupt(f"search ledger unreadable: {error}") from None
            finally:
                database.close()

    def close(self) -> None:
        """Nothing to release: each operation opens and closes its own handle."""


__all__ = [
    "SQLiteSearchLedger",
    "SearchBudget",
    "SearchBudgetExceeded",
    "SearchLedgerCorrupt",
    "SearchReservation",
]
