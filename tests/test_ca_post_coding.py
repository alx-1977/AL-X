"""Post-coding completion through Core, broker, providers and real local git.

Only the model's decisions, GitHub HTTP transport and elapsed waiting time are
simulated. No CA stage is dispatched; repository operations execute real git
against a temporary canonical checkout and bare remote (no worktrees).
"""
from __future__ import annotations

import subprocess
import asyncio
from threading import Thread
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from alx.bootstrap.repository import build_repository_runtime
from alx.bootstrap.repository_authority import build_repository_authority_runtime
from alx.bootstrap.review import build_review_runtime
from alx.bootstrap.tasks import build_task_runtime
from alx.bootstrap.live_voice import _watch_review
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (
    AgentDecision, ApprovalProposal, ApprovalScope, CapabilityAttempt,
    CapabilityAttemptDisposition, CapabilityCall, CapabilityResult,
    CapabilityResultState, ConversationOrigin, ConversationSnapshot,
    ConversationTurn, Evidence, GoalMutationKind, GoalProposal, GoalState,
    GoalStatus, Objective, SuccessCriterion, WorkItem,
)
from alx.contracts.repository import MergeError, MergeRequest
from alx.contracts.repository_authority import CanonicalSystem, Operation, RepositoryRequest
from alx.contracts.review_provider import ReviewProvider, profile_for
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.providers.github_merge import GitHubMergeProvider
from alx.providers.github_review import GitHubReviewProvider
from alx.providers.repository_authority import RepositoryAuthority
from alx.safety import AuthorityContext, SafetyGate
from tests.review_transcript import install_grace_clock


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.headers = body, status, {}

    def json(self):
        return self.body


class Decisions:
    def __init__(self, decisions):
        self.decisions, self.contexts = iter(decisions), []

    def decide(self, context):
        self.contexts.append(context)
        return next(self.decisions)


class PostCodingTests(unittest.TestCase):
    def git(self, *args, root=None):
        return subprocess.run(['git', *args], cwd=root or self.checkout,
                              check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        # No automatic round is modelled on this pull request, so a request
        # waits out the grace before triggering; instantly, here.
        install_grace_clock(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.checkout = self.root / 'checkout'
        self.checkout.mkdir()
        self.remote = self.root / 'remote.git'
        self.git('init', '--bare', str(self.remote))
        # The simulated squash uses commit-tree on this bare remote. A runner
        # with no global identity refuses that commit; the checkout config
        # does not apply to it.
        self.git('--git-dir', str(self.remote), 'config', 'user.email', 'test@example.invalid')
        self.git('--git-dir', str(self.remote), 'config', 'user.name', 'Test')
        self.git('init', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        (self.checkout / 'file.txt').write_text('base\n')
        self.git('add', 'file.txt')
        self.git('commit', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('remote', 'add', 'origin', 'https://github.com/owner/repo.git')
        self.git('push', str(self.remote), 'main')
        self.git('switch', '-c', 'fix/completed')
        (self.checkout / 'file.txt').write_text('completed implementation\n')
        self.git('commit', '-am', 'completed and reviewed')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('push', str(self.remote), 'fix/completed')
        self.now = datetime.now(UTC)
        self.request_count = self.review_reads = self.check_reads = self.merge_count = 0
        self.already_reviewed = True
        self.review_pending, self.ci_pending = 3, 3
        self.behind = 0
        self.live_head = self.head
        self.refusal = None
        self.merged = False
        self.merge_sha = ''
        self.check_conclusion = 'success'
        self.operations = []

        def run(argv, **kwargs):
            self.operations.append(tuple(argv))
            argv = [str(self.remote) if arg == 'https://github.com/owner/repo.git' else arg for arg in argv]
            return subprocess.run(argv, **kwargs)

        authority = RepositoryAuthority(
            CanonicalSystem(self.checkout, 'owner/repo'), runner=run,
            verified_remote='https://github.com/owner/repo.git',
        )
        self.call_id = ['']
        self.repo = build_repository_authority_runtime(
            True, self.checkout, 'owner/repo', 30, lambda: self.call_id[0],
            authority=authority,
        )
        reader = GitHubReviewProvider('owner/repo', 'token', profile_for(ReviewProvider.CODERABBIT))
        self.tasks = build_task_runtime(self.root / 'state', 'owner/repo', 'token',
                                       lambda *a: None, lambda *a: None,
                                       interval_seconds=0.001, review_provider=reader)
        self.review = build_review_runtime(
            True, 'owner/repo', 'token', lambda: self.call_id[0],
            provider=reader, content_provider=reader,
            started=lambda number, sha, at: _watch_review(
                self.tasks, 'conversation', number, sha, at, reader.reviewer),
        )
        self.merge = build_repository_runtime(
            True, 'owner/repo', 'token', lambda: self.call_id[0],
            repository_runtime=self.repo, review_reader=reader,
        )
        self.merge.provider._sleep = lambda seconds: None
        self.merge.provider._max_polls = 5
        self.definitions = self.review.definitions + self.merge.definitions + self.repo.definitions
        self.permissions = self.review.permissions | self.merge.permissions | self.repo.permissions
        self.broker = CapabilityBroker(
            CapabilityRegistry(self.definitions),
            SafetyGate({**self.review.policies, **self.merge.policies, **self.repo.policies}),
            {**self.review.executors, **self.merge.executors, **self.repo.executors},
        )
        self.goals = SQLiteGoalStore(self.root / 'goals.sqlite3')
        self.addCleanup(self.goals.close)
        finished = CapabilityCall('coding-finished', 'run_coding_task', {})
        self.finished = CapabilityAttempt(
            finished, CapabilityAttemptDisposition.EXECUTED, True,
            CapabilityResult(finished.call_id, finished.capability_id, CapabilityResultState.SUCCEEDED,
                             {'commit': {'branch': 'fix/completed', 'commit_sha': self.head},
                              'all_required_verification_passed': True, 'review_findings': ()}),
        )
        self.goals.create(GoalState(
            'goal', Objective('turn:turn', 'Finish the completed coding job'),
            (SuccessCriterion('done', 'Reviewed, merged and main synced'),),
            attempts=(self.finished,),
            outstanding_work=(WorkItem("finish", "Complete review, merge and sync"),),
        ), 'conversation', self.now + timedelta(days=1))
        self.attempts = []
        self.patches = [
            patch('alx.providers.github_review.httpx.request', self.http),
            patch('alx.providers.github_merge.httpx.get', lambda url, **kw: self.http('GET', url, **kw)),
            patch('alx.providers.github_merge.httpx.put', lambda url, **kw: self.http('PUT', url, **kw)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.loop = asyncio.new_event_loop()
        self.polling = self.loop.create_task(self.tasks.poller.run())
        def run_poller():
            try:
                self.loop.run_until_complete(self.polling)
            except asyncio.CancelledError:
                pass
        self.thread = Thread(target=run_poller, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_poller)

    def stop_poller(self):
        self.loop.call_soon_threadsafe(self.polling.cancel)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())
        self.loop.close()

    def http(self, method, url, **kw):
        path = url.split('/repos/owner/repo')[-1]
        if method == 'POST':
            self.request_count += 1
            return Response({'created_at': self.now.isoformat()}, 201)
        if method == 'PUT':
            self.merge_count += 1
            self.assertEqual(kw['json']['sha'], self.head)
            if self.refusal:
                return Response({'message': self.refusal}, 405)
            # Simulated GitHub squash operates on the bare remote, leaving the
            # canonical checkout untouched until the real sync implementation.
            tree = self.git('--git-dir', str(self.remote), 'rev-parse', f'{self.head}^{{tree}}')
            self.merge_sha = self.git('--git-dir', str(self.remote), 'commit-tree', tree,
                                      '-p', self.base, '-m', 'squashed by GitHub')
            self.git('--git-dir', str(self.remote), 'update-ref', 'refs/heads/main', self.merge_sha)
            self.merged = True
            return Response({'merged': True, 'sha': self.merge_sha})
        if path == '/pulls/72':
            return Response({'head': {'sha': self.live_head, 'ref': 'fix/completed', 'repo': {'full_name': 'owner/repo'}},
                             'base': {'sha': self.base, 'ref': 'main'}, 'state': 'open',
                             'merged': self.merged, 'merge_commit_sha': self.merge_sha,
                             'mergeable': True, 'mergeable_state': 'clean'})
        if path.startswith(f'/commits/{self.head}/statuses'):
            # The reviewer's own round marker: nothing until the trigger goes
            # out, pending for the first `review_pending` looks at it, then
            # completed. The round advances as the waiter polls its status,
            # because the reader fetches no content until the round ends.
            # D-042: a merge reads the review of its exact head first. Tests
            # about what happens at merge time start from a head that was
            # already reviewed clean; tests that request a review say so.
            if (not self.request_count and not self.already_reviewed) or 'page=1' not in path:
                return Response([])
            self.review_reads += 1
            prior = self.already_reviewed and not self.request_count
            state = 'pending' if self.review_reads <= self.review_pending and not prior else 'success'
            return Response([{'context': 'CodeRabbit', 'state': state,
                              'creator': {'login': 'coderabbitai[bot]'},
                              'created_at': self.now.isoformat()}])
        if path.startswith('/issues/72/comments'):
            prior = self.already_reviewed and not self.request_count
            if self.review_reads <= self.review_pending and not prior:
                return Response([{'id': 1, 'user': {'login': 'coderabbitai[bot]'},
                                  'body': 'Review in progress', 'created_at': self.now.isoformat()}])
            return Response([{'id': 1, 'user': {'login': 'coderabbitai[bot]'},
                              'body': f'No actionable findings. Reviewed {self.head}',
                              'created_at': self.now.isoformat()}])
        if path.startswith('/pulls/72/comments') or path.startswith('/pulls/72/reviews'):
            return Response([])
        if path.startswith('/compare/'):
            return Response({'behind_by': self.behind})
        if path == '/branches/main/protection/required_status_checks':
            return Response({'checks': [{'context': 'law-gates', 'app_id': 1}]})
        if path == '/rules/branches/main':
            return Response([])
        if '/check-runs?' in path:
            self.check_reads += 1
            return Response({'total_count': 1, 'check_runs': [
                {'id': 1, 'name': 'law-gates', 'app': {'id': 1},
                 'status': 'in_progress' if self.check_reads <= self.ci_pending else 'completed',
                 'conclusion': self.check_conclusion}]})
        if '/status?' in path:
            return Response({'total_count': 0, 'statuses': []})
        raise AssertionError((method, path))

    def dispatch(self, call, state):
        self.call_id[0] = call.call_id
        attempt = self.broker.dispatch(call, AuthorityContext(
            principal_reference='alx', granted_permission_references=self.permissions,
            approvals=state.approvals, evaluated_at=datetime.now(UTC),
        ))
        self.attempts.append(attempt)
        return attempt

    def run_core(self, decisions):
        reasoner = Decisions(decisions)
        core = CoreAgent(self.goals, reasoner, self.dispatch, self.definitions,
                         approval_free_capabilities=frozenset({'merge_pull_request', 'read_external_review', 'repository_operation'}),
                         turn_bound_capabilities=frozenset({'request_external_review'}))
        conversation = ConversationSnapshot('conversation', (
            ConversationTurn('conversation', 'turn', ConversationOrigin.TYPED,
                             'Request one external review and finish the completed coding job.',
                             self.now, 'friedl'),), 1, self.now + timedelta(days=1))
        outcome = core.process(conversation, self.now + timedelta(days=1), 25)
        return outcome, reasoner

    def merge_decision(self, identifier='merge'):
        return AgentDecision(goal_id='goal', call=CapabilityCall(
            identifier, 'merge_pull_request', {'pull_request_number': 72, 'head_sha': self.head}))

    def test_real_post_coding_path_waits_merges_syncs_and_finishes(self):
        self.already_reviewed = False
        request = CapabilityCall('request', 'request_external_review', {'pull_request_number': 72}, approval_id='approval')
        outcome, reasoner = self.run_core([
            AgentDecision(goal_id='goal', call=request,
                          approval_proposal=ApprovalProposal('approval', ApprovalScope(request.capability_id, request.arguments), 'turn:turn')),
            AgentDecision(goal_id='goal', call=CapabilityCall('read', 'read_external_review', {'pull_request_number': 72, 'head_sha': self.head})),
            self.merge_decision(),
            AgentDecision(goal_id='goal', response='Merged and main is synced.',
                          goal_proposal=GoalProposal(GoalMutationKind.REQUEST_COMPLETION, outstanding_work=(),
                                                    new_evidence=(Evidence('done-evidence', 'Merged exact reviewed head and synced main', supports=('done',), source_references=('attempt:merge',)),))),
        ])
        self.assertEqual(outcome.response, 'Merged and main is synced.', (outcome, self.attempts))
        self.assertEqual(self.goals.load('goal').state.status, GoalStatus.COMPLETED)
        self.assertEqual(len(reasoner.contexts), 4)
        self.assertEqual([a.call.capability_id for a in self.attempts],
                         ['request_external_review', 'read_external_review', 'merge_pull_request'])
        self.assertTrue(all(a.result.state is CapabilityResultState.SUCCEEDED for a in self.attempts), self.attempts)
        self.assertEqual(self.request_count, 1)
        self.assertGreater(self.review_reads, self.review_pending)
        self.assertEqual(self.check_reads, 4)
        self.assertEqual(self.merge_count, 1)
        self.assertEqual(self.git('branch', '--show-current'), 'main')
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('--git-dir', str(self.remote), 'rev-parse', 'main'))
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(self.tasks.store.completed_unhandled(), ())
        self.assertEqual(self.goals.load('goal').state.attempts[0], self.finished)

    def test_unknown_refusal_is_explained_once_without_retry(self):
        self.ci_pending = 0
        self.refusal = 'A protection rule requires judgement.'
        outcome, reasoner = self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='Protection blocks this merge.')])
        self.assertEqual(outcome.response, 'Protection blocks this merge.')
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.merge_count, 1)
        self.assertEqual(self.attempts[0].result.failure['http_status'], 405)
        self.assertEqual(self.attempts[0].result.failure['github_message'], self.refusal)

    def test_unknown_refusal_cannot_be_retried_by_core(self):
        self.ci_pending = 0
        self.refusal = 'Unknown protection'
        outcome, reasoner = self.run_core([self.merge_decision(), self.merge_decision('retry')])
        self.assertEqual(outcome.reason, 'merge_refused')
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.merge_count, 1)

    def test_changed_head_never_merges(self):
        self.live_head = 'f' * 40
        outcome, reasoner = self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='The new head needs review.')])
        self.assertEqual(self.attempts[0].result.failure['code'], 'head_changed')
        self.assertEqual(self.merge_count, 0)
        self.assertEqual(len(reasoner.contexts), 2)

    def test_pending_ci_times_out_without_core_polling(self):
        self.ci_pending = 999
        outcome, reasoner = self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='Checks timed out.')])
        self.assertEqual(self.attempts[0].result.failure['code'], 'checks_timed_out')
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.check_reads, 5)
        self.assertEqual(self.merge_count, 0)

    def test_failed_required_check_does_not_merge(self):
        self.ci_pending = 0
        self.check_conclusion = 'failure'
        self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='Law gates failed.')])
        self.assertEqual(self.attempts[0].result.failure['code'], 'checks_failed')
        self.assertEqual(self.merge_count, 0)

    def test_behind_rebases_once_and_requires_new_review_approval(self):
        # Advance only remote main through a temporary clone, not a worktree.
        upstream = self.root / 'upstream'
        self.git('clone', '-b', 'main', str(self.remote), str(upstream))
        self.git('config', 'user.email', 'test@example.invalid', root=upstream)
        self.git('config', 'user.name', 'Test', root=upstream)
        (upstream / 'other.txt').write_text('main advanced\n')
        self.git('add', 'other.txt', root=upstream)
        self.git('commit', '-m', 'main advanced', root=upstream)
        self.git('push', 'origin', 'main', root=upstream)
        self.base = self.git('rev-parse', 'HEAD', root=upstream)
        self.behind = 1
        outcome, reasoner = self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='Rebased; new review approval is required.')])
        failure = self.attempts[0].result.failure
        self.assertEqual(failure['code'], 'review_required', failure)
        self.assertTrue(failure['new_review_approval_required'])
        self.assertNotEqual(failure['current_head'], self.head)
        self.assertEqual(self.merge_count, 0)
        self.assertEqual(self.request_count, 0)
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(sum('rebase' in op for op in self.operations), 1)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.git('--git-dir', str(self.remote), 'rev-parse', 'fix/completed'))

    def test_a_rebase_conflict_is_aborted_and_needs_judgement(self):
        upstream = self.root / 'upstream'
        self.git('clone', '-b', 'main', str(self.remote), str(upstream))
        self.git('config', 'user.email', 'test@example.invalid', root=upstream)
        self.git('config', 'user.name', 'Test', root=upstream)
        (upstream / 'file.txt').write_text('main changed the same lines\n')
        self.git('add', 'file.txt', root=upstream)
        self.git('commit', '-m', 'main conflicts', root=upstream)
        self.git('push', 'origin', 'main', root=upstream)
        self.base = self.git('rev-parse', 'HEAD', root=upstream)
        self.behind = 1
        self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='The rebase conflicts.')])
        failure = self.attempts[0].result.failure
        self.assertEqual(failure['code'], 'merge_conflict', failure)
        self.assertEqual(self.merge_count, 0)
        self.assertEqual(self.git('rev-parse', 'HEAD'), self.head)
        self.assertEqual(self.git('branch', '--show-current'), 'fix/completed')
        self.assertFalse((self.checkout / '.git' / 'rebase-merge').exists())
        self.assertFalse((self.checkout / '.git' / 'rebase-apply').exists())
        self.assertEqual((self.checkout / 'file.txt').read_text(), 'completed implementation\n')

    def test_review_timeout_returns_once_without_another_paid_request(self):
        self.already_reviewed = False
        self.review_pending = 10000
        self.tasks.poller._maximum_wait = 0.02
        request = CapabilityCall('request', 'request_external_review', {'pull_request_number': 72}, approval_id='approval')
        outcome, reasoner = self.run_core([
            AgentDecision(goal_id='goal', call=request,
                          approval_proposal=ApprovalProposal('approval', ApprovalScope(request.capability_id, request.arguments), 'turn:turn')),
            AgentDecision(goal_id='goal', response='The review timed out.'),
        ])
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.request_count, 1)
        self.assertEqual(self.attempts[0].result.failure['reason'], 'timed_out')
        self.assertEqual(self.tasks.store.outstanding(), ())
        self.assertEqual(self.tasks.store.completed_unhandled(), ())

    def test_unpublished_read_is_not_a_core_polling_capability(self):
        self.already_reviewed = False
        read = AgentDecision(goal_id='goal', call=CapabilityCall(
            'read', 'read_external_review', {'pull_request_number': 72, 'head_sha': self.head}))
        retry = AgentDecision(goal_id='goal', call=CapabilityCall(
            'retry', 'read_external_review', {'pull_request_number': 72, 'head_sha': self.head}))
        outcome, reasoner = self.run_core([read, retry])
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(len(self.attempts), 1)
        self.assertEqual(outcome.reason, 'review_unavailable')

    def test_review_request_is_unavailable_without_the_sole_waiter(self):
        self.assertIsNone(build_review_runtime(True, 'owner/repo', 'token', lambda: 'request'))

    def test_conflict_needs_judgement_and_never_rebases(self):
        original = self.http
        def conflict(method, url, **kw):
            response = original(method, url, **kw)
            if url.endswith('/pulls/72'):
                response.body['mergeable'] = False
            return response
        with patch('alx.providers.github_merge.httpx.get', lambda url, **kw: conflict('GET', url, **kw)):
            self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='There is a merge conflict.')])
        self.assertEqual(self.attempts[0].result.failure['code'], 'merge_conflict')
        self.assertEqual(self.merge_count, 0)
        self.assertFalse(any('rebase' in op for op in self.operations))

    def test_transport_failure_returns_once(self):
        import httpx
        with patch('alx.providers.github_merge.httpx.get', side_effect=httpx.ConnectError('unavailable')):
            _, reasoner = self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='GitHub is unavailable.')])
        self.assertEqual(self.attempts[0].result.failure['code'], 'merge_unavailable')
        self.assertEqual(len(reasoner.contexts), 2)
        self.assertEqual(self.merge_count, 0)

    def test_dirty_checkout_preserves_work_and_reports_merge_already_done(self):
        self.ci_pending = 0
        (self.checkout / 'file.txt').write_text('unrelated local work')
        self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='Merged; local work blocks synchronization.')])
        failure = self.attempts[0].result.failure
        self.assertEqual(failure['code'], 'local_sync_failed')
        self.assertTrue(failure['merged'])
        self.assertEqual(self.git('branch', '--show-current'), 'fix/completed')
        self.assertEqual((self.checkout / 'file.txt').read_text(), 'unrelated local work')
        # Resume synchronization of the same merged head without another PUT.
        (self.checkout / 'file.txt').write_text('completed implementation\n')
        result = self.merge.provider.merge(MergeRequest(72, self.head))
        self.assertEqual(result.checkout_branch, 'main')
        self.assertEqual(self.merge_count, 1)

    def test_refusal_detail_is_bounded(self):
        self.ci_pending = 0
        self.refusal = 'x' * 1000
        self.run_core([self.merge_decision(), AgentDecision(goal_id='goal', response='GitHub refused.')])
        self.assertEqual(len(self.attempts[0].result.failure['github_message']), 300)

    def test_force_push_lease_stays_bound_to_authorised_head(self):
        self.git('fetch', str(self.remote), '+refs/heads/*:refs/remotes/origin/*')
        (self.checkout / 'file.txt').write_text('new local revision\n')
        self.git('commit', '-am', 'new local revision')
        result = self.repo.authority.perform(RepositoryRequest(
            Operation.FORCE_PUSH,
            {'branch': 'fix/completed', 'expected_head': self.base},
        ))
        self.assertFalse(result.succeeded)
        self.assertEqual(self.git('--git-dir', str(self.remote), 'rev-parse', 'fix/completed'), self.head)
        self.assertTrue(any(f'--force-with-lease=fix/completed:{self.base}' in op for op in self.operations))
