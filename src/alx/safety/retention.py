"""What a retention purge would remove, computed without removing anything.

Governance decision D-013. This module answers two questions and performs no
deletion:

  1. What is in the stores now, and how much of it is mail-derived?
  2. If the policy ran at a given moment, what would expire, and what would be
     left behind as a tombstone?

Deletion is a separate authorisation Friedl has not given. Building the
preview first means the first real purge can be inspected before it runs,
rather than discovered afterwards.

The unclassifiable case is deliberately visible. Records written before
provenance existed carry no origin, and nothing can tell by inspection whether
a sentence AL/X wrote came from a mail body or from Friedl's own words — that
is the same limit that defeated the similarity guard. Such records are
reported as UNCLASSIFIED rather than assumed safe or assumed mail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from alx.contracts.provenance import (
    ContentProvenance,
    ContentTombstone,
    ExpiryReason,
)


class Classification(str, Enum):
    """What is known about a record's origin."""

    MAIL_DERIVED = "mail_derived"
    NOT_MAIL_DERIVED = "not_mail_derived"
    UNCLASSIFIED = "unclassified"


@dataclass(frozen=True, slots=True)
class RecordSurvey:
    """One durable record as the inventory sees it.

    Carries no content: a record's identity, where it lives, and what is known
    about its origin. An inventory that quoted the content it was measuring
    would be another copy of the thing being retained.
    """

    store: str
    record_id: str
    provenance: ContentProvenance | None = None

    def __post_init__(self) -> None:
        if not self.store.strip() or not self.record_id.strip():
            raise ValueError("a surveyed record requires a store and an identifier")
        if self.provenance is not None and not isinstance(
            self.provenance, ContentProvenance
        ):
            raise TypeError("provenance must be ContentProvenance or None")

    @property
    def classification(self) -> Classification:
        """Derive classification so it cannot contradict provenance."""
        return classify(self.provenance)

    def would_expire_at(self, at: datetime) -> bool:
        """Whether a purge running at `at` would remove this record's content."""
        if self.provenance is None:
            return False
        if not self.provenance.governed_by_retention():
            return False
        return self.provenance.is_expired(at)


@dataclass(frozen=True, slots=True)
class PurgePreview:
    """What a purge would do, reported before anything is authorised to run.

    `unclassified` is the number that matters most on the first run: records
    predating provenance, whose origin cannot be established by inspection.
    They are never silently purged.
    """

    evaluated_at: datetime
    surveyed: tuple[RecordSurvey, ...] = ()
    would_expire: tuple[RecordSurvey, ...] = ()
    tombstones: tuple[ContentTombstone, ...] = ()
    unclassified: tuple[RecordSurvey, ...] = ()

    def is_destructive(self) -> bool:
        """Always false. A preview computes; it never removes."""
        return False

    def render(self) -> str:
        """A plain-language summary, carrying no content."""
        lines = [
            f"Retention preview at {self.evaluated_at.isoformat()}",
            f"  records surveyed:     {len(self.surveyed)}",
            f"  mail-derived expired: {len(self.would_expire)}",
            f"  tombstones to leave:  {len(self.tombstones)}",
            f"  unclassified:         {len(self.unclassified)}",
        ]
        if self.unclassified:
            lines.append(
                "  unclassified records predate provenance and are not purged; "
                "their origin cannot be established by inspection"
            )
        return "\n".join(lines)


def classify(provenance: ContentProvenance | None) -> Classification:
    """Classify one record from its provenance, if it has any."""
    if provenance is None:
        return Classification.UNCLASSIFIED
    if provenance.governed_by_retention():
        return Classification.MAIL_DERIVED
    return Classification.NOT_MAIL_DERIVED


def tombstone_for(record: RecordSurvey, expired_at: datetime) -> ContentTombstone:
    """The bookmark left where a record's content was.

    Built from the record's identity and provenance alone, so there is no path
    by which a subject, summary, or extracted fact could reach it.
    """
    if record.provenance is None:
        raise ValueError("a record without provenance has no content to expire")
    if not record.provenance.governed_by_retention():
        raise ValueError("only mail-derived content receives a D-013 tombstone")
    if not record.provenance.is_expired(expired_at):
        raise ValueError("live content cannot be replaced by a tombstone")
    return ContentTombstone(
        record_id=record.record_id,
        origins=record.provenance.origins,
        recorded_at=record.provenance.recorded_at,
        expired_at=expired_at,
        reason=ExpiryReason.RETENTION_ELAPSED,
        mail_references=record.provenance.mail_references,
    )


def preview_purge(
    records: tuple[RecordSurvey, ...],
    at: datetime,
) -> PurgePreview:
    """Compute what a purge at `at` would do. Removes nothing.

    The deadline stamped on each record is authoritative. D-013 permits no
    alternate lifetime to inject at preview time.
    """
    expiring = tuple(item for item in records if item.would_expire_at(at))
    return PurgePreview(
        evaluated_at=at,
        surveyed=tuple(records),
        would_expire=expiring,
        tombstones=tuple(tombstone_for(item, at) for item in expiring),
        unclassified=tuple(
            item
            for item in records
            if item.classification is Classification.UNCLASSIFIED
        ),
    )
