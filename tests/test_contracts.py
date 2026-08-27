from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    Approval, ApprovalLifecycle, ApprovalScope, BackgroundEvent, CapabilityCall, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationTurn, Evidence, GoalState,
    GoalStatus, GoalStopReason, Objective, SuccessCriterion, WorkItem,
)


NOW = datetime(2026, 8, 27, tzinfo=UTC)


def goal(**changes: object) -> GoalState:
    values: dict[str, object] = {
        "goal_id": "goal-1",
        "objective": Objective("turn-1", "prepared objective"),
        "success_criteria": (SuccessCriterion("criterion-1", "supported outcome"),),
    }
    values.update(changes)
    return GoalState(**values)  # type: ignore[arg-type]


class ConversationContractTests(unittest.TestCase):
    def test_typed_and_speech_have_structural_parity(self) -> None:
        typed = ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED, "same words", NOW)
        speech = ConversationTurn("conversation-1", "turn-2", ConversationOrigin.SPEECH_TRANSCRIPT, "same words", NOW)
        self.assertEqual(set(typed.__dataclass_fields__), set(speech.__dataclass_fields__))
        self.assertEqual(typed.content, speech.content)

    def test_background_event_is_structured_and_immutable(self) -> None:
        event = BackgroundEvent("event-1", "source_change", NOW, {"source_id": "source-1"})
        self.assertEqual(event.data["source_id"], "source-1")
        with self.assertRaises(TypeError):
            event.data["source_id"] = "other"

    def test_conversation_and_event_times_must_be_timezone_aware(self) -> None:
        naive = datetime(2026, 8, 27)
        with self.assertRaises(ValueError):
            ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED, "words", naive)
        with self.assertRaises(ValueError):
            BackgroundEvent("event-1", "source_change", naive)


class CapabilityContractTests(unittest.TestCase):
    def test_calls_and_results_have_structured_language_blind_boundaries(self) -> None:
        call = CapabilityCall("call-1", "capability-1", {"record_id": "record-1"})
        result = CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, {"found": True})
        self.assertEqual(call.arguments["record_id"], "record-1")
        self.assertEqual(result.values["found"], True)
        with self.assertRaises(TypeError):
            CapabilityCall("call-2", "capability-1", {"turn": ConversationTurn("c", "t", ConversationOrigin.TYPED, "words", NOW)})

    def test_partial_and_failed_results_are_distinct_and_validated(self) -> None:
        partial = CapabilityResult("call-1", "capability-1", CapabilityResultState.PARTIAL, {"available": 1})
        failed = CapabilityResult("call-2", "capability-1", CapabilityResultState.FAILED, failure={"code": "unavailable"})
        self.assertEqual(partial.state, CapabilityResultState.PARTIAL)
        self.assertEqual(failed.failure["code"], "unavailable")
        with self.assertRaises(ValueError):
            CapabilityResult("call-3", "capability-1", CapabilityResultState.FAILED)


class GoalContractTests(unittest.TestCase):
    def test_active_goal_continues_and_terminal_states_are_limited(self) -> None:
        self.assertTrue(goal().continues)
        completed = goal(status=GoalStatus.COMPLETED, stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET, evidence=(Evidence("evidence-1", "observation", supports=("criterion-1",)),))
        self.assertFalse(completed.continues)
        with self.assertRaises(ValueError):
            goal(status=GoalStatus.COMPLETED, stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET)

    def test_blocked_goal_requires_a_blocker(self) -> None:
        with self.assertRaises(ValueError):
            goal(status=GoalStatus.BLOCKED, stop_reason=GoalStopReason.GENUINELY_BLOCKED)
        blocked = goal(status=GoalStatus.BLOCKED, stop_reason=GoalStopReason.GENUINELY_BLOCKED, blockers=(WorkItem("blocker-1", "missing required information"),))
        self.assertEqual(blocked.status, GoalStatus.BLOCKED)

    def test_approval_has_exact_action_scope_and_lifecycle(self) -> None:
        scope = ApprovalScope("capability-1", {"record_id": "record-1"})
        approval = Approval("approval-1", scope, ApprovalLifecycle.GRANTED, NOW + timedelta(minutes=1))
        permitted = CapabilityCall("call-1", "capability-1", {"record_id": "record-1"}, "approval-1")
        different_scope = CapabilityCall("call-2", "capability-1", {"record_id": "record-2"}, "approval-1")
        self.assertTrue(approval.permits(permitted, NOW))
        self.assertFalse(approval.permits(different_scope, NOW))
        self.assertIn(ApprovalLifecycle.DENIED, ApprovalLifecycle)
        with self.assertRaises(ValueError):
            approval.permits(permitted, datetime(2026, 8, 27))
        with self.assertRaises(ValueError):
            goal(status=GoalStatus.AWAITING_APPROVAL, stop_reason=GoalStopReason.REQUIRED_APPROVAL)

    def test_records_and_nested_data_are_immutable(self) -> None:
        item = CapabilityCall("call-1", "capability-1", {"nested": {"value": 1}})
        with self.assertRaises(FrozenInstanceError):
            item.call_id = "other"  # type: ignore[misc]
        with self.assertRaises(TypeError):
            item.arguments["nested"]["value"] = 2  # type: ignore[index]
        state = goal(success_criteria=[SuccessCriterion("criterion-1", "supported outcome")])
        self.assertIsInstance(state.success_criteria, tuple)

    def test_invalid_goal_stop_reason_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            goal(status=GoalStatus.CANCELLED, stop_reason=GoalStopReason.REQUIRED_INPUT)

    def test_awaiting_input_requires_recorded_outstanding_work(self) -> None:
        with self.assertRaises(ValueError):
            goal(status=GoalStatus.AWAITING_INPUT, stop_reason=GoalStopReason.REQUIRED_INPUT)
        waiting = goal(
            status=GoalStatus.AWAITING_INPUT,
            stop_reason=GoalStopReason.REQUIRED_INPUT,
            outstanding_work=(WorkItem("needed-1", "required information"),),
        )
        self.assertEqual(waiting.status, GoalStatus.AWAITING_INPUT)

    def test_completed_goal_rejects_unresolved_state_and_blank_references(self) -> None:
        with self.assertRaises(ValueError):
            goal(
                status=GoalStatus.COMPLETED,
                stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET,
                blockers=(WorkItem("blocker-1", "unresolved"),),
                evidence=(Evidence("evidence-1", "observation", supports=("criterion-1",)),),
            )
        with self.assertRaises(ValueError):
            CapabilityResult("call-1", "capability-1", CapabilityResultState.SUCCEEDED, evidence_refs=("",))


if __name__ == "__main__":
    unittest.main()
