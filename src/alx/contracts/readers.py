"""Provider-neutral shapes for BHL room readers and their schedules (D-039)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
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


def reader_event(session: ReaderSession) -> dict[str, Any]:
    """One session exactly as a reader is sent it: whole seconds, text cut to fit."""
    return {
        "id": session.event_id, "st": int(session.starts_at.timestamp()),
        "en": int(session.ends_at.timestamp()), "t": session.title[:TITLE_CHARACTERS],
        "fn": session.first_name[:NAME_CHARACTERS], "ln": session.last_name[:NAME_CHARACTERS],
        "hbd": session.hbd,
    }


def session_fingerprint(session: ReaderSession) -> str:
    """Everything a reader is told about one session, as one comparable value.

    Built from what is sent, not from the calendar's full values, so a sent
    schedule is confirmed exactly when a resend would deliver the same thing,
    and an event whose time, room or text changed under the same ID is not.
    """
    fields = [session.reader_uid, session.room[:TITLE_CHARACTERS], session.mode,
              session.offset_hours, reader_event(session)]
    return hashlib.sha256(
        json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


class ReaderFleet(Protocol):
    def devices(self, product_id: int) -> tuple[ReaderDevice, ...]: ...


class ReaderConfigSource(Protocol):
    def config(self, reader_uid: str) -> Mapping[str, Any]: ...


class ReaderControl(Protocol):
    def call_function(
        self, product_id: int, device_id: str, function: str, argument: str
    ) -> int: ...
