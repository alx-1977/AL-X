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
import logging
import time
from logging.handlers import TimedRotatingFileHandler
import sqlite3
from collections import deque
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from threading import Lock
from typing import Any

from alx.contracts.diagnostics import mask_credentials as _mask

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
                subsystem: str = "", limit: int = MAX_RECORDS,
                zone: tzinfo | None = None) -> tuple[list[dict], bool]:
        """Steps in [start, end), oldest first, and whether more were cut off.

        `at` is given in local time with its offset (`zone`, the machine's own
        unless a test names one). Stored in UTC, it was returned in UTC, and on
        2026-10-17 AL/X read her 10:09 own time as 08:09 and told Friedl it had
        not fired: everything else she sees is local.
        """
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
        for step in steps:
            moment = _moment(step["at"])
            if moment is not None:
                step["at"] = moment.astimezone(zone).isoformat(timespec="seconds")
        return steps, len(rows) > limit

    def close(self) -> None:
        with self._lock:
            self._connection.close()


# ---- the runtime log -------------------------------------------------------

_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} (\w+) ([\w.]+): (.*)$")
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
# Shared masking also protects diagnostics before they enter failure storage.

MAX_LINE_CHARACTERS = 400
MAX_ENTRY_CHARACTERS = 4000
LOG_RETENTION = timedelta(days=30)


def runtime_log_paths(path: Path) -> tuple[Path, ...]:
    """The runtime log, its daily archives, and process output from the launcher."""
    candidates = (
        *path.parent.glob(path.name + ".*"),
        path,
        *path.parent.glob("live_voice.process.log*"),
    )
    return tuple(sorted(
        (item for item in set(candidates) if item.is_file()),
        key=lambda item: (item.stat().st_mtime, item.name),
    ))


class RuntimeLogHandler(TimedRotatingFileHandler):
    """Daily logs with time-based retention; directory work only at startup/rotation."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, when="midnight", backupCount=0, encoding="utf-8")
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        self.prune()

    def prune(self) -> None:
        cutoff = time.time() - LOG_RETENTION.total_seconds()
        active = {Path(self.baseFilename), Path(self.baseFilename).with_name("live_voice.process.log")}
        for path in runtime_log_paths(Path(self.baseFilename)):
            if path not in active and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def doRollover(self) -> None:
        super().doRollover()
        self.prune()


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
    # Only the newest `limit` are held; the rest are counted.
    entries: deque[dict] = deque(maxlen=limit)
    matched = 0
    current: dict | None = None

    def keep(entry: dict | None) -> None:
        nonlocal matched
        if entry is None:
            return
        if _LEVELS.get(entry["level"], 20) < threshold:
            return
        if contains and contains.lower() not in entry["message"].lower():
            return
        entry["message"] = _mask(entry["message"])
        newest = sorted([*entries, entry], key=lambda item: datetime.fromisoformat(item["at"]).timestamp())[-limit:]
        entries.clear()
        entries.extend(newest)
        matched += 1

    for path in paths:
        if not path.exists():
            continue
        # Process output can precede logging entirely. Its file modification
        # time is the only available timestamp; keep that provenance explicit.
        raw_at = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
        structured = False
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                match = _LINE.match(line)
                if match is None:
                    if (current is not None and current["source"] == "process"
                            and len(current["message"]) + min(len(line), MAX_LINE_CHARACTERS) + 1
                            > MAX_ENTRY_CHARACTERS):
                        keep(current)
                        current = None
                    if not structured and current is None and line.strip() and start <= raw_at < end:
                        current = {"at": raw_at.isoformat(timespec="seconds"),
                                   "level": "ERROR", "source": "process",
                                   "timestamp_source": "file_mtime", "message": ""}
                    if (current is not None and line.strip()
                            and len(current["message"]) < MAX_ENTRY_CHARACTERS):
                        current["message"] = (current["message"] + "\n"
                                              + _mask(line)[:MAX_LINE_CHARACTERS])[:MAX_ENTRY_CHARACTERS]
                    continue
                keep(current)
                current = None
                structured = True
                at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
                if not local_start <= at < local_end:
                    continue
                # Local, as the log writes it, now with its offset stated.
                current = {"at": at.astimezone().isoformat(timespec="seconds"),
                           "level": match.group(2),
                           "source": match.group(3), "message": _mask(match.group(4))[:MAX_LINE_CHARACTERS]}
        keep(current)
        current = None
    return list(entries), matched > limit


# How many coding briefings are kept, newest first.
MAX_KEPT_BRIEFINGS = 30


def keep_briefing(directory: Path, job_id: str, text: str,
                  keep: int = MAX_KEPT_BRIEFINGS) -> Path:
    """Write one coding job's briefing, and drop all but the newest `keep`.

    The briefing is what the coding model was told: the task, the plan and,
    when the job addresses a review, the reviewer's own words. Kept in AL/X's
    runtime storage, which coding sessions cannot read.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = "".join(c if c.isalnum() or c in "._-" else "_" for c in job_id)[:120] or "job"
    path = directory / f"{name}.txt"
    path.write_text(text, encoding="utf-8")
    kept = sorted(directory.glob("*.txt"), key=lambda item: item.stat().st_mtime,
                  reverse=True)
    for old in kept[keep:]:
        old.unlink(missing_ok=True)
    return path


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = ["MAX_KEPT_BRIEFINGS", "MAX_RECORDS", "PROBLEM_STATUSES", "SQLiteTraceRecord",
           "keep_briefing", "read_log"]
