"""The BHL tile: present whenever the reader calendar has an event today (D-041).

Friedl asked for a standing reminder that a BHL event day is under way, so
he does not have to remember it. The tile is a view of the calendar and
nothing more. It states facts the calendar already holds (which events are
today, which is running, which is next, how many of today's readers Particle
reports online, when today's schedules were last read) and makes no judgement
about whether anything is wrong; that remains AL/X's.

"Today" is the event's own day: each session's date is taken in its
schedule's offset, so an evening session abroad belongs to the day it is held
on. Times shown are in this machine's local time, Friedl's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any

from alx.contracts.readers import ReaderSession


VISUAL = {"src": "/tile-bhl-venue.jpg", "alt": ""}
NOT_MONITORED = (
    {"icon": "scanner", "label": "Registration Scanners", "tone": "disabled",
     "note": "Not monitored yet"},
    {"icon": "plug", "label": "PSUs", "tone": "disabled", "note": "In development"},
)


def _event_day(moment: datetime, offset_hours: int):
    return (moment + timedelta(hours=offset_hours)).date()


def _clock(moment: datetime | str, local: tzinfo | None) -> str:
    if isinstance(moment, str):
        moment = datetime.fromisoformat(moment)
    return moment.astimezone(local).strftime("%H:%M")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def compose_tile(
    snapshot: tuple[str, Sequence[str], Sequence[Mapping[str, Any]], Sequence[ReaderSession]],
    now: datetime,
    local: tzinfo | None = None,
) -> dict[str, Any] | None:
    """The tile's data, or None when the calendar has no event today."""
    refreshed_at, _problems, readers, sessions = snapshot
    if not refreshed_at:
        return None
    today = [item for item in sessions
             if _event_day(item.starts_at, item.offset_hours) == _event_day(now, item.offset_hours)]
    if not today:
        return None
    # One event is held in a room; each of its readers carries it.
    events: dict[tuple[str, int], ReaderSession] = {}
    for item in sorted(today, key=lambda value: (value.starts_at, value.reader_uid)):
        events.setdefault((item.room, item.event_id), item)
    running = [item for item in events.values() if item.starts_at <= now < item.ends_at]
    upcoming = [item for item in events.values() if item.starts_at > now]
    if running:
        state = {
            "title": "Event under way" if len(running) == 1
            else f"{len(running)} events under way",
            "detail": " · ".join(
                f"{item.title} · {item.room} · until {_clock(item.ends_at, local)}"
                for item in running[:2]
            ),
        }
    elif upcoming:
        first = upcoming[0]
        state = {"title": f"Next event at {_clock(first.starts_at, local)}",
                 "detail": f"{first.title} · {first.room}"}
    else:
        last = max(events.values(), key=lambda item: item.ends_at)
        state = {"title": "Today's events have finished",
                 "detail": f"Last ended at {_clock(last.ends_at, local)}"}
    in_use = {item.reader_uid for item in today}
    online = sum(1 for item in readers
                 if item.get("reader_uid") in in_use and item.get("online") is True)
    # The oldest schedule shown, so a schedule kept through an outage never
    # looks fresher than it is.
    updated = min((item.get("schedule_as_of") or refreshed_at for item in readers
                   if item.get("reader_uid") in in_use), default=refreshed_at,
                  key=lambda value: datetime.fromisoformat(value))
    rooms = sorted({item.room for item in events.values() if item.room})
    return {
        "visual": VISUAL,
        "icon": "device",
        "title": "BHL Event Hardware",
        "subtitle": f"{_plural(len(events), 'event')} today",
        "place": ", ".join(rooms),
        "tone": "ok",
        "activity": f"Updated {_clock(updated, local)}",
        "state": state,
        "facts": [
            {"icon": "reader", "value": f"{online}/{len(in_use)}", "label": "Room Readers",
             "tone": "ok" if online == len(in_use) else "attention"},
            *NOT_MONITORED,
        ],
    }


def tile_source(
    calendar: Any, clock: Callable[[], datetime], local: tzinfo | None = None
) -> Callable[[], dict[str, Any] | None]:
    def current() -> dict[str, Any] | None:
        at = clock()
        return compose_tile(calendar.snapshot(at), at, local)

    return current
