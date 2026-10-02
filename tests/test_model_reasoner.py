from __future__ import annotations

import json
import sys
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap import build_model_reasoner  # noqa: E402
from alx.contracts import (  # noqa: E402
    AgentDecision, BackgroundEvent, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState,
    ConversationOrigin, ConversationTurn, DecisionValidationError,
    GoalMutationKind, GoalState, GoalSummary, MemoryKind, ModelCompletion, Objective,
    PlanOperation, ReasoningContext, SideEffect, Evidence, history_evidence_ids,
    StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import ModelReasoner  # noqa: E402
from alx.core.model_reasoner import decision_schema  # noqa: E402
from alx.tools.notebook import RECORD_ENTRY_DEFINITION  # noqa: E402

NOW = datetime(2026, 8, 28, tzinfo=UTC)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
CAPABILITY = CapabilityDefinition(
    "search_records", "Search structured records", SCHEMA, SCHEMA,
    SideEffect.NONE,
)


def goal() -> GoalState:
    return GoalState(
        "goal-1", Objective("turn:turn-1", "investigate"),
        (SuccessCriterion("criterion-1", "verified"),),
    )


def base_output(**changes):
    values = {
        "goal_id": None,
        "goal_update": None,
        "plan_update": None,
        "memory_proposals": [],
    }
    action_type = changes.pop("disposition", "respond")
    if action_type == "respond":
        action = {
            "type": "respond",
            "response": changes.pop("response", "A normal response."),
            "response_requires_goal_commit": changes.pop(
                "response_requires_goal_commit", False
            ),
        }
    elif action_type == "finish_silently":
        changes.pop("response", None)
        action = {"type": "finish_silently"}
    elif action_type == "call_capability":
        changes.pop("response", None)
        action = {
            "type": "call_capability",
            "call_id": changes.pop("call_id"),
            "capability_id": changes.pop("capability_id"),
            "arguments_json": changes.pop("arguments_json"),
            "approval_id": changes.pop("approval_id", None),
            "approval_proposal": changes.pop("approval_proposal", None),
        }
    else:
        changes.pop("response", None)
        action = {
            "type": "retrieve_memories",
            "memory_query_id": changes.pop("memory_query_id"),
            "memory_kinds": changes.pop("memory_kinds", []),
            "memory_ids": changes.pop("memory_ids", []),
            "memory_person_id": changes.pop("memory_person_id", None),
            "memory_formed_after": changes.pop("memory_formed_after", None),
            "memory_formed_before": changes.pop("memory_formed_before", None),
            "memory_source_references": changes.pop("memory_source_references", []),
            "memory_source_match": changes.pop("memory_source_match", "any"),
            "memory_include_superseded": changes.pop(
                "memory_include_superseded", False
            ),
        }
    values["action"] = action
    values.update(changes)
    return values


def goal_update(operation="update", **changes):
    values = {
        "operation": operation,
        "objective_summary": None,
        "success_criteria": None,
        "context_json": None,
        "referents": None,
        "new_decisions": [],
        "new_corrections": [],
        "new_progress": [],
        "blockers": None,
        "outstanding_work": None,
        "new_evidence": [],
    }
    values.update(changes)
    return values


class FakeModel:
    def __init__(self, output) -> None:
        self.output = output
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ModelCompletion("fake", "fake-model", self.output)


class ModelReasonerTests(unittest.TestCase):
    def context(self, active_goal=goal()):
        return ReasoningContext(
            active_goal,
            (ConversationTurn("conversation-1", "turn-1",
                              ConversationOrigin.SPEECH_TRANSCRIPT,
                              "Please investigate", NOW, "friedl"),),
            (CAPABILITY,),
            unfinished_goals=(
                () if active_goal is None else (GoalSummary.of(active_goal),)
            ),
        )

    def plan_step(self, *, wait_seconds=0, max_wait_seconds=0, completion=1):
        condition = {"path": "values.ready", "equals_json": "true", "negate": False}
        return {
            "call_id": "call-1", "capability_id": "search_records",
            "arguments_json": "{}", "approval_id": None,
            "completion_conditions": [condition] * completion,
            "wait_seconds": wait_seconds, "max_wait_seconds": max_wait_seconds,
            "wake_core_on_completion": False,
            "waiting_for": "CI" if wait_seconds else None,
        }

    def plan_output(self, *steps, disposition="respond", **plan):
        output = base_output(goal_id="goal-1", disposition=disposition)
        output["plan_update"] = {
            "operation": "install",
            "plan": {"plan_id": "plan-1", "context_preconditions_json": "{}",
                     "steps": list(steps), **plan},
        }
        return output

    def plan_step_schema(self):
        install = decision_schema()["properties"]["plan_update"]["anyOf"][1]
        return install["properties"]["plan"]["properties"]["steps"]["items"]

    def test_plan_update_is_parsed_as_durable_structured_intent(self) -> None:
        decision = ModelReasoner(
            FakeModel(self.plan_output(self.plan_step(wait_seconds=30, max_wait_seconds=600))),
            "laws", "identity",
        ).decide(self.context())
        update = decision.plan_update
        self.assertEqual(update.operation, PlanOperation.INSTALL)
        self.assertEqual(decision.response, "A normal response.")
        step = update.plan.steps[0]
        self.assertEqual((step.wait_seconds, step.max_wait_seconds), (30, 600))
        self.assertIs(step.completion_conditions[0].equals, True)
        # Runtime-owned provenance: the Core binds it on installation.
        self.assertIsNone(update.plan.source_turn_id)

    def test_resolution_carries_no_plan_and_travels_with_words_or_silence(self) -> None:
        for operation in ("resume", "accept", "finish", "cancel"):
            for disposition in ("respond", "finish_silently"):
                with self.subTest(operation=operation, disposition=disposition):
                    output = base_output(goal_id="goal-1", disposition=disposition)
                    output["plan_update"] = {"operation": operation}
                    self.assertTrue(_schema_accepts(decision_schema(), output))
                    decision = ModelReasoner(FakeModel(output), "laws", "identity").decide(
                        self.context())
                    self.assertEqual(decision.plan_update.operation.value, operation)
                    self.assertIsNone(decision.plan_update.plan)

    def test_plan_update_cannot_accompany_a_call(self) -> None:
        output = base_output(goal_id="goal-1", disposition="call_capability",
                             call_id="call-9", capability_id="search_records",
                             arguments_json="{}")
        output["plan_update"] = {"operation": "resume"}
        with self.assertRaisesRegex(DecisionValidationError, "response or silence"):
            ModelReasoner(FakeModel(output), "laws", "identity").decide(self.context())

    def test_plan_step_schema_holds_the_execution_step_contract(self) -> None:
        schema = self.plan_step_schema()
        cases = {
            "interval without bound": (self.plan_step(wait_seconds=30), False),
            "bound without interval": (self.plan_step(max_wait_seconds=30), False),
            "valid no-wait step": (self.plan_step(), True),
            "valid step without completion conditions": (self.plan_step(completion=0), True),
            "valid waiting step": (self.plan_step(wait_seconds=30, max_wait_seconds=600), True),
        }
        for name, (step_value, accepted) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(_schema_accepts(schema, step_value), accepted)
                if accepted:
                    # Whatever the schema accepts, ExecutionStep accepts too.
                    decision = ModelReasoner(
                        FakeModel(self.plan_output(step_value)), "laws", "identity",
                    ).decide(self.context())
                    self.assertEqual(len(decision.plan_update.plan.steps), 1)

    def test_malformed_plan_inputs_are_rejected_at_the_parser_boundary(self) -> None:
        condition = {"path": "values.ready", "equals_json": "true", "negate": False}
        cases = {
            "duplicate call ids": (
                [self.plan_step(), self.plan_step()], "call_id values must be unique"),
            "path outside the result": (
                [dict(self.plan_step(), completion_conditions=[dict(condition, path="other.x")])],
                "path is unusable"),
            "empty path segment": (
                [dict(self.plan_step(), completion_conditions=[dict(condition, path="values..x")])],
                "path is unusable"),
            "private path segment": (
                [dict(self.plan_step(), completion_conditions=[dict(condition, path="values._x")])],
                "path is unusable"),
            # Collections are settled by their capability's outcome, so a
            # condition names one field and never a wildcard.
            "wildcard": (
                [dict(self.plan_step(), completion_conditions=[dict(condition, path="values.*")])],
                "path is unusable"),
            "equals_json not JSON": (
                [dict(self.plan_step(),
                      completion_conditions=[dict(condition, equals_json="{not json")])],
                "equals_json is not JSON"),
            "capability absent from the catalogue": (
                [dict(self.plan_step(), capability_id="invented")],
                "absent from catalogue"),
        }
        for name, (steps, message) in cases.items():
            with self.subTest(case=name):
                reasoner = ModelReasoner(FakeModel(self.plan_output(*steps)), "laws", "identity")
                with self.assertRaisesRegex(DecisionValidationError, message):
                    reasoner.decide(self.context())

    def test_plan_schema_leaves_objective_and_source_turn_to_the_runtime(self) -> None:
        install = decision_schema()["properties"]["plan_update"]["anyOf"][1]
        plan = install["properties"]["plan"]
        for runtime_owned in ("objective_source", "objective_summary", "source_turn_id",
                              "cursor", "status", "attention"):
            self.assertNotIn(runtime_owned, plan["properties"])
        restated = self.plan_output(self.plan_step(), objective_summary="investigate")
        self.assertFalse(_schema_accepts(decision_schema(), restated))
        decision = ModelReasoner(FakeModel(self.plan_output(self.plan_step())),
                                 "laws", "identity").decide(self.context())
        self.assertIsNone(decision.plan_update.plan.objective_source)
        self.assertIsNone(decision.plan_update.plan.objective_summary)

    def test_malformed_condition_structures_are_rejected(self) -> None:
        good = {"path": "values.ready", "equals_json": "true", "negate": False}
        nested = {"type": "object", "properties": good, "required": list(good),
                  "additionalProperties": False}
        cases = {
            "condition nested as a schema": nested,
            "condition missing a field": {key: good[key] for key in ("path", "equals_json")},
            "condition with an extra field": dict(good, quantifier="all"),
        }
        for name, condition in cases.items():
            with self.subTest(case=name):
                output = self.plan_output(dict(self.plan_step(),
                                               completion_conditions=[condition]))
                self.assertFalse(_schema_accepts(decision_schema(), output))
                if name != "condition with an extra field":
                    with self.assertRaises(DecisionValidationError):
                        ModelReasoner(FakeModel(output), "laws", "identity").decide(
                            self.context())

    def test_response_only_schema_admits_no_plan_update(self) -> None:
        from alx.core.model_reasoner import response_only_schema

        output = base_output(goal_id=None)
        output["plan_update"] = {"operation": "resume"}
        self.assertFalse(_schema_accepts(response_only_schema(), output))

    def test_provider_shaped_plan_passes_schema_parser_and_runtime(self) -> None:
        """One raw decision, in the exact shape the model is asked for, end to end."""
        import tempfile
        from alx.contracts import CognitionOrigin, ConversationSnapshot, PlanStatus
        from alx.core import CoreAgent, CoreState
        from alx.goals import SQLiteGoalStore
        from alx.tools.pull_request_checks import DEFINITION as CHECKS

        head = "c" * 40
        raw = {
            "goal_id": "goal-1",
            "goal_update": None,
            "memory_proposals": [],
            "action": {"type": "respond", "response": "Watching the checks.",
                       "response_requires_goal_commit": False},
            "plan_update": {
                "operation": "install",
                "plan": {
                    "plan_id": "watch-and-record",
                    "context_preconditions_json": json.dumps({"head": head}),
                    "steps": [
                        {
                            "call_id": "read-checks",
                            "capability_id": "read_pull_request_checks",
                            "arguments_json": json.dumps({"pull_request_number": 95,
                                                          "head_sha": head}),
                            "approval_id": None,
                            "completion_conditions": [],
                            "wait_seconds": 60,
                            "max_wait_seconds": 3600,
                            "wake_core_on_completion": False,
                            "waiting_for": "required checks",
                        },
                        {
                            "call_id": "search-after",
                            "capability_id": "search_records",
                            "arguments_json": json.dumps({"query": {"pull_request": 95}}),
                            "approval_id": None,
                            "completion_conditions": [
                                {"path": "state", "equals_json": '"succeeded"',
                                 "negate": False}],
                            "wait_seconds": 0,
                            "max_wait_seconds": 0,
                            "wake_core_on_completion": True,
                            "waiting_for": None,
                        },
                    ],
                },
            },
        }
        # 1. The exported schema accepts it.
        self.assertTrue(_schema_accepts(decision_schema(), raw))
        # 2. The parser reads it.
        active = replace(goal(), context={"head": head})
        context = replace(self.context(active), capabilities=(CAPABILITY, CHECKS))
        decision = ModelReasoner(FakeModel(raw), "laws", "identity").decide(context)
        proposed = decision.plan_update.plan
        self.assertEqual([item.call.call_id for item in proposed.steps],
                         ["read-checks", "search-after"])
        self.assertEqual(proposed.steps[0].call.arguments,
                         {"pull_request_number": 95, "head_sha": head})
        # 3. It installs into a real active goal, bound to the runtime's
        # provenance, and nothing runs inside the turn: the executor does that.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        self.addCleanup(store.close)
        retention = NOW.replace(year=NOW.year + 1)
        store.create(active, "conversation-1", retention)
        dispatched = []

        def dispatch(call, _state):
            dispatched.append(call)
            raise AssertionError("no planned step runs inside a Core turn")

        class Decisions:
            def decide(self, _context):
                return decision

        thread = ConversationSnapshot("conversation-1", context.turns, 1, retention)
        outcome = CoreAgent(store, Decisions(), dispatch, (CAPABILITY, CHECKS),
                            clock=lambda: NOW, plan_continuation=True).process(
            thread, retention, 3, origin=CognitionOrigin.PERSON_TURN)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Watching the checks.")
        installed = store.load("goal-1").state.execution_plan
        self.assertEqual((installed.status, installed.cursor), (PlanStatus.RUNNING, 0))
        self.assertEqual(installed.objective_source, active.objective.source_reference)
        self.assertEqual(installed.objective_summary, active.objective.summary)
        self.assertEqual(installed.source_turn_id, "turn-1")
        self.assertEqual(dispatched, [])

    def test_respond_requires_nonblank_text_even_with_a_goal_id(self) -> None:
        for goal_id in (None, "goal-1"):
            for response in (None, "", " \n\t", 0, False, [], {}):
                with self.subTest(goal_id=goal_id, response=response):
                    model = FakeModel(base_output(goal_id=goal_id, response=response))
                    with self.assertRaisesRegex(
                        DecisionValidationError, "respond.response must be a nonblank string"
                    ):
                        ModelReasoner(model, "laws", "identity").decide(self.context())

    def test_ordinary_response_has_no_required_goal_metadata(self) -> None:
        model = FakeModel(base_output())
        decision = ModelReasoner(model, "Approved Laws", "Approved identity").decide(
            self.context(None)
        )
        self.assertEqual(decision.response, "A normal response.")
        self.assertIsNone(decision.goal_proposal)
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertIsNone(supplied["active_goal"])
        self.assertEqual(supplied["conversation"][0]["content"], "Please investigate")
        self.assertNotIn("rejected_decision_feedback", supplied)
        self.assertEqual(supplied["continuation_notices"], [])
        self.assertEqual(model.requests[0].affinity_key, "conversation-1")

    def test_continuation_notices_reach_the_reasoner_payload(self) -> None:
        model = FakeModel(base_output())
        notice = {
            "reason": "remaining_work_still_executable",
            "outstanding_work": ["item-2"],
        }
        active = goal()
        ModelReasoner(model, "Approved Laws", "Approved identity").decide(
            ReasoningContext(
                active,
                (ConversationTurn("conversation-1", "turn-1",
                                  ConversationOrigin.SPEECH_TRANSCRIPT,
                                  "Please investigate", NOW, "friedl"),),
                (CAPABILITY,),
                unfinished_goals=(GoalSummary.of(active),),
                continuation_notices=(notice,),
            )
        )
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertEqual(supplied["continuation_notices"], [notice])

    def test_terminal_checkpoint_schema_only_permits_a_grounded_response(self) -> None:
        model = FakeModel(base_output(response="The review is still in progress."))
        context = replace(self.context(), response_only_reason="review_unavailable")
        decision = ModelReasoner(model, "laws", "identity").decide(context)
        self.assertEqual(decision.response, "The review is still in progress.")
        request = model.requests[0]
        payload = json.loads(request.messages[-1].content)
        self.assertEqual(payload["terminal_checkpoint_reason"], "review_unavailable")
        properties = request.output_schema["properties"]
        self.assertEqual(properties["action"]["properties"]["type"]["const"], "respond")
        self.assertEqual(properties["goal_id"], {"type": "null"})
        self.assertEqual(properties["goal_update"], {"type": "null"})
        self.assertEqual(properties["memory_proposals"]["maxItems"], 0)

    def test_terminal_checkpoint_parser_rejects_work_even_if_provider_ignores_schema(self):
        context = replace(self.context(), response_only_reason="review_unavailable")
        for output in (
            base_output(disposition="call_capability", call_id="c2",
                        capability_id="search_records", arguments_json="{}"),
            base_output(response="Done", goal_update=goal_update()),
            base_output(response="Done", goal_id="goal-1"),
            base_output(response="Done", response_requires_goal_commit=True),
        ):
            with self.subTest(output=output):
                with self.assertRaisesRegex(DecisionValidationError, "response only"):
                    ModelReasoner(FakeModel(output), "laws", "identity").decide(context)

    def test_silent_completion_is_a_general_core_decision(self) -> None:
        model = FakeModel(base_output(disposition="finish_silently"))
        decision = ModelReasoner(model, "Approved Laws", "Approved identity").decide(
            self.context(None)
        )
        self.assertTrue(decision.finish_silently)
        self.assertIsNone(decision.response)
        variants = decision_schema()["properties"]["action"]["anyOf"]
        silent = next(
            item for item in variants
            if item["properties"]["type"].get("const") == "finish_silently"
        )
        self.assertNotIn("capability_id", silent["properties"])

    def test_silent_completion_cannot_also_call_or_respond(self) -> None:
        with self.assertRaises(ValueError):
            AgentDecision(response="must be heard", finish_silently=True)
        with self.assertRaises(ValueError):
            AgentDecision(
                call=CapabilityCall("call-1", "search_records", {}),
                finish_silently=True,
            )

    def test_durable_call_projection_cannot_fabricate_or_change_arguments(self) -> None:
        with self.assertRaises(ValueError):
            CapabilityCall(
                "call-1", "search_records", {"scope": "one"},
                durable_arguments={"scope": "different"},
            )
        with self.assertRaises(ValueError):
            CapabilityCall(
                "call-1", "search_records", {"scope": "one"},
                durable_arguments={"invented": "value"},
            )

    def test_goal_output_is_a_proposal_not_replacement_state(self) -> None:
        update = goal_update(
            "request_completion",
            new_evidence=[{
                "id": "evidence-1", "kind": "observation",
                "attributes_json": "{}", "supports": ["criterion-1"],
                "source_references": ["turn:turn-1"],
            }],
        )
        decision = ModelReasoner(FakeModel(base_output(
            response="Verified.", response_requires_goal_commit=True,
            goal_update=update,
        )), "laws", "identity").decide(self.context())
        self.assertEqual(decision.goal_proposal.kind, GoalMutationKind.REQUEST_COMPLETION)
        self.assertEqual(
            decision.goal_proposal.new_evidence[0].source_references,
            ("turn:turn-1",),
        )
        self.assertTrue(decision.response_requires_goal_commit)
        self.assertFalse(hasattr(decision, "goal"))

    def test_language_blind_capability_call_remains_one_core_action(self) -> None:
        output = base_output(
            disposition="call_capability", response=None,
            call_id="call-1", capability_id="search_records",
            arguments_json=json.dumps({"record_type": "note"}),
        )
        decision = ModelReasoner(FakeModel(output), "laws", "identity").decide(
            self.context()
        )
        self.assertEqual(decision.call.capability_id, "search_records")
        self.assertEqual(decision.call.arguments["record_type"], "note")

    def test_notebook_content_is_transient_but_identity_is_durable(self) -> None:
        finding = "The complete finding must live only in the notebook."
        output = base_output(
            disposition="call_capability",
            call_id="call-1",
            capability_id="record_research_entry",
            arguments_json=json.dumps({
                "entry_id": "entry-1",
                "thread_id": "thread-1",
                "kind": "conclusion",
                "content": finding,
                "source_references": ["attempt:research-1"],
            }),
        )
        context = ReasoningContext(
            goal(),
            (ConversationTurn(
                "conversation-1", "turn-1", ConversationOrigin.TYPED,
                "Continue", NOW, "friedl",
            ),),
            (RECORD_ENTRY_DEFINITION,),
            unfinished_goals=(GoalSummary.of(goal()),),
        )
        decision = ModelReasoner(FakeModel(output), "laws", "identity").decide(context)
        self.assertEqual(decision.call.arguments["content"], finding)
        self.assertNotIn("content", decision.call.durable_arguments)
        self.assertEqual(decision.call.durable_arguments["entry_id"], "entry-1")

    def test_exact_action_approval_proposal_stays_bound_to_same_call(self) -> None:
        arguments = '{"mailbox_id":"INBOX","uid_validity":"777","uid":"2"}'
        output = base_output(
            disposition="call_capability",
            response=None,
            call_id="call-1",
            capability_id="move_mail_message_to_trash",
            arguments_json=arguments,
            approval_id="approval-1",
            approval_proposal={
                "approval_id": "approval-1",
                "capability_id": "move_mail_message_to_trash",
                "arguments_json": arguments,
                "source_reference": "turn:turn-1",
            },
        )
        decision = ModelReasoner(
            FakeModel(output), "laws", "identity"
        ).decide(self.context())
        self.assertEqual(decision.approval_proposal.approval_id, "approval-1")
        self.assertTrue(decision.approval_proposal.scope.matches(decision.call))

    def test_the_protocol_states_how_a_memory_identifier_may_be_used(self) -> None:
        """A reused identifier ended a live conversation mid-sentence.

        The store refuses to let one identifier come to mean a different
        memory, and offers supersession for refining what is already
        remembered. The protocol never said so, while retrieved_memories
        showed the model identifiers it could reuse. This states the rule; it
        does not script wording or route anything.
        """
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "A memory identifier names one memory permanently",
            "Every memory you form takes a\nnew identifier",
            "set supersedes_memory_id to the identifier being replaced",
            "same\nkind and concern the same person",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_only_executed_attempts_are_offered_as_evidence_sources(self) -> None:
        """A call that has not run is not a source, and must not be listed.

        On 2026-09-10 the Core cited attempt:call-trash-59094 as evidence while
        that call had not executed. available_memory_sources listed every
        attempt regardless of whether anything had happened, so the list its
        own name presents as available offered references the grounding check
        then refused.
        """
        from alx.core.model_reasoner import _context_payload

        def attempt(call_id, disposition, invoked, result_state, reason=None):
            result = None
            if result_state is not None:
                result = CapabilityResult(
                    call_id, "search_records", result_state, {"v": 1},
                    failure=None if result_state is CapabilityResultState.SUCCEEDED
                    else {"code": "task_failed"},
                )
            return CapabilityAttempt(
                CapabilityCall(call_id, "search_records", {}),
                disposition, invoked, result, reason_code=reason,
            )

        state = replace(goal(), attempts=(
            attempt("done-ok", CapabilityAttemptDisposition.EXECUTED, True,
                    CapabilityResultState.SUCCEEDED),
            attempt("done-failed", CapabilityAttemptDisposition.EXECUTED, True,
                    CapabilityResultState.FAILED),
            attempt("refused", CapabilityAttemptDisposition.REJECTED, False,
                    None, "approval_invalid"),
            # An unresolved dispatch must be the latest attempt.
            attempt("still-pending", CapabilityAttemptDisposition.PENDING, None,
                    None, "dispatch_pending"),
        ))
        payload = json.loads(_context_payload(ReasoningContext(
            active_goal=state, turns=(), capabilities=(CAPABILITY,),
            unfinished_goals=(GoalSummary.of(state),),
            conversation_id="conversation-1",
        )))
        offered = {
            item["reference"] for item in payload["available_memory_sources"]
        }
        # Executed attempts, succeeded or failed, are real sources.
        self.assertIn("attempt:done-ok", offered)
        self.assertIn("attempt:done-failed", offered)
        # An intention is not.
        self.assertNotIn("attempt:still-pending", offered)
        self.assertNotIn("attempt:refused", offered)

    def test_the_offered_sources_match_what_the_runtime_accepts(self) -> None:
        """The list and the grounding check must not disagree.

        Whatever else changes, an attempt offered here has to be one the
        runtime will accept: the divergence is the defect, not either rule.
        """
        import inspect
        from alx.core.model_reasoner import _attempt_is_citable
        from alx.core.loop import CoreAgent

        def rules(function):
            return [
                line.strip()
                for line in inspect.getsource(function).splitlines()
                if line.strip().startswith(("if ", "return"))
            ]

        self.assertEqual(
            rules(_attempt_is_citable),
            rules(CoreAgent._attempt_is_citable_evidence_source),
        )

    def test_history_evidence_ids_offered_to_core_match_runtime_grounding(self) -> None:
        """History records cite bare evidence IDs, never provenance references."""
        from alx.core.model_reasoner import _context_payload

        state = replace(goal(), evidence=(Evidence(
            "evidence-mail-59093", "mail_observation",
            source_references=("turn:turn-1",),
        ),))
        payload = json.loads(_context_payload(ReasoningContext(
            active_goal=state, turns=(), capabilities=(CAPABILITY,),
            unfinished_goals=(GoalSummary.of(state),),
            conversation_id="conversation-1",
        )))
        offered = set(payload["available_history_evidence_ids"])
        self.assertEqual(offered, history_evidence_ids(state.evidence))
        self.assertNotIn("evidence:evidence-mail-59093", offered)

    def test_protocol_and_schema_forbid_invented_history_evidence_ids(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        self.assertIn("available_history_evidence_ids", PROTOCOL_INSTRUCTIONS)
        self.assertIn("never\ninvent an identifier", PROTOCOL_INSTRUCTIONS)
        description = json.dumps(decision_schema())
        self.assertIn("Never invent", description)

    def test_a_future_call_id_is_never_offered(self) -> None:
        """The live case: an identifier for a call that does not exist yet."""
        from alx.core.model_reasoner import _context_payload

        state = goal()
        payload = json.loads(_context_payload(ReasoningContext(
            active_goal=state, turns=(), capabilities=(CAPABILITY,),
            unfinished_goals=(GoalSummary.of(state),),
            conversation_id="conversation-1",
        )))
        offered = {
            item["reference"] for item in payload["available_memory_sources"]
        }
        self.assertNotIn("attempt:call-trash-59094", offered)
        self.assertFalse([r for r in offered if r.startswith("attempt:")])

    def test_the_protocol_states_when_an_attempt_becomes_citable(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "Evidence records what has already happened",
            "an attempt is citable\nonly once it has actually run",
            "not among them",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_evidence_schema_states_the_same_rule(self) -> None:
        description = json.dumps(decision_schema())
        self.assertIn("an attempt that has not run yet is not a source", description)

    def test_the_protocol_states_when_a_queue_must_continue_this_turn(self) -> None:
        from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS

        for stated in (
            "A queue of remaining actions is recorded as outstanding_work",
            "issue the next executable capability call now",
            "Say you are still working only when",
            "continuation_notices is shown when",
        ):
            self.assertIn(stated, PROTOCOL_INSTRUCTIONS)

    def test_the_identifier_rule_reaches_the_model_with_every_decision(self) -> None:
        """It belongs in the stable cached prefix, not a per-turn instruction."""
        model = FakeModel(base_output())
        ModelReasoner(model, "laws", "identity").decide(self.context())
        protocol = model.requests[0].messages[1].content
        self.assertIn("A memory identifier names one memory permanently", protocol)

    def test_supersession_is_expressible_in_the_decision_schema(self) -> None:
        """The guidance names a field the model can actually set."""
        variants = decision_schema()["properties"]["memory_proposals"]["items"]["anyOf"]
        for variant in variants:
            self.assertIn("supersedes_memory_id", variant["properties"])

    def test_structured_background_event_can_be_the_only_current_input(self) -> None:
        event = BackgroundEvent(
            "mail:777:2",
            "mail.message_arrived",
            NOW,
            {"mailbox_id": "INBOX", "uid": "2"},
        )
        model = FakeModel(base_output(response="I noticed new mail."))
        decision = ModelReasoner(model, "laws", "identity").decide(
            ReasoningContext(
                None, (), (), events=(event,), conversation_id="conversation-1",
                trigger_event_id=event.event_id,
            )
        )
        self.assertEqual(decision.response, "I noticed new mail.")
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertEqual(supplied["background_events"][0]["kind"],
                         "mail.message_arrived")
        self.assertEqual(
            supplied["background_events"][0]["semantic_role"],
            "external_event_not_conversation",
        )
        self.assertEqual(supplied["current_trigger"], {
            "kind": "background_event", "reference": "event:mail:777:2",
        })

    def test_transient_tool_result_is_explicitly_not_conversation(self) -> None:
        call = CapabilityCall("call-1", "search_records", {})
        result = CapabilityResult(
            "call-1", "search_records", CapabilityResultState.SUCCEEDED,
            {"content": "Can you answer this question?"},
        )
        attempt = CapabilityAttempt(
            call, CapabilityAttemptDisposition.EXECUTED, True, result,
        )
        model = FakeModel(base_output(response="It contains a question."))
        ModelReasoner(model, "laws", "identity").decide(
            ReasoningContext(
                None,
                (ConversationTurn(
                    "conversation-1", "turn-1", ConversationOrigin.SPEECH_TRANSCRIPT,
                    "What did the document say?", NOW, "friedl",
                ),),
                (CAPABILITY,),
                transient_attempts=(attempt,),
            )
        )
        supplied = json.loads(model.requests[0].messages[-1].content)
        self.assertEqual(
            supplied["transient_attempts"][0]["semantic_role"],
            "capability_observation_not_conversation",
        )
        self.assertEqual(
            supplied["transient_attempts"][0]["content_trust"],
            "external_untrusted_data",
        )
        protocol = model.requests[0].messages[1].content
        self.assertIn("Only entries in conversation are conversational turns", protocol)
        self.assertIn(
            "Never claim that no later item exists merely because", protocol
        )
        self.assertIn("Use natural person-facing language", protocol)
        self.assertIn("Mail attention is deliberately one item at a time", protocol)
        self.assertIn("A failed\nor uncertain reply does not release it", protocol)
        self.assertIn(
            "Dismissing it or asking to move\non uses local acknowledgement and deliberately leaves the message Unseen",
            protocol,
        )
        self.assertIn("These exact post-reply\nactions have standing authority", protocol)
        self.assertIn(
            "If active_goal is null and you\nchoose an effectful call, include a create goal",
            protocol,
        )

    def test_memory_proposal_retains_semantic_choice_and_real_source(self) -> None:
        proposal = {
            "id": "memory-1", "kind": MemoryKind.AUTOBIOGRAPHICAL.value,
            "content": "I challenged an assumption.",
            "source_references": ["turn:turn-1"],
            "supersedes_memory_id": None, "person_id": None,
            "meaning": "I became more willing to challenge weak assumptions.",
        }
        decision = ModelReasoner(FakeModel(base_output(memory_proposals=[proposal])),
                                 "laws", "identity").decide(self.context())
        self.assertEqual(decision.memory_proposals[0].kind,
                         MemoryKind.AUTOBIOGRAPHICAL)
        self.assertEqual(decision.memory_proposals[0].formed_at, NOW)

    def test_schema_requires_sourced_evidence_and_has_no_model_completion_flag(self) -> None:
        schema = decision_schema()
        update = schema["properties"]["goal_update"]["anyOf"][1]
        evidence = update["properties"]["new_evidence"]["items"]
        self.assertIn("source_references", evidence["required"])
        encoded = json.dumps(schema)
        self.assertNotIn('"complete"', encoded)
        self.assertNotIn("respond_completed", encoded)

    def test_schema_makes_response_and_capability_call_mutually_exclusive(self) -> None:
        variants = decision_schema()["properties"]["action"]["anyOf"]
        response = next(
            item for item in variants
            if item["properties"]["type"].get("const") == "respond"
        )
        capability = next(
            item for item in variants
            if item["properties"]["type"].get("const") == "call_capability"
        )
        self.assertNotIn("call_id", response["properties"])
        self.assertNotIn("response", capability["properties"])
        self.assertFalse(response["additionalProperties"])
        self.assertFalse(capability["additionalProperties"])

    def test_malformed_output_fails_once_at_provider_boundary(self) -> None:
        with self.assertRaises(DecisionValidationError):
            ModelReasoner(FakeModel({"action": {"type": "respond"}}),
                          "laws", "identity").decide(self.context())

    def test_composition_root_loads_only_approved_identity_sources(self) -> None:
        root = Path(__file__).resolve().parents[1]
        model = FakeModel(base_output())
        build_model_reasoner(model, root).decide(self.context(None))
        constitutional = model.requests[0].messages[0].content
        self.assertIn("Laws of AL/X", constitutional)
        self.assertIn("AL/X Identity and Memory", constitutional)

def _schema_accepts(schema, value) -> bool:
    """A strict checker for the JSON-schema keywords the decision schema uses."""
    if "anyOf" in schema:
        return any(_schema_accepts(item, value) for item in schema["anyOf"])
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else (kinds or [])
    python = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if kinds:
        def matches(kind):
            if kind == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            return isinstance(value, python[kind])
        if not any(matches(kind) for kind in kinds):
            return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", ())):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(_schema_accepts(properties[key], item)
                   for key, item in value.items() if key in properties)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            return False
        return all(_schema_accepts(schema.get("items", {}), item) for item in value)
    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
