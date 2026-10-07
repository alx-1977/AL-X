"""The BHL tile: present whenever the reader calendar has an event today (D-041).

Friedl asked for a standing reminder that a BHL event day is under way, so
he does not have to remember it, small enough to stay out of the way. The
tile is a view of facts AL/X already holds, coloured by rules Friedl set
(D-041, amended 2026-10-07):

- red: a reader whose event is running, or starts within 30 minutes, is
  offline;
- yellow: anything else not confirmed: a reader offline with no event close,
  a reader that has not accepted today's remaining events from AL/X exactly
  as the calendar now has them, or
  schedules that could not be read from BehaviorLive for 15 minutes;
- green: every reader in use today is online and holds today's schedule, and
  the schedules are current.

It makes no judgement beyond those rules; what to do about a problem remains
AL/X's. "Today" is the event's own day, taken in its schedule's offset. Times
shown are in this machine's local time, Friedl's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any

from alx.contracts.readers import ReaderSession, session_fingerprint


# Friedl's rules, 2026-10-07.
WARNING_BEFORE_EVENT = timedelta(minutes=30)
LINK_STALE_AFTER = timedelta(minutes=15)
# Until AL/X watches the readers herself (the health check), the tile says so.
ALX_ACTIVITY = {"text": "not monitoring yet", "idle": True}


def _event_day(moment: datetime, offset_hours: int):
    return (moment + timedelta(hours=offset_hours)).date()


def _clock(moment: datetime, local: tzinfo | None) -> str:
    return moment.astimezone(local).strftime("%H:%M")


def _count(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _holds_today(record: Mapping[str, Any], remaining: Sequence[ReaderSession]) -> bool:
    """The reader accepted from AL/X every event it has left, exactly as the calendar has it.

    Compared by content, not ID: an event moved or renamed under the same ID
    since it was sent is not confirmed.
    """
    return {session_fingerprint(item) for item in remaining} <= set(record.get("fingerprints") or ())


def compose_tile(
    snapshot: tuple[str, Sequence[str], Sequence[Mapping[str, Any]], Sequence[ReaderSession]],
    now: datetime,
    local: tzinfo | None = None,
    sent: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """The tile's data, or None when the calendar has no event today."""
    refreshed_at, _problems, readers, sessions = snapshot
    if not refreshed_at:
        return None
    today = [item for item in sessions
             if _event_day(item.starts_at, item.offset_hours) == _event_day(now, item.offset_hours)]
    if not today:
        return None
    sent = sent or {}

    # One event is held in a room; each of its readers carries it.
    events: dict[tuple[str, int], ReaderSession] = {}
    for item in sorted(today, key=lambda value: (value.starts_at, value.reader_uid)):
        events.setdefault((item.room, item.event_id), item)
    running = [item for item in events.values() if item.starts_at <= now < item.ends_at]
    upcoming = [item for item in events.values() if item.starts_at > now]

    in_use = sorted({item.reader_uid for item in today})
    summary = {item.get("reader_uid"): item for item in readers}
    offline = [uid for uid in in_use if summary.get(uid, {}).get("online") is not True]
    critical = [
        uid for uid in offline
        if any(item.reader_uid == uid and item.starts_at - WARNING_BEFORE_EVENT <= now < item.ends_at
               for item in today)
    ]
    unconfirmed = [
        uid for uid in in_use
        if not _holds_today(sent.get(uid) or {}, [
            item for item in today if item.reader_uid == uid and item.ends_at > now])
    ]
    read_times = [datetime.fromisoformat(summary.get(uid, {}).get("schedule_as_of") or refreshed_at)
                  for uid in in_use]
    last_read = min([datetime.fromisoformat(refreshed_at), *read_times])
    link_stale = now - last_read > LINK_STALE_AFTER

    if critical:
        tone = "bad"
    elif offline or unconfirmed or link_stale:
        tone = "warn"
    else:
        tone = "ok"

    if offline:
        title = f"{_count(len(offline), 'room reader')} offline"
    elif link_stale:
        title = "BHL link not answering"
    elif unconfirmed:
        title = "Schedules not confirmed"
    else:
        title = "All systems normal"

    rooms_running = sorted({item.room for item in running})
    if len(running) == 1:
        detail = f"Event under way until {_clock(running[0].ends_at, local)}"
    elif running:
        detail = f"{_count(len(rooms_running), 'room')} in session"
    else:
        detail = ""
    if upcoming:
        start = upcoming[0].starts_at
        starting = sorted({item.room for item in upcoming if item.starts_at == start})
        started = any(item.starts_at <= now for item in events.values())
        lead = "next starts" if running else ("Next event at" if started else "First event at")
        part = f"{lead} {_clock(start, local)}"
        if len(starting) > 1:
            part += f" ({len(starting)} rooms)"
        detail = f"{detail} · {part}" if detail else part
    elif not running:
        detail = "Today's events have finished"
    if unconfirmed and title != "Schedules not confirmed":
        detail += " · schedules not confirmed"

    rooms = sorted({item.room for item in events.values() if item.room})
    where = ", ".join(rooms) if len(rooms) <= 2 else _count(len(rooms), "room")
    return {
        "tone": tone,
        "name": "BHL",
        "context": f"{_count(len(events), 'event')} today · {where}",
        "state": {"title": title, "detail": detail},
        "alx": dict(ALX_ACTIVITY),
        "chips": [
            {"icon": "reader", "value": f"{len(in_use) - len(offline)}/{len(in_use)}",
             "tone": "bad" if critical else ("warn" if offline else "ok"),
             "label": "Room readers online"},
            {"icon": "link", "value": "BHL link", "tone": "warn" if link_stale else "ok",
             "label": f"Schedules last read from BehaviorLive at {_clock(last_read, local)}"},
        ],
    }


def tile_source(
    calendar: Any, clock: Callable[[], datetime], local: tzinfo | None = None
) -> Callable[[], dict[str, Any] | None]:
    def current() -> dict[str, Any] | None:
        at = clock()
        snapshot = calendar.snapshot(at)
        sent = {item["reader_uid"]: calendar.sent(item["reader_uid"]) for item in snapshot[2]}
        return compose_tile(snapshot, at, local, sent)

    return current
