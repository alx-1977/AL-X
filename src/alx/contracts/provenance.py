"""Mechanical provenance and retention facts for durable content.

Governance decision D-013 requires every durable record derived from mail to
expire thirty days after it is written. Provenance is therefore a union of
derivation sources, not a caller-selected label and not a judgement made from
the record's wording. Authorship and derivation are deliberately separate: a
record authored by AL/X can still be derived from one or more mail messages.

Nothing in this module deletes or writes durable state. It defines the facts
that the stores will persist in the next, separately reviewed slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable

from alx.contracts.mail import MailReference


MAIL_CONTENT_LIFETIME = timedelta(days=30)


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone aware")


def _origins(values: Iterable["ContentOrigin"]) -> frozenset["ContentOrigin"]:
    result = frozenset(values)
    if not result:
        raise ValueError("content provenance requires at least one origin")
    if any(not isinstance(item, ContentOrigin) for item in result):
        raise TypeError("origins must contain only ContentOrigin values")
    return result


def _mail_references(values: Iterable[MailReference]) -> tuple[MailReference, ...]:
    result: list[MailReference] = []
    for item in values:
        if not isinstance(item, MailReference):
            raise TypeError("mail references must contain only MailReference values")
        if item not in result:
            result.append(item)
    return tuple(result)


class ContentOrigin(str, Enum):
    """A mechanical derivation source, not the author of the final wording."""

    MAIL_MESSAGE = "mail_message"
    PERSON = "person"
    ALX = "alx"


class ExpiryReason(str, Enum):
    """Why a record's content is no longer present."""

    RETENTION_ELAPSED = "retention_elapsed"
    SUPERSEDED = "superseded"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class ContentProvenance:
    """The transitive sources of one durable record and its own deadline.

    `origins` is the union of every input provenance plus the author of the new
    record. `mail_references` contains only typed, content-free bookmarks. If
    any input was mail-derived, `MAIL_MESSAGE` and at least one bookmark must
    survive the union and D-013's deadline is mandatory.
    """

    origins: frozenset[ContentOrigin]
    recorded_at: datetime
    mail_references: tuple[MailReference, ...] = ()
    content_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "origins", _origins(self.origins))
        object.__setattr__(
            self, "mail_references", _mail_references(self.mail_references)
        )
        _aware(self.recorded_at, "recorded_at")
        mail_derived = ContentOrigin.MAIL_MESSAGE in self.origins
        if mail_derived != bool(self.mail_references):
            raise ValueError(
                "mail origin and typed mail references must be present together"
            )
        if mail_derived:
            if self.content_expires_at is None:
                raise ValueError("mail-derived content requires an expiry")
            _aware(self.content_expires_at, "content_expires_at")
            if self.content_expires_at > self.recorded_at + MAIL_CONTENT_LIFETIME:
                raise ValueError(
                    "D-013 allows at most thirty days from the moment content is "
                    "recorded; a later deadline would extend a message's life"
                )
        elif self.content_expires_at is not None:
            raise ValueError(
                "D-013 content expiry is reserved for mail-derived records"
            )

    def is_expired(self, at: datetime) -> bool:
        _aware(at, "at")
        return (
            self.content_expires_at is not None
            and self.content_expires_at <= at
        )

    def governed_by_retention(self) -> bool:
        return ContentOrigin.MAIL_MESSAGE in self.origins


@dataclass(frozen=True, slots=True)
class ContentTombstone:
    """A content-free bookmark left after mail-derived content expires."""

    record_id: str
    origins: frozenset[ContentOrigin]
    recorded_at: datetime
    expired_at: datetime
    reason: ExpiryReason
    mail_references: tuple[MailReference, ...]

    def __post_init__(self) -> None:
        _required(self.record_id, "record_id")
        object.__setattr__(self, "origins", _origins(self.origins))
        object.__setattr__(
            self, "mail_references", _mail_references(self.mail_references)
        )
        _aware(self.recorded_at, "recorded_at")
        _aware(self.expired_at, "expired_at")
        if self.expired_at < self.recorded_at:
            raise ValueError("content cannot expire before it was recorded")
        if ContentOrigin.MAIL_MESSAGE not in self.origins:
            raise ValueError("D-013 tombstones are only for mail-derived content")
        if not self.mail_references:
            raise ValueError("a mail tombstone requires a re-readable reference")

    def is_evidence(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """The exact mail-content lifetime approved in D-013."""

    mail_content_lifetime: timedelta = field(default=MAIL_CONTENT_LIFETIME)

    def __post_init__(self) -> None:
        if self.mail_content_lifetime != MAIL_CONTENT_LIFETIME:
            raise ValueError(
                "D-013 authorises exactly thirty days for mail-derived content; "
                "a different lifetime requires a new decision"
            )

    def expires_at(self, recorded_at: datetime) -> datetime:
        _aware(recorded_at, "recorded_at")
        return recorded_at + self.mail_content_lifetime

    def direct_mail(
        self,
        recorded_at: datetime,
        references: Iterable[MailReference],
    ) -> ContentProvenance:
        """Seed provenance from a fresh primitive mail read."""
        refs = _mail_references(references)
        if not refs:
            raise ValueError("a direct mail record requires a mail reference")
        return ContentProvenance(
            origins=frozenset({ContentOrigin.MAIL_MESSAGE}),
            recorded_at=recorded_at,
            mail_references=refs,
            content_expires_at=self.expires_at(recorded_at),
        )

    def non_mail(
        self, origin: ContentOrigin, recorded_at: datetime
    ) -> ContentProvenance:
        """Seed content known mechanically not to come from a mail read."""
        if origin is ContentOrigin.MAIL_MESSAGE:
            raise ValueError("mail provenance must be seeded with direct_mail")
        return ContentProvenance(
            origins=frozenset({origin}),
            recorded_at=recorded_at,
        )

    def derive(
        self,
        author: ContentOrigin,
        recorded_at: datetime,
        inputs: Iterable[ContentProvenance],
    ) -> ContentProvenance:
        """Union every input mechanically into a newly authored record.

        A fresh record receives its own deadline, as D-013 specifies. Store
        rewrites of an existing logical record must preserve that record's
        provenance rather than calling this method again; otherwise an update
        would silently renew its clock.
        """
        if author is ContentOrigin.MAIL_MESSAGE:
            raise ValueError("MAIL_MESSAGE is a source, not an author")
        origins = {author}
        references: list[MailReference] = []
        provided = tuple(inputs)
        for item in provided:
            if not isinstance(item, ContentProvenance):
                raise TypeError("inputs must contain only ContentProvenance values")
            origins.update(item.origins)
            for reference in item.mail_references:
                if reference not in references:
                    references.append(reference)
        mail_derived = ContentOrigin.MAIL_MESSAGE in origins
        # A derived record inherits the earliest deadline among its mail-derived
        # inputs, never a fresh thirty days. D-013 is explicit that re-reading
        # must not renew an existing record: without this, AL/X summarising a
        # thread every few weeks would carry one message's content indefinitely,
        # each summary honestly stamped and each one thirty days newer.
        inherited = [
            item.content_expires_at
            for item in provided
            if item.content_expires_at is not None
        ]
        deadline: datetime | None = None
        if mail_derived:
            fresh = self.expires_at(recorded_at)
            deadline = min([fresh, *inherited]) if inherited else fresh
        return ContentProvenance(
            origins=frozenset(origins),
            recorded_at=recorded_at,
            mail_references=tuple(references),
            content_expires_at=deadline,
        )
