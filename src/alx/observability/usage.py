"""Durable per-call reasoning usage and a runaway-reasoning guardrail.

Token usage was previously streamed to the development panel and discarded, so
reasoning spend was invisible after the fact. This records each call and its
task rollup, and refuses to keep reasoning once a routine task has clearly run
away.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Mapping


# Cost is only reported for models whose price is actually known. An unpriced
# model records tokens and reports no cost rather than inventing a number.
USD_PER_MILLION: dict[str, tuple[float, float, float]] = {}


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """Reasoning-call limits for one kind of task."""

    expected: int
    warn_above: int
    stop_above: int
    # Declared recovery buys a bounded allowance, never an open budget. No
    # execution path may reason without a ceiling.
    recovery_allowance: int = 2

    def __post_init__(self) -> None:
        if not 0 < self.expected <= self.warn_above <= self.stop_above:
            raise ValueError("budget must be 0 < expected <= warn_above <= stop_above")
        if self.recovery_allowance <= 0:
            raise ValueError("recovery_allowance must be positive")

    @property
    def recovery_limit(self) -> int:
        return self.stop_above + self.recovery_allowance


# A routine supplier bill is one or two Core reasoning calls: one to start it
# and one to report. Anything beyond four is not a slow success, it is a loop.
# Specialist calls are counted separately and are not subject to this ceiling.
XERO_BILL_BUDGET = ExecutionBudget(expected=2, warn_above=2, stop_above=4)


class BudgetExceeded(Exception):
    """Raised to stop a task that has exceeded its reasoning-call ceiling."""

    def __init__(self, task_id: str, calls: int, limit: int) -> None:
        self.task_id = task_id
        self.calls = calls
        self.limit = limit
        super().__init__(f"{task_id}: {calls} reasoning calls exceeds limit {limit}")


class SQLiteUsageRecorder:
    """Record reasoning usage per call and per task, and enforce a ceiling."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = Lock()
        self._budgets: dict[str, ExecutionBudget] = {}
        # Call id the ceiling starts counting from, per task.
        self._windows: dict[str, int] = {}
        # Tasks whose work finished, so the next one starts a fresh window.
        self._settled: dict[str, bool] = {}
        self._recovery: set[str] = set()
        database = self._db()
        try:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS reasoning_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    service_tier TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    cache_write_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    reasoning_tokens INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'core',
                    tier TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS reasoning_calls_task
                    ON reasoning_calls(task_id);
                """
            )
            # Existing databases predate these columns. Adding them here keeps
            # one schema rather than a second recorder for research calls.
            existing = {
                str(row["name"])
                for row in database.execute("PRAGMA table_info(reasoning_calls)")
            }
            if "kind" not in existing:
                database.execute(
                    "ALTER TABLE reasoning_calls ADD COLUMN kind TEXT NOT NULL "
                    "DEFAULT 'core'"
                )
            if "tier" not in existing:
                database.execute(
                    "ALTER TABLE reasoning_calls ADD COLUMN tier TEXT NOT NULL "
                    "DEFAULT ''"
                )
            database.commit()
        finally:
            database.close()

    def _db(self) -> sqlite3.Connection:
        database = sqlite3.connect(str(self._path), timeout=30)
        database.row_factory = sqlite3.Row
        return database

    def set_budget(self, task_id: str, budget: ExecutionBudget) -> None:
        """Declare that a task is routine work with a known call ceiling.

        A conversation is not a task. It may already carry hours of unrelated
        reasoning, and counting that against a bill's ceiling stopped a bill
        mid-task at 26 calls when only five belonged to it. The window opens
        here: only calls recorded from this point count.
        """
        with self._lock:
            if task_id in self._budgets and not self._settled.get(task_id, False):
                # An unfinished task keeps its window, so reaching for a second
                # capability within one bill does not hand it a fresh ceiling.
                # Its recovery state is kept for the same reason: a task that
                # was stopped and is now recovering must not have that state
                # cleared, in either direction, by selecting another
                # capability. Only completing the work opens a new window.
                self._budgets[task_id] = budget
                return
            self._settled.pop(task_id, None)
            self._budgets[task_id] = budget
            self._windows[task_id] = self._window_start(task_id)
            self._recovery.discard(task_id)

    def _window_start(self, task_id: str) -> int:
        """Open the window before the call that reached for the capability.

        Arming happens while dispatching, so the reasoning call that chose to
        act has already been recorded. That call is the first step of the bill
        task and must count against its ceiling.
        """
        database = self._db()
        try:
            latest = database.execute(
                "SELECT COALESCE(MAX(id), 0) FROM reasoning_calls "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        finally:
            database.close()
        return max(0, int(latest) - 1)

    def settle(self, task_id: str) -> None:
        """Mark the budgeted work complete so the next one counts afresh.

        Two invoices processed in one conversation shared a single ceiling, so
        the second was refused for the first one's calls. A completed piece of
        work closes its window; the next capability call opens a new one.
        """
        with self._lock:
            if task_id in self._budgets:
                self._settled[task_id] = True

    def enter_recovery(self, task_id: str) -> None:
        """Record that AL/X is handling ambiguity or a failure on this task.

        Recovery is genuine extra work, so it buys a bounded allowance of
        further calls rather than an open budget. Only the Core may declare it;
        a provider or tool cannot lift its own limit.

        Declaring it twice grants nothing further: the allowance is a property
        of the one window, not a credit issued per call. That is what stops a
        stopped task from reasoning indefinitely by re-entering recovery.
        """
        with self._lock:
            if task_id in self._budgets:
                self._recovery.add(task_id)

    # Core, specialist and research all spend reasoning tokens and all belong
    # in one table. A separate recorder per kind would make total spend
    # unanswerable without joining two stores.
    RECORDED_CODES = ("reasoning.completed", "research.completed")

    def record(self, task_id: str, values: Mapping[str, Any]) -> None:
        if values.get("code") not in self.RECORDED_CODES or not task_id.strip():
            return
        database = self._db()
        try:
            database.execute(
                "INSERT INTO reasoning_calls(task_id, occurred_at, provider, model, "
                "reasoning_effort, service_tier, input_tokens, cached_tokens, "
                "cache_write_tokens, output_tokens, reasoning_tokens, duration_ms, "
                "kind, tier) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    datetime.now(UTC).isoformat(),
                    str(values.get("provider") or ""),
                    str(values.get("model") or ""),
                    str(values.get("reasoning_effort") or ""),
                    str(values.get("service_tier") or ""),
                    int(values.get("input_tokens") or 0),
                    int(values.get("cached_tokens") or 0),
                    int(values.get("cache_write_tokens") or 0),
                    int(values.get("output_tokens") or 0),
                    int(values.get("reasoning_tokens") or 0),
                    int(values.get("duration_ms") or 0),
                    # Core, specialist and research spend the same tokens, so
                    # only the caller can say which kind of work this was.
                    str(values.get("kind") or "core"),
                    str(values.get("tier") or ""),
                ),
            )
            database.commit()
        finally:
            database.close()

    def task(self, task_id: str) -> dict[str, Any]:
        """Return the rollup for one task."""
        database = self._db()
        try:
            row = database.execute(
                "SELECT COUNT(*) AS calls, "
                "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
                "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
                "COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens, "
                "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens "
                "FROM reasoning_calls WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            rows = database.execute(
                "SELECT DISTINCT provider, model FROM reasoning_calls "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchall()
            models = tuple(str(item["model"]) for item in rows)
            providers = tuple(sorted({str(item["provider"]) for item in rows}))
        finally:
            database.close()
        values = {"task_id": task_id, **{key: row[key] for key in row.keys()}}
        values["models"] = models
        values["providers"] = providers
        values["estimated_usd"] = self._cost(values, models)
        with self._lock:
            budget = self._budgets.get(task_id)
            recovering = task_id in self._recovery
            since = self._windows.get(task_id, 0)
        # The ceiling applies to the budgeted window, not the whole task.
        budgeted = (
            values["calls"] if budget is None else self._calls_since(task_id, since)
        )
        values["budgeted_calls"] = budgeted
        values["budget_state"] = self._state(budgeted, budget, recovering)
        return values

    def _calls_since(self, task_id: str, since: int) -> int:
        database = self._db()
        try:
            return int(
                database.execute(
                    "SELECT COUNT(*) FROM reasoning_calls "
                    "WHERE task_id = ? AND id > ?",
                    (task_id, since),
                ).fetchone()[0]
            )
        finally:
            database.close()

    @staticmethod
    def _cost(values: Mapping[str, Any], models: tuple[str, ...]) -> float | None:
        if len(models) != 1 or models[0] not in USD_PER_MILLION:
            return None
        uncached_price, cached_price, output_price = USD_PER_MILLION[models[0]]
        uncached = max(0, values["input_tokens"] - values["cached_tokens"])
        return round(
            uncached / 1e6 * uncached_price
            + values["cached_tokens"] / 1e6 * cached_price
            + values["output_tokens"] / 1e6 * output_price,
            6,
        )

    @staticmethod
    def _state(
        calls: int, budget: ExecutionBudget | None, recovering: bool
    ) -> str:
        if budget is None:
            return "unbudgeted"
        if recovering:
            # Matches check(): the next call is refused once calls reach the limit.
            return "stopped" if calls >= budget.recovery_limit else "recovering"
        if calls > budget.stop_above:
            return "stopped"
        if calls > budget.warn_above:
            return "warning"
        return "expected"

    def check(self, task_id: str) -> None:
        """Raise before another reasoning call when a task has run away.

        Called before dispatching, so exceeding the ceiling prevents further
        spend rather than merely recording that it happened.
        """
        with self._lock:
            budget = self._budgets.get(task_id)
            if budget is None:
                return
            if self._settled.get(task_id, False):
                # The work finished. Its ceiling stopped that task running
                # away; it must not also stop AL/X saying what she did. The
                # next capability call opens a new window.
                return
            recovering = task_id in self._recovery
            since = self._windows.get(task_id, 0)
        calls = self._calls_since(task_id, since)
        limit = budget.recovery_limit if recovering else budget.stop_above
        if calls >= limit:
            raise BudgetExceeded(task_id, calls, limit)

    def sink(self, task_id: str, values: Mapping[str, Any]) -> None:
        """Telemetry-sink signature, so the provider needs no new wiring."""
        self.record(task_id, values)
