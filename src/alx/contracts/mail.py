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
class MailParticipants:
    """Addresses observed on a message, reported as facts.

    These are read from the message. Nothing here decides who a reply should
    go to; that judgement belongs to the Core.
    """

    sender: str = ""
    reply_to: str = ""
    recipients: tuple[str, ...] = ()
    carbon_copy: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "recipients", tuple(self.recipients))
        object.__setattr__(self, "carbon_copy", tuple(self.carbon_copy))


@dataclass(frozen=True, slots=True)
class MailThreading:
    """RFC 5322 identifier headers, the only confirmed threading evidence."""

    message_id: str = ""
    in_reply_to: str = ""
    references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "references", tuple(self.references))

    def reply_references(self) -> tuple[str, ...]:
        """Build the References chain a reply carries, per RFC 5322.

        The parent's own chain is followed by the parent itself, order
        preserved and duplicates removed.
        """
        chain: list[str] = []
        for item in (*self.references, self.message_id):
            if item and item not in chain:
                chain.append(item)
        return tuple(chain)


@dataclass(frozen=True, slots=True)
class MailContent:
    reference: MailReference
    subject: str
    sender: str
    received_at: str
    body: str
    participants: MailParticipants = MailParticipants()
    threading: MailThreading = MailThreading()


@dataclass(frozen=True, slots=True)
class OutboundReply:
    """One fully specified reply, assembled but not sent.

    Every value a send would transmit is present and inspectable, so the
    artifact approved is the artifact that leaves. There is no sender field:
    the identity is configuration and may not be chosen.
    """

    to: tuple[str, ...]
    subject: str
    body: str
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    carbon_copy: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "to", tuple(self.to))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "carbon_copy", tuple(self.carbon_copy))
        if not self.to:
            raise ValueError("a reply requires at least one recipient")
        for address in (*self.to, *self.carbon_copy):
            if "@" not in address or address.strip() != address:
                raise ValueError("every recipient must be a complete address")
        _required(self.subject, "subject")
        _required(self.body, "body")


@dataclass(frozen=True, slots=True)
class ReplyOutcome:
    """What actually happened when one reply was transmitted."""

    transmitted_message_id: str
    accepted: bool
    recipients_accepted: tuple[str, ...] = ()
    recipients_refused: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required(self.transmitted_message_id, "transmitted_message_id")
        object.__setattr__(self, "recipients_accepted", tuple(self.recipients_accepted))
        object.__setattr__(self, "recipients_refused", tuple(self.recipients_refused))


class MailSendError(Exception):
    """A sanitised send failure that never carries a credential or body."""

    def __init__(self, code: str) -> None:
        _required(code, "code")
        self.code = code
        super().__init__(code)


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
