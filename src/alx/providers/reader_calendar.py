"""The one calendar of BHL reader sessions, durable across restarts (D-039).

A refresh replaces the whole calendar in one transaction: it is always one
consistent snapshot of every reader, never a mixture of old and new. Sessions
carry presenters' names, so any that ended more than the retention period ago
are removed whenever the calendar is written or read.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from alx.contracts.readers import ReaderSession


SESSION_RETENTION = timedelta(days=30)


class SQLiteReaderCalendar:
    def __init__(self, path: str | Path) -> None:
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.RLock()
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS calendar (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "refreshed_at TEXT NOT NULL, problems_json TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS readers (reader_uid TEXT PRIMARY KEY, "
                "summary_json TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS sessions (reader_uid TEXT NOT NULL, "
                "event_id INTEGER NOT NULL, room TEXT NOT NULL, mode INTEGER NOT NULL, "
                "starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, title TEXT NOT NULL, "
                "first_name TEXT NOT NULL, last_name TEXT NOT NULL, hbd INTEGER NOT NULL, "
                "offset_hours INTEGER NOT NULL, PRIMARY KEY (reader_uid, event_id))"
            )
            # D-040: the last schedule each reader accepted, kept apart from
            # the calendar so a refresh never forgets what a reader holds.
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS sent_schedules (reader_uid TEXT PRIMARY KEY, "
                "version TEXT NOT NULL, sent_at TEXT NOT NULL, event_ids_json TEXT NOT NULL, "
                "fingerprints_json TEXT NOT NULL DEFAULT '[]')"
            )
            # D-043: every step taken with the readers, for AL/X to account for.
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS reader_log (at TEXT NOT NULL, "
                "reader_uid TEXT NOT NULL, kind TEXT NOT NULL, detail_json TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS reader_log_at ON reader_log(at)"
            )
            # The BHL tile looks up one reader's steps every few seconds.
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS reader_log_reader ON reader_log(reader_uid, kind)"
            )
            columns = {row[1] for row in self._connection.execute(
                "PRAGMA table_info(sent_schedules)")}
            if "fingerprints_json" not in columns:
                # A record from before this column confirms nothing.
                self._connection.execute(
                    "ALTER TABLE sent_schedules ADD COLUMN "
                    "fingerprints_json TEXT NOT NULL DEFAULT '[]'"
                )

    def replace(
        self,
        refreshed_at: datetime,
        readers: Sequence[Mapping[str, Any]],
        sessions: Sequence[ReaderSession],
        problems: Sequence[str],
    ) -> None:
        cutoff = refreshed_at - SESSION_RETENTION
        with self._lock, self._connection:
            self._connection.execute("DELETE FROM calendar")
            self._connection.execute("DELETE FROM readers")
            self._connection.execute("DELETE FROM sessions")
            self._connection.execute(
                "INSERT INTO calendar(id, refreshed_at, problems_json) VALUES (1, ?, ?)",
                (refreshed_at.isoformat(), json.dumps(list(problems))),
            )
            self._connection.executemany(
                "INSERT INTO readers(reader_uid, summary_json) VALUES (?, ?)",
                ((str(item["reader_uid"]), json.dumps(dict(item))) for item in readers),
            )
            self._connection.executemany(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (s.reader_uid, s.event_id, s.room, s.mode, s.starts_at.isoformat(),
                     s.ends_at.isoformat(), s.title, s.first_name, s.last_name, s.hbd,
                     s.offset_hours)
                    for s in sessions if s.ends_at >= cutoff
                ),
            )

    def snapshot(
        self, as_of: datetime
    ) -> tuple[str, tuple[str, ...], tuple[Mapping[str, Any], ...], tuple[ReaderSession, ...]]:
        """(refreshed_at, problems, readers, sessions), expired sessions removed."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM sessions WHERE ends_at < ?",
                ((as_of - SESSION_RETENTION).isoformat(),),
            )
            row = self._connection.execute(
                "SELECT refreshed_at, problems_json FROM calendar WHERE id = 1"
            ).fetchone()
            readers = tuple(
                json.loads(summary) for (summary,) in self._connection.execute(
                    "SELECT summary_json FROM readers ORDER BY reader_uid"
                )
            )
            sessions = tuple(
                ReaderSession(
                    r[0], r[1], r[2], r[3], datetime.fromisoformat(r[4]),
                    datetime.fromisoformat(r[5]), r[6], r[7], r[8], r[9], r[10],
                )
                for r in self._connection.execute(
                    "SELECT * FROM sessions ORDER BY starts_at, reader_uid"
                )
            )
        if row is None:
            return "", (), readers, sessions
        return row[0], tuple(json.loads(row[1])), readers, sessions

    def record_sent(
        self, reader_uid: str, version: str, sent_at: datetime, event_ids: Sequence[int],
        held: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        """Record what the reader accepted: per event, its fingerprint and window end.

        Stored with a format marker, so a record written before held events
        were recorded (a bare list) is told apart from a genuinely empty day.
        """
        record = {"format": 2, "held": [dict(item) for item in held]}
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR REPLACE INTO sent_schedules "
                "(reader_uid, version, sent_at, event_ids_json, fingerprints_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (reader_uid, version, sent_at.isoformat(), json.dumps(list(event_ids)),
                 json.dumps(record)),
            )

    def sent(self, reader_uid: str) -> Mapping[str, Any]:
        """The last schedule the reader accepted, or empty.

        `held` is None for a record from before held events were recorded.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT version, sent_at, event_ids_json, fingerprints_json FROM sent_schedules "
                "WHERE reader_uid = ?", (reader_uid,),
            ).fetchone()
        if row is None:
            return {}
        stored = json.loads(row[3])
        held = (tuple(stored.get("held") or ())
                if isinstance(stored, dict) and stored.get("format") == 2 else None)
        return {"version": row[0], "sent_at": row[1], "event_ids": tuple(json.loads(row[2])),
                "held": held}

    def log(self, at: datetime, reader_uid: str, kind: str,
            detail: Mapping[str, Any] | None = None) -> None:
        """Record one step taken with a reader. Kept for the retention period."""
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM reader_log WHERE at < ?", ((at - SESSION_RETENTION).isoformat(),)
            )
            self._connection.execute(
                "INSERT INTO reader_log VALUES (?, ?, ?, ?)",
                (at.isoformat(), reader_uid, kind, json.dumps(dict(detail or {}), default=str)),
            )

    def log_between(self, start: datetime, end: datetime, reader_uid: str = "",
                    limit: int = 1000) -> tuple[tuple[dict[str, Any], ...], bool]:
        """Steps in [start, end), oldest first, and whether more were cut off."""
        query = ("SELECT at, reader_uid, kind, detail_json FROM reader_log "
                 "WHERE at >= ? AND at < ?")
        params: list[Any] = [start.isoformat(), end.isoformat()]
        if reader_uid:
            query += " AND reader_uid = ?"
            params.append(reader_uid)
        # Written order, which is the order things happened: `at` is when a
        # step was observed and can trail a step logged before it.
        query += " ORDER BY rowid LIMIT ?"
        params.append(limit + 1)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        steps = tuple({"at": r[0], "reader_uid": r[1], "kind": r[2],
                       "detail": json.loads(r[3])} for r in rows[:limit])
        return steps, len(rows) > limit

    def log_latest(self, reader_uid: str, limit: int) -> tuple[dict[str, Any], ...]:
        """One reader's last `limit` steps, oldest first."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT at, reader_uid, kind, detail_json FROM reader_log "
                "WHERE reader_uid = ? ORDER BY rowid DESC LIMIT ?", (reader_uid, limit),
            ).fetchall()
        return tuple({"at": r[0], "reader_uid": r[1], "kind": r[2],
                      "detail": json.loads(r[3])} for r in reversed(rows))

    def log_started(self, reader_uid: str, kind: str, cleared_by: str = "") -> str | None:
        """When a condition logged as `kind` began, or None if it never was.

        With `cleared_by`, the first `kind` step after the last `cleared_by`
        step (the first send attempt since the last confirmed schedule);
        without, the last `kind` step (each is logged once, when it begins).
        """
        with self._lock:
            if cleared_by:
                row = self._connection.execute(
                    "SELECT at FROM reader_log WHERE reader_uid = ? AND kind = ? AND rowid > "
                    "COALESCE((SELECT MAX(rowid) FROM reader_log WHERE reader_uid = ? "
                    "AND kind = ?), 0) ORDER BY rowid LIMIT 1",
                    (reader_uid, kind, reader_uid, cleared_by),
                ).fetchone()
            else:
                row = self._connection.execute(
                    "SELECT at FROM reader_log WHERE reader_uid = ? AND kind = ? "
                    "ORDER BY rowid DESC LIMIT 1", (reader_uid, kind),
                ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._connection.close()
