"""Small Core-boundary checks and an opt-in synthetic conversation sanity check.

Fake completions prove unchanged context and decision transport, not personality.
The five live samples are read for broad conversational behavior; they are not a
style certification suite and have no phrase, opening or variation evaluator.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
import json
import os
from pathlib import Path
import unittest

import test_model_reasoner as fixtures
from alx.bootstrap import build_model_reasoner
from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResult, CapabilityResultState, CognitionOrigin, ConversationOrigin,
    ConversationTurn, MemoryKind, MemoryRevision, MemorySnapshot, ReasoningContext,
)
from alx.core.model_reasoner import ModelReasoner

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Sample:
    name: str
    request: str
    facts: dict
    prior_request: str = 'Make part-number search case-insensitive.'

    def context(self):
        return ReasoningContext(
            None,
            (ConversationTurn('sanity-check', 'original-request', ConversationOrigin.TYPED,
                              self.prior_request, fixtures.NOW, 'friedl'),
             ConversationTurn('sanity-check', 'request', ConversationOrigin.TYPED,
                              self.request, fixtures.NOW, 'friedl')),
            (), conversation_id='sanity-check',
            transient_attempts=(CapabilityAttempt(
                CapabilityCall('observed-result', 'fixture_observation', {}),
                CapabilityAttemptDisposition.EXECUTED, True,
                CapabilityResult('observed-result', 'fixture_observation',
                                 CapabilityResultState.SUCCEEDED, self.facts),
            ),),
        )


RESULT = {
    'business_outcome': 'part-number search is case-insensitive; change merged', 'pr_number': 80,
    'commit': 'abc123def456', 'tests_passed': 142,
    'main_sync': 'complete', 'branch_cleanup': 'deleted',
    'capability': 'run_coding_task', 'provider': 'claude_subscription',
    'reviewer': 'coding_agent_review',
}
SAMPLES = (
    Sample('ordinary_success', 'How did the change turn out?', RESULT),
    Sample('resolved_issue', 'How did the change turn out? Anything that mattered?',
           {**RESULT, 'review_issue': 'incorrect file-change claim corrected'}),
    Sample('blocker', 'Capture this invoice.', {
        'supplier_identity': 'ambiguous',
        'choices': ['Alpha Components', 'Alpha Electronics'],
        'xero_post': 'not performed',
    }, prior_request='Capture this invoice.'),
    Sample('technical_follow_up',
           'What technical details do you have about the capability, provider and review?',
           {**RESULT, 'review_issue': 'incorrect file-change claim corrected'}),
    Sample('requested_report',
           'Give me a completion report with the PR, commit, checks and results.',
           {**RESULT, 'checks': {'architecture': 'passed', 'governance': 'passed',
                               'full_suite': '142 passed'}}),
)


class ResponseBoundaryTests(unittest.TestCase):
    def test_every_sample_uses_the_same_core_instructions_and_full_context(self):
        prefixes = []
        for sample in SAMPLES:
            model = fixtures.FakeModel(fixtures.base_output())
            ModelReasoner(model, 'laws', 'identity').decide(sample.context())
            request = model.requests[0]
            prefixes.append(request.messages[:-1])
            payload = json.loads(request.messages[-1].content)
            self.assertEqual(payload['conversation'][-1]['content'], sample.request)
            self.assertEqual(payload['transient_attempts'][0]['result_values'], sample.facts)
        self.assertTrue(all(prefix == prefixes[0] for prefix in prefixes))

    def test_core_authored_prose_and_requested_reports_are_not_rewritten(self):
        for response in ('The change is merged.', 'PR 80\nChecks:\n- Architecture: passed'):
            decision = ModelReasoner(
                fixtures.FakeModel(fixtures.base_output(response=response)),
                'laws', 'identity',
            ).decide(SAMPLES[0].context())
            # Equality proves transport, not preferred wording. No template,
            # brevity limit or formatting filter may rewrite the Core's reply.
            self.assertEqual(decision.response, response)
            self.assertIsNone(decision.goal_proposal)

    def test_structured_calls_goal_evidence_and_silence_remain_unchanged(self):
        context = fixtures.ModelReasonerTests().context()
        for output in (
            fixtures.base_output(disposition='finish_silently'),
            fixtures.base_output(disposition='call_capability', call_id='next-call',
                                 capability_id=fixtures.CAPABILITY.capability_id,
                                 arguments_json='{}'),
            fixtures.base_output(response='Verified.', response_requires_goal_commit=True,
                goal_update=fixtures.goal_update('request_completion', new_evidence=[{
                    'id': 'proof', 'kind': 'observation', 'attributes_json': '{}',
                    'supports': ['criterion-1'], 'source_references': ['turn:turn-1'],
                }])),
        ):
            person = ModelReasoner(fixtures.FakeModel(output), 'laws', 'identity').decide(context)
            autonomous = ModelReasoner(fixtures.FakeModel(output), 'laws', 'identity').decide(
                replace(context, origin=CognitionOrigin.SELF_REQUESTED))
            self.assertEqual(person, autonomous)
        self.assertEqual(person.goal_proposal.new_evidence[0].source_references, ('turn:turn-1',))
        self.assertEqual(person.goal_proposal.new_evidence[0].supports, ('criterion-1',))
        self.assertTrue(person.response_requires_goal_commit)

    def test_relationship_preferences_use_the_existing_memory_context(self):
        memory = MemorySnapshot(
            'synthetic-preference', MemoryKind.RELATIONSHIP, 'friedl', None,
            (MemoryRevision(1, 'Friedl prefers high-level outcomes and asks for specifics.',
                            ('turn:request',), fixtures.NOW),),
            fixtures.NOW + timedelta(days=30),
        )
        model = fixtures.FakeModel(fixtures.base_output())
        context = replace(SAMPLES[0].context(), memories=(memory,))
        ModelReasoner(model, 'laws', 'identity').decide(context)
        supplied = json.loads(model.requests[0].messages[-1].content)['retrieved_memories'][0]
        self.assertEqual(supplied['content'], memory.current.content)
        self.assertEqual(supplied['person_id'], 'friedl')
        self.assertEqual(supplied['source_references'], ['turn:request'])


@unittest.skipUnless(os.environ.get('ALX_STYLE_LIVE_MODEL'), 'opt-in synthetic conversation sanity check')
class LiveConversationSanityTests(unittest.TestCase):
    def test_five_synthetic_conversations(self):
        from alx.providers.claude_subscription import ClaudeSubscriptionReasoningModel
        reasoner = build_model_reasoner(
            ClaudeSubscriptionReasoningModel(os.environ['ALX_STYLE_LIVE_MODEL'], 180), ROOT,
        )
        for sample in SAMPLES:
            with self.subTest(sample=sample.name):
                decision = reasoner.decide(sample.context())
                print(f'\n{sample.name}: {decision.response or "[silent]"}', flush=True)
                self.assertIsNotNone(decision.response)
        # Read these replies for normal dialogue, material information, detail
        # on request and one coherent agent. No grading by exact prose or syntax.
