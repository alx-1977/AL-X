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
from datetime import UTC, datetime, timedelta
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
                   input={"capability_id": "xero", "arguments_json": "{}"}),
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
        self.assertEqual(capture["capability"], "xero")
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


class SessionPrivacyAndDetailTests(unittest.TestCase):
    """Review of 14c9887: mail kept its D-013 expiry only by luck, a long detail
    could be stored but not read back, and failures skipped masking."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.long_detail = "Figure. " * 400
        _record(self.root / "2026-11-08", "085910-cccccccc.jsonl", "Capture the bill", [
            _event("tool_use", id="t1", name="mcp__alx__call_capability",
                   input={"capability_id": "mail", "arguments_json": json.dumps(
                       {"action": "list_attachments", "mailbox_id": "INBOX", "uid_validity": "1376545928", "uid": "61560"})}),
            _event("tool_result", tool_use_id="t1", content=[{"type": "text", "text": json.dumps({
                "result_state": "failed",
                "failure": {"code": "provider_failed", "service_message": "api_key=abc123secretvalue"},
                "result_values": {}})}]),
            _event("tool_use", id="t2", name="StructuredOutput", input={"action": {
                "type": "respond", "response": "Captured.", "detail": self.long_detail}}),
        ])
        self.read = build_self_diagnosis_executors(
            None, None, lambda: "call-1",
            clock=lambda: datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
            sessions_dir=self.root, session_days=7,
        )[READ_OWN_SESSIONS]

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_session_that_read_mail_carries_its_bookmarks_and_an_expiry(self) -> None:
        result = self.read({})
        provenance = result.provenance
        self.assertIsNotNone(provenance)
        self.assertEqual([ref.uid for ref in provenance.mail_references], ["61560"])
        self.assertEqual(provenance.content_expires_at, datetime(2026, 11, 15, 8, 59, 10, tzinfo=UTC))

    def test_the_goal_audit_keeps_only_which_sessions_were_read(self) -> None:
        result = self.read({})
        self.assertEqual(dict(result.durable_values),
                         {"sessions": ("2026-11-08/085910-cccccccc.jsonl",), "truncated": False})

    def test_one_named_session_returns_the_whole_detail(self) -> None:
        brief = self.read({}).values["sessions"][0]["answer"]["detail"]
        self.assertLess(len(brief), len(self.long_detail.strip()))
        full = self.read({"session": "2026-11-08/085910-cccccccc.jsonl"})
        self.assertEqual(full.values["sessions"][0]["answer"]["detail"], self.long_detail.strip())

    def test_a_session_name_cannot_point_outside_the_records(self) -> None:
        for name in ("../../etc/passwd", "2026-11-08/../../x.jsonl", "/etc/hosts"):
            with self.subTest(name=name):
                self.assertEqual(self.read({"session": name}).failure["code"], "arguments_unusable")

    def test_failures_are_masked_like_everything_else(self) -> None:
        step = self.read({}).values["sessions"][0]["steps"][0]
        self.assertNotIn("abc123secretvalue", json.dumps(dict(step["result"]["failure"])))


class SessionContextProvenanceTests(unittest.TestCase):
    """Review of 12e350a: a session's words can come from conversation turns
    about mail that carry no bookmark in the record. Their D-013 sources live in
    the conversation store, and the session's words inherit them."""

    TURN = ("conv-1", "turn-mail")

    def setUp(self) -> None:
        from alx.contracts import MailReference
        from alx.contracts.provenance import RetentionPolicy
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name) / "2026-11-08"
        root.mkdir(parents=True)
        header = {
            "started_at": "2026-11-08T08:57:37+00:00", "ended_at": "2026-11-08T08:59:10+00:00",
            "user_prompt": json.dumps({
                "current_trigger": {"kind": "conversation_turn", "reference": None},
                "conversation": [
                    {"conversation_id": "conv-1", "turn_id": "turn-mail", "content": "Greptile charged $94.50"},
                    {"conversation_id": "conv-1", "turn_id": "turn-said", "content": "Is it in Xero?"},
                ],
            }),
        }
        answer = _event("tool_use", id="t1", name="StructuredOutput", input={"action": {
            "type": "respond", "response": "Yes, $94.50.", "detail": "From the receipt."}})
        (root / "085910-dddddddd.jsonl").write_text(json.dumps(header) + "\n" + answer + "\n")
        self.root = root.parent
        # Read on 14 October, so its D-013 deadline (13 November) is earlier
        # than the session record's own (15 November).
        self.mail = RetentionPolicy().direct_mail(
            datetime(2026, 10, 14, 6, 0, tzinfo=UTC), [MailReference("INBOX", "1376545928", "61560")])

    def tearDown(self) -> None:
        self.directory.cleanup()

    def read(self, lookup):
        self.batches = []

        def batch(keys):
            self.batches.append(list(keys))
            return {key: lookup(*key) for key in keys}
        return build_self_diagnosis_executors(
            None, None, lambda: "call-1", clock=lambda: datetime(2026, 11, 8, 12, 0, tzinfo=UTC),
            sessions_dir=self.root, turn_provenance=batch,
        )[READ_OWN_SESSIONS]({})

    def test_all_turns_are_looked_up_in_one_batch(self) -> None:
        self.read(lambda cid, tid: (True, None))
        self.assertEqual(self.batches, [[("conv-1", "turn-mail"), ("conv-1", "turn-said")]])

    def test_the_live_lookup_loads_each_conversation_once(self) -> None:
        from alx.bootstrap.live_voice import _turn_provenance

        class Store:
            loads = 0

            def load(self, conversation_id):
                Store.loads += 1
                from types import SimpleNamespace
                return SimpleNamespace(turns=(SimpleNamespace(turn_id="turn-mail", provenance=None),))
        found = _turn_provenance(Store())([("conv-1", "turn-mail"), ("conv-1", "turn-gone")])
        self.assertEqual(Store.loads, 1)
        self.assertEqual(found[("conv-1", "turn-mail")], (True, None))
        self.assertEqual(found[("conv-1", "turn-gone")], (False, None))

    def test_words_from_a_mail_turn_inherit_its_earlier_expiry(self) -> None:
        result = self.read(lambda cid, tid: (True, self.mail if (cid, tid) == self.TURN else None))
        self.assertEqual(result.values["sessions"][0]["answer"]["response"], "Yes, $94.50.")
        self.assertEqual([ref.uid for ref in result.provenance.mail_references], ["61560"])
        # The mail's own earlier deadline, not the record's and not a fresh one.
        self.assertEqual(result.provenance.content_expires_at, datetime(2026, 11, 13, 6, 0, tzinfo=UTC))

    def test_a_session_that_saw_expired_or_removed_content_keeps_only_its_shape(self) -> None:
        for lookup in (
            lambda cid, tid: (False, None) if (cid, tid) == self.TURN else (True, None),
            lambda cid, tid: (True, replace(self.mail, content_expires_at=datetime(2026, 11, 2, tzinfo=UTC)))
            if (cid, tid) == self.TURN else (True, None),
        ):
            with self.subTest():
                session = self.read(lookup).values["sessions"][0]
                self.assertTrue(session["withheld"])
                self.assertNotIn("94.50", json.dumps(session, default=dict))
                self.assertEqual(session["answer"]["type"], "respond")


def _goal(status: GoalStatus, result_state: CapabilityResultState) -> GoalState:
    call = CapabilityCall("c-1", "xero", {}, None)
    attempt = CapabilityAttempt(
        call, CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult("c-1", "xero", result_state, {},
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
        self.assertEqual(failed["capability_id"], "xero")
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

    def test_her_examples_cover_a_request_by_email_and_a_status_question(self) -> None:
        # 2026-11-09: she narrated a parcel's journey and asked whether to
        # look something up she could simply read.
        self.assertIn("\"DHL emailed about an overdue payment. It's already recorded.\"",
                      PROTOCOL_INSTRUCTIONS)
        self.assertIn("\"The JLC parcel is due on the 19th and the DigiKey one on the 12th. "
                      "I can't track the Communica one.\"", PROTOCOL_INSTRUCTIONS)

    def test_a_free_read_is_done_not_offered_and_approval_is_asked_plainly(self) -> None:
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("A check that only reads and costs nothing needs no asking: do it, "
                      "then answer from it.", text)
        self.assertIn("When something needs his approval anyway, ask for it; "
                      "do not list what you have not done.", text)

    def test_a_status_answer_is_dates_and_what_he_must_act_on(self) -> None:
        from alx.core.model_reasoner import RESPONSE_FIELD
        self.assertIn("For a status question: the dates and anything he must act on, "
                      "not journeys, reasons or reassurance.", RESPONSE_FIELD["description"])

    def test_why_questions_are_answered_from_her_session_records(self) -> None:
        self.assertIn("read your session records", PROTOCOL_INSTRUCTIONS)
        self.assertIn("When Friedl says something is done, check it once", PROTOCOL_INSTRUCTIONS)


if __name__ == "__main__":
    unittest.main()
