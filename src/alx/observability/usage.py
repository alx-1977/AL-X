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


# A routine supplier bill is one decision and one execution. Anything beyond
# four calls is not a slow success, it is a loop.
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
        self._recovery: set[str] = set()
        database = self._db()
        try:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS reasoning_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    reasoning_effort TEXT NOT NULL,
                    service_tier TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    cached_tokens INTEGER NOT NULL,
                    cache_write_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    reasoning_tokens INTEGER NOT NULL,
                    duration_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reasoning_calls_task
                    ON reasoning_calls(task_id);
                """
            )
            database.commit()
        finally:
            database.close()

    def _db(self) -> sqlite3.Connection:
        database = sqlite3.connect(str(self._path), timeout=30)
        database.row_factory = sqlite3.Row
        return database

    def set_budget(self, task_id: str, budget: ExecutionBudget) -> None:
        """Declare that a task is routine work with a known call ceiling."""
        with self._lock:
            self._budgets[task_id] = budget
            self._recovery.discard(task_id)

    def enter_recovery(self, task_id: str) -> None:
        """Record that AL/X is handling ambiguity or a failure on this task.

        Recovery is genuine extra work, so the ceiling no longer applies. Only
        the Core may declare it; a provider or tool cannot lift its own limit.
        """
        with self._lock:
            self._recovery.add(task_id)

    def record(self, task_id: str, values: Mapping[str, Any]) -> None:
        if values.get("code") != "reasoning.completed" or not task_id.strip():
            return
        database = self._db()
        try:
            database.execute(
                "INSERT INTO reasoning_calls(task_id, occurred_at, model, "
                "reasoning_effort, service_tier, input_tokens, cached_tokens, "
                "cache_write_tokens, output_tokens, reasoning_tokens, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    datetime.now(UTC).isoformat(),
                    str(values.get("model") or ""),
                    str(values.get("reasoning_effort") or ""),
                    str(values.get("service_tier") or ""),
                    int(values.get("input_tokens") or 0),
                    int(values.get("cached_tokens") or 0),
                    int(values.get("cache_write_tokens") or 0),
                    int(values.get("output_tokens") or 0),
                    int(values.get("reasoning_tokens") or 0),
                    int(values.get("duration_ms") or 0),
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
            models = tuple(
                str(item["model"])
                for item in database.execute(
                    "SELECT DISTINCT model FROM reasoning_calls WHERE task_id = ?",
                    (task_id,),
                ).fetchall()
            )
        finally:
            database.close()
        values = {"task_id": task_id, **{key: row[key] for key in row.keys()}}
        values["models"] = models
        values["estimated_usd"] = self._cost(values, models)
        with self._lock:
            budget = self._budgets.get(task_id)
            recovering = task_id in self._recovery
        values["budget_state"] = self._state(values["calls"], budget, recovering)
        return values

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
            recovering = task_id in self._recovery
        database = self._db()
        try:
            calls = database.execute(
                "SELECT COUNT(*) FROM reasoning_calls WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        finally:
            database.close()
        limit = budget.recovery_limit if recovering else budget.stop_above
        if calls >= limit:
            raise BudgetExceeded(task_id, calls, limit)

    def sink(self, task_id: str, values: Mapping[str, Any]) -> None:
        """Telemetry-sink signature, so the provider needs no new wiring."""
        self.record(task_id, values)
