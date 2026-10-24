"""D-033 continuation: a goal may continue a branch its own coding work used.

Ownership is the branch, not the commit at HEAD (2026-10-24).
"""
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
             succeeded=True, durable=True, implementation_reached=None):
    values = {'commit': {'branch': branch, 'commit_sha': sha}}
    call = CapabilityCall(identity, capability, {'task': 'work'})
    failure = None
    if not succeeded:
        failure = {"code": "task_failed"}
        if implementation_reached is not None:
            failure["implementation_reached"] = implementation_reached
    result = CapabilityResult(identity, capability,
        CapabilityResultState.SUCCEEDED if succeeded else CapabilityResultState.FAILED,
        values, failure=failure,
        durable_values=values if durable else {})
    return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)


class Ownership(unittest.TestCase):
    def test_repeated_resume_recovers_original_request_fields(self):
        original = {
            'task': 'repair', 'context': 'original context',
            'acceptance_criteria': ['specific result'], 'test_guidance': 'run focused tests',
            # A record from before step_budget was removed still resumes.
            'step_budget': 7, 'repair_branch': 'feat/a', 'commit_message': 'repair',
        }
        resumed = {'resume_job_id': 'job-1'}

        def failed(job_id, arguments):
            call = CapabilityCall(job_id, 'run_coding_task', arguments)
            result = CapabilityResult(
                job_id, 'run_coding_task', CapabilityResultState.FAILED,
                failure={'code': 'required_verification_failed'},
                durable_values={'checkpoint': json.dumps({
                    'job_id': job_id, 'branch': 'feat/a-2', 'stage': 'test',
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
        self.assertFalse(hasattr(captured[0], 'step_budget'))
        self.assertEqual(captured[0].resume_checkpoint['job_id'], 'job-2')

    def test_any_branch_the_goals_coding_work_used_is_owned(self):
        state = goal(recorded('feat/a', 'a', '1'), recorded('feat/b', 'b', '2'),
                     recorded('feat/c', 'c', '3', succeeded=False),
                     recorded('feat/d', 'd', '4', succeeded=False,
                              implementation_reached=False),
                     recorded('feat/e', 'e', '5', capability='repository_git'),
                     recorded('feat/f', 'f', '6', durable=False))
        for branch in ('feat/a', 'feat/b', 'feat/c'):
            with self.subTest(branch=branch):
                self.assertEqual(goal_coding_branch(state, branch),
                                 BranchContinuation(branch))
        # Refused before implementation, another capability, nothing recorded.
        for branch in ('feat/d', 'feat/e', 'feat/f', 'feat/missing', ' feat/a', 'main'):
            with self.subTest(branch=branch):
                self.assertIsNone(goal_coding_branch(state, branch))
        self.assertIsNone(goal_coding_branch(goal(), 'feat/a'))
        self.assertIsNone(goal_coding_branch(None, 'feat/a'))
        self.assertIn('created or worked on', DEFINITION.purpose)
        self.assertIn('hand commit', DEFINITION.purpose)

    def test_requested_name_counts_only_when_no_branch_was_recorded(self):
        def attempt(arguments, values, identity, *, failure=None):
            call = CapabilityCall(identity, 'run_coding_task', arguments)
            result = CapabilityResult(
                identity, 'run_coding_task',
                CapabilityResultState.FAILED if failure else CapabilityResultState.SUCCEEDED,
                values, failure=failure, durable_values=values)
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)

        # Asked for feat/x, which was taken, so the job made feat/x-2.
        numbered = attempt({'task': 'w', 'repair_branch': 'feat/x'},
                           {'commit': {'branch': 'feat/x-2', 'commit_sha': 'a'},
                            'branch': 'feat/x-2'}, '1')
        self.assertIsNone(goal_coding_branch(goal(numbered), 'feat/x'))
        self.assertEqual(goal_coding_branch(goal(numbered), 'feat/x-2'),
                         BranchContinuation('feat/x-2'))
        # A failed job that recorded no branch: the name it was given counts.
        bare = attempt({'task': 'w', 'repair_branch': 'feat/y'}, {}, '2',
                       failure={'code': 'session_failed'})
        self.assertEqual(goal_coding_branch(goal(bare), 'feat/y'),
                         BranchContinuation('feat/y'))
        # Only a checkpoint recorded the branch, as on a failed resume.
        checkpointed = attempt({'resume_job_id': 'job-1'},
                               {'checkpoint': json.dumps({'branch': 'feat/z-3'})}, '3',
                               failure={'code': 'required_verification_failed'})
        self.assertEqual(goal_coding_branch(goal(checkpointed), 'feat/z-3'),
                         BranchContinuation('feat/z-3'))

    def test_a_malformed_record_is_skipped_not_fatal(self):
        good = recorded('feat/a', 'a', '1')
        broken = (
            recorded('feat/a', '', 'blank'),
            recorded('feat/a', 12, 'number'),
            recorded('main', 'abc', 'mainline'),
            recorded('../bad', 'abc', 'bad'),
            recorded(12, 'abc', 'not-a-name'),
        )
        for attempt in broken:
            with self.subTest(identity=attempt.call.call_id):
                self.assertEqual(goal_coding_branch(goal(attempt, good), 'feat/a'),
                                 BranchContinuation('feat/a'))
                self.assertEqual(goal_coding_branch(goal(good, attempt), 'feat/a'),
                                 BranchContinuation('feat/a'))
        for branch in ('main', '../bad'):
            self.assertIsNone(goal_coding_branch(goal(*broken), branch))

    def test_contract_validates_and_freezes(self):
        for branch in ('main', '../bad', '', 'refs/heads/x', 12):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                BranchContinuation(branch)
        continuation = BranchContinuation('feat/a')
        self.assertFalse(hasattr(continuation, 'permitted_heads'))
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
            ('diff', '--no-color'): 'shas',
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

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_job(self, edits=None, *, continuation=False, completed=True, branch='feat/goal'):
        identity = f'job-{len(self.state.attempts)}'
        session = RecordingSession(edits=edits, completed=completed)
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: identity, session=session,
            repository=self.root,
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
        return attempt.result

    def first(self):
        result = self.run_job({'app.py': _FIXED})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        return result.values['commit_sha']

    def assert_refused(self, result, reason=None):
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure['code'], 'git_refused')
        self.assertIs(result.failure['implementation_reached'], False)
        self.assertEqual(self.sessions[-1].calls, [])
        self.assertEqual(CoreAgent._consecutive_coding_failures(self.state), 0)
        if reason is not None:
            self.assertEqual(result.failure['reason_code'], reason)

    def plant(self, *attempts):
        self.state = replace(self.state, attempts=(*self.state.attempts, *attempts))

    def later_verified_branch(self, branch='feat/other'):
        self.git('switch', 'main')
        result = self.run_job({'other.md': f'{branch}\n'}, branch=branch)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values['branch'], branch)
        return result.values['commit_sha']

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
        self.assertFalse(result.values['tests_run'])
        self.assertTrue(result.values['all_required_verification_passed'])
        self.assertIn('BRANCH continued', [t.transition for t in self.telemetry])
        self.assertEqual(goal_coding_branch(self.state, 'feat/goal'),
                         BranchContinuation('feat/goal'))
        self.assertIsNone(goal_coding_branch(self.state, 'feat/other'))

    def test_unrelated_goal_and_mismatched_request_cannot_continue(self):
        self.first()
        owned = self.state
        self.state = replace(goal(), goal_id='unrelated')
        self.assert_refused(self.run_job(continuation=True), 'continuation_ownership_unproven')
        self.state = owned
        self.assert_refused(self.run_job(continuation=True, branch='feat/different'),
                            'continuation_ownership_unproven')
        self.assert_refused(self.run_job(continuation=True, branch=' feat/goal '),
                            'continuation_ownership_unproven')

    def test_dirty_continuation_refused_and_preserved(self):
        self.first()
        (self.root / 'mine.txt').write_text('keep me\n')
        self.assert_refused(self.run_job(continuation=True), 'canonical_checkout_dirty')
        self.assertEqual((self.root / 'mine.txt').read_text(), 'keep me\n')

    def test_detached_and_wrong_branch_refused(self):
        sha = self.first()
        self.git('checkout', '--detach', sha)
        self.assert_refused(self.run_job(continuation=True), 'continuation_branch_mismatch')
        self.git('switch', 'main')
        self.assert_refused(self.run_job(continuation=True), 'continuation_branch_mismatch')

    def test_main_is_never_continued(self):
        self.first()
        self.git('switch', 'main')
        self.assert_refused(self.run_job(continuation=True, branch='main'),
                            'continuation_ownership_unproven')
        self.assertEqual(self.git('branch', '--show-current'), 'main')

    def test_branch_never_used_by_the_goal_is_refused(self):
        self.first()
        self.git('switch', '-c', 'feat/hand-made', 'main')
        self.assert_refused(self.run_job(continuation=True, branch='feat/hand-made'),
                            'continuation_ownership_unproven')

    def test_each_low_level_precondition(self):
        sha = self.first()
        for branch in ('main', '../bad'):
            with self.subTest(branch=branch):
                with self.assertRaises(CodingError) as caught:
                    continue_feature_branch(self.root, branch)
                self.assertEqual(
                    caught.exception.details['reason_code'], 'branch_name_not_permitted',
                )
        with self.assertRaises(CodingError) as caught:
            continue_feature_branch(self.root, 'feat/other')
        self.assertEqual(
            caught.exception.details['reason_code'], 'continuation_branch_mismatch',
        )
        self.assertEqual(continue_feature_branch(self.root, 'feat/goal'), 'feat/goal')
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)

    def test_earlier_branch_continues_after_a_later_commit_on_another_branch(self):
        sha = self.first()
        self.later_verified_branch()
        self.git('switch', 'feat/goal')
        self.assertEqual(self.git('rev-parse', 'HEAD'), sha)
        result = self.run_job({'rollback.md': 'Continued from the earlier branch.\n'},
                              continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values['branch'], 'feat/goal')
        self.assertEqual(self.git('rev-parse', 'HEAD^'), sha)
        self.assertEqual(self.git('branch', '--show-current'), 'feat/goal')

    def test_hand_commit_on_the_goals_branch_continues(self):
        sha = self.first()
        (self.root / 'hold.txt').write_text('committed by hand, not by a coding job\n')
        self.git('add', 'hold.txt')
        self.git('commit', '-m', 'hand commit')
        hand = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('rev-parse', 'HEAD^'), sha)
        result = self.run_job({'more.md': 'After the hand commit.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(result.values['baseline']['head_sha'], hand)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), hand)
        self.assertEqual(list(result.values['commit']['committed_files']), ['more.md'])

    def test_rebased_and_force_pushed_branch_continues(self):
        # A merge that rebased the pull request leaves HEAD on a commit no
        # coding job recorded. The branch is still this goal's.
        self.first()
        self.git('switch', 'main')
        self.git('commit', '--allow-empty', '-m', 'main moved on')
        self.git('switch', 'feat/goal')
        self.git('rebase', 'main')
        rebased = self.git('rev-parse', 'HEAD')
        self.assertEqual(self.git('branch', '--show-current'), 'feat/goal')
        result = self.run_job({'notes.md': 'After the rebase.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), rebased)

    def test_failed_job_branch_is_owned_unless_refused_before_implementation(self):
        self.first()
        self.git('switch', '-c', 'feat/failed', 'main')
        self.plant(recorded('feat/failed', 'f' * 40, 'failed', succeeded=False))
        result = self.run_job({'notes.md': 'Continued a failed job.\n'},
                              continuation=True, branch='feat/failed')
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.git('switch', '-c', 'feat/refused', 'main')
        self.plant(recorded('feat/refused', 'e' * 40, 'refused', succeeded=False,
                            implementation_reached=False))
        self.assert_refused(self.run_job(continuation=True, branch='feat/refused'),
                            'continuation_ownership_unproven')

    def test_malformed_record_does_not_disable_the_goal(self):
        self.first()
        self.plant(recorded('main', '', 'malformed'), recorded('../bad', 7, 'worse'))
        result = self.run_job({'notes.md': 'Still continues.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)

    def test_other_branch_checked_out_is_a_mismatch(self):
        sha = self.first()
        other = self.later_verified_branch()
        self.assertEqual(self.git('branch', '--show-current'), 'feat/other')
        self.assert_refused(self.run_job(continuation=True, branch='feat/goal'),
                            'continuation_branch_mismatch')
        self.assertEqual(self.git('rev-parse', 'HEAD'), other)
        self.assertEqual(self.git('rev-parse', 'feat/goal'), sha)

    def test_rollback_to_previously_recorded_goal_commit_can_continue(self):
        sha = self.first()
        result = self.run_job({'notes.md': 'Later commit.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        # Test fixture simulates AL/X's independent repository authority.
        self.git('reset', '--hard', sha)
        result = self.run_job({'rollback.md': 'New delta from earlier head.\n'}, continuation=True)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED, result.failure)
        self.assertEqual(self.git('rev-parse', 'HEAD^'), sha)

    def test_failed_continued_implementation_keeps_work(self):
        sha = self.first()
        result = self.run_job({'notes.md': 'Partial work.\n'}, continuation=True, completed=False)
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        # The session never completed: a CLI failure, not counted (2026-10-24).
        self.assertEqual(CoreAgent._consecutive_coding_failures(self.state), 0)
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
