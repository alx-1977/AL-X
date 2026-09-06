"""Durable run and wall-time accounting for local experimentation, under D-027.

The sandbox spends a different resource from every other ledger here. Research,
search and autonomous cognition spend money; a sandbox run spends time on
Friedl's own machine. Recording a run in a USD ledger would require inventing a
price for it, and a durable audit record asserting that a local execution cost
money it never cost would be a false record. So this is a separate store whose
columns are true of the thing they describe.

Two ceilings are enforced, not one, because they fail differently. The run
count bounds repetition: a loop that re-runs a broken experiment burns it
quickly and visibly. The wall-second ceiling bounds occupation of the machine:
one long run exhausts it while the count is barely touched. Neither implies the
other, so both are checked before every reservation.

Neither ceiling is memory containment. A single allocation can exhaust memory
inside one short run, well within both. D-027 records that as an accepted
limitation of the current development host, and nothing here should be
described as protecting against it.

Accounting is conservative. A run's full permitted wall time is withdrawn
before it starts and the actual duration is settled afterwards, so a crash
between the two leaves the day over-recorded rather than unrecorded.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from uuid import uuid4

# Mirrors the D-027 ceilings. `observability` is a leaf boundary that imports
# nothing internal, so these are literals here and in the contracts module; a
# test asserts the two stay equal so a drift cannot pass unnoticed.
DAILY_RUNS = 20
DAILY_WALL_SECONDS = 300


class SandboxBudgetExceeded(Exception):
    """The day cannot cover another run."""

    def __init__(self, reason: str, remaining_seconds: float, remaining_runs: int) -> None:
        self.reason = reason
        self.remaining_seconds = remaining_seconds
        self.remaining_runs = remaining_runs
        super().__init__(
            f"sandbox run refused: {reason} "
            f"({remaining_runs} runs and {remaining_seconds:.1f} seconds left today)"
        )


class SandboxLedgerCorrupt(Exception):
    """Durable accounting could not be read or is internally inconsistent.

    Raised rather than assuming a clean day. A ledger that cannot be trusted
    must stop execution, because the alternative is running against a ceiling
    nobody is measuring.
    """


@dataclass(frozen=True, slots=True)
class SandboxReservation:
    """One run's permitted wall time, withdrawn until its duration is known."""

    reservation_id: str
    reserved_seconds: float


@dataclass(frozen=True, slots=True)
class SandboxBudget:
    """Friedl's hard daily boundary for local experimentation.

    `daily_runs` and `daily_seconds` are independent invariants. Neither
    implies the other, and both are checked before every reservation.
    """

    daily_runs: int = DAILY_RUNS
    daily_seconds: int = DAILY_WALL_SECONDS

    def __post_init__(self) -> None:
        for value, name in ((self.daily_runs, "daily_runs"), (self.daily_seconds, "daily_seconds")):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must not be negative")


class SQLiteSandboxLedger:
    """Durable per-day run and wall-second accounting."""

    def __init__(self, path: Path, budget: SandboxBudget) -> None:
        self._path = Path(path)
        self._budget = budget
        self._lock = Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            database = self._db()
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS sandbox_runs (
                    reservation_id TEXT PRIMARY KEY,
                    day TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    reserved_seconds REAL NOT NULL,
                    actual_seconds REAL,
                    settled_at TEXT,
                    outcome TEXT NOT NULL DEFAULT 'reserved',
                    failure_code TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS sandbox_runs_day ON sandbox_runs(day);
                """
            )
            database.commit()
            database.close()
        except sqlite3.Error as error:
            raise SandboxLedgerCorrupt(str(error)) from error

    def _db(self) -> sqlite3.Connection:
        # `timeout` makes a competing writer wait for the lock rather than
        # failing immediately; the ledger is tiny and contention is brief.
        return sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False, timeout=10.0
        )

    @property
    def budget(self) -> SandboxBudget:
        return self._budget

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _totals(self, database: sqlite3.Connection, day: str) -> tuple[float, int]:
        """Committed seconds and runs for one day.

        A reserved-but-unsettled run counts at its reservation, so a run in
        flight cannot be spent twice.
        """
        try:
            row = database.execute(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN actual_seconds IS NULL
                                    THEN reserved_seconds ELSE actual_seconds END), 0.0),
                  COUNT(*)
                FROM sandbox_runs
                WHERE day = ? AND outcome != 'abandoned'
                """,
                (day,),
            ).fetchone()
        except sqlite3.Error as error:
            raise SandboxLedgerCorrupt(str(error)) from error
        if row is None:
            raise SandboxLedgerCorrupt("day totals unavailable")
        seconds, runs = float(row[0]), int(row[1])
        if seconds < 0 or runs < 0:
            raise SandboxLedgerCorrupt("negative day totals")
        return seconds, runs

    def reserve(self, wall_seconds: int) -> SandboxReservation:
        """Withdraw one run's permitted time, or refuse the day.

        The check and the insert are one immediate transaction. A thread lock
        alone bounds nothing across processes: two runtimes sharing this file
        could each read the same remaining budget and each insert a
        reservation, so the day's ceilings would hold in neither. BEGIN
        IMMEDIATE takes the write lock before the totals are read, so a second
        reserver waits and then sees the first reservation.
        """
        with self._lock:
            database = self._db()
            try:
                try:
                    database.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as error:
                    # A ledger that cannot be locked cannot be measured, and an
                    # unmeasured ceiling is not a ceiling.
                    raise SandboxLedgerCorrupt(str(error)) from error
                day = self._today()
                seconds, runs = self._totals(database, day)
                if runs + 1 > self._budget.daily_runs:
                    database.rollback()
                    raise SandboxBudgetExceeded(
                        "daily run ceiling reached",
                        max(0.0, self._budget.daily_seconds - seconds),
                        max(0, self._budget.daily_runs - runs),
                    )
                if seconds + wall_seconds > self._budget.daily_seconds:
                    database.rollback()
                    raise SandboxBudgetExceeded(
                        "daily wall-time ceiling reached",
                        max(0.0, self._budget.daily_seconds - seconds),
                        max(0, self._budget.daily_runs - runs),
                    )
                reservation_id = uuid4().hex
                try:
                    database.execute(
                        "INSERT INTO sandbox_runs (reservation_id, day, opened_at, reserved_seconds)"
                        " VALUES (?, ?, ?, ?)",
                        (reservation_id, day, datetime.now(UTC).isoformat(), float(wall_seconds)),
                    )
                except sqlite3.Error as error:
                    database.rollback()
                    raise SandboxLedgerCorrupt(str(error)) from error
                database.commit()
                return SandboxReservation(reservation_id, float(wall_seconds))
            finally:
                database.close()

    def settle(self, reservation: SandboxReservation, actual_seconds: float) -> float:
        """Record what the run actually took, never more than was reserved."""
        recorded = max(0.0, min(float(actual_seconds), reservation.reserved_seconds))
        self._close(reservation, "settled", recorded, "")
        return recorded

    def abandon(self, reservation: SandboxReservation, failure_code: str = "") -> None:
        """Release a reservation for a run that never started."""
        self._close(reservation, "abandoned", None, failure_code)

    def _close(
        self,
        reservation: SandboxReservation,
        outcome: str,
        actual_seconds: float | None,
        failure_code: str,
    ) -> None:
        with self._lock:
            database = self._db()
            try:
                database.execute(
                    "UPDATE sandbox_runs SET outcome = ?, actual_seconds = ?, settled_at = ?,"
                    " failure_code = ? WHERE reservation_id = ? AND outcome = 'reserved'",
                    (
                        outcome,
                        actual_seconds,
                        datetime.now(UTC).isoformat(),
                        failure_code,
                        reservation.reservation_id,
                    ),
                )
            except sqlite3.Error as error:
                raise SandboxLedgerCorrupt(str(error)) from error
            finally:
                database.close()

    def committed_seconds(self, day: str | None = None) -> float:
        return self._day(day)[0]

    def committed_runs(self, day: str | None = None) -> int:
        return self._day(day)[1]

    def remaining_seconds(self, day: str | None = None) -> float:
        return max(0.0, self._budget.daily_seconds - self._day(day)[0])

    def remaining_runs(self, day: str | None = None) -> int:
        return max(0, self._budget.daily_runs - self._day(day)[1])

    def _day(self, day: str | None) -> tuple[float, int]:
        database = self._db()
        try:
            return self._totals(database, day or self._today())
        finally:
            database.close()
