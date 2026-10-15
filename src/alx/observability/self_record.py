"""What AL/X can read back about her own running (D-050).

Two records, both already free of content by construction:

- her execution trace (the operator trace on Friedl's screen: each step's
  subsystem, status, label, capability or goal reference, reason code and
  duration), kept here for 30 days instead of existing only on a screen;
- her runtime log, read back from its file with anything shaped like a key or
  token masked, in case a line ever carries one.

Friedl, 2026-10-15: "ALX should be self sustaining." She could not see that
two of her own notebook searches had been refused, because the only record
was the trace on his screen. This lets her look.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any

TRACE_RETENTION = timedelta(days=30)
MAX_RECORDS = 500
PROBLEM_STATUSES = ("failed", "refused")
_TRACE_FIELDS = ("subsystem", "status", "label", "reference", "reason_code",
                 "duration_ms", "count")


class SQLiteTraceRecord:
    """Her execution trace, kept for TRACE_RETENTION."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS trace (at TEXT NOT NULL, subsystem TEXT, status TEXT, "
            "label TEXT, reference TEXT, reason_code TEXT, duration_ms INTEGER, count INTEGER)")
        self._connection.execute("CREATE INDEX IF NOT EXISTS trace_at ON trace (at)")
        self._connection.commit()

    def record(self, event: dict[str, Any]) -> None:
        """Keep one published trace event (code "trace"); anything else is ignored."""
        if event.get("code") != "trace":
            return
        at = _moment(event.get("at")) or datetime.now(UTC)
        with self._lock:
            self._connection.execute(
                "INSERT INTO trace VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (at.astimezone(UTC).isoformat(timespec="milliseconds"),
                 *(event.get(name) for name in _TRACE_FIELDS)))
            self._connection.execute(
                "DELETE FROM trace WHERE at < ?",
                ((datetime.now(UTC) - TRACE_RETENTION).isoformat(),))
            self._connection.commit()

    def between(self, start: datetime, end: datetime, problems_only: bool = False,
                subsystem: str = "", limit: int = MAX_RECORDS) -> tuple[list[dict], bool]:
        """Steps in [start, end), oldest first, and whether more were cut off."""
        query = "SELECT at, " + ", ".join(_TRACE_FIELDS) + " FROM trace WHERE at >= ? AND at < ?"
        params: list[Any] = [start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()]
        if problems_only:
            query += " AND status IN (?, ?)"
            params.extend(PROBLEM_STATUSES)
        if subsystem:
            query += " AND subsystem = ?"
            params.append(subsystem)
        query += " ORDER BY rowid LIMIT ?"
        params.append(limit + 1)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        steps = [
            {key: value for key, value in zip(("at", *_TRACE_FIELDS), row) if value is not None}
            for row in rows[:limit]
        ]
        return steps, len(rows) > limit

    def close(self) -> None:
        with self._lock:
            self._connection.close()


# ---- the runtime log -------------------------------------------------------

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} (\w+) ([\w.]+): (.*)$")
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# Anything shaped like a credential is masked before she sees the line, and
# ordinary text, paths included, is left alone.
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[^\s\"',]+")
_ASSIGNED = re.compile(
    r"(?i)\b(token|api[_-]?key|access[_-]?key|secret[_-]?key|secret|password|signature|"
    r"authorization)(\s*[=:]\s*[\"']?)(?!\[masked\])([^\s\"',&]+)")
_PREFIXED = re.compile(r"\b(sk|xi|ghp|gho|ghs|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{8,}")
# A long unbroken run of letters and digits (no slash, so never a path).
_LONG = re.compile(r"(?<![/\w])[A-Za-z0-9+_\-]{40,}={0,2}(?![/\w])")
MAX_LINE_CHARACTERS = 400


def _mask(text: str) -> str:
    text = _BEARER.sub(lambda match: f"{match.group(1)} [masked]", text)
    text = _ASSIGNED.sub(lambda match: f"{match.group(1)}{match.group(2)}[masked]", text)
    text = _PREFIXED.sub("[masked]", text)
    return _LONG.sub("[masked]", text)


def read_log(paths: tuple[Path, ...], start: datetime, end: datetime,
             minimum_level: str = "INFO", contains: str = "",
             limit: int = MAX_RECORDS) -> tuple[list[dict], bool]:
    """Log entries in [start, end) (local time, as the log writes it), oldest first.

    A traceback's lines belong to the entry above them. Each entry is masked
    and shortened; `truncated` says more matched than `limit`.
    """
    threshold = _LEVELS.get(minimum_level.upper(), 20)
    local_start = start.astimezone().replace(tzinfo=None)
    local_end = end.astimezone().replace(tzinfo=None)
    entries: list[dict] = []
    current: dict | None = None

    def keep(entry: dict | None) -> None:
        if entry is None:
            return
        if _LEVELS.get(entry["level"], 20) < threshold:
            return
        if contains and contains.lower() not in entry["message"].lower():
            return
        entry["message"] = _mask(entry["message"])[:MAX_LINE_CHARACTERS * 4]
        entries.append(entry)

    for path in paths:
        if not path.exists():
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                match = _LINE.match(line)
                if match is None:
                    if current is not None and line.strip():
                        current["message"] += "\n" + line[:MAX_LINE_CHARACTERS]
                    continue
                keep(current)
                current = None
                at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                if not local_start <= at < local_end:
                    continue
                current = {"at": match.group(1), "level": match.group(2),
                           "source": match.group(3), "message": match.group(4)[:MAX_LINE_CHARACTERS]}
        keep(current)
        current = None
    return entries[-limit:], len(entries) > limit


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = ["MAX_RECORDS", "PROBLEM_STATUSES", "SQLiteTraceRecord", "read_log"]
