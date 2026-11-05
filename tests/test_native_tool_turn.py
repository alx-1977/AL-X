"""D-065: every step of a Core turn is a native tool, run through the Core's own loop.

The fake transport pauses on each scripted native step and then answers. Each
step is handled by `CoreAgent.process` exactly as any decision would be, and
the transport is continued with what the Core recorded, not with anything the
transport made up: an attempt, a selected goal, a goal mutation's outcome,
stored memories, or a refusal and its reason.

Stage 3 moved goal selection and updates, approvals, plans and memories into
native tools and deleted the decision's own call, selection, retrieval and
write fields (Law 0): a turn's final decision is words or silence only.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition,
    CapabilityResult, CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, DecisionValidationError, GoalState, GoalStatus, GoalSummary,
    ModelCompletion, Objective, ReasoningContext, SideEffect, StructuredSchema,
    SuccessCriterion, ValueKind,
)
from alx.core.loop import CoreAgent, CoreState  # noqa: E402
from alx.core.model_reasoner import (  # noqa: E402
    ModelReasoner, _NativePending, decision_schema, native_tools,
)
from alx.goals import SQLiteGoalStore  # noqa: E402

from tests.test_model_reasoner import base_output, goal_update  # noqa: E402

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=1)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
READ = CapabilityDefinition("read_note", "Read a note", SCHEMA, SCHEMA, SideEffect.NONE)
CLEAN = CapabilityDefinition("clean_branch", "Delete a branch", SCHEMA, SCHEMA,
                             SideEffect.EFFECTFUL)


def tool(name: str, **arguments) -> dict:
    return {"name": name, "input": arguments}


def native(call_id: str, capability_id: str = "read_note") -> dict:
    return tool("call_capability", call_id=call_id, capability_id=capability_id,
                arguments_json=json.dumps({"id": call_id}), approval_id=None,
                approval_source_reference=None)


class PausingTransport:
    """Pauses on each scripted native step, then answers with the last output."""

    def __init__(self, script: list, final: dict) -> None:
        self.script = list(script)
        self.final = final
        self.requests = []
        self.abandoned = 0

    def complete(self, request):
        self.requests.append(request)
        if self.script:
            return ModelCompletion("fake", "fake", {}, {}, tool_use=self.script.pop(0))
        return ModelCompletion("fake", "fake", self.final, {})

    def abandon_session(self) -> None:
        self.abandoned += 1

    def results(self) -> list:
        return [json.loads(item.tool_result) for item in self.requests[1:]]


def conversation(text: str = "What do my notes say?") -> ConversationSnapshot:
    turn = ConversationTurn("c1", "t1", ConversationOrigin.TYPED, text, NOW, "friedl")
    return ConversationSnapshot("c1", (turn,), 1, RETENTION)


def goal(goal_id: str = "g1") -> GoalState:
    return GoalState(goal_id, Objective("turn:t1", "Tidy the branches"),
                     (SuccessCriterion("clean", "the old branch is gone"),))


class NativeTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3", clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.dispatched = []

    def dispatch(self, call, _state):
        self.dispatched.append(call.call_id)
        return CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(call.call_id, call.capability_id, CapabilityResultState.SUCCEEDED,
                             {"text": f"note {call.call_id}"}))

    def run_turn(self, transport, budget=8, text="What do my notes say?"):
        core = CoreAgent(self.store, ModelReasoner(transport, "laws", "identity"),
                         self.dispatch, (READ, CLEAN), clock=lambda: NOW,
                         identifier_factory=lambda: "g-new")
        return core.process(conversation(text), RETENTION, budget)

    def test_two_native_reads_and_an_answer_in_one_paused_session(self) -> None:
        transport = PausingTransport([native("n1"), native("n2")],
                                     base_output(response="Both notes read."))
        outcome = self.run_turn(transport)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Both notes read.")
        self.assertEqual(self.dispatched, ["n1", "n2"])
        first = transport.requests[0]
        self.assertEqual([item["name"] for item in first.tools],
                         [item["name"] for item in native_tools()])
        self.assertIsNone(first.tool_result)
        # Each continuation carries the attempt the Core recorded for that call.
        first_result, second_result = transport.results()
        self.assertEqual(first_result["call_id"], "n1")
        self.assertEqual(first_result["result_values"], {"text": "note n1"})
        self.assertEqual(second_result["call_id"], "n2")
        self.assertEqual(transport.abandoned, 0)

    def test_a_refused_native_call_continues_with_the_refusal(self) -> None:
        transport = PausingTransport([native("x1", "not_a_capability")],
                                     base_output(response="That tool does not exist."))
        outcome = self.run_turn(transport)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, [])
        self.assertIn("refused", transport.results()[0])

    def test_a_reused_call_id_is_answered_with_its_refusal_not_the_old_result(self) -> None:
        """Review of #197: the earlier success must not answer the new call."""
        second = native("n1")
        second["input"] = {**second["input"], "arguments_json": json.dumps({"id": "other"})}
        transport = PausingTransport([native("n1"), second], base_output(response="ok"))
        self.run_turn(transport)
        self.assertEqual(self.dispatched, ["n1"])
        self.assertIn("refused", transport.results()[1])

    def test_a_turn_that_starts_fresh_abandons_a_stale_session(self) -> None:
        transport = PausingTransport([native("n1")], base_output(response="done"))
        reasoner = ModelReasoner(transport, "laws", "identity")
        core = CoreAgent(self.store, reasoner, self.dispatch, (READ,), clock=lambda: NOW)
        core.process(conversation(), RETENTION, 8)
        # A step paused in an earlier turn: this turn never continues it.
        reasoner._native_pending = _NativePending(
            "call_capability", "gone", "an-earlier-turn", 0, None, 0, 0, 0, frozenset(), False)
        transport.script = []
        core.process(conversation(), RETENTION, 8)
        self.assertEqual(transport.abandoned, 1)
        self.assertIsNone(transport.requests[-1].tool_result)

    def test_selecting_the_goal_after_a_refused_call_lets_the_call_run(self) -> None:
        """2026-11-04: an effectful call before selecting her goal was refused,
        she could only retry it, and the person turn ended without a word.
        Selecting is now a step of the same session, and the call then runs."""
        self.store.create(goal(), "c1", RETENTION)
        call = tool("call_capability", call_id="clean-1", capability_id="clean_branch",
                    arguments_json="{}", approval_id=None, approval_source_reference=None)
        retry = {**call, "input": {**call["input"], "call_id": "clean-2"}}
        transport = PausingTransport(
            [call, tool("select_goal", goal_id="g1"), retry],
            base_output(response="The old branch is gone."))
        outcome = self.run_turn(transport, text="Clean up the old branch.")
        self.assertEqual((outcome.state, outcome.response),
                         (CoreState.RESPONDED, "The old branch is gone."))
        self.assertEqual(self.dispatched, ["clean-2"])
        refused, selected, ran = transport.results()
        self.assertEqual(refused["refused"]["reason"], "active_goal_required")
        self.assertEqual(selected["selected_goal"]["goal_id"], "g1")
        self.assertEqual(selected["selected_goal"]["status"], "active")
        self.assertEqual(ran["call_id"], "clean-2")
        self.assertEqual(transport.abandoned, 0)

    def test_the_same_refusal_twice_still_answers_the_person(self) -> None:
        call = tool("call_capability", call_id="clean-1", capability_id="clean_branch",
                    arguments_json="{}", approval_id=None, approval_source_reference=None)
        retry = {**call, "input": {**call["input"], "call_id": "clean-2"}}
        transport = PausingTransport([call, retry],
                                     base_output(response="I need the goal for that first."))
        outcome = self.run_turn(transport, text="Clean up the old branch.")
        self.assertEqual((outcome.state, outcome.reason),
                         (CoreState.RESPONDED, "active_goal_required"))
        self.assertEqual(outcome.response, "I need the goal for that first.")
        # The words come from a step offered no tools.
        self.assertEqual(transport.requests[-1].tools, ())
        self.assertEqual(self.dispatched, [])

    def test_a_goal_is_created_then_worked_under_in_the_same_session(self) -> None:
        create = tool("update_goal", **goal_update(
            "create", objective_summary="Tidy the branches",
            success_criteria=[{"id": "clean", "description": "the old branch is gone"}]))
        call = tool("call_capability", call_id="clean-1", capability_id="clean_branch",
                    arguments_json="{}", approval_id=None, approval_source_reference=None)
        transport = PausingTransport([create, call], base_output(response="Done."))
        outcome = self.run_turn(transport, text="Clean up the old branch.")
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        created, ran = transport.results()
        self.assertEqual(created["goal_updated"]["goal_id"], "g-new")
        self.assertEqual(created["goal_updated"]["status"], "active")
        self.assertEqual(ran["call_id"], "clean-1")
        self.assertEqual(self.store.load("g-new").state.attempts[-1].call.call_id, "clean-1")

    def test_a_refused_goal_update_is_answered_and_changes_nothing(self) -> None:
        update = tool("update_goal", **goal_update("request_completion"))
        transport = PausingTransport([update], base_output(response="Nothing to close."))
        outcome = self.run_turn(transport)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        (refused,) = transport.results()
        self.assertEqual(refused["refused"]["reason"], "goal_missing")
        self.assertEqual(self.store.list_goals(), ())

    def test_a_malformed_step_is_answered_with_its_rejection_in_the_same_session(self) -> None:
        broken = tool("call_capability", call_id="n1", capability_id="read_note",
                      arguments_json="not json", approval_id=None,
                      approval_source_reference=None)
        transport = PausingTransport([broken, native("n2")], base_output(response="ok"))
        outcome = self.run_turn(transport)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        rejected, ran = transport.results()
        self.assertEqual(rejected["refused"]["reason"], "decision_rejected")
        self.assertEqual(ran["call_id"], "n2")
        self.assertEqual(transport.abandoned, 0)

    def test_memories_formed_in_a_step_are_reported_stored(self) -> None:
        from alx.memories import SQLiteMemoryStore

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        memories = SQLiteMemoryStore(Path(directory.name) / "memories.sqlite3")
        self.addCleanup(memories.close)
        remember = tool("remember", memories=[{
            "id": "m-1", "kind": "factual", "content": "Friedl keeps notes in the notebook.",
            "source_references": ["turn:t1"], "supersedes_memory_id": None,
            "person_id": None, "meaning": None,
        }])
        transport = PausingTransport([remember], base_output(response="Noted."))
        core = CoreAgent(self.store, ModelReasoner(transport, "laws", "identity"),
                         self.dispatch, (READ,), clock=lambda: NOW, memory_store=memories)
        outcome = core.process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(transport.results()[0], {"stored_memories": ["m-1"]})
        self.assertEqual(memories.load("m-1").current.content,
                         "Friedl keeps notes in the notebook.")


    # D-067: details on request, inside the session, at no step of the Core.

    def test_a_description_is_answered_in_the_session_without_a_core_step(self) -> None:
        contexts = []
        reasoner = ModelReasoner(None, "laws", "identity")
        transport = PausingTransport(
            [tool("describe_capabilities", capability_ids=["read_note", "invented"]),
             native("n1")], base_output(response="Read."))
        reasoner._model = transport
        original = reasoner.decide

        def counting(context):
            contexts.append(context)
            return original(context)
        reasoner.decide = counting
        core = CoreAgent(self.store, reasoner, self.dispatch, (READ, CLEAN), clock=lambda: NOW)
        outcome = core.process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(self.dispatched, ["n1"])
        # Two steps of the Core: the call and the words. The description
        # continued the same session in between.
        self.assertEqual(len(contexts), 2)
        described = json.loads(transport.requests[1].tool_result)
        self.assertEqual([item["id"] for item in described["capabilities"]], ["read_note"])
        self.assertIn("input_schema", described["capabilities"][0])
        self.assertEqual(described["not_listed"], ["invented"])
        self.assertEqual(transport.abandoned, 0)

    def test_a_long_result_is_bounded_and_read_in_full_inside_the_session(self) -> None:
        """Review of #203: a long email must stay readable to the end."""
        body = "x" * 12_000 + "THE END"

        def long_dispatch(call, _state):
            self.dispatched.append(call.call_id)
            return CapabilityAttempt(
                call, CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult(call.call_id, call.capability_id,
                                 CapabilityResultState.SUCCEEDED, {"body": body}))

        transport = PausingTransport(
            [native("n1"), tool("read_full_result", call_id="n1", offset=8000)],
            base_output(response="Read it all."))
        core = CoreAgent(self.store, ModelReasoner(transport, "laws", "identity"),
                         long_dispatch, (READ,), clock=lambda: NOW)
        outcome = core.process(conversation(), RETENTION, 8)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        bounded, window = transport.results()
        self.assertEqual(bounded["result_values"]["body"]["original_length"], len(body))
        self.assertIn('THE END', window["text"])
        self.assertIsNone(window["next_offset"])
        self.assertGreater(window["total_length"], len(body))

    def test_lookups_inside_a_step_are_on_the_trace(self) -> None:
        """A description or full-result read is traced, though no Core step sees it.

        On 2026-11-05 a turn made sixteen model calls and the trace showed
        eight: the lookups answered inside a step left no line.
        """
        events = []
        transport = PausingTransport(
            [tool("describe_capabilities", capability_ids=["read_note", "clean_branch"]),
             native("n1"), tool("read_full_result", call_id="n1", offset=0)],
            base_output(response="Read."))
        core = CoreAgent(self.store, ModelReasoner(transport, "laws", "identity",
                                                   trace=events.append),
                         self.dispatch, (READ, CLEAN), clock=lambda: NOW)
        self.assertEqual(core.process(conversation(), RETENTION, 8).state, CoreState.RESPONDED)
        lookups = [(event.label, event.status.value, event.reference, event.count)
                   for event in events]
        self.assertEqual(lookups, [
            ("Read capability details", "completed", "read_note,clean_branch", 2),
            ("Read full result", "completed", "n1", None)])
        self.assertTrue(all(event.conversation_id == "c1" for event in events))

    def test_a_failed_full_result_read_is_traced_as_failed(self) -> None:
        """Review of #204: a read that found nothing must not look done."""
        events = []
        transport = PausingTransport(
            [tool("read_full_result", call_id="never-ran", offset=0),
             tool("read_full_result", call_id="never-ran", offset=-1)],
            base_output(response="Nothing there."))
        core = CoreAgent(self.store, ModelReasoner(transport, "laws", "identity",
                                                   trace=events.append),
                         self.dispatch, (READ,), clock=lambda: NOW)
        self.assertEqual(core.process(conversation(), RETENTION, 8).state, CoreState.RESPONDED)
        self.assertEqual([(event.status.value, event.reason_code) for event in events],
                         [("failed", "result_unavailable")] * 2)

    def test_a_full_transient_result_is_read_before_its_stored_summary(self) -> None:
        """Second review of #203: a returning plan's full result wins."""
        from alx.core.model_reasoner import _full_result_window
        call = CapabilityCall("p1", "read_note", {}, None)

        def attempt(values):
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult("p1", "read_note",
                                                      CapabilityResultState.SUCCEEDED, values))
        stored = GoalState("g1", Objective("turn:t1", "Read"), (SuccessCriterion("r", "r"),),
                           attempts=(attempt({"detail": "summary"}),))
        context = ReasoningContext(stored, conversation().turns, (READ,), conversation_id="c1",
                                   transient_attempts=(attempt({"detail": "the whole text"}),),
                                   unfinished_goals=(GoalSummary.of(stored),))
        self.assertIn("the whole text", json.loads(_full_result_window(context, "p1", 0))["text"])

    def test_a_paused_session_settles_its_spend_once_when_it_ends(self) -> None:
        """Review of #203: usage after a pause is never left unaccounted."""
        from unittest import mock
        authority = mock.Mock()
        authority.reserve.return_value = "reservation-1"
        transport = PausingTransport(
            [tool("describe_capabilities", capability_ids=["read_note"])],
            base_output(response="Done."))
        final_usage = {"input_tokens": 900, "output_tokens": 40}
        original = transport.complete

        def with_usage(request):
            completion = original(request)
            if completion.tool_use is None:
                return ModelCompletion("fake", "fake", completion.output, final_usage)
            return completion
        transport.complete = with_usage
        reasoner = ModelReasoner(transport, "laws", "identity", max_output_tokens=1000,
                                 max_input_tokens=10**7, spend_authority=authority)
        context = ReasoningContext(None, conversation().turns, (READ,), conversation_id="c1",
                                   turn_key="t", step_index=0)
        decision = reasoner.decide(context)
        self.assertEqual(decision.response, "Done.")
        authority.reserve.assert_called_once()
        authority.settle.assert_called_once_with("reservation-1", final_usage)

    def test_an_abandoned_paused_session_settles_with_usage_unknown(self) -> None:
        from unittest import mock
        authority = mock.Mock()
        authority.reserve.return_value = "reservation-1"
        transport = PausingTransport([native("n1")], base_output(response="Done."))
        reasoner = ModelReasoner(transport, "laws", "identity", max_output_tokens=1000,
                                 max_input_tokens=10**7, spend_authority=authority)
        context = ReasoningContext(None, conversation().turns, (READ,), conversation_id="c1",
                                   turn_key="t", step_index=0)
        reasoner.decide(context)
        authority.settle.assert_not_called()
        reasoner.abandon()
        authority.settle.assert_called_once_with("reservation-1", None)

    def test_the_catalogue_lists_one_line_per_capability(self) -> None:
        from alx.core.model_reasoner import _catalogue_payload
        long = CapabilityDefinition(
            "long_one", "Do one thing. Then a great deal more detail follows here.",
            SCHEMA, SCHEMA, SideEffect.EFFECTFUL)
        (entry,) = json.loads(_catalogue_payload((long,)))["capabilities"]
        self.assertEqual(entry, {"id": "long_one", "purpose": "Do one thing.",
                                 "side_effect": "effectful"})

    def test_a_program_waits_two_minutes_before_the_background(self) -> None:
        from alx.providers.program import FOREGROUND_SECONDS, ProgramRunner
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.assertEqual(ProgramRunner(Path(directory.name), ()).foreground_seconds,
                         FOREGROUND_SECONDS)
        self.assertGreaterEqual(FOREGROUND_SECONDS, 120)


class OneProductionPathTests(unittest.TestCase):
    """Law 0: each outcome the decision used to carry now has one native path."""

    def test_the_final_decision_is_words_or_silence_only(self) -> None:
        schema = decision_schema()
        self.assertEqual(set(schema["properties"]), {"action"})
        kinds = {item["properties"]["type"]["const"]
                 for item in schema["properties"]["action"]["anyOf"]}
        self.assertEqual(kinds, {"respond", "finish_silently"})
        rendered = json.dumps(schema)
        for removed in ("call_capability", "select_goal", "retrieve_memories",
                        "goal_update", "plan_update", "memory_proposals",
                        "approval_proposal", "response_requires_goal_commit"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, rendered)

    def test_the_parser_refuses_an_old_decision_action(self) -> None:
        context = ReasoningContext(None, conversation().turns, (READ,), conversation_id="c1")
        for action in (
            {"type": "call_capability", "call_id": "n1", "capability_id": "read_note",
             "arguments_json": "{}", "approval_id": None, "approval_proposal": None},
            {"type": "select_goal"},
            {"type": "retrieve_memories", "memory_query_id": "q1"},
        ):
            with self.subTest(action=action["type"]):
                transport = PausingTransport([], {"action": action})
                with self.assertRaisesRegex(DecisionValidationError, "unknown decision action"):
                    ModelReasoner(transport, "laws", "identity").decide(context)

    def test_each_step_kind_has_exactly_one_tool(self) -> None:
        names = [item["name"] for item in native_tools()]
        self.assertEqual(sorted(names), sorted(set(names)))
        self.assertEqual(set(names), {"call_capability", "select_goal", "update_goal",
                                      "update_plan", "retrieve_memories", "remember",
                                      "describe_capabilities", "read_full_result"})


class ContinuationBoundaryTests(unittest.TestCase):
    """Review of #197: a session is continued only by the next step of its own turn."""

    def setUp(self) -> None:
        call = CapabilityCall("n1", "read_note", {}, None)
        recorded = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult("n1", "read_note", CapabilityResultState.SUCCEEDED, {"text": "x"}))
        self.goal = GoalState("g1", Objective("turn:t1", "Read notes"),
                              (SuccessCriterion("read", "read"),), attempts=(recorded,))
        self.transport = PausingTransport([], base_output(response="done"))
        self.reasoner = ModelReasoner(self.transport, "laws", "identity")
        # Paused on n1 at step 0 of turn "turn-a".
        self.reasoner._native_pending = _NativePending(
            "call_capability", "n1", "turn-a", 0, "g1", 0, 0, 0, frozenset(), False)

    def context(self, **changes) -> ReasoningContext:
        values = dict(conversation_id="c1", unfinished_goals=(GoalSummary.of(self.goal),),
                      turn_key="turn-a", step_index=1)
        values.update(changes)
        return ReasoningContext(self.goal, conversation().turns, (READ,), **values)

    def test_an_attempt_from_an_earlier_turn_does_not_continue_the_session(self) -> None:
        self.assertFalse(self.reasoner.can_continue(self.context()))
        self.reasoner.decide(self.context())
        self.assertIsNone(self.transport.requests[-1].tool_result)
        self.assertEqual(self.transport.abandoned, 1)

    def test_the_same_attempt_dispatched_in_this_turn_does(self) -> None:
        self.assertTrue(self.reasoner.can_continue(self.context(turn_call_ids=("n1",))))
        self.reasoner.decide(self.context(turn_call_ids=("n1",)))
        self.assertEqual(json.loads(self.transport.requests[-1].tool_result)["call_id"], "n1")

    def test_another_turn_or_a_later_step_does_not(self) -> None:
        for changes in ({"turn_key": "turn-b"}, {"step_index": 2}, {"turn_key": None}):
            with self.subTest(changes=changes):
                self.assertFalse(self.reasoner.can_continue(
                    self.context(turn_call_ids=("n1",), **changes)))

    def test_a_words_only_step_starts_fresh_instead_of_failing(self) -> None:
        decision = self.reasoner.decide(self.context(turn_call_ids=("n1",),
                                                     response_only_reason="blocked"))
        self.assertEqual(decision.response, "done")
        self.assertEqual(self.transport.requests[-1].tools, ())
        self.assertEqual(self.transport.abandoned, 1)

    def test_a_selected_goal_answers_a_select_step(self) -> None:
        self.reasoner._native_pending = _NativePending(
            "select_goal", "g1", "turn-a", 0, None, 0, 0, 0, frozenset(), False)
        self.reasoner.decide(self.context())
        answer = json.loads(self.transport.requests[-1].tool_result)
        self.assertEqual(answer["selected_goal"]["goal_id"], "g1")
        self.assertIn("attempt:n1", answer["selected_goal"]["goal_memory_sources"])
        self.assertIs(GoalStatus(answer["selected_goal"]["status"]), GoalStatus.ACTIVE)


if __name__ == "__main__":
    unittest.main()
