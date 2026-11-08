"""She can see her own history, her words carry only the outcome, and a failure
of her own capability is not recorded as a block without the fact in view.

2026-11-08, the Greptile bill: she said she would fix a failure of her own
invoice capture, recorded the goal as genuinely blocked, and nothing picked it
up again. Asked why, she answered from the goal record, which keeps only its
latest state, so the block she had set was no longer there to see. Her
replies stayed at about 300 characters where "Bill captured and approved."
was the whole answer.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResult,
    CapabilityResultState,
    GoalState,
    GoalStatus,
    GoalStopReason,
    Objective,
    SuccessCriterion,
    WorkItem,
)
from alx.core.model_reasoner import (  # noqa: E402
    PROTOCOL_INSTRUCTIONS,
    _goal_outcome,
    decision_schema,
)
from alx.tools.self_diagnosis import (  # noqa: E402
    DEFINITIONS,
    READ_OWN_SESSIONS,
    build_self_diagnosis_executors,
)

GOAL = "34db4f47-18bc-47fa-999a-b6d2dd9691df"


def _event(kind: str, **item) -> str:
    role = "assistant" if kind == "tool_use" else "user"
    return json.dumps({"type": role, "message": {"content": [{"type": kind, **item}]}})


def _record(folder: Path, name: str, said: str, lines: list[str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    header = {
        "started_at": "2026-11-08T08:57:37+00:00", "ended_at": "2026-11-08T08:59:10+00:00",
        "purpose": "interpreting_request", "outcome": "finished",
        "user_prompt": json.dumps({
            "current_trigger": {"kind": "conversation_turn", "reference": None},
            "conversation": [{"content": said}],
        }),
    }
    (folder / name).write_text("\n".join([json.dumps(header), *lines]) + "\n", encoding="utf-8")


class ReadOwnSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        _record(self.root / "2026-11-08", "085910-aaaaaaaa.jsonl", "Put the Greptile bill in", [
            _event("tool_use", id="t1", name="mcp__alx__call_capability",
                   input={"capability_id": "capture_supplier_invoice", "arguments_json": "{}"}),
            _event("tool_result", tool_use_id="t1", content=[{"type": "text", "text": json.dumps({
                "result_state": "failed", "failure": {"code": "source_mismatch"},
                "result_values": {"steps": ["executed_bill"]}})}]),
            _event("tool_use", id="t2", name="mcp__alx__update_goal", input={"goal_id": GOAL}),
            _event("tool_result", tool_use_id="t2", content=[{"type": "text", "text": json.dumps({
                "goal_updated": {"goal_id": GOAL, "status": "blocked",
                                 "stop_reason": "genuinely_blocked",
                                 "blockers": ["bl-source-mismatch"], "outstanding_work": []}})}]),
            _event("tool_use", id="t3", name="StructuredOutput", input={"action": {
                "type": "respond", "response": "Not in Xero yet. I'll fix it.",
                "detail": "Refused twice: source_mismatch."}}),
        ])
        _record(self.root / "2026-11-08", "095201-bbbbbbbb.jsonl", "yes please", [])
        self.read = build_self_diagnosis_executors(
            None, None, lambda: "call-1",
            clock=lambda: datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
            sessions_dir=self.root,
        )[READ_OWN_SESSIONS]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_block_she_set_is_visible_with_what_led_to_it(self) -> None:
        result = self.read({"goal_id": GOAL})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        [session] = result.values["sessions"]
        self.assertEqual(session["trigger"]["said"], "Put the Greptile bill in")
        capture, update = session["steps"]
        self.assertEqual(capture["capability"], "capture_supplier_invoice")
        self.assertEqual(capture["result"]["failure"]["code"], "source_mismatch")
        self.assertEqual(update["result"]["status"], "blocked")
        self.assertEqual(update["result"]["stop_reason"], "genuinely_blocked")
        self.assertEqual(session["answer"]["response"], "Not in Xero yet. I'll fix it.")
        self.assertEqual(session["answer"]["detail"], "Refused twice: source_mismatch.")

    def test_the_window_and_limit_keep_the_newest(self) -> None:
        result = self.read({"limit": 1})
        self.assertEqual([s["trigger"]["said"] for s in result.values["sessions"]], ["yes please"])
        self.assertTrue(result.values["truncated"])
        early = self.read({"to": "2026-11-08T09:00:00+00:00"})
        self.assertEqual(len(early.values["sessions"]), 1)

    def test_bad_arguments_and_missing_records_fail_plainly(self) -> None:
        self.assertEqual(self.read({"limit": 99}).failure["code"], "arguments_unusable")
        unkept = build_self_diagnosis_executors(None, None, lambda: "c")[READ_OWN_SESSIONS]
        self.assertEqual(unkept({}).failure["code"], "unavailable")

    def test_it_is_a_read_only_capability(self) -> None:
        definition = next(item for item in DEFINITIONS if item.capability_id == READ_OWN_SESSIONS)
        self.assertEqual(definition.side_effect.name, "NONE")
        self.assertIn("latest state", definition.purpose)


def _goal(status: GoalStatus, result_state: CapabilityResultState) -> GoalState:
    call = CapabilityCall("c-1", "capture_supplier_invoice", {}, None)
    attempt = CapabilityAttempt(
        call, CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult("c-1", "capture_supplier_invoice", result_state, {},
                         None if result_state is CapabilityResultState.SUCCEEDED
                         else {"code": "source_mismatch"}))
    state = GoalState(GOAL, Objective("turn:t", "Put the Greptile bill in Xero"),
                      (SuccessCriterion("sc-bill", "Bill captured"),), attempts=(attempt,))
    if status is GoalStatus.BLOCKED:
        return replace(state, status=status, stop_reason=GoalStopReason.GENUINELY_BLOCKED,
                       blockers=(WorkItem("bl-source-mismatch", "Capture refused"),))
    return state


class OwnCapabilityFailureTests(unittest.TestCase):
    def test_blocking_after_her_own_capability_failed_puts_the_fact_in_view(self) -> None:
        outcome = _goal_outcome(_goal(GoalStatus.BLOCKED, CapabilityResultState.FAILED))
        failed = outcome["own_capability_failed"]
        self.assertEqual(failed["capability_id"], "capture_supplier_invoice")
        self.assertEqual(failed["code"], "source_mismatch")
        self.assertIn("toolbox gap", failed["note"])

    def test_no_note_when_not_blocked_or_nothing_failed(self) -> None:
        self.assertNotIn("own_capability_failed",
                         _goal_outcome(_goal(GoalStatus.ACTIVE, CapabilityResultState.FAILED)))
        self.assertNotIn("own_capability_failed",
                         _goal_outcome(_goal(GoalStatus.BLOCKED, CapabilityResultState.SUCCEEDED)))


class ReplyLevelTests(unittest.TestCase):
    def test_her_words_and_their_detail_have_separate_places(self) -> None:
        respond = decision_schema()["properties"]["action"]["anyOf"][0]
        self.assertIn("detail", respond["required"])
        self.assertIn("spoken aloud", respond["properties"]["response"]["description"])
        self.assertIn("Not spoken or shown", respond["properties"]["detail"]["description"])

    def test_her_examples_are_at_the_level_he_wants(self) -> None:
        self.assertIn('"Captured and approved."', PROTOCOL_INSTRUCTIONS)
        # An example carrying an identifier taught the opposite of the rule.
        self.assertNotIn("5779714592", PROTOCOL_INSTRUCTIONS)

    def test_why_questions_are_answered_from_her_session_records(self) -> None:
        self.assertIn("read your session records", PROTOCOL_INSTRUCTIONS)
        self.assertIn("When Friedl says something is done, check it once", PROTOCOL_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
