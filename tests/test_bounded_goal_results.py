"""All durable attempt results share one bounded recording boundary."""

import hashlib
import json
import sqlite3
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResult, CapabilityResultState, Evidence, GoalState, GoalStatus,
    GoalStopReason, MemoryKind, MemoryProposal, Objective, SuccessCriterion,
)
from alx.core.model_reasoner import _attempt_payload, _state_payload
from alx.goals import SQLiteGoalStore
from alx.goals.result_summary import LIST_LIMIT, RESULT_LIMIT, STRING_LIMIT, bound_result
from alx.goals.store import SCHEMA_VERSION, _goal_to_data

NOW = datetime(2026, 10, 24, tzinfo=UTC)


def goal(values, failure=None, capability_id="read_coding_job"):
    result = CapabilityResult(
        "call-1", capability_id,
        CapabilityResultState.SUCCEEDED if failure is None else CapabilityResultState.FAILED,
        values, failure,
    )
    return GoalState(
        "goal-1", Objective("turn:1", "inspect the result"),
        (SuccessCriterion("checked", "result checked"),),
        attempts=(CapabilityAttempt(
            CapabilityCall(result.call_id, result.capability_id, {}),
            CapabilityAttemptDisposition.EXECUTED, True, result,
        ),),
    )


@pytest.mark.parametrize("field", ["git_diff", "content"])
@pytest.mark.parametrize("write", ["create", "replace", "memory_batch"])
def test_large_results_keep_structure_identity_and_whole_immediate_observation(field, write):
    text = "x" * 500_000
    state = goal({field: text, "report": {"files": ["module.py"]},
                  "exit_code": 0, "done": True, "job_id": "job-7"},
                 capability_id="read_source" if field == "content" else "read_coding_job")
    with SQLiteGoalStore(":memory:", clock=lambda: NOW) as store:
        saved = store.create(state if write == "create" else replace(state, attempts=()),
                             "conversation-1", NOW + timedelta(days=1))
        if write == "replace":
            saved = store.replace(state, saved.retention_until, saved.revision)
        elif write == "memory_batch":
            proposal = MemoryProposal("memory-1", MemoryKind.FACTUAL, "Result inspected",
                                      ("attempt:call-1",), NOW)
            saved = store.replace_with_memory_batch(
                state, saved.retention_until, saved.revision, (proposal,))
        assert _state_payload(saved.state)["attempts"][0]["result_values"][field] == text
        assert _attempt_payload(state.attempts[0])["result_values"][field] == text
        loaded = store.load(state.goal_id)
        values = _state_payload(loaded.state)["attempts"][0]["result_values"]
        summary = values[field]
        assert summary == {"head": text[:STRING_LIMIT], "original_length": len(text),
                           "sha256": hashlib.sha256(text.encode()).hexdigest()}
        assert (values["exit_code"], values["done"], values["job_id"]) == (0, True, "job-7")
        assert values["report"] == {"files": ["module.py"]}
        history, _ = store.read_history(goal_id=state.goal_id)
        recorded = history["goals"][0]["state"]["attempts"][0][6]
        assert len(json.dumps(recorded)) <= RESULT_LIMIT
        assert recorded[3] == values
        # Replacing any other goal field must not re-expand or re-summarize.
        store.replace(loaded.state, loaded.retention_until, loaded.revision)
        assert store.load(state.goal_id).state == loaded.state


def test_failure_and_values_share_one_serialized_budget():
    text = "\U0001f600\n\"\\" * 100_000
    state = goal({"partial_log": text}, {"code": "unavailable", "details": text})
    with SQLiteGoalStore(":memory:") as store:
        saved = store.create(state, "c1", NOW + timedelta(days=1))
        assert saved.state.attempts[0].result.failure["details"] == text
        result = store.load(state.goal_id).state.attempts[0].result
        assert result.failure["code"] == "unavailable"
        assert result.failure["details"]["original_length"] == len(text)
        assert result.failure["details"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert len(json.dumps(_goal_to_data(store.load(state.goal_id).state)["attempts"][0][6])) <= RESULT_LIMIT


def test_list_and_aggregate_omissions_are_explicit_and_idempotent():
    listed = bound_result({"items": list(range(1000))})
    assert len(listed["items"]) == LIST_LIMIT
    assert listed["items"][-1] == {"_omitted_items": 1000 - LIST_LIMIT + 1}
    listed = bound_result({"items": ["x" * 5000 for _ in range(1000)]})["items"]
    assert listed[-1]["_omitted_items"] == 1000 - len(listed) + 1
    for values in (
        {str(index): "a" * 5000 for index in range(1000)},
        {"items": [{"log": "\u2603" * 5000} for _ in range(1000)]},
        {"_omitted_items": 0, "huge-key" * 2000: 1},
    ):
        bounded = bound_result(values)
        assert len(json.dumps(bounded)) <= RESULT_LIMIT
        assert "_omitted_items" in json.dumps(bounded)
        assert bound_result(bounded) == bounded


def test_legacy_migration_shrinks_disk_preserves_metadata_and_completion_evidence(tmp_path):
    path = tmp_path / "goals.sqlite3"
    state = goal({"git_diff": "x" * 500_000})
    evidence = Evidence("verified", "inspection", supports=("checked",),
                        source_references=("attempt:call-1",))
    state = replace(state, evidence=(evidence,), status=GoalStatus.COMPLETED,
                    stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET)
    with SQLiteGoalStore(path, clock=lambda: NOW) as store:
        store.create(state, "conversation-1", NOW + timedelta(days=1))
    with sqlite3.connect(path) as connection:
        data = json.loads(connection.execute("SELECT state_json FROM goals").fetchone()[0])
        data["attempts"][0][6][3] = {"git_diff": "x" * 500_000}
        connection.execute("UPDATE goals SET state_json = ?", (json.dumps(data),))
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION - 1}")
        before = connection.execute(
            "SELECT revision, updated_at, retention_until, finished_at FROM goals"
        ).fetchone()
    with SQLiteGoalStore(path, clock=lambda: NOW + timedelta(hours=1)) as store:
        loaded = store.load(state.goal_id)
        assert loaded.state.status is GoalStatus.COMPLETED
        assert loaded.state.evidence == (evidence,)
        assert loaded.state.attempts[0].result.call_id == "call-1"
        assert loaded.state.attempts[0].result.values["git_diff"]["original_length"] == 500_000
        row = store._connection.execute("SELECT state_json FROM goals").fetchone()[0]
        assert len(row) < 10_000
        assert store._connection.execute(
            "SELECT revision, updated_at, retention_until, finished_at FROM goals"
        ).fetchone() == before
    with SQLiteGoalStore(path) as store:
        assert store._connection.execute("SELECT state_json FROM goals").fetchone()[0] == row


def test_no_projection_size_replacement_path_remains():
    from alx.core import model_reasoner
    assert not hasattr(model_reasoner, "_stored_result")
