"""Provider-neutral mail records and ports for primitive observation and action."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol

from alx.contracts.records import BackgroundEvent


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


@dataclass(frozen=True, slots=True)
class MailReference:
    mailbox_id: str
    uid_validity: str
    uid: str

    def __post_init__(self) -> None:
        _required(self.mailbox_id, "mailbox_id")
        _required(self.uid_validity, "uid_validity")
        _required(self.uid, "uid")


@dataclass(frozen=True, slots=True)
class MailContent:
    reference: MailReference
    subject: str
    sender: str
    received_at: str
    body: str


class MailAccessError(Exception):
    def __init__(self, code: str) -> None:
        _required(code, "code")
        self.code = code
        super().__init__(code)


class MailAccount(Protocol):
    def read(self, reference: MailReference) -> MailContent: ...

    def move_to_trash(self, reference: MailReference) -> str: ...


class MailObservationControl(Protocol):
    def acknowledge(self, reference: MailReference) -> None: ...


class BackgroundEventSource(Protocol):
    def events(self) -> AsyncIterator[BackgroundEvent]: ...

    def record_delivery(self, event_id: str) -> None: ...
