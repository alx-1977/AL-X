"""SQLite persistence for AL/X goal records without conversational authority."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from alx.contracts import (
    Approval, ApprovalLifecycle, ApprovalScope, CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationTurn, Evidence, GoalState,
    GoalStatus, GoalStopReason, MemoryKind, MemoryProposal, Objective,
    PendingMemoryBatch, ProgressRecord, Referent, SuccessCriterion, WorkItem,
    GoalSnapshot,
)


SCHEMA_VERSION = 3


class GoalStoreError(Exception):
    """Base error for deterministic durable-goal store failures."""


class GoalNotFound(GoalStoreError):
    pass


class GoalAlreadyExists(GoalStoreError):
    pass


class GoalRevisionConflict(GoalStoreError):
    pass


class UnsupportedSchema(GoalStoreError):
    pass


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _time_to_data(value: datetime | None) -> str | None:
    if value is None:
        return None
    _aware(value, "datetime")
    return value.isoformat()


def _time_from_data(value: str | None) -> datetime | None:
    if value is None:
        return None
    result = datetime.fromisoformat(value)
    _aware(result, "persisted datetime")
    return result


def _data(value: Any) -> Any:
    """Convert immutable structured contract values to JSON-compatible values."""
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_data(item) for item in value]
    return value


def _goal_to_data(goal: GoalState) -> dict[str, Any]:
    return {
        "objective": [goal.objective.source_reference, goal.objective.summary],
        "success_criteria": [[item.criterion_id, item.description] for item in goal.success_criteria],
        "context": _data(goal.context),
        "referents": [[item.referent_id, _data(item.attributes)] for item in goal.referents],
        "decisions": [[item.record_id, item.summary, list(item.evidence_refs)] for item in goal.decisions],
        "corrections": [[item.record_id, item.summary, list(item.evidence_refs)] for item in goal.corrections],
        "progress": [[item.record_id, item.summary, list(item.evidence_refs)] for item in goal.progress],
        "attempts": [
            [None if item.call is None else item.call.call_id, None if item.call is None else item.call.capability_id, None if item.call is None else _data(item.call.arguments), None if item.call is None else item.call.approval_id, item.disposition.value, item.implementation_invoked,
             None if item.result is None else [item.result.call_id, item.result.capability_id, item.result.state.value, _data(item.result.values), _data(item.result.failure), list(item.result.evidence_refs)], item.reason_code]
            for item in goal.attempts
        ],
        "blockers": [[item.item_id, item.summary] for item in goal.blockers],
        "outstanding_work": [[item.item_id, item.summary] for item in goal.outstanding_work],
        "evidence": [[item.evidence_id, item.kind, _data(item.attributes), list(item.supports)] for item in goal.evidence],
        "approvals": [
            [item.approval_id, item.scope.capability_id, _data(item.scope.arguments), item.lifecycle.value, _time_to_data(item.expires_at)]
            for item in goal.approvals
        ],
        "status": goal.status.value,
        "stop_reason": None if goal.stop_reason is None else goal.stop_reason.value,
    }


def _progress(values: list[list[Any]]) -> tuple[ProgressRecord, ...]:
    return tuple(ProgressRecord(item[0], item[1], tuple(item[2])) for item in values)


def _attempts(data: dict[str, Any]) -> tuple[CapabilityAttempt, ...]:
    if "attempts" not in data:
        return tuple(
            CapabilityAttempt(None, CapabilityAttemptDisposition.LEGACY, None,
                              CapabilityResult(item[0], item[1], CapabilityResultState(item[2]), item[3], item[4], tuple(item[5])), "legacy_v1")
            for item in data.get("completed_actions", [])
        )
    values = []
    for item in data["attempts"]:
        result_data = item[6]
        if result_data is None:
            result = None
        elif len(result_data) == 4:
            result = CapabilityResult(item[0], item[1], CapabilityResultState(result_data[0]), result_data[1], result_data[2], tuple(result_data[3]))
        else:
            result = CapabilityResult(result_data[0], result_data[1], CapabilityResultState(result_data[2]), result_data[3], result_data[4], tuple(result_data[5]))
        call = None if item[0] is None else CapabilityCall(item[0], item[1], item[2], item[3])
        values.append(CapabilityAttempt(call, CapabilityAttemptDisposition(item[4]), item[5], result, item[7]))
    return tuple(values)


def _goal_from_data(goal_id: str, data: dict[str, Any]) -> GoalState:
    return GoalState(
        goal_id=goal_id,
        objective=Objective(*data["objective"]),
        success_criteria=tuple(SuccessCriterion(*item) for item in data["success_criteria"]),
        context=data["context"],
        referents=tuple(Referent(*item) for item in data["referents"]),
        decisions=_progress(data["decisions"]),
        corrections=_progress(data["corrections"]),
        progress=_progress(data["progress"]),
        attempts=_attempts(data),
        blockers=tuple(WorkItem(*item) for item in data["blockers"]),
        outstanding_work=tuple(WorkItem(*item) for item in data["outstanding_work"]),
        evidence=tuple(Evidence(item[0], item[1], item[2], tuple(item[3])) for item in data["evidence"]),
        approvals=tuple(
            Approval(item[0], ApprovalScope(item[1], item[2]), ApprovalLifecycle(item[3]), _time_from_data(item[4]))
            for item in data["approvals"]
        ),
        status=GoalStatus(data["status"]),
        stop_reason=None if data["stop_reason"] is None else GoalStopReason(data["stop_reason"]),
    )


def _turn_to_data(turn: ConversationTurn) -> str:
    return json.dumps(
        [turn.conversation_id, turn.turn_id, turn.origin.value, turn.content, _time_to_data(turn.occurred_at), turn.person_id],
        separators=(",", ":"),
    )


def _turn_from_data(value: str) -> ConversationTurn:
    data = json.loads(value)
    conversation_id, turn_id, origin, content, occurred_at = data[:5]
    person_id = None if len(data) == 5 else data[5]
    parsed = _time_from_data(occurred_at)
    assert parsed is not None
    return ConversationTurn(conversation_id, turn_id, ConversationOrigin(origin), content, parsed, person_id)


def _memory_proposal_to_data(proposal: MemoryProposal) -> str:
    return json.dumps(
        {
            "memory_id": proposal.memory_id,
            "kind": proposal.kind.value,
            "content": proposal.content,
            "source_references": list(proposal.source_references),
            "formed_at": _time_to_data(proposal.formed_at),
            "person_id": proposal.person_id,
            "meaning": proposal.meaning,
            "supersedes_memory_id": proposal.supersedes_memory_id,
        },
        separators=(",", ":"),
    )


def _memory_proposal_from_data(value: str) -> MemoryProposal:
    data = json.loads(value)
    formed_at = _time_from_data(data["formed_at"])
    assert formed_at is not None
    return MemoryProposal(
        memory_id=data["memory_id"],
        kind=MemoryKind(data["kind"]),
        content=data["content"],
        source_references=tuple(data["source_references"]),
        formed_at=formed_at,
        person_id=data["person_id"],
        meaning=data["meaning"],
        supersedes_memory_id=data["supersedes_memory_id"],
    )


class SQLiteGoalStore:
    """Small transactional store; it persists facts but never reads their meaning."""

    def __init__(self, database_path: str | Path) -> None:
        self._connection = sqlite3.connect(str(database_path))
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteGoalStore:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            self._connection.close()
            raise UnsupportedSchema(f"database schema {version} is newer than supported schema {SCHEMA_VERSION}")
        if version == SCHEMA_VERSION:
            return
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS goals (goal_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, retention_until TEXT NOT NULL, state_json TEXT NOT NULL)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS conversation_turns (goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE, ordinal INTEGER NOT NULL, turn_id TEXT NOT NULL, turn_json TEXT NOT NULL, PRIMARY KEY(goal_id, ordinal), UNIQUE(goal_id, turn_id))"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS pending_memory_batches (goal_id TEXT NOT NULL REFERENCES goals(goal_id) ON DELETE CASCADE, goal_revision INTEGER NOT NULL, ordinal INTEGER NOT NULL, proposal_json TEXT NOT NULL, retention_until TEXT NOT NULL, PRIMARY KEY(goal_id, goal_revision, ordinal))"
            )
            self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def create(self, state: GoalState, turns: Iterable[ConversationTurn], retention_until: datetime) -> GoalSnapshot:
        _aware(retention_until, "retention_until")
        turn_list = tuple(turns)
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO goals(goal_id, revision, retention_until, state_json) VALUES (?, ?, ?, ?)",
                    (state.goal_id, 1, _time_to_data(retention_until), json.dumps(_goal_to_data(state), separators=(",", ":"))),
                )
                self._write_turns(state.goal_id, turn_list)
        except sqlite3.IntegrityError as error:
            if self._connection.execute("SELECT 1 FROM goals WHERE goal_id = ?", (state.goal_id,)).fetchone():
                raise GoalAlreadyExists(state.goal_id) from error
            raise
        return GoalSnapshot(state, turn_list, 1, retention_until)

    def load(self, goal_id: str) -> GoalSnapshot:
        row = self._connection.execute(
            "SELECT revision, retention_until, state_json FROM goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        if row is None:
            raise GoalNotFound(goal_id)
        turns = tuple(
            _turn_from_data(item[0])
            for item in self._connection.execute(
                "SELECT turn_json FROM conversation_turns WHERE goal_id = ? ORDER BY ordinal", (goal_id,)
            )
        )
        return GoalSnapshot(_goal_from_data(goal_id, json.loads(row[2])), turns, row[0], _time_from_data(row[1]))  # type: ignore[arg-type]

    def list_goals(self) -> tuple[GoalSnapshot, ...]:
        identifiers = self._connection.execute("SELECT goal_id FROM goals ORDER BY goal_id").fetchall()
        return tuple(self.load(item[0]) for item in identifiers)

    def replace(self, state: GoalState, turns: Iterable[ConversationTurn], retention_until: datetime, expected_revision: int) -> GoalSnapshot:
        _aware(retention_until, "retention_until")
        turn_list = tuple(turns)
        with self._connection:
            self._replace_rows(state, turn_list, retention_until, expected_revision)
        return GoalSnapshot(state, turn_list, expected_revision + 1, retention_until)

    def replace_with_memory_batch(
        self,
        state: GoalState,
        turns: Iterable[ConversationTurn],
        retention_until: datetime,
        expected_revision: int,
        proposals: tuple[MemoryProposal, ...],
    ) -> GoalSnapshot:
        """Commit a goal revision and its exact memory intent in one transaction."""
        _aware(retention_until, "retention_until")
        proposal_list = tuple(proposals)
        if not proposal_list:
            raise ValueError("replace_with_memory_batch requires proposals")
        turn_list = tuple(turns)
        goal_revision = expected_revision + 1
        with self._connection:
            self._replace_rows(state, turn_list, retention_until, expected_revision)
            self._connection.executemany(
                "INSERT INTO pending_memory_batches(goal_id, goal_revision, ordinal, proposal_json, retention_until) VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        state.goal_id,
                        goal_revision,
                        ordinal,
                        _memory_proposal_to_data(proposal),
                        _time_to_data(retention_until),
                    )
                    for ordinal, proposal in enumerate(proposal_list)
                ),
            )
        return GoalSnapshot(state, turn_list, goal_revision, retention_until)

    def pending_memory_batches(self, goal_id: str) -> tuple[PendingMemoryBatch, ...]:
        rows = self._connection.execute(
            "SELECT goal_revision, proposal_json, retention_until FROM pending_memory_batches WHERE goal_id = ? ORDER BY goal_revision, ordinal",
            (goal_id,),
        ).fetchall()
        batches: list[PendingMemoryBatch] = []
        current_revision: int | None = None
        current_retention: datetime | None = None
        current_proposals: list[MemoryProposal] = []
        for goal_revision, proposal_json, retention_value in rows:
            if current_revision is not None and goal_revision != current_revision:
                assert current_retention is not None
                batches.append(
                    PendingMemoryBatch(
                        goal_id,
                        current_revision,
                        tuple(current_proposals),
                        current_retention,
                    )
                )
                current_proposals = []
            current_revision = goal_revision
            current_retention = _time_from_data(retention_value)
            current_proposals.append(_memory_proposal_from_data(proposal_json))
        if current_revision is not None:
            assert current_retention is not None
            batches.append(
                PendingMemoryBatch(
                    goal_id,
                    current_revision,
                    tuple(current_proposals),
                    current_retention,
                )
            )
        return tuple(batches)

    def acknowledge_memory_batch(self, goal_id: str, goal_revision: int) -> None:
        with self._connection:
            self._connection.execute(
                "DELETE FROM pending_memory_batches WHERE goal_id = ? AND goal_revision = ?",
                (goal_id, goal_revision),
            )

    def delete(self, goal_id: str, expected_revision: int) -> None:
        with self._connection:
            deleted = self._connection.execute(
                "DELETE FROM goals WHERE goal_id = ? AND revision = ?", (goal_id, expected_revision)
            ).rowcount
            if not deleted:
                if self._connection.execute("SELECT 1 FROM goals WHERE goal_id = ?", (goal_id,)).fetchone():
                    raise GoalRevisionConflict(goal_id)
                raise GoalNotFound(goal_id)

    def purge_expired(self, at: datetime) -> tuple[str, ...]:
        _aware(at, "at")
        with self._connection:
            rows = self._connection.execute(
                "SELECT goal_id, retention_until FROM goals ORDER BY goal_id"
            ).fetchall()
            identifiers = tuple(
                item[0] for item in rows if _time_from_data(item[1]) <= at
            )
            self._connection.executemany(
                "DELETE FROM goals WHERE goal_id = ?", ((identifier,) for identifier in identifiers)
            )
        return identifiers

    def _write_turns(self, goal_id: str, turns: tuple[ConversationTurn, ...]) -> None:
        for ordinal, turn in enumerate(turns):
            self._connection.execute(
                "INSERT INTO conversation_turns(goal_id, ordinal, turn_id, turn_json) VALUES (?, ?, ?, ?)",
                (goal_id, ordinal, turn.turn_id, _turn_to_data(turn)),
            )

    def _replace_rows(
        self,
        state: GoalState,
        turns: tuple[ConversationTurn, ...],
        retention_until: datetime,
        expected_revision: int,
    ) -> None:
        updated = self._connection.execute(
            "UPDATE goals SET revision = revision + 1, retention_until = ?, state_json = ? WHERE goal_id = ? AND revision = ?",
            (_time_to_data(retention_until), json.dumps(_goal_to_data(state), separators=(",", ":")), state.goal_id, expected_revision),
        ).rowcount
        if not updated:
            if self._connection.execute("SELECT 1 FROM goals WHERE goal_id = ?", (state.goal_id,)).fetchone():
                raise GoalRevisionConflict(state.goal_id)
            raise GoalNotFound(state.goal_id)
        self._connection.execute("DELETE FROM conversation_turns WHERE goal_id = ?", (state.goal_id,))
        self._write_turns(state.goal_id, turns)
