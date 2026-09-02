"""Provider-neutral mail records and ports for primitive observation and action."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
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
    has_attachments: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.has_attachments, bool):
            raise TypeError("has_attachments must be a bool")


@dataclass(frozen=True, slots=True)
class MailAttachment:
    """One exact MIME attachment and its provider-extracted readable text."""

    attachment_id: str
    filename: str
    media_type: str
    size: int
    sha256: str
    text: str = ""

    def __post_init__(self) -> None:
        _required(self.attachment_id, "attachment_id")
        _required(self.filename, "filename")
        _required(self.media_type, "media_type")
        _required(self.sha256, "sha256")
        if not isinstance(self.size, int) or isinstance(self.size, bool) or self.size < 0:
            raise ValueError("size must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class MailSearchCriteria:
    """Structured IMAP search facts; no user wording or workflow intent."""

    mailbox_id: str
    sender: str = ""
    subject: str = ""
    date_from: str = ""
    date_to: str = ""
    seen_state: str = "any"
    has_attachments: bool | None = None
    limit: int = 50

    def __post_init__(self) -> None:
        _required(self.mailbox_id, "mailbox_id")
        for value in (self.mailbox_id, self.sender, self.subject):
            if any(character in value for character in ("\r", "\n", "\x00")):
                raise ValueError("mail search text contains a control character")
        for name in ("date_from", "date_to"):
            value = getattr(self, name)
            if value:
                date.fromisoformat(value)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        if self.seen_state not in ("any", "seen", "unseen"):
            raise ValueError("seen_state must be any, seen, or unseen")
        if self.has_attachments is not None and not isinstance(
            self.has_attachments, bool
        ):
            raise TypeError("has_attachments must be a bool or None")
        if (
            not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or not 1 <= self.limit <= 100
        ):
            raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class MailSearchResult:
    reference: MailReference
    subject: str
    sender: str
    received_at: str
    has_attachments: bool
    seen: bool

    def __post_init__(self) -> None:
        if not isinstance(self.has_attachments, bool) or not isinstance(self.seen, bool):
            raise TypeError("mail search flags must be bools")


@dataclass(frozen=True, slots=True)
class OutboundReply:
    """One fully specified reply, assembled but not sent.

    Every value a send would transmit is present and inspectable, so the
    artifact approved is the artifact that leaves. There is no sender field:
    the identity is configuration and may not be chosen.

    DECISIONS.md D-011 authorises replying to an existing message and nothing
    else, so a reply must identify the message it answers and may only reach
    addresses observed on that message. Without those constraints this record
    could carry new correspondence to an arbitrary recipient, which is outside
    the recorded authority.
    """

    to: tuple[str, ...]
    subject: str
    body: str
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    carbon_copy: tuple[str, ...] = ()
    permitted_recipients: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "to", tuple(self.to))
        object.__setattr__(self, "references", tuple(self.references))
        object.__setattr__(self, "carbon_copy", tuple(self.carbon_copy))
        object.__setattr__(
            self, "permitted_recipients", tuple(self.permitted_recipients)
        )
        if not self.to:
            raise ValueError("a reply requires at least one recipient")
        if not self.in_reply_to.strip():
            raise ValueError("a reply must identify the message it answers")
        if self.in_reply_to not in self.references:
            raise ValueError("a reply must cite the message it answers")
        allowed = {item.lower() for item in self.permitted_recipients}
        if not allowed:
            raise ValueError("a reply requires the addresses observed on its source")
        unknown = sorted(
            address for address in (*self.to, *self.carbon_copy)
            if address.lower() not in allowed
        )
        if unknown:
            raise ValueError(
                "a reply may only reach addresses observed on the message it "
                f"answers: {', '.join(unknown)}"
            )
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
    def search(
        self, criteria: MailSearchCriteria
    ) -> tuple[tuple[MailSearchResult, ...], bool]: ...

    def read(self, reference: MailReference) -> MailContent: ...

    def list_attachments(self, reference: MailReference) -> tuple[MailAttachment, ...]: ...

    def read_attachment(
        self, reference: MailReference, attachment_id: str
    ) -> tuple[MailAttachment, bytes]: ...

    def mark_seen(self, reference: MailReference) -> None: ...

    def file_message(self, reference: MailReference, mailbox: str) -> str: ...

    def move_to_trash(self, reference: MailReference) -> str: ...


class MailObservationControl(Protocol):
    def acknowledge(self, reference: MailReference) -> None: ...

