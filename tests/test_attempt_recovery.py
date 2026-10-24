"""Private recovery facts survive bounding, reloads and legacy migration."""

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.test_bounded_goal_results import NOW, goal
from alx.contracts import CapabilityCall, ExecutionStep, PlanCondition
from alx.core import CoreAgent
from alx.core.model_reasoner import _attempt_payload, _state_payload
from alx.core.plan_results import classify_planned_result, PlanResultKind
from alx.goals import SQLiteGoalStore
from alx.goals.result_summary import STRING_LIMIT
from alx.tools.coding import build_coding_executors, goal_coding_branch


def reload_goal(tmp_path, state):
    path = tmp_path / 'goals.sqlite3'
    with SQLiteGoalStore(path, clock=lambda: NOW) as store:
        store.create(state, 'conversation', NOW + timedelta(days=1))
    return SQLiteGoalStore(path, clock=lambda: NOW)


def test_large_checkpoint_resumes_after_close_and_reopen(tmp_path):
    checkpoint = {'job_id': 'call-1', 'branch': 'fix/work', 'stage': 'execution',
                  'plan': {'files': ['file-' + str(i) for i in range(1000)]}}
    raw = json.dumps(checkpoint)
    assert len(raw) > STRING_LIMIT
    state = goal({'status': 'failed', 'checkpoint': raw},
                 {'code': 'task_failed', 'details': 'failure detail' * 1000},
                 capability_id='run_coding_task')
    attempt = state.attempts[0]
    state = replace(state, attempts=(replace(attempt, call=CapabilityCall(
        'call-1', 'run_coding_task', {'task': 'repair', 'repair_branch': 'fix/work',
                                    'commit_message': 'repair'})),))
    with reload_goal(tmp_path, state) as store:
        loaded = store.load(state.goal_id)
        requests = []

        def run(request):
            requests.append(request)
            return SimpleNamespace(status='succeeded', finished_at=NOW,
                                   as_values=lambda: {'done': True},
                                   durable_values=lambda: {'done': True})

        result = build_coding_executors(run, lambda: 'resumed', lambda: loaded.state)[
            'run_coding_task']({'resume_job_id': 'call-1', 'corrective_action': 'repair it'})
        assert result.values.get('done') is True
        assert requests[0].resume_checkpoint == checkpoint
        assert requests[0].corrected_failure['details'] == 'failure detail' * 1000
        from alx.contracts import ReasoningContext
        from alx.contracts.core import GoalSummary
        context = ReasoningContext(
            loaded.state, (), (), conversation_id='conversation',
            unfinished_goals=(GoalSummary.of(loaded.state),),
            transient_attempts=loaded.state.attempts,
            recent_finished_goals=(loaded,),
        )
        for attempt in (context.active_goal.attempts[0], context.transient_attempts[0],
                        context.recent_finished_goals[0].state.attempts[0]):
            assert attempt.result._recovery is None
            assert attempt.result.durable_values['checkpoint'] != raw
        for public in (_state_payload(loaded.state), _attempt_payload(loaded.state.attempts[0]),
                       store.read_history(goal_id=state.goal_id)[0]):
            assert raw not in json.dumps(public)
            assert 'file-999' not in json.dumps(public)
            assert 'original_length' in json.dumps(public)
        assert store.read_history(contains='file-999')[0]['goals'] == []
        store.replace(loaded.state, loaded.retention_until, loaded.revision)
    with SQLiteGoalStore(tmp_path / 'goals.sqlite3') as store:
        assert store.load(state.goal_id).state.attempts[0].result.recovery_values()['checkpoint'] == raw


@pytest.mark.parametrize('section', ['values', 'failure'])
def test_plan_conditions_read_exact_oversized_facts_after_reload(tmp_path, section):
    text = 'x' * 10000
    state = goal({'fact': text}, {'code': 'failed', 'fact': text} if section == 'failure' else None)
    with reload_goal(tmp_path, state) as store:
        attempt = store.load(state.goal_id).state.attempts[0]
        from alx.core.plan_results import _document, conditions_match
        condition = PlanCondition(section + '.fact', text)
        assert conditions_match(_document(attempt), (condition,))
        if section == 'values':
            step = ExecutionStep(attempt.call, (condition,))
            assert classify_planned_result(step, attempt, None).kind is PlanResultKind.ADVANCE


def test_coding_branch_uses_exact_facts_after_reload(tmp_path):
    branch = 'fix/work'
    state = goal({**{f'evidence_{i}': i for i in range(1000)},
                  'commit': {'branch': branch, 'commit_sha': 'a' * 40}},
                 capability_id='run_coding_task')
    with reload_goal(tmp_path, state) as store:
        continuation = goal_coding_branch(store.load(state.goal_id).state, branch)
        assert continuation is not None
        assert continuation.permitted_heads == frozenset({'a' * 40})


def test_failure_signature_survives_large_checkpoint_lists_and_failure(tmp_path):
    from tests.test_coding_failure_episodes import verification_failed
    attempt = verification_failed('job', {}, tests=tuple('test-' + str(i) for i in range(100)))
    values = dict(attempt.result.values)
    values['checkpoint'] = json.dumps({**json.loads(values['checkpoint']), 'padding': 'x' * 10000})
    attempt = replace(attempt, result=replace(attempt.result, values=values, durable_values=values,
                      failure={**attempt.result.failure, 'reason_code': 'x' * 10000}))
    state = replace(goal({}), attempts=(attempt,))
    expected = CoreAgent._coding_failure_signature(attempt)
    with reload_goal(tmp_path, state) as store:
        assert CoreAgent._coding_failure_signature(store.load(state.goal_id).state.attempts[0]) == expected


@pytest.mark.parametrize('legacy_shape', ['attempts', 'completed_actions', 'bounded'])
def test_migration_preserves_intact_facts_and_does_not_promote_summary_markers(tmp_path, legacy_shape):
    path = tmp_path / 'goals.sqlite3'
    raw = json.dumps({'stage': 'test', 'padding': 'x' * 10000})
    with SQLiteGoalStore(path) as store:
        store.create(goal({}), 'conversation', NOW + timedelta(days=1))
    with sqlite3.connect(path) as connection:
        data = json.loads(connection.execute('SELECT state_json FROM goals').fetchone()[0])
        values = {'checkpoint': raw, 'status': 'failed'}
        if legacy_shape == 'bounded':
            from alx.goals.result_summary import bound_result
            values = bound_result(values)
        data['attempts'][0][6][3] = values
        if legacy_shape == 'completed_actions':
            data['completed_actions'] = [data.pop('attempts')[0][6][:6]]
        connection.execute('UPDATE goals SET state_json = ?', (json.dumps(data),))
        connection.execute('DROP TABLE attempt_recovery')
        connection.execute('PRAGMA user_version = 10')
    for _ in range(2):
        with SQLiteGoalStore(path) as store:
            result = store.load('goal-1').state.attempts[0].result
            recovered = result.recovery_values()
            assert recovered.get('checkpoint') == (None if legacy_shape == 'bounded' else raw)
            assert recovered['status'] == 'failed'
            assert result.values['checkpoint']['original_length'] == len(raw)


@pytest.mark.parametrize('capability,values,reader', [
    ('capture_supplier_invoice', {'completed': True}, 'captured_invoice_filing_scopes'),
    ('process_dhl_import', {'completed': True, 'stage': 'customs_documents',
                          'source_mail': [{'mailbox_id': 'mail', 'uid_validity': 'v', 'uid': str(i)}
                                          for i in range(100)]}, 'dhl_mail_scopes'),
    ('send_mail_reply', {'accepted': True, 'source_has_attachments': False},
     'mail_post_reply_standing_scopes'),
])
def test_mail_authority_uses_exact_completion_and_references(tmp_path, capability, values, reader):
    from alx.bootstrap import mail
    values = {**{f'item_{i}': i for i in range(1000)}, **values}
    state = goal(values, capability_id=capability)
    attempt = state.attempts[0]
    state = replace(state, attempts=(replace(attempt, call=replace(
        attempt.call, arguments={'mailbox_id': 'mail', 'uid_validity': 'v', 'uid': '1'}, durable_arguments=None)),))
    read = getattr(mail, reader)
    expected = read(state)
    assert expected
    with reload_goal(tmp_path, state) as store:
        assert read(store.load(state.goal_id).state) == expected


def test_invoice_completion_and_copied_mail_subject_survive_reload(tmp_path):
    from alx.bootstrap.live_voice import _completed
    from alx.contracts import ConversationSnapshot
    subject = 'source subject ' * 1000
    state = goal({'subject': subject, **{f'item_{i}': i for i in range(1000)}, 'completed': True},
                 capability_id='read_mail_message')
    attempt = state.attempts[0]
    reference = {'mailbox_id': 'mail', 'uid_validity': 'v', 'uid': '1'}
    state = replace(state, attempts=(replace(attempt, call=replace(attempt.call, arguments=reference, durable_arguments=None)),))
    with reload_goal(tmp_path, state) as store:
        loaded = store.load(state.goal_id).state
        assert _completed(loaded.attempts[0])
        conversation = ConversationSnapshot('conversation', (), 1, NOW + timedelta(days=1))
        call = CapabilityCall('send', 'send_mail_reply', {**reference, 'subject': 'Re: ' + subject})
        assert not CoreAgent._unheard_authored_text(conversation, call, loaded)


def test_interruption_progress_uses_exact_checkpoints_after_reload(tmp_path):
    from tests.test_coding_failure_episodes import timed_out
    first = timed_out('job1', {})
    second = timed_out('job2', {'resume_job_id': 'job1'})
    attempts = []
    for attempt in (first, second):
        checkpoint = {**json.loads(attempt.result.values['checkpoint']),
                      'state_digest': 'same', 'padding': 'x' * 10000}
        values = {**attempt.result.values, 'checkpoint': json.dumps(checkpoint)}
        attempts.append(replace(attempt, result=replace(attempt.result, values=values, durable_values=values)))
    state = replace(goal({}), attempts=tuple(attempts))
    call = CapabilityCall('resume', 'run_coding_task', {'resume_job_id': 'job2'})
    with reload_goal(tmp_path, state) as store:
        assert CoreAgent._coding_interruption_exhaustion_reason(store.load(state.goal_id).state, call) == 'coding_interruption_no_progress'


def test_legacy_noop_and_failure_classification_survive_reload(tmp_path):
    import hashlib
    from alx.contracts import outcome_of, ExecutionOutcome
    checkpoint = {'stage': 'test', 'branch': 'fix/work', 'head_sha': 'a' * 40,
                  'files': [], 'preexisting_dirty': [], 'state_digest': 'b' * 64,
                  'padding': 'x' * 10000}
    state = goal({'checkpoint': json.dumps(checkpoint), 'status': 'failed',
                  'baseline': {'clean': True, 'head_sha': 'a' * 40, 'branch': 'fix/work',
                               'inherited_dirty': []},
                  'file_count': 0, 'diff_digest': hashlib.sha256(b'').hexdigest(),
                  'all_required_verification_passed': True},
                 {**{f'item_{i}': i for i in range(1000)}, 'code': 'task_failed',
                  'phase': 'test', 'session_completed': True, 'requires_judgement': True},
                 capability_id='run_coding_task')
    with reload_goal(tmp_path, state) as store:
        attempt = store.load(state.goal_id).state.attempts[0]
        assert CoreAgent._completed_unchanged_coding_result(attempt)
        assert not CoreAgent._is_coding_execution_failure(attempt)
        assert outcome_of(attempt.result) is ExecutionOutcome.AMBIGUOUS


@pytest.mark.parametrize('facts', [
    {'code': 'planning_failed', 'phase': 'planning', 'implementation_reached': False},
    {'code': 'task_failed', 'failure_class': 'request_conflict', 'phase': 'test'},
    {'code': 'review_failed', 'review_classification': 'infrastructure'},
])
def test_coding_failure_and_correction_guards_ignore_summary_omissions(tmp_path, facts):
    failure = {**{f'item_{i}': i for i in range(1000)}, **facts}
    state = goal({'status': 'failed'}, failure, capability_id='run_coding_task')
    attempt = state.attempts[0]
    state = replace(state, attempts=(replace(attempt, call=replace(
        attempt.call, arguments={'corrective_action': 'diagnosed'}, durable_arguments=None)),))
    with reload_goal(tmp_path, state) as store:
        loaded = store.load(state.goal_id).state
        for read in (CoreAgent._is_coding_execution_failure, CoreAgent._is_corrective_coding_dispatch):
            assert read(loaded.attempts[0]) == read(state.attempts[0])
        for read in (CoreAgent._coding_execution_failures, CoreAgent._planning_coding_failures,
                     CoreAgent._request_conflict_coding_executions, CoreAgent._failed_coding_executions):
            assert read(loaded) == read(state)


@pytest.mark.parametrize('write', ['replace', 'memory_batch'])
def test_recovery_writes_are_atomic_with_revision_and_memory_batch(tmp_path, write):
    from alx.goals.store import GoalRevisionConflict
    from alx.contracts import MemoryKind, MemoryProposal
    state = goal({'checkpoint': 'original' * 10000})
    with reload_goal(tmp_path, state) as store:
        loaded = store.load(state.goal_id)
        changed = goal({'checkpoint': 'replacement' * 10000})
        def save(state, revision):
            if write == 'replace':
                return store.replace(state, loaded.retention_until, revision)
            return store.replace_with_memory_batch(
                state, loaded.retention_until, revision,
                (MemoryProposal('m1', MemoryKind.FACTUAL, 'fact', ('attempt:call-1',), NOW),))
        with pytest.raises(GoalRevisionConflict):
            save(changed, loaded.revision + 1)
        assert store.load(state.goal_id).state.attempts[0].result.recovery_values()['checkpoint'] == 'original' * 10000
        assert not store.pending_memory_batches(state.goal_id)
        saved = save(changed, loaded.revision)
    with SQLiteGoalStore(tmp_path / 'goals.sqlite3') as store:
        loaded = store.load(state.goal_id)
        assert loaded.revision == saved.revision
        assert loaded.state.attempts[0].result.recovery_values()['checkpoint'] == 'replacement' * 10000
        # Migration must prefer existing exact recovery over bounded summaries.
        store._connection.execute('PRAGMA user_version = 11')
    with SQLiteGoalStore(tmp_path / 'goals.sqlite3') as store:
        assert store.load(state.goal_id).state.attempts[0].result.recovery_values()['checkpoint'] == 'replacement' * 10000
        store.delete(state.goal_id, loaded.revision)
        assert store._connection.execute('SELECT COUNT(*) FROM attempt_recovery').fetchone()[0] == 0


def test_private_recovery_inherits_goal_retention_provenance(tmp_path):
    from scripts.inventory_retention import survey
    from alx.contracts.provenance import RetentionPolicy, ContentOrigin
    path = tmp_path / 'goals.sqlite3'
    provenance = RetentionPolicy().non_mail(ContentOrigin.ALX, NOW)
    with SQLiteGoalStore(path, clock=lambda: NOW) as store:
        state = goal({'checkpoint': 'x' * 10000})
        store.create(state, 'conversation', NOW + timedelta(days=1), provenance)
        records = survey(tmp_path)
        recovery = next(item for item in records if item.record_id.startswith('attempt_recovery:'))
        assert recovery.provenance == store.load(state.goal_id).provenance
        assert store.purge_expired(NOW + timedelta(days=2)) == (state.goal_id,)
        assert store._connection.execute('SELECT COUNT(*) FROM attempt_recovery').fetchone()[0] == 0
