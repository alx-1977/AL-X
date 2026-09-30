"""A rejected goal mutation must never terminate the conversation or runtime.

The live trace: a goal `request_completion` was correctly refused with
`completion_lacks_sourced_evidence`. The answer that claimed completion
depended on that commit, so the Core discarded it and returned an ERROR outcome
named `goal_proposal_invalid`; the voice server treated that reason as fatal and
closed the exchange while the process itself was healthy.

The mutation may fail, the dependent answer is suppressed and the goal stays
unresolved, but Core and voice remain operational. The evidence and goal
validators are untouched.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition,
    CapabilityCall, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationSnapshot, ConversationTurn, Evidence, GoalMutationKind,
    GoalProposal, GoalState, GoalStatus, Objective, SideEffect,
    StructuredSchema, SuccessCriterion, ValueKind,
)
from alx.core import CoreAgent, CoreOutcome, CoreState  # noqa: E402
from alx.goals import SQLiteGoalStore  # noqa: E402
from alx.interfaces.live_voice import (  # noqa: E402
    VoiceEvent, VoiceEventKind, VoiceSession,
)
from alx.interfaces.server import (  # noqa: E402
    MID_EXCHANGE_RECOVERABLE_REASONS, RECOVERABLE_TRANSPORT_REASONS,
    LiveVoiceServer,
)

NOW = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
RETENTION = NOW + timedelta(days=30)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
INSPECT = CapabilityDefinition(
    "inspect", "Inspect structured material", SCHEMA, SCHEMA, SideEffect.NONE,
)
UNSOURCED = Evidence(
    "evidence-1", "a claim with no source", supports=("criterion-1",),
    source_references=("turn:not-real",),
)


def conversation() -> ConversationSnapshot:
    return ConversationSnapshot(
        "conversation-1",
        (ConversationTurn("conversation-1", "turn-1", ConversationOrigin.TYPED,
                          "Is it done?", NOW, "friedl"),),
        1,
        RETENTION,
    )


def goal() -> GoalState:
    return GoalState(
        "goal-1", Objective("turn:turn-1", "Do the work"),
        (SuccessCriterion("criterion-1", "verified"),),
    )


def unsupported_completion() -> GoalProposal:
    """A completion request that offers no evidence for any criterion."""
    return GoalProposal(GoalMutationKind.REQUEST_COMPLETION)


def unsourced_completion() -> GoalProposal:
    """A completion request whose evidence cites nothing the goal offered."""
    return GoalProposal(GoalMutationKind.REQUEST_COMPLETION, new_evidence=(UNSOURCED,))


def executed(call: CapabilityCall, state) -> CapabilityAttempt:
    return CapabilityAttempt(
        call, CapabilityAttemptDisposition.EXECUTED, True,
        CapabilityResult(call.call_id, call.capability_id,
                         CapabilityResultState.SUCCEEDED, {"value": 1}),
    )


class Queued:
    def __init__(self, *decisions) -> None:
        self.decisions = list(decisions)
        self.contexts = []

    def decide(self, context):
        self.contexts.append(context)
        item = self.decisions.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Harness(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteGoalStore(Path(directory.name) / "goals.sqlite3")
        self.addCleanup(self.store.close)
        self.store.create(goal(), "conversation-1", RETENTION)

    def core(self, reasoner, dispatch=lambda call, state: None) -> CoreAgent:
        return CoreAgent(
            self.store, reasoner, dispatch, (INSPECT,), clock=lambda: NOW,
        )

    def dependent(self, response: str = "It is complete.") -> AgentDecision:
        return AgentDecision(
            goal_id="goal-1", response=response,
            goal_proposal=unsupported_completion(), response_requires_goal_commit=True,
        )


class DependentAnswerIsSuppressed(Harness):
    def test_answer_suppressed_goal_unchanged_outcome_recoverable(self) -> None:
        reasoner = Queued(self.dependent("It is complete."))
        outcome = self.core(reasoner).process(conversation(), RETENTION, 1)

        self.assertIsNot(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertIsNone(outcome.response)
        stored = self.store.load("goal-1").state
        self.assertIs(stored.status, GoalStatus.ACTIVE)
        self.assertEqual(stored.success_criteria, goal().success_criteria)
        self.assertEqual(reasoner.contexts[0].refused_calls, ())

    def test_the_reason_reaches_the_core_when_a_step_remains(self) -> None:
        reasoner = Queued(
            self.dependent(),
            AgentDecision(goal_id="goal-1", response="I could not close it."),
        )
        outcome = self.core(reasoner).process(conversation(), RETENTION, 3)

        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "I could not close it.")
        refusal = reasoner.contexts[1].refused_calls[0]
        self.assertEqual(refusal["reason"], "completion_lacks_sourced_evidence")
        self.assertEqual(refusal["mutation_kind"], "request_completion")
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

    def test_silence_chosen_on_a_refused_mutation_is_recoverable_too(self) -> None:
        outcome = self.core(Queued(AgentDecision(
            goal_id="goal-1", finish_silently=True,
            goal_proposal=unsupported_completion(),
        ))).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")


class IndependentAnswerIsKept(Harness):
    def test_optional_mutation_rejection_keeps_the_answer(self) -> None:
        reasoner = Queued(AgentDecision(
            goal_id="goal-1", response="Independent answer.",
            goal_proposal=unsupported_completion(),
        ))
        outcome = self.core(reasoner).process(conversation(), RETENTION, 1)
        self.assertEqual(outcome.state, CoreState.RESPONDED)
        self.assertEqual(outcome.response, "Independent answer.")
        self.assertEqual(outcome.reason, "goal_proposal_rejected")
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)


class NoRetryLoop(Harness):
    def test_the_same_refusal_twice_checkpoints_without_another_step(self) -> None:
        reasoner = Queued(
            self.dependent("First claim."),
            self.dependent("Second claim."),
            AssertionError("a third step was bought for an unchanged refusal"),
        )
        outcome = self.core(reasoner).process(conversation(), RETENTION, 10)

        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertIsNone(outcome.response)
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

    def test_a_repeated_refused_mutation_without_an_answer_checkpoints(self) -> None:
        bare = AgentDecision(
            goal_id="goal-1", call=CapabilityCall("call-1", "inspect", {}),
            goal_proposal=unsupported_completion(),
        )
        again = AgentDecision(
            goal_id="goal-1", call=CapabilityCall("call-2", "inspect", {}),
            goal_proposal=unsupported_completion(),
        )
        reasoner = Queued(bare, again, AssertionError("unbounded retry"))
        outcome = self.core(reasoner, executed).process(conversation(), RETENTION, 10)
        self.assertEqual(outcome.state, CoreState.CHECKPOINTED)
        self.assertEqual(outcome.reason, "goal_proposal_invalid")
        self.assertEqual(len(reasoner.contexts), 2)


class ValidatorsAreUnchanged(Harness):
    def test_completion_still_requires_sourced_evidence(self) -> None:
        reasoner = Queued(
            self.dependent(),
            AgentDecision(goal_id="goal-1", response="Not closed."),
        )
        self.core(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"],
            "completion_lacks_sourced_evidence",
        )
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)

    def test_evidence_that_cites_nothing_real_is_still_refused(self) -> None:
        reasoner = Queued(
            AgentDecision(
                goal_id="goal-1", response="Done.",
                goal_proposal=unsourced_completion(),
                response_requires_goal_commit=True,
            ),
            AgentDecision(goal_id="goal-1", response="Not closed."),
        )
        self.core(reasoner).process(conversation(), RETENTION, 3)
        self.assertEqual(
            reasoner.contexts[1].refused_calls[0]["reason"], "evidence_source_unknown",
        )
        self.assertIs(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)


class TrueInternalErrorsStayFatal(Harness):
    def test_a_dispatch_fault_is_still_an_error(self) -> None:
        def explode(call, state):
            raise RuntimeError("broker down")

        outcome = self.core(
            Queued(AgentDecision(call=CapabilityCall("call-1", "inspect", {}))),
            explode,
        ).process(conversation(), RETENTION, 2)
        self.assertEqual(outcome.state, CoreState.ERROR)
        self.assertEqual(outcome.reason, "dispatch_error")

    def test_only_the_goal_refusal_became_recoverable(self) -> None:
        for reason in ("dispatch_error", "attempt_invalid", "call_id_reused",
                       "repeated_rejected_call", "memory_proposal_invalid",
                       "memory_identity_conflict", "clock_error",
                       "voice_transport_error", "conversation_gateway_error"):
            with self.subTest(reason=reason):
                self.assertNotIn(reason, RECOVERABLE_TRANSPORT_REASONS)
                self.assertNotIn(reason, MID_EXCHANGE_RECOVERABLE_REASONS)


class VoiceStaysConnected(unittest.TestCase):
    def test_the_voice_layer_reports_the_checkpoint_then_listens(self) -> None:
        session = VoiceSession.__new__(VoiceSession)
        session._diagnostics = None

        async def collect():
            return [
                event async for event in session._response_events(
                    "conversation-1",
                    CoreOutcome(CoreState.CHECKPOINTED, None,
                                reason="goal_proposal_invalid"),
                )
            ]

        events = asyncio.run(collect())
        self.assertEqual(
            [event.kind for event in events],
            [VoiceEventKind.ERROR, VoiceEventKind.LISTENING],
        )
        self.assertEqual(events[0].reason, "goal_proposal_invalid")

    def test_the_server_recovers_inside_the_exchange_and_keeps_hearing(self) -> None:
        consumed: list[str] = []
        exchanges: list[int] = []

        class Session:
            async def exchange(self, conversation_id, audio, deliveries=None,
                               typed=None, **_kwargs):
                exchanges.append(1)
                iterator = audio.__aiter__()
                consumed.append(await iterator.__anext__())
                yield VoiceEvent(VoiceEventKind.ERROR, reason="goal_proposal_invalid")
                yield VoiceEvent(VoiceEventKind.LISTENING)
                consumed.append(await iterator.__anext__())
                yield VoiceEvent(VoiceEventKind.LISTENING)

        sent: list[str] = []

        class Connection:
            async def send(self, payload):
                sent.append(payload)

        server = LiveVoiceServer.__new__(LiveVoiceServer)
        server._session = Session()
        server._await_audio_confirmation = False
        server._delivery_queues = {}

        async def audio():
            yield "before the refusal"
            yield "after the refusal"

        server._audio = lambda _connection, _stream_id: audio()
        resume = asyncio.run(server._exchange_once(Connection(), "conversation-1"))

        self.assertEqual(len(exchanges), 1)
        self.assertEqual(consumed, ["before the refusal", "after the refusal"])
        self.assertFalse(resume)
        frames = [json.loads(item) for item in sent]
        self.assertTrue(any(
            item.get("code") == "voice.recovered_in_exchange"
            and item.get("reason") == "goal_proposal_invalid"
            for item in frames
        ))
        # The exchange was never closed: no second consumer, no re-entry.
        self.assertEqual(len(exchanges), 1)


if __name__ == "__main__":
    unittest.main()
