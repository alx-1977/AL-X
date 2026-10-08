"""Provider-neutral shapes for BHL room readers and their schedules (D-039)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol


# A reader's UID is the last eight characters of its Particle device ID; the
# BehaviorLive config endpoint is addressed by it.
READER_UID_LENGTH = 8


class ReaderAccessError(Exception):
    """A sanitised Particle or BehaviorLive failure, carrying no content."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or not code.strip():
            raise ValueError("code must not be blank")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReaderDevice:
    device_id: str
    name: str
    product_id: int
    online: bool
    last_heard: str

    @property
    def reader_uid(self) -> str:
        return self.device_id[-READER_UID_LENGTH:]


@dataclass(frozen=True, slots=True)
class ReaderSession:
    """One scheduled event in one reader's room, as the calendar holds it."""

    reader_uid: str
    event_id: int
    room: str
    mode: int
    starts_at: datetime
    ends_at: datetime
    title: str
    first_name: str
    last_name: str
    hbd: int
    offset_hours: int


# What a reader stores of an event's text (docs/READER_SCHEDULE_PROTOCOL.md).
TITLE_CHARACTERS = 64
NAME_CHARACTERS = 32


# D-043: which event a reader treats as current is decided here, once, and
# sent to the reader as a window per event. V1's rule, kept: an IN reader moves
# to the next event halfway through the current one (attendees scan in ahead);
# an OUT reader moves on halfway through the next one, and holds the last event
# until 30 minutes after it ends (attendees scan out afterwards).
OUT_LAST_EVENT_GRACE = timedelta(minutes=30)
MODE_IN, MODE_OUT = 0, 1


def active_windows(sessions: Sequence[ReaderSession], mode: int) -> dict[int, tuple[int, int]]:
    """Each event's window (Unix seconds, from, until) in one reader's day.

    The first window opens at local midnight of the first event's day, so a
    reader shows the day's first event from the start of the day.
    """
    ordered = sorted(sessions, key=lambda item: item.starts_at)
    if not ordered:
        return {}

    def seconds(moment: datetime) -> int:
        return int(moment.timestamp())

    def middle(item: ReaderSession) -> int:
        return (seconds(item.starts_at) + seconds(item.ends_at)) // 2

    offset = ordered[0].offset_hours * 3600
    first = seconds(ordered[0].starts_at)
    day_start = (first + offset) // 86400 * 86400 - offset
    windows: dict[int, tuple[int, int]] = {}
    for index, item in enumerate(ordered):
        if mode == MODE_OUT:
            opens = day_start if index == 0 else middle(item)
            closes = (middle(ordered[index + 1]) if index + 1 < len(ordered)
                      else seconds(item.ends_at + OUT_LAST_EVENT_GRACE))
        else:
            opens = day_start if index == 0 else middle(ordered[index - 1])
            closes = middle(item)
        windows[item.event_id] = (opens, max(closes, opens + 1))
    return windows


def expected_event(sessions: Sequence[ReaderSession], mode: int, now: datetime) -> int:
    """The event ID a reader should be running now, or 0 for none."""
    moment = int(now.timestamp())
    for event_id, (opens, closes) in active_windows(sessions, mode).items():
        if opens <= moment < closes:
            return event_id
    return 0


def reader_event(session: ReaderSession,
                 window: tuple[int, int] | None = None) -> dict[str, Any]:
    """One session exactly as a reader is sent it: whole seconds, text cut to fit."""
    event = {
        "id": session.event_id, "st": int(session.starts_at.timestamp()),
        "en": int(session.ends_at.timestamp()), "t": session.title[:TITLE_CHARACTERS],
        "fn": session.first_name[:NAME_CHARACTERS], "ln": session.last_name[:NAME_CHARACTERS],
        "hbd": session.hbd,
    }
    if window is not None:
        event["a"], event["u"] = window
    return event


def session_fingerprint(session: ReaderSession,
                        window: tuple[int, int] | None = None) -> str:
    """Everything a reader is told about one session, as one comparable value.

    Built from what is sent, not from the calendar's full values, so a sent
    schedule is confirmed exactly when a resend would deliver the same thing,
    and an event whose time, window, room or text changed under the same ID is
    not.
    """
    fields = [session.reader_uid, session.room[:TITLE_CHARACTERS], session.mode,
              session.offset_hours, reader_event(session, window)]
    return hashlib.sha256(
        json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def still_to_run(sessions: Sequence[ReaderSession], mode: int,
                 now: datetime) -> list[tuple[ReaderSession, tuple[int, int]]]:
    """A reader's events whose window has not closed, each with its window."""
    windows = active_windows(sessions, mode)
    moment = int(now.timestamp())
    return [(item, windows[item.event_id])
            for item in sorted(sessions, key=lambda value: value.starts_at)
            if windows[item.event_id][1] > moment]


def holds_current(record: Mapping[str, Any], sessions: Sequence[ReaderSession],
                  mode: int, now: datetime) -> bool:
    """What the reader accepted and can still run is exactly what it should run.

    Both ways: an event moved earlier or deleted is still running on a reader
    that accepted its old window, and an added or changed event is not yet on
    it. A record from before held events were recorded confirms nothing.
    """
    held = record.get("held")
    if not isinstance(held, (list, tuple)) or any(
            not isinstance(item, Mapping) or not isinstance(item.get("fp"), str)
            or not isinstance(item.get("until"), int) for item in held):
        return False
    moment = int(now.timestamp())
    running = {item["fp"] for item in held if item["until"] > moment}
    return running == {session_fingerprint(item, window)
                       for item, window in still_to_run(sessions, mode, now)}


class ReaderFleet(Protocol):
    def devices(self, product_id: int) -> tuple[ReaderDevice, ...]: ...


class ReaderConfigSource(Protocol):
    def config(self, reader_uid: str) -> Mapping[str, Any]: ...


class ReaderControl(Protocol):
    def call_function(
        self, product_id: int, device_id: str, function: str, argument: str
    ) -> int: ...
