"""D-033 goal-owned continuation uses durable evidence and no new Git authority."""
from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.bootstrap.coding import build_coding_runtime
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall,
    CapabilityResult, CapabilityResultState, GoalState, Objective, SuccessCriterion,
)
from alx.contracts.coding import BranchContinuation, CodingError
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.providers.coding_git import continue_feature_branch, _WRITE_SHAPES
from alx.safety import AuthorityContext, SafetyGate
from alx.tools.coding import DEFINITION, build_coding_executors, goal_coding_branch, parse_coding_arguments
from test_coding_agent import NOW, PlanningModel, RecordingSession, _FIXED, _worktree


def goal(*attempts):
    return GoalState('goal-a', Objective('turn:t', 'development'),
                     (SuccessCriterion('c', 'done'),), attempts=attempts)


def recorded(branch, sha, identity='record', *, capability='run_coding_task',
             succeeded=True, durable=True):
    values = {'commit': {'branch': branch, 'commit_sha': sha}}
    call = CapabilityCall(identity, capability, {'task': 'work'})
    result = CapabilityResult(identity, capability,
        CapabilityResultState.SUCCEEDED if succeeded else CapabilityResultState.FAILED,
        values, failure=None if succeeded else {"code": "task_failed"},
        durable_values=values if durable else {})
    return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)


class Ownership(unittest.TestCase):
    def test_repeated_resume_recovers_original_request_fields(self):
        original = {
            'task': 'repair', 'context': 'original context',
            'acceptance_criteria': ['specific result'], 'test_guidance': 'run focused tests',
            'step_budget': 7, 'repair_branch': 'feat/a', 'commit_message': 'repair',
        }
        resumed = {'resume_job_id': 'job-1'}

        def failed(job_id, arguments):
            call = CapabilityCall(job_id, 'run_coding_task', arguments)
            result = CapabilityResult(
                job_id, 'run_coding_task', CapabilityResultState.FAILED,
                failure={'code': 'review_failed'},
                durable_values={'checkpoint': json.dumps({
                    'job_id': job_id, 'branch': 'feat/a-2', 'stage': 'review',
                })},
            )
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)

        state = goal(failed('job-1', original), failed('job-2', resumed))
        captured = []

        def run_job(request):
            captured.append(request)
            raise CodingError('coding_unavailable')

        result = build_coding_executors(
            run_job, lambda: 'job-3', lambda: state,
        )['run_coding_task']({**resumed, 'resume_job_id': 'job-2'})
        self.assertEqual(result.failure['code'], 'coding_unavailable')
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].context, 'original context')
        self.assertEqual(captured[0].acceptance_criteria, ('specific result',))
        self.assertEqual(captured[0].test_guidance, 'run focused tests')
        self.assertEqual(captured[0].step_budget, 7)
        self.assertEqual(captured[0].resume_checkpoint['job_id'], 'job-2')

    def test_latest_successful_commit_owns_branch_and_all_its_heads(self):
        state = goal(recorded('feat/a', 'a', '1'), recorded('feat/b', 'b', '2'),
                     recorded('feat/a', 'c', '3'),
                     recorded('feat/b', 'd', '4', succeeded=False),
                     recorded('feat/b', 'e', '5', capability='repository_git'),
                     recorded('feat/b', 'f', '6', durable=False))
        self.assertEqual(goal_coding_branch(state), BranchContinuation('feat/a', frozenset({'a', 'c'})))
        self.assertIsNone(goal_coding_branch(goal()))
        self.assertIsNone(goal_coding_branch(None))

    def test_contract_validates_and_freezes(self):
        for branch, heads in [('main', {'a'}), ('../bad', {'a'}), ('feat/a', set()), ('feat/a', {''})]:
            with self.subTest(branch=branch, heads=heads), self.assertRaises(ValueError):
                BranchContinuation(branch, frozenset(heads))
        continuation = BranchContinuation('feat/a', {'a'})
        self.assertIsInstance(continuation.permitted_heads, frozenset)
        with self.assertRaises(FrozenInstanceError):
            continuation.branch = 'feat/b'

    def test_model_cannot_supply_internal_continuation(self):
        arguments = {'task': 'work', 'repair_branch': 'feat/a', 'commit_message': 'work'}
        request, error = parse_coding_arguments(arguments, 'job')
        self.assertIsNone(error)
        self.assertIsNone(request.continuation)
        for supplied in ({}, None, {'branch': 'feat/a', 'permitted_heads': ['a']}):
            request, error = parse_coding_arguments({**arguments, 'continuation': supplied}, 'job')
            self.assertIsNone(request)
            self.assertEqual(error['invalid_field'], 'continuation')
        for value in ('true', 1, None):
            self.assertIsNotNone(parse_coding_arguments({**arguments, 'continue_goal_branch': value}, 'job')[1])
        self.assertNotIn('continuation', DEFINITION.input_schema.properties)
        self.assertIn('continue_goal_branch', DEFINITION.durable_input_fields)

    def test_write_shapes_are_exactly_unchanged(self):
        self.assertEqual(_WRITE_SHAPES, {
            ('rev-parse', 'HEAD'): 'none',
            ('rev-parse', '--show-toplevel'): 'none',
            ('rev-parse', '--git-common-dir'): 'none',
            ('symbolic-ref', '--quiet', '--short', 'HEAD'): 'none',
            ('status', '--porcelain=v1', '-z', '-uall'): 'none',
            ('diff', '--cached', '--name-only', '-z'): 'none',
            ('show', '--name-status', '--pretty=format:', '-z'): 'sha',
            ('check-attr', '-z', 'filter', '--'): 'paths',
            ('check-ignore', '-q', '--'): 'paths',
            ('ls-files', '--stage', '-z'): 'none',
            ('diff', '--cached', '--name-status', '-z'): 'none',
            ('add', '--'): 'paths', ('reset', '--quiet', '--'): 'paths',
            ('commit', '--quiet', '-m'): 'value', ('switch', '-c'): 'pair',
        })


class ContinuationJobs(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name)
        self.root = _worktree(self.parent).resolve()
        self.state = goal()
        self.telemetry = []
        self.sessions = []
        self.reviewers = []

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_job(self, edits=None, *, continuation=False, completed=True, branch='feat/goal'):
        identity = f'job-{len(self.state.attempts)}'
        session = RecordingSession(edits=edits, completed=completed)
        reviewer = PlanningModel()
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: identity, session=session,
            reviewer=reviewer, repository=self.root,
            goal_state_source=lambda: self.state, telemetry_sink=self.telemetry.append,
        )
        broker = CapabilityBroker(CapabilityRegistry(runtime.definitions),
                                  SafetyGate(runtime.policies), runtime.executors)
        call = CapabilityCall(identity, 'run_coding_task', {
            'task': 'make the bounded change', 'repair_branch': branch,
            'commit_message': identity, 'continue_goal_branch': continuation,
        })
        attempt = broker.dispatch(call, AuthorityContext('friedl', runtime.permissions, NOW))
        self.state = replace(self.state, attempts=(*self.state.attempts, attempt))
        self.sessions.append(session)
        self.reviewers.append(reviewer)
        return attempt.result

    def first(self):
        result = self.run_job({'app.py': _FIXED})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        return result.values['commit_sha']

    def assert_refused(self, result):
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertIs(result.failure['implementation_reached'], False)
        self.assertEqual(self.sessions[-1].calls, [])
        self.assertEqual(CoreAgent._failed_coding_executions(self.state), 0)
        self.assertEqual(CoreAgent._request_conflict_coding_executions(self.state), 0)

    def test_sequential_jobs_restart_and_only_own_delta(self):
        first_sha = self.first()
        store_path = self.parent / 'goals.sqlite3'
        store = SQLiteGoalStore(store_path)
        store.create(self.state, 'conversation', NOW + timedelta(days=1))
        store.close()
        store = SQLiteGoalStore(store_path)
        self.state = store.load('goal-a').state
        store.close()
        # AL/X positions the visible checkout, not the Coding Agent.
        self.git('switch', 'main')
        self.git('switch', 'feat/goal')
        result = self.run_job({'notes.md': 'Second job only.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values['branch'], 'feat/goal')
        self.assertEqual(result.values['baseline']['head_sha'], first_sha)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), first_sha)
        self.assertEqual(list(result.values['commit']['committed_files']), ['notes.md'])
        self.assertEqual(list(result.values['files_changed']), ['notes.md'])
        self.assertNotIn('app.py', result.values['git_diff'])
        reviews = [r for r in self.reviewers[-1].requests if r.output_schema_name == 'alx_coding_local_review']
        self.assertEqual(len(reviews), 1)
        self.assertIn('Second job only.', str(reviews[0]))
        self.assertNotIn('return a + b', str(reviews[0]))
        self.assertFalse(result.values['tests_run'])
        self.assertTrue(result.values['all_required_verification_passed'])
        self.assertIn('BRANCH continued', [t.transition for t in self.telemetry])
        self.assertEqual(goal_coding_branch(self.state).permitted_heads,
                         frozenset({first_sha, result.values['commit_sha']}))

    def test_unrelated_goal_and_mismatched_request_cannot_continue(self):
        self.first()
        owned = self.state
        self.state = replace(goal(), goal_id='unrelated')
        self.assert_refused(self.run_job(continuation=True))
        self.state = owned
        self.assert_refused(self.run_job(continuation=True, branch='feat/different'))
        self.assert_refused(self.run_job(continuation=True, branch=' feat/goal '))

    def test_dirty_continuation_refused_and_preserved(self):
        self.first()
        (self.root / 'mine.txt').write_text('keep me\n')
        self.assert_refused(self.run_job(continuation=True))
        self.assertEqual((self.root / 'mine.txt').read_text(), 'keep me\n')

    def test_detached_wrong_branch_and_unrecorded_head_refused(self):
        sha = self.first()
        self.git('checkout', '--detach', sha)
        self.assert_refused(self.run_job(continuation=True))
        self.git('switch', 'main')
        self.assert_refused(self.run_job(continuation=True))
        self.git('switch', 'feat/goal')
        self.git('commit', '--allow-empty', '-m', 'AL/X authored, not a CA job')
        self.assert_refused(self.run_job(continuation=True))

    def test_each_low_level_precondition(self):
        sha = self.first()
        for branch, heads in [('main', {sha}), ('../bad', {sha}), ('feat/other', {sha}), ('feat/goal', set()), ('feat/goal', {'unknown'})]:
            with self.subTest(branch=branch, heads=heads), self.assertRaises(CodingError):
                continue_feature_branch(self.root, branch, frozenset(heads))
        self.assertEqual(continue_feature_branch(self.root, 'feat/goal', frozenset({sha})), 'feat/goal')
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)

    def test_rollback_to_previously_recorded_goal_commit_can_continue(self):
        sha = self.first()
        result = self.run_job({'notes.md': 'Later commit.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        # Test fixture simulates AL/X's independent repository authority.
        self.git('reset', '--hard', sha)
        result = self.run_job({'rollback.md': 'New delta from earlier head.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), sha)

    def test_failed_continued_implementation_keeps_work_and_spends_allowance(self):
        sha = self.first()
        result = self.run_job({'notes.md': 'Partial work.\n'}, continuation=True, completed=False)
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(CoreAgent._failed_coding_executions(self.state), 1)
        self.assertEqual(CoreAgent._request_conflict_coding_executions(self.state), 0)
        self.assertEqual((self.root / 'notes.md').read_text(), 'Partial work.\n')
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)
        self.assertEqual(self.git('branch', '--show-current'), 'feat/goal')

    def test_one_shot_still_requires_main_and_creates_new_branch(self):
        self.first()
        self.assert_refused(self.run_job())
        self.git('switch', 'main')
        result = self.run_job({'app.py': _FIXED})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values['branch'], 'feat/goal-2')

    def test_bootstrap_uses_existing_goal_context(self):
        source = (Path(__file__).resolve().parents[1] / 'src/alx/bootstrap/live_voice.py').read_text()
        tree = ast.parse(source)
        call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == 'build_coding_runtime')
        argument = next(k.value for k in call.keywords if k.arg == 'goal_state_source')
        self.assertEqual(ast.unparse(argument), 'current_goal_state.get')
        self.assertNotIn('alx_current_notebook_goal_state', source)


if __name__ == '__main__':
    unittest.main()
