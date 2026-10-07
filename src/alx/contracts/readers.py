"""Provider-neutral shapes for BHL room readers and their schedules (D-039)."""

from __future__ import annotations

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


class ReaderFleet(Protocol):
    def devices(self, product_id: int) -> tuple[ReaderDevice, ...]: ...


class ReaderConfigSource(Protocol):
    def config(self, reader_uid: str) -> Mapping[str, Any]: ...
