"""Where a durable record came from, and when its content must expire.

Governance decision D-013. Friedl clears his mail as it arrives, but reading a
message writes fragments of it into AL/X's own stores on his Mac, and nothing
made those copies expire. This records, per record, what a record was derived
from and when its content dies, so a deadline can be enforced instead of
inferred.

The safeguard this replaces tried to detect mail content by comparing text for
resemblance. That failed six times, because no threshold separates a faithful
summary of a short message from a copied fragment. Provenance does not try to
recognise content. It records that a record was *derived from* a message when
the message was read, and expires it on that basis regardless of how it reads.

Nothing here deletes anything. These are the facts a purge would act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from alx.contracts.records import StructuredData, freeze_data


def _required(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be blank")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone aware")


class ContentOrigin(str, Enum):
    """What a durable record's content was derived from.

    `MAIL_MESSAGE` is the origin D-013 governs. The others exist so a record
    without mail provenance is stated as such rather than merely lacking a
    field, which would make an unstamped record indistinguishable from one
    deliberately marked as carrying no mail content.
    """

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
    """One durable record's origin and its content's own deadline.

    The deadline belongs to the content, not to the goal that cites it. A goal
    may outlive its content; D-013 requires that it does rather than holding
    the content alive. `source_reference` is the durable mail reference, which
    survives expiry as the bookmark AL/X re-reads from.
    """

    origin: ContentOrigin
    content_expires_at: datetime
    recorded_at: datetime
    source_reference: StructuredData | None = None

    def __post_init__(self) -> None:
        _aware(self.content_expires_at, "content_expires_at")
        _aware(self.recorded_at, "recorded_at")
        if self.content_expires_at <= self.recorded_at:
            raise ValueError("content must expire after it was recorded")
        if self.origin is ContentOrigin.MAIL_MESSAGE and self.source_reference is None:
            raise ValueError(
                "mail-derived content requires the reference it can be re-read from"
            )
        if self.source_reference is not None:
            object.__setattr__(
                self, "source_reference", freeze_data(self.source_reference)
            )

    def is_expired(self, at: datetime) -> bool:
        _aware(at, "at")
        return self.content_expires_at <= at

    def governed_by_retention(self) -> bool:
        """Whether D-013's deadline applies to this record."""
        return self.origin is ContentOrigin.MAIL_MESSAGE


@dataclass(frozen=True, slots=True)
class ContentTombstone:
    """What remains once a record's content has expired.

    D-013: "a bookmark, not a copy". This carries the record's identity, where
    the message can be re-read from, and when and why the content went. It
    carries **no subject, no summary, and no extracted fact** — those are the
    content, and structure is not a retention loophole.

    A tombstone is deliberately not evidence. `Evidence` records support a
    success criterion; a tombstone records that support has been lost. Anything
    that consumed the expired content must treat its criterion as unsupported
    until AL/X re-reads the message.
    """

    record_id: str
    origin: ContentOrigin
    recorded_at: datetime
    expired_at: datetime
    reason: ExpiryReason
    source_reference: StructuredData | None = None

    def __post_init__(self) -> None:
        _required(self.record_id, "record_id")
        _aware(self.recorded_at, "recorded_at")
        _aware(self.expired_at, "expired_at")
        if self.source_reference is not None:
            object.__setattr__(
                self, "source_reference", freeze_data(self.source_reference)
            )

    def is_evidence(self) -> bool:
        """Always false. Present so the intent is stated, not assumed."""
        return False


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """The configured lifetime of mail-derived content.

    D-013 sets thirty days. It is configuration rather than a constant because
    the decision records a review condition for it, but it is not open-ended:
    a policy longer than the decision authorises is refused here rather than
    silently honoured.
    """

    mail_content_lifetime: timedelta = field(default=timedelta(days=30))

    def __post_init__(self) -> None:
        if self.mail_content_lifetime <= timedelta(0):
            raise ValueError("mail content lifetime must be positive")
        if self.mail_content_lifetime > timedelta(days=30):
            raise ValueError(
                "D-013 authorises at most thirty days for mail-derived content; "
                "a longer lifetime requires a new decision"
            )

    def expires_at(self, recorded_at: datetime) -> datetime:
        _aware(recorded_at, "recorded_at")
        return recorded_at + self.mail_content_lifetime

    def stamp(
        self,
        origin: ContentOrigin,
        recorded_at: datetime,
        source_reference: StructuredData | None = None,
    ) -> ContentProvenance:
        """Build the provenance a newly written record carries.

        Re-reading a message calls this again and gets a fresh deadline. It
        never renews an existing record, or touching a message would defeat
        the policy.
        """
        return ContentProvenance(
            origin=origin,
            content_expires_at=self.expires_at(recorded_at),
            recorded_at=recorded_at,
            source_reference=source_reference,
        )
