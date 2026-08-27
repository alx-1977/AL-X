from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import AgentDecision, CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition, CapabilityResult, CapabilityResultState, Evidence, GoalState, GoalStatus, GoalStopReason, Objective, ProgressRecord, SideEffect, StructuredSchema, SuccessCriterion, ValueKind
from alx.core import CoreAgent, CoreState
from alx.goals import GoalRevisionConflict, SQLiteGoalStore
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.contracts import Approval, ApprovalLifecycle, ApprovalScope

NOW = datetime(2026, 8, 27, tzinfo=UTC)
SCHEMA = StructuredSchema(ValueKind.OBJECT)
DEFINITIONS = (CapabilityDefinition("capability-a", "generic primitive A", SCHEMA, SCHEMA, SideEffect.NONE), CapabilityDefinition("capability-b", "generic primitive B", SCHEMA, SCHEMA, SideEffect.NONE))

def active(attempts=()):
    return GoalState("goal-1", Objective("turn-1", "objective"), (SuccessCriterion("criterion-1", "criterion"),), attempts=attempts)

def complete(state):
    return replace(state, status=GoalStatus.COMPLETED, stop_reason=GoalStopReason.SUCCESS_CRITERIA_MET, evidence=(Evidence("evidence-1", "fact", supports=("criterion-1",)),))

class Queued:
    def __init__(self, decisions): self.decisions = decisions; self.contexts = []
    def decide(self, context):
        self.contexts.append(context)
        if isinstance(self.decisions, Exception): raise self.decisions
        return self.decisions.pop(0)

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name) / "x.sqlite"; self.store = SQLiteGoalStore(self.path)
        self.store.create(active(), (), NOW + timedelta(days=1))
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def agent(self, reasoner, dispatch): return CoreAgent(self.store, reasoner, dispatch, DEFINITIONS)
    def test_a_zero_tool_completion_and_active_response_rejection(self):
        out = self.agent(Queued([AgentDecision(complete(active()), response="done")]), lambda c, s: None).run("goal-1", 1)
        self.assertEqual(out.state, CoreState.RESPONDED); self.assertEqual(self.store.load("goal-1").state.status, GoalStatus.COMPLETED)
        self.store.delete("goal-1", out.snapshot.revision); self.store.create(active(), (), NOW + timedelta(days=1))
        out = self.agent(Queued([AgentDecision(active(), response="still active")]), lambda c,s: self.fail("dispatch")).run("goal-1", 1)
        self.assertEqual(out.reason, "active_response"); self.assertEqual(self.store.load("goal-1").state.status, GoalStatus.ACTIVE)
    def test_b_d_multistep_provider_selected_order_and_context(self):
        a = CapabilityCall("a", "capability-a", {}); at = CapabilityAttempt(a, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult("a", "capability-a", CapabilityResultState.FAILED, failure={"code":"x"}))
        b = CapabilityCall("b", "capability-b", {}); bt = CapabilityAttempt(b, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult("b", "capability-b", CapabilityResultState.PARTIAL, {"v":1}))
        q = Queued([AgentDecision(active(), call=a), AgentDecision(active((at,)), call=b), AgentDecision(complete(active((at,bt))), response="done")]); seen=[]
        out = self.agent(q, lambda c,s: (seen.append(c.capability_id) or (at if c==a else bt))).run("goal-1",3)
        self.assertEqual(out.state,CoreState.RESPONDED); self.assertEqual(seen,["capability-a","capability-b"]); self.assertEqual(q.contexts[1].goal.attempts,(at,)); self.assertEqual(q.contexts[0].capabilities,DEFINITIONS)
    def test_c_h_i_attempt_checkpoint_before_next_turn(self):
        call = CapabilityCall("a", "capability-a", {})
        attempt = CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.REJECTED,
            False,
            reason_code="input_invalid",
        )
        revisions = []

        class InspectingReasoner:
            def __init__(inner):
                inner.calls = 0

            def decide(inner, context):
                inner.calls += 1
                if inner.calls == 1:
                    return AgentDecision(active(), call=call)
                durable = self.store.load("goal-1")
                self.assertEqual(durable.state.attempts, context.goal.attempts)
                self.assertEqual(context.goal.attempts, (attempt,))
                return AgentDecision(complete(context.goal), response="done")

        out = self.agent(
            InspectingReasoner(),
            lambda proposed, state: (
                revisions.append(self.store.load("goal-1").revision) or attempt
            ),
        ).run("goal-1", 2)
        self.assertEqual(out.state, CoreState.RESPONDED)
        self.assertGreaterEqual(revisions[0], 2)
    def test_e_l_restart_budget_and_validation(self):
        call=CapabilityCall("a","capability-a",{}); at=CapabilityAttempt(call,CapabilityAttemptDisposition.EXECUTED,True,CapabilityResult("a","capability-a",CapabilityResultState.SUCCEEDED,{}))
        self.assertEqual(self.agent(Queued([AgentDecision(active(),call=call)]),lambda c,s:at).run("goal-1",1).state,CoreState.CHECKPOINTED)
        self.store.close(); self.store=SQLiteGoalStore(self.path); state=self.store.load("goal-1").state
        self.assertEqual(self.agent(Queued([AgentDecision(complete(state),response="done")]),lambda c,s:at).run("goal-1",1).state,CoreState.RESPONDED)
        with self.assertRaises(ValueError): self.agent(Queued([]),lambda c,s:at).run("goal-1",0)
    def test_f_g_invalid_goal_and_fabricated_attempt_do_not_dispatch(self):
        call=CapabilityCall("a","capability-a",{}); fabricated=CapabilityAttempt(call,CapabilityAttemptDisposition.REJECTED,False,reason_code="x")
        out=self.agent(Queued([AgentDecision(active((fabricated,)),call=call)]),lambda c,s:self.fail("dispatch")).run("goal-1",1)
        self.assertEqual(out.reason,"decision_invalid"); self.assertEqual(self.store.load("goal-1").state.attempts,())
        wrong_goal = replace(active(), goal_id="other-goal")
        out = self.agent(
            Queued([AgentDecision(wrong_goal, call=call)]),
            lambda proposed, state: self.fail("dispatch"),
        ).run("goal-1", 1)
        self.assertEqual(out.reason, "decision_invalid")
        self.assertEqual(self.store.load("goal-1").state, active())
        terminal = replace(active(), status=GoalStatus.CANCELLED, stop_reason=GoalStopReason.CANCELLED)
        out = self.agent(Queued([AgentDecision(terminal, call=call)]), lambda c,s:self.fail("dispatch")).run("goal-1",1)
        self.assertEqual(out.reason, "inactive_call")

    def test_terminal_goal_cannot_reenter_reasoning_or_dispatch(self):
        terminal_states = (
            complete(active()),
            replace(
                active(),
                status=GoalStatus.CANCELLED,
                stop_reason=GoalStopReason.CANCELLED,
            ),
        )
        for terminal in terminal_states:
            with self.subTest(status=terminal.status):
                snapshot = self.store.load("goal-1")
                self.store.delete("goal-1", snapshot.revision)
                self.store.create(terminal, (), NOW + timedelta(days=1))
                reasoner = Queued(AssertionError("terminal goal reached reasoner"))
                out = self.agent(
                    reasoner,
                    lambda call, state: self.fail("terminal goal reached dispatch"),
                ).run("goal-1", 1)
                self.assertEqual(out.reason, "goal_inactive")
                self.assertEqual(reasoner.contexts, [])
                self.assertEqual(self.store.load("goal-1").state, terminal)

    def test_g_each_authoritative_history_cannot_be_erased(self):
        prior_call = CapabilityCall("prior-call", "capability-a", {})
        prior_attempt = CapabilityAttempt(
            prior_call,
            CapabilityAttemptDisposition.EXECUTED,
            True,
            CapabilityResult(
                "prior-call",
                "capability-a",
                CapabilityResultState.SUCCEEDED,
                {},
            ),
        )
        histories = {
            "corrections": (ProgressRecord("correction-1", "corrected"),),
            "decisions": (ProgressRecord("decision-1", "decided"),),
            "progress": (ProgressRecord("progress-1", "progressed"),),
            "evidence": (Evidence("evidence-1", "fact"),),
            "attempts": (prior_attempt,),
        }
        for field_name, history in histories.items():
            with self.subTest(field=field_name):
                current = replace(active(), **{field_name: history})
                snapshot = self.store.load("goal-1")
                self.store.delete("goal-1", snapshot.revision)
                self.store.create(current, (), NOW + timedelta(days=1))
                erased = replace(current, **{field_name: ()})
                proposed = CapabilityCall("new-call", "capability-a", {})
                out = self.agent(
                    Queued([AgentDecision(erased, call=proposed)]),
                    lambda proposed_call, state: self.fail("dispatch"),
                ).run("goal-1", 1)
                self.assertEqual(out.reason, "decision_invalid")
                self.assertEqual(self.store.load("goal-1").state, current)

    def test_duplicate_call_id_is_rejected_without_dispatch(self):
        call = CapabilityCall("same", "capability-a", {})
        prior = CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult("same", "capability-a", CapabilityResultState.SUCCEEDED, {}))
        self.store.delete("goal-1", 1); self.store.create(active((prior,)), (), NOW + timedelta(days=1))
        out = self.agent(Queued([AgentDecision(active((prior,)), call=call)]), lambda c,s:self.fail("dispatch")).run("goal-1",1)
        self.assertEqual(out.reason, "call_id_reused"); self.assertEqual(self.store.load("goal-1").state.attempts,(prior,))

    def test_revision_conflict_propagates_without_overwrite(self):
        call = CapabilityCall("a", "capability-a", {})
        other = SQLiteGoalStore(self.path)
        try:
            class RacingReasoner:
                def decide(inner, context):
                    snapshot = other.load("goal-1")
                    other.replace(snapshot.state, snapshot.turns, NOW + timedelta(days=2), snapshot.revision)
                    return AgentDecision(active(), call=call)
            with self.assertRaises(GoalRevisionConflict):
                self.agent(RacingReasoner(), lambda c,s:self.fail("dispatch")).run("goal-1",1)
            self.assertEqual(other.load("goal-1").retention_until, NOW + timedelta(days=2))
        finally: other.close()
    def test_k_provider_and_dispatch_error_preserve_checkpoint(self):
        self.assertEqual(self.agent(Queued(RuntimeError()),lambda c,s:None).run("goal-1",1).state,CoreState.ERROR)
        call=CapabilityCall("a","capability-a",{}); out=self.agent(Queued([AgentDecision(active(),call=call)]),lambda c,s:(_ for _ in ()).throw(RuntimeError())).run("goal-1",1)
        self.assertEqual(out.reason,"dispatch_error")
        pending = self.store.load("goal-1").state.attempts
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].disposition, CapabilityAttemptDisposition.PENDING)
        reasoner = Queued(AssertionError("unresolved dispatch reached reasoner"))
        resumed = self.agent(reasoner, lambda c,s:self.fail("dispatch repeated")).run("goal-1",1)
        self.assertEqual(resumed.reason, "dispatch_unresolved")
        self.assertEqual(reasoner.contexts, [])

    def test_dispatch_failure_keeps_durable_claim_and_blocks_restart(self):
        approval = Approval(
            "approval-1",
            ApprovalScope("capability-a", {}),
            ApprovalLifecycle.GRANTED,
            NOW + timedelta(days=1),
        )
        self.store.delete("goal-1", 1)
        self.store.create(
            replace(active(), approvals=(approval,)),
            (),
            NOW + timedelta(days=1),
        )
        call = CapabilityCall("call", "capability-a", {}, "approval-1")
        out = self.agent(
            Queued([AgentDecision(replace(active(), approvals=(approval,)), call=call)]),
            lambda proposed, state: (_ for _ in ()).throw(RuntimeError()),
        ).run("goal-1", 1)
        self.assertEqual(out.reason, "dispatch_error")
        self.store.close()
        self.store = SQLiteGoalStore(self.path)
        recovered = self.store.load("goal-1")
        self.assertEqual(
            recovered.state.approvals[0].lifecycle,
            ApprovalLifecycle.CLAIMED,
        )
        self.assertEqual(
            recovered.state.attempts[0].disposition,
            CapabilityAttemptDisposition.PENDING,
        )
        reasoner = Queued(AssertionError("unresolved restart reached reasoner"))
        resumed = self.agent(
            reasoner,
            lambda proposed, state: self.fail("unresolved action repeated"),
        ).run("goal-1", 1)
        self.assertEqual(resumed.reason, "dispatch_unresolved")
        self.assertEqual(reasoner.contexts, [])

    def test_j_real_broker_success_consumes_and_reuse_is_rejected(self):
        approval = Approval("approval-1", ApprovalScope("capability-a", {}), ApprovalLifecycle.GRANTED, NOW + timedelta(days=1))
        self.store.delete("goal-1", 1); self.store.create(replace(active(), approvals=(approval,)), (), NOW + timedelta(days=1))
        calls = [0]
        def execute(data):
            calls[0] += 1
            return CapabilityResult("first", "capability-a", CapabilityResultState.SUCCEEDED, {})
        broker = CapabilityBroker(CapabilityRegistry((DEFINITIONS[0],)), SafetyGate({"capability-a": AuthorityPolicy(approval_required=True)}), {"capability-a": execute})
        def dispatch(call, state):
            return broker.dispatch(call, AuthorityContext("principal", frozenset(), NOW, state.approvals))
        first = CapabilityCall("first", "capability-a", {}, "approval-1")
        first_attempt = CapabilityAttempt(first, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult("first", "capability-a", CapabilityResultState.SUCCEEDED, {}))
        second = CapabilityCall("second", "capability-a", {}, "approval-1")
        rejected = CapabilityAttempt(second, CapabilityAttemptDisposition.REJECTED, False, reason_code="approval_invalid")
        q = Queued([AgentDecision(replace(active(), approvals=(approval,)), call=first), AgentDecision(replace(active((first_attempt,)), approvals=(replace(approval, lifecycle=ApprovalLifecycle.CONSUMED),)), call=second), AgentDecision(complete(replace(active((first_attempt, rejected)), approvals=(replace(approval, lifecycle=ApprovalLifecycle.CONSUMED),))), response="done")])
        out = self.agent(q, dispatch).run("goal-1", 3)
        self.assertEqual(out.state, CoreState.RESPONDED); self.assertEqual(calls[0], 1)
        self.assertEqual(out.snapshot.state.approvals[0].lifecycle, ApprovalLifecycle.CONSUMED)
        self.assertEqual(out.snapshot.state.attempts[1], rejected); self.assertEqual(q.contexts[2].goal.attempts[1], rejected)

    def test_provider_cannot_fabricate_or_regrant_approval(self):
        call = CapabilityCall(
            "call",
            "capability-a",
            {},
            "approval-1",
        )
        granted = Approval(
            "approval-1",
            ApprovalScope("capability-a", {}),
            ApprovalLifecycle.GRANTED,
            NOW + timedelta(days=1),
        )
        cases = (
            (active(), replace(active(), approvals=(granted,))),
            (
                replace(
                    active(),
                    approvals=(
                        replace(granted, lifecycle=ApprovalLifecycle.CONSUMED),
                    ),
                ),
                replace(active(), approvals=(granted,)),
            ),
        )
        for persisted, proposed in cases:
            with self.subTest(persisted=persisted.approvals):
                snapshot = self.store.load("goal-1")
                self.store.delete("goal-1", snapshot.revision)
                self.store.create(persisted, (), NOW + timedelta(days=1))
                out = self.agent(
                    Queued([AgentDecision(proposed, call=call)]),
                    lambda proposed_call, state: self.fail(
                        "provider-controlled approval reached dispatch"
                    ),
                ).run("goal-1", 1)
                self.assertEqual(out.reason, "decision_invalid")
                self.assertEqual(self.store.load("goal-1").state, persisted)

    def test_j_noninvoked_real_broker_paths_leave_approval_granted(self):
        approval = Approval("approval-1", ApprovalScope("capability-a", {}), ApprovalLifecycle.GRANTED, NOW + timedelta(days=1))
        for policy, implementations, expected in ((AuthorityPolicy(enabled=False), {"capability-a": lambda data: self.fail("execute")}, "policy_denied"), (AuthorityPolicy(approval_required=True), {}, "implementation_missing")):
            with self.subTest(expected=expected):
                self.store.delete("goal-1", self.store.load("goal-1").revision); self.store.create(replace(active(), approvals=(approval,)), (), NOW + timedelta(days=1))
                broker = CapabilityBroker(CapabilityRegistry((DEFINITIONS[0],)), SafetyGate({"capability-a": policy}), implementations)
                call = CapabilityCall("call", "capability-a", {}, "approval-1")
                out = self.agent(Queued([AgentDecision(replace(active(), approvals=(approval,)), call=call)]), lambda c,s: broker.dispatch(c, AuthorityContext("principal", frozenset(), NOW, s.approvals))).run("goal-1",1)
                self.assertEqual(out.snapshot.state.approvals[0].lifecycle, ApprovalLifecycle.GRANTED)
                self.assertFalse(out.snapshot.state.attempts[0].implementation_invoked)

    def test_overlapping_run_cannot_reuse_claimed_approval(self):
        approval = Approval(
            "approval-1",
            ApprovalScope("capability-a", {}),
            ApprovalLifecycle.GRANTED,
            NOW + timedelta(days=1),
        )
        self.store.delete("goal-1", 1)
        self.store.create(
            replace(active(), approvals=(approval,)),
            (),
            NOW + timedelta(days=1),
        )
        call = CapabilityCall("first", "capability-a", {}, "approval-1")
        result = CapabilityResult(
            "first",
            "capability-a",
            CapabilityResultState.SUCCEEDED,
            {},
        )
        attempt = CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.EXECUTED,
            True,
            result,
        )
        overlapping_store = SQLiteGoalStore(self.path)
        overlapping_reasoner = Queued(
            AssertionError("claimed approval reached overlapping reasoner")
        )
        overlapping = []

        def dispatch(proposed, state):
            other = CoreAgent(
                overlapping_store,
                overlapping_reasoner,
                lambda inner_call, inner_state: self.fail("approval reused"),
                DEFINITIONS,
            ).run("goal-1", 1)
            overlapping.append(other)
            return attempt

        try:
            out = self.agent(
                Queued([AgentDecision(replace(active(), approvals=(approval,)), call=call)]),
                dispatch,
            ).run("goal-1", 1)
        finally:
            overlapping_store.close()
        self.assertEqual(out.state, CoreState.CHECKPOINTED)
        self.assertEqual(overlapping[0].reason, "dispatch_unresolved")
        self.assertEqual(overlapping_reasoner.contexts, [])
        self.assertEqual(
            out.snapshot.state.approvals[0].lifecycle,
            ApprovalLifecycle.CONSUMED,
        )
        self.assertEqual(out.snapshot.state.attempts, (attempt,))

    def test_c_real_broker_executor_exception_is_invoked_once(self):
        approval = Approval("approval-1", ApprovalScope("capability-a", {}), ApprovalLifecycle.GRANTED, NOW + timedelta(days=1))
        self.store.delete("goal-1", 1); self.store.create(replace(active(), approvals=(approval,)), (), NOW + timedelta(days=1))
        calls=[0]
        def execute(data): calls[0]+=1; raise RuntimeError()
        broker=CapabilityBroker(CapabilityRegistry((DEFINITIONS[0],)),SafetyGate({"capability-a":AuthorityPolicy(approval_required=True)}),{"capability-a":execute})
        call=CapabilityCall("call","capability-a",{},"approval-1")
        out=self.agent(Queued([AgentDecision(replace(active(),approvals=(approval,)),call=call)]),lambda c,s:broker.dispatch(c,AuthorityContext("principal",frozenset(),NOW,s.approvals))).run("goal-1",1)
        self.assertEqual(calls[0],1); self.assertTrue(out.snapshot.state.attempts[0].implementation_invoked); self.assertEqual(out.snapshot.state.approvals[0].lifecycle,ApprovalLifecycle.CONSUMED)

    def test_j_malformed_real_broker_result_consumes_once(self):
        approval = Approval("approval-1", ApprovalScope("capability-a", {}), ApprovalLifecycle.GRANTED, NOW + timedelta(days=1))
        self.store.delete("goal-1", 1); self.store.create(replace(active(), approvals=(approval,)), (), NOW + timedelta(days=1))
        calls=[0]
        def execute(data): calls[0]+=1; return object()
        broker=CapabilityBroker(CapabilityRegistry((DEFINITIONS[0],)),SafetyGate({"capability-a":AuthorityPolicy(approval_required=True)}),{"capability-a":execute})
        call=CapabilityCall("call","capability-a",{},"approval-1")
        out=self.agent(Queued([AgentDecision(replace(active(),approvals=(approval,)),call=call)]),lambda c,s:broker.dispatch(c,AuthorityContext("principal",frozenset(),NOW,s.approvals))).run("goal-1",1)
        self.assertEqual(calls[0],1); self.assertTrue(out.snapshot.state.attempts[0].implementation_invoked); self.assertEqual(out.snapshot.state.approvals[0].lifecycle,ApprovalLifecycle.CONSUMED)

    def test_c_real_broker_attempt_reentry_matrix(self):
        cases = (
            ("succeeded", CapabilityResultState.SUCCEEDED, {"value": 1}, None, None),
            ("partial", CapabilityResultState.PARTIAL, {"value": 1}, {"code": "limited", "detail": "x"}, None),
            ("failed", CapabilityResultState.FAILED, {}, {"code": "limited", "detail": "x"}, None),
            ("rejected", None, None, None, "input_invalid"),
        )
        for name, result_state, values, failure, expected_reason in cases:
            with self.subTest(name=name):
                snapshot = self.store.load("goal-1")
                self.store.delete("goal-1", snapshot.revision)
                self.store.create(active(), (), NOW + timedelta(days=1))
                schema = StructuredSchema(ValueKind.OBJECT, {"value": StructuredSchema(ValueKind.INTEGER)}, (), extra_properties=False)
                definition = CapabilityDefinition("capability-a", "generic primitive", schema, SCHEMA, SideEffect.NONE, ("limited",))
                call = CapabilityCall("call-" + name, "capability-a", {} if name != "rejected" else {"value": True})
                invocations = [0]
                def execute(data, state=result_state, output=values, issue=failure):
                    invocations[0] += 1
                    return CapabilityResult(call.call_id, call.capability_id, state, output or {}, issue, ("evidence-1",))
                broker = CapabilityBroker(CapabilityRegistry((definition,)), SafetyGate({"capability-a": AuthorityPolicy()}), {"capability-a": execute})
                class Inspecting:
                    def __init__(inner): inner.calls = 0; inner.seen = None
                    def decide(inner, context):
                        inner.calls += 1
                        if inner.calls == 1: return AgentDecision(active(), call=call)
                        inner.seen = context.goal.attempts[0]
                        return AgentDecision(complete(context.goal), response="done")
                reasoner = Inspecting()
                out = CoreAgent(self.store, reasoner, lambda c,s: broker.dispatch(c, AuthorityContext("principal", frozenset(), NOW, s.approvals)), (definition,)).run("goal-1", 2)
                attempt = reasoner.seen
                self.assertEqual(out.state, CoreState.RESPONDED)
                self.assertEqual(attempt.call, call)
                self.assertEqual(attempt.reason_code, expected_reason)
                if expected_reason is None:
                    self.assertEqual(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
                    self.assertTrue(attempt.implementation_invoked)
                    self.assertEqual(attempt.result.state, result_state)
                    self.assertEqual(attempt.result.values, values)
                    self.assertEqual(attempt.result.failure, failure)
                    self.assertEqual(attempt.result.evidence_refs, ("evidence-1",))
                    self.assertEqual(invocations[0], 1)
                else:
                    self.assertEqual(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
                    self.assertFalse(attempt.implementation_invoked)
                    self.assertEqual(invocations[0], 0)

if __name__ == "__main__": unittest.main()
