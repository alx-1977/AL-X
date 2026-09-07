"""Durable record of external work AL/X is waiting on.

An outstanding task survives a restart, because the service it was handed to
does not stop working when the process does. Without this, closing the browser
would lose the fact that a review was ever requested, and the result would
arrive with nothing waiting to notice it.

This stores state and timestamps. It holds no result, no findings and no
service output: what a result says is read by the Core from the source, not
carried through here.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from alx.contracts.task import ExternalTask, TaskState

class TaskStoreCorrupt(Exception):
    """Durable task state could not be read or written.

    Raised rather than assuming there is nothing outstanding: a store that
    cannot be trusted must stop the watcher, because the alternative is
    silently forgetting work that is still running.
    """


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


class SQLiteTaskStore:
    """Outstanding external tasks, by identifier."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._lock = Lock()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            database = self._db()
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_tasks (
                    task_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    service TEXT NOT NULL,
                    subject_reference TEXT NOT NULL,
                    state TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    completed_at TEXT,
                    conversation_id TEXT NOT NULL DEFAULT '',
                    handed_over INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS external_tasks_state
                    ON external_tasks(state);
                """
            )
            # A store created before this column existed keeps its rows.
            # `CREATE TABLE IF NOT EXISTS` leaves an existing table alone, so
            # without this the handover query fails on every older database
            # and a completed task is never given to the Core - the same
            # silent gap the handover exists to close.
            #
            # Existing rows default to 0: nothing recorded before this column
            # existed had been handed over, because there was nothing to hand
            # it to.
            columns = {
                item[1]
                for item in database.execute("PRAGMA table_info(external_tasks)")
            }
            if "handed_over" not in columns:
                database.execute(
                    "ALTER TABLE external_tasks "
                    "ADD COLUMN handed_over INTEGER NOT NULL DEFAULT 0"
                )
            database.commit()
            database.close()
        except sqlite3.Error as error:
            raise TaskStoreCorrupt(str(error)) from error

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False, timeout=10.0
        )

    def _connect(self) -> sqlite3.Connection:
        try:
            return self._db()
        except sqlite3.Error as error:
            raise TaskStoreCorrupt(str(error)) from error

    def record(self, task: ExternalTask) -> None:
        """Write one task, replacing any earlier state for the same id."""
        with self._lock:
            database = self._connect()
            try:
                database.execute(
                    """
                    INSERT INTO external_tasks (task_id, kind, service,
                        subject_reference, state, requested_at, last_checked_at,
                        completed_at, conversation_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        kind = excluded.kind,
                        service = excluded.service,
                        subject_reference = excluded.subject_reference,
                        state = excluded.state,
                        requested_at = excluded.requested_at,
                        last_checked_at = excluded.last_checked_at,
                        completed_at = excluded.completed_at,
                        conversation_id = excluded.conversation_id,
                        handed_over = 0
                    """,
                    (
                        task.task_id,
                        task.kind,
                        task.service,
                        task.subject_reference,
                        task.state.value,
                        task.requested_at.isoformat(),
                        None if task.last_checked_at is None
                        else task.last_checked_at.isoformat(),
                        None if task.completed_at is None
                        else task.completed_at.isoformat(),
                        task.conversation_id,
                    ),
                )
            except sqlite3.Error as error:
                raise TaskStoreCorrupt(str(error)) from error
            finally:
                database.close()

    def completed_unhandled(self) -> tuple[ExternalTask, ...]:
        """Completed tasks whose completion the Core has not yet been given.

        Completion is durable, so the handover survives a restart: a process
        that stopped between noticing a result and running the turn finds the
        completion still waiting rather than losing it.
        """
        with self._lock:
            database = self._connect()
            try:
                rows = database.execute(
                    """
                    SELECT task_id, kind, service, subject_reference, state,
                           requested_at, last_checked_at, completed_at,
                           conversation_id
                    FROM external_tasks
                    WHERE state = ? AND handed_over = 0
                    ORDER BY completed_at
                    """,
                    (TaskState.COMPLETED.value,),
                ).fetchall()
            except sqlite3.Error as error:
                raise TaskStoreCorrupt(str(error)) from error
            finally:
                database.close()
        return self._valid_tasks(rows)

    def mark_handed_over(self, task_id: str) -> None:
        """Record that the Core has been given this completion."""
        with self._lock:
            database = self._connect()
            try:
                database.execute(
                    "UPDATE external_tasks SET handed_over = 1 WHERE task_id = ?",
                    (task_id,),
                )
            except sqlite3.Error as error:
                raise TaskStoreCorrupt(str(error)) from error
            finally:
                database.close()

    def outstanding(self) -> tuple[ExternalTask, ...]:
        """Every task still worth watching, oldest first."""
        with self._lock:
            database = self._connect()
            try:
                rows = database.execute(
                    """
                    SELECT task_id, kind, service, subject_reference, state,
                           requested_at, last_checked_at, completed_at,
                           conversation_id
                    FROM external_tasks
                    WHERE state NOT IN (?, ?)
                    ORDER BY requested_at
                    """,
                    (TaskState.COMPLETED.value, TaskState.FAILED.value),
                ).fetchall()
            except sqlite3.Error as error:
                raise TaskStoreCorrupt(str(error)) from error
            finally:
                database.close()
        return self._valid_tasks(rows)

    @classmethod
    def _valid_tasks(cls, rows: list[tuple]) -> tuple[ExternalTask, ...]:
        tasks: list[ExternalTask] = []
        for row in rows:
            try:
                tasks.append(cls._task(row))
            except (TypeError, ValueError) as error:
                task_id = row[0] if row else "<missing>"
                raise TaskStoreCorrupt(
                    f"corrupt external task row {task_id!r}: {type(error).__name__}"
                ) from error
        return tuple(tasks)

    @staticmethod
    def _task(row: tuple) -> ExternalTask:
        return ExternalTask(
            task_id=row[0],
            kind=row[1],
            service=row[2],
            subject_reference=row[3],
            state=TaskState(row[4]),
            requested_at=datetime.fromisoformat(row[5]),
            last_checked_at=_moment(row[6]),
            completed_at=_moment(row[7]),
            conversation_id=row[8],
        )
