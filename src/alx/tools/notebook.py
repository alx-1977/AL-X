"""Language-blind primitives for AL/X's research notebook.

Each capability performs one storage outcome through structured values. None of
them reads AL/X's wording, decides what a claim means, judges whether an
enquiry is worthwhile, or continues anything. The notebook records thinking she
has already done.

There is deliberately no capability that lists the whole notebook. Retrieval
takes a scope, because injecting every thread into Core context is the cost the
notebook exists to avoid.
"""

from __future__ import annotations

from datetime import UTC as _UTC, datetime, timedelta as _timedelta
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts.notebook import MAX_RETRIEVAL_LIMIT
from alx.contracts.provenance import ContentOrigin, RetentionPolicy
from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    EntryKind,
    EntryProposal,
    EntryRevisionProposal,
    ResearchQuery,
    RevisionAuthor,
    SideEffect,
    StructuredSchema,
    ThreadProposal,
    ThreadStatus,
    ValueKind,
)


OPEN_RESEARCH_THREAD = "open_research_thread"
RECORD_RESEARCH_ENTRY = "record_research_entry"
REVISE_RESEARCH_ENTRY = "revise_research_entry"
SEARCH_RESEARCH = "search_research"
READ_RESEARCH_THREAD = "read_research_thread"
SET_RESEARCH_STATUS = "set_research_status"
CORRECT_RESEARCH_ENTRY = "correct_research_entry"
DELETE_RESEARCH = "delete_research"

class _EvidenceUnresolved(Exception):
    """A cited evidence identifier could not be resolved to its provenance."""


def _policy() -> RetentionPolicy:
    return RetentionPolicy()


_STRING = StructuredSchema(ValueKind.STRING)
_INTEGER = StructuredSchema(ValueKind.INTEGER)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_STRINGS = StructuredSchema(ValueKind.ARRAY, items=_STRING)

_FAILURES = (
    "arguments_unusable",
    "thread_not_found",
    "entry_not_found",
    "thread_already_exists",
    "entry_already_exists",
    "revision_conflict",
    "thread_archived",
    "storage_failed",
    # A citation whose provenance cannot be established is refused rather
    # than written without its retention deadline.
    "evidence_unresolved",
)

_ENTRY = StructuredSchema(
    ValueKind.OBJECT,
    {
        "entry_id": _STRING,
        "thread_id": _STRING,
        "kind": _STRING,
        "revision": _INTEGER,
        "content": _STRING,
        "recorded_at": _STRING,
        "reason": _STRING,
        "author": _STRING,
        "source_references": _STRINGS,
    },
    ("entry_id", "thread_id", "kind", "revision", "content", "recorded_at",
     "author", "source_references"),
    extra_properties=False,
)

_THREAD = StructuredSchema(
    ValueKind.OBJECT,
    {
        "thread_id": _STRING,
        "question": _STRING,
        "interest": _STRING,
        "status": _STRING,
        "opened_at": _STRING,
        "retention_until": _STRING,
        "entries": StructuredSchema(ValueKind.ARRAY, items=_ENTRY),
        "total_entries": _INTEGER,
        "next_offset": _INTEGER,
    },
    ("thread_id", "question", "interest", "status", "opened_at",
     "retention_until", "entries", "total_entries"),
    extra_properties=False,
)


OPEN_THREAD_DEFINITION = CapabilityDefinition(
    OPEN_RESEARCH_THREAD,
    "Open a durable research thread recording the question and why it is of "
    "interest. Storage only: it starts no work and schedules nothing.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"thread_id": _STRING, "question": _STRING, "interest": _STRING},
        ("thread_id", "question", "interest"),
        extra_properties=False,
    ),
    _THREAD,
    SideEffect.EFFECTFUL,
    _FAILURES,
    ("thread_id",),
)

RECORD_ENTRY_DEFINITION = CapabilityDefinition(
    RECORD_RESEARCH_ENTRY,
    "Record one claim, hypothesis, doubt, question or conclusion against a "
    "thread, referencing evidence by identifier rather than copying it.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "entry_id": _STRING,
            "thread_id": _STRING,
            "kind": _STRING,
            "content": _STRING,
            "source_references": _STRINGS,
        },
        ("entry_id", "thread_id", "kind", "content"),
        extra_properties=False,
    ),
    _ENTRY,
    SideEffect.EFFECTFUL,
    _FAILURES,
    ("entry_id", "thread_id", "kind", "source_references"),
)

REVISE_ENTRY_DEFINITION = CapabilityDefinition(
    REVISE_RESEARCH_ENTRY,
    "Record AL/X's own changed view as a new revision attributed to her, "
    "preserving every earlier version and the stated reason.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "entry_id": _STRING,
            "content": _STRING,
            "reason": _STRING,
            "expected_revision": _INTEGER,
            "source_references": _STRINGS,
        },
        ("entry_id", "content", "reason", "expected_revision"),
        extra_properties=False,
    ),
    _ENTRY,
    SideEffect.EFFECTFUL,
    _FAILURES,
    ("entry_id", "expected_revision", "source_references"),
)

SEARCH_DEFINITION = CapabilityDefinition(
    SEARCH_RESEARCH,
    "Retrieve research entries matching a scope. A scope is required: thread, "
    "cited source, or time window. Kind alone is not a scope.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "query_id": _STRING,
            "thread_ids": _STRINGS,
            "kinds": _STRINGS,
            "source_references": _STRINGS,
            "recorded_after": _STRING,
            "recorded_before": _STRING,
            "include_archived": _BOOLEAN,
            "limit": _INTEGER,
        },
        ("query_id",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"entries": StructuredSchema(ValueKind.ARRAY, items=_ENTRY)},
        ("entries",),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
    (
        "query_id", "thread_ids", "kinds", "source_references",
        "recorded_after", "recorded_before", "include_archived", "limit",
    ),
)

READ_THREAD_DEFINITION = CapabilityDefinition(
    READ_RESEARCH_THREAD,
    "Read one page of a research thread. Entries and revisions are bounded; "
    "next_offset continues a larger thread.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"thread_id": _STRING, "offset": _INTEGER},
        ("thread_id",),
        extra_properties=False,
    ),
    _THREAD,
    SideEffect.NONE,
    _FAILURES,
    ("thread_id", "offset"),
)

SET_STATUS_DEFINITION = CapabilityDefinition(
    SET_RESEARCH_STATUS,
    "Pause, resume or archive a research thread. Archived research takes no "
    "new entries and stays out of ordinary retrieval.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"thread_id": _STRING, "status": _STRING},
        ("thread_id", "status"),
        extra_properties=False,
    ),
    _THREAD,
    SideEffect.EFFECTFUL,
    _FAILURES,
    ("thread_id", "status"),
)

CORRECT_ENTRY_DEFINITION = CapabilityDefinition(
    CORRECT_RESEARCH_ENTRY,
    "Record Friedl's correction to an entry as a new revision attributed to "
    "him, preserving what was written before and why it was corrected.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "entry_id": _STRING,
            "content": _STRING,
            "reason": _STRING,
            "expected_revision": _INTEGER,
            "source_references": _STRINGS,
        },
        ("entry_id", "content", "reason", "expected_revision"),
        extra_properties=False,
    ),
    _ENTRY,
    SideEffect.EFFECTFUL,
    _FAILURES,
    ("entry_id", "expected_revision", "source_references"),
)

DELETE_DEFINITION = CapabilityDefinition(
    DELETE_RESEARCH,
    "Permanently delete a research thread or entry and every version of it. "
    "The content is removed, not hidden; only an identifier and a time remain.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"record_id": _STRING, "kind": _STRING},
        ("record_id", "kind"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"record_id": _STRING, "kind": _STRING, "deleted_at": _STRING},
        ("record_id", "kind", "deleted_at"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
    ("record_id", "kind"),
)


DEFINITIONS = (
    OPEN_THREAD_DEFINITION,
    RECORD_ENTRY_DEFINITION,
    REVISE_ENTRY_DEFINITION,
    SEARCH_DEFINITION,
    READ_THREAD_DEFINITION,
    SET_STATUS_DEFINITION,
    CORRECT_ENTRY_DEFINITION,
    DELETE_DEFINITION,
)


def _entry_values(snapshot: Any) -> dict[str, Any]:
    current = snapshot.current
    values: dict[str, Any] = {
        "entry_id": snapshot.entry_id,
        "thread_id": snapshot.thread_id,
        "kind": snapshot.kind.value,
        "revision": current.revision,
        "content": current.content,
        "recorded_at": current.recorded_at.isoformat(),
        "author": current.author.value,
        "source_references": list(current.source_references),
    }
    if current.reason is not None:
        values["reason"] = current.reason
    return values


def _thread_values(snapshot: Any) -> dict[str, Any]:
    return {
        "thread_id": snapshot.thread_id,
        "question": snapshot.question,
        "interest": snapshot.interest,
        "status": snapshot.status.value,
        "opened_at": snapshot.opened_at.isoformat(),
        "retention_until": snapshot.retention_until.isoformat(),
        "entries": [_entry_values(item) for item in snapshot.entries],
        "total_entries": snapshot.total_entries,
        **(
            {} if snapshot.next_offset is None
            else {"next_offset": snapshot.next_offset}
        ),
    }


def _ok(
    call_id: str,
    capability_id: str,
    values: Mapping[str, Any],
    durable_values: Mapping[str, Any] | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        call_id,
        capability_id,
        CapabilityResultState.SUCCEEDED,
        values,
        durable_values={} if durable_values is None else durable_values,
    )


def _entry_receipt(snapshot: Any) -> dict[str, Any]:
    """The goal keeps continuity identifiers, never notebook prose."""
    return {
        "entry_id": snapshot.entry_id,
        "thread_id": snapshot.thread_id,
        "revision": snapshot.current.revision,
    }


def _thread_receipt(snapshot: Any) -> dict[str, Any]:
    """The goal keeps the thread identity and state, never its contents."""
    return {"thread_id": snapshot.thread_id, "status": snapshot.status.value}


def _failed(call_id: str, capability_id: str, code: str) -> CapabilityResult:
    """Report a failure as a declared code carrying no research content."""
    return CapabilityResult(
        call_id, capability_id, CapabilityResultState.FAILED, {}, {"code": code}
    )


class NotebookCapabilities:
    """Bind the notebook primitives to durable storage.

    Every method takes structured values and returns a structured result. None
    inspects AL/X's wording: `question`, `interest` and `content` are carried to
    storage verbatim and never parsed for meaning.
    """

    def __init__(
        self,
        store: Any,
        retention_days: int,
        clock: Any = None,
        provenance_of: Callable[[tuple[str, ...], datetime], Any] | None = None,
    ) -> None:
        self._store = store
        self._retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(_UTC))
        # Resolves cited evidence identifiers to the provenance of that
        # evidence. Without it a mail-derived quotation would be written with no
        # origin and no deadline, and D-013 would silently not apply to research
        # written through capabilities. Absent, research records only that AL/X
        # authored it, and citing evidence is refused rather than unstamped.
        self._provenance_of = provenance_of

    def _now(self) -> datetime:
        return self._clock()

    def open_thread(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        try:
            now = self._now()
            proposal = ThreadProposal(
                thread_id=str(values["thread_id"]),
                question=str(values["question"]),
                interest=str(values["interest"]),
                opened_at=now,
                provenance=_policy().non_mail(ContentOrigin.ALX, now),
            )
            snapshot = self._store.open_thread(
                proposal, now + _timedelta(days=self._retention_days)
            )
        except KeyError:
            return _failed(call_id, OPEN_RESEARCH_THREAD, "arguments_unusable")
        except (TypeError, ValueError):
            return _failed(call_id, OPEN_RESEARCH_THREAD, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, OPEN_RESEARCH_THREAD, _code(error))
        return _ok(
            call_id,
            OPEN_RESEARCH_THREAD,
            _thread_values(snapshot),
            _thread_receipt(snapshot),
        )

    def record_entry(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        try:
            now = self._now()
            sources = tuple(values.get("source_references", ()) or ())
            provenance = self._provenance(sources, now)
            proposal = EntryProposal(
                entry_id=str(values["entry_id"]),
                thread_id=str(values["thread_id"]),
                kind=EntryKind(str(values["kind"])),
                content=str(values["content"]),
                recorded_at=now,
                source_references=sources,
                provenance=provenance,
            )
            snapshot = self._store.record_entry(proposal)
        except _EvidenceUnresolved:
            return _failed(call_id, RECORD_RESEARCH_ENTRY, "evidence_unresolved")
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, RECORD_RESEARCH_ENTRY, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, RECORD_RESEARCH_ENTRY, _code(error))
        return _ok(
            call_id,
            RECORD_RESEARCH_ENTRY,
            _entry_values(snapshot),
            _entry_receipt(snapshot),
        )

    def revise_entry(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        """AL/X changes her own thinking. No approval; history is preserved."""
        return self._append_revision(
            call_id, values, REVISE_RESEARCH_ENTRY, RevisionAuthor.ALX
        )

    def correct_entry(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        """Friedl corrects the record, recorded as his correction.

        This is not a wrapper around the revise capability. Both append through
        the same storage primitive, but the author is fixed here and cannot be
        chosen by the caller, so AL/X cannot reach this outcome through the
        ungated revise route or present her own revision as Friedl's.
        """
        return self._append_revision(
            call_id, values, CORRECT_RESEARCH_ENTRY, RevisionAuthor.FRIEDL
        )

    def _append_revision(
        self,
        call_id: str,
        values: Mapping[str, Any],
        capability_id: str,
        author: RevisionAuthor,
    ) -> CapabilityResult:
        """The shared storage step. The author comes from the caller, never
        from the arguments: a value AL/X could supply would let her sign a
        revision as Friedl."""
        try:
            now = self._now()
            sources = tuple(values.get("source_references", ()) or ())
            proposal = EntryRevisionProposal(
                content=str(values["content"]),
                reason=str(values["reason"]),
                recorded_at=now,
                source_references=sources,
                provenance=self._provenance(sources, now),
            )
            snapshot = self._store.revise_entry(
                str(values["entry_id"]),
                proposal,
                int(values["expected_revision"]),
                author,
            )
        except _EvidenceUnresolved:
            return _failed(call_id, capability_id, "evidence_unresolved")
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, capability_id, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, capability_id, _code(error))
        return _ok(
            call_id,
            capability_id,
            _entry_values(snapshot),
            _entry_receipt(snapshot),
        )

    def _provenance(self, sources: tuple[str, ...], now: datetime) -> Any:
        """Provenance for a record, derived from the evidence it cites.

        Mail-derived evidence carries D-013's deadline, and deriving here is
        what makes that deadline reach research written through capabilities
        rather than only research written directly to the store.
        """
        policy = _policy()
        if not sources:
            return policy.non_mail(ContentOrigin.ALX, now)
        if self._provenance_of is None:
            # Refusing is the safe failure. Writing the citation with no origin
            # would create exactly the unstamped mail-derived record D-013
            # forbids.
            raise _EvidenceUnresolved()
        inputs = self._provenance_of(sources, now)
        if inputs is None:
            raise _EvidenceUnresolved()
        return policy.derive(ContentOrigin.ALX, now, tuple(inputs))

    def search(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        try:
            query = ResearchQuery(
                query_id=str(values["query_id"]),
                thread_ids=tuple(values.get("thread_ids", ()) or ()),
                kinds=tuple(
                    EntryKind(str(item)) for item in values.get("kinds", ()) or ()
                ),
                source_references=tuple(values.get("source_references", ()) or ()),
                recorded_after=_time(values.get("recorded_after")),
                recorded_before=_time(values.get("recorded_before")),
                include_archived=bool(values.get("include_archived", False)),
                limit=int(values.get("limit", MAX_RETRIEVAL_LIMIT)),
            )
            found = self._store.retrieve(query)
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, SEARCH_RESEARCH, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, SEARCH_RESEARCH, _code(error))
        return _ok(call_id, SEARCH_RESEARCH, {"entries": [_entry_values(i) for i in found]})

    def read_thread(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        try:
            snapshot = self._store.read_thread(
                str(values["thread_id"]), int(values.get("offset", 0))
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, READ_RESEARCH_THREAD, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, READ_RESEARCH_THREAD, _code(error))
        return _ok(
            call_id,
            READ_RESEARCH_THREAD,
            _thread_values(snapshot),
            _thread_receipt(snapshot),
        )

    def set_status(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        try:
            snapshot = self._store.set_status(
                str(values["thread_id"]), ThreadStatus(str(values["status"]))
            )
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, SET_RESEARCH_STATUS, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, SET_RESEARCH_STATUS, _code(error))
        return _ok(
            call_id,
            SET_RESEARCH_STATUS,
            _thread_values(snapshot),
            _thread_receipt(snapshot),
        )

    def delete(
        self, call_id: str, values: Mapping[str, Any]
    ) -> CapabilityResult:
        try:
            record_id = str(values["record_id"])
            kind = str(values["kind"])
            now = self._now()
            if kind == "thread":
                record = self._store.delete_thread(record_id, now)
            elif kind == "entry":
                record = self._store.delete_entry(record_id, now)
            else:
                return _failed(call_id, DELETE_RESEARCH, "arguments_unusable")
        except (KeyError, TypeError, ValueError):
            return _failed(call_id, DELETE_RESEARCH, "arguments_unusable")
        except Exception as error:
            return _failed(call_id, DELETE_RESEARCH, _code(error))
        values = {
            "record_id": record.record_id,
            "kind": record.kind,
            "deleted_at": record.deleted_at.isoformat(),
        }
        return _ok(
            call_id,
            DELETE_RESEARCH,
            values,
            values,
        )


def _code(error: Exception) -> str:
    """Map a storage failure to a declared code, carrying no research content."""
    name = type(error).__name__
    return {
        "ThreadNotFound": "thread_not_found",
        "EntryNotFound": "entry_not_found",
        "ThreadAlreadyExists": "thread_already_exists",
        "EntryAlreadyExists": "entry_already_exists",
        "EntryRevisionConflict": "revision_conflict",
        "ArchivedThreadWrite": "thread_archived",
    }.get(name, "storage_failed")


def _time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromisoformat(str(value))


def build_notebook_executors(
    store: Any,
    retention_days: int,
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
    provenance_of: Callable[[tuple[str, ...], datetime], Any] | None = None,
) -> Mapping[str, Callable[[Any], CapabilityResult]]:
    """Bind the eight notebook primitives to the one durable store.

    Every capability reaches the notebook through this map and no other way, so
    there is exactly one production path to research storage.
    """
    capabilities = NotebookCapabilities(
        store, retention_days, clock, provenance_of
    )
    return {
        OPEN_RESEARCH_THREAD: lambda values: capabilities.open_thread(
            call_id_source(), values
        ),
        RECORD_RESEARCH_ENTRY: lambda values: capabilities.record_entry(
            call_id_source(), values
        ),
        REVISE_RESEARCH_ENTRY: lambda values: capabilities.revise_entry(
            call_id_source(), values
        ),
        SEARCH_RESEARCH: lambda values: capabilities.search(
            call_id_source(), values
        ),
        READ_RESEARCH_THREAD: lambda values: capabilities.read_thread(
            call_id_source(), values
        ),
        SET_RESEARCH_STATUS: lambda values: capabilities.set_status(
            call_id_source(), values
        ),
        CORRECT_RESEARCH_ENTRY: lambda values: capabilities.correct_entry(
            call_id_source(), values
        ),
        DELETE_RESEARCH: lambda values: capabilities.delete(
            call_id_source(), values
        ),
    }
