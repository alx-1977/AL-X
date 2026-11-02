"""D-046: reuse review evidence only for conflict-free, identical merged trees."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from alx.bootstrap.repository import require_clean_review
from alx.contracts.repository import MergeError, MergeRequest
from alx.contracts.repository_authority import CanonicalSystem, RepositoryAuthorityError
from alx.contracts.review_content import ReviewContent
from alx.contracts.review_provider import ReviewProvider, profile_for
from alx.providers.github_review import GitHubReviewProvider
from alx.providers.repository_authority import RepositoryAuthority
from alx.providers.github_merge import GitHubMergeProvider
from alx.contracts.repository import MergeOutcome
from alx.bootstrap.repository import build_repository_runtime
from alx.tools.repository import MERGE_PULL_REQUEST


class GateReuseTests(unittest.TestCase):
    """Exercise the actual gate and continuation without a git executable."""

    def test_tree_comparison_refuses_conflicts_and_unusable_command_evidence(self):
        reviewed, current, main, tree = (char * 40 for char in 'abcd')
        for status, output, equal in (
            (0, tree + '\n', True),
            (1, tree + '\n', False),  # A conflicted merge still prints a tree.
            (128, '', False),
            (0, '', False),
            (0, tree + '\nextra evidence\n', False),
            (0, 'e' * 40 + '\n', False),
        ):
            with self.subTest(status=status, output=output):
                authority = RepositoryAuthority(CanonicalSystem(Path.cwd(), 'owner/repo'))
                responses = [subprocess.CompletedProcess([], 0, main + '\n', ''),
                             subprocess.CompletedProcess([], 0, tree + '\n', ''),
                             subprocess.CompletedProcess([], status, output, '')]
                with patch.object(authority, '_run', side_effect=responses):
                    self.assertEqual(authority.own_changes_equal(reviewed, current), equal)

    def test_unavailable_commit_or_command_refuses_tree_comparison(self):
        authority = RepositoryAuthority(CanonicalSystem(Path.cwd(), 'owner/repo'))
        with patch.object(authority, '_run', side_effect=[
            subprocess.CompletedProcess([], 0, 'c' * 40, ''),
            subprocess.CompletedProcess([], 128, '', 'missing current commit'),
        ]):
            self.assertFalse(authority.own_changes_equal('a' * 40, 'b' * 40))
        with patch.object(authority, '_run', side_effect=RepositoryAuthorityError('repository_unavailable')):
            self.assertFalse(authority.own_changes_equal('a' * 40, 'b' * 40))

    def test_gate_requires_both_review_evidence_and_patch_equivalence(self):
        old, current = 'a' * 40, 'b' * 40
        for available, score, number, equal, succeeds in (
            (True, 5, 7, True, True), (True, 5, 7, False, False),
            (False, 5, 7, True, False), (True, 4, 7, True, False),
            (True, 5, 99, True, False),
        ):
            with self.subTest(available=available, score=score, number=number, equal=equal):
                class Reader:
                    def reviewed_heads(self, pr):
                        return (old,)

                    def read(self, request):
                        done = available and request.head_sha == old
                        return ReviewContent(
                            pull_request_number=number, head_sha=request.head_sha,
                            reviewer='greptile', available=done,
                            summary=f'<!-- greptile_confidence_score:{score} -->' if done else '',
                            retrieved_at=datetime.now(UTC),
                            submitted_at=datetime.now(UTC) if done else None,
                            unavailable_reason='' if done else 'no_review_for_revision')

                calls = []

                def compare(left, right):
                    calls.append((left, right))
                    return equal

                if succeeds:
                    require_clean_review(Reader(), MergeRequest(7, current), compare)
                    self.assertEqual(calls, [(old, current)])
                else:
                    with self.assertRaises(MergeError) as caught:
                        require_clean_review(Reader(), MergeRequest(7, current), compare)
                    self.assertEqual(caught.exception.code, 'review_missing')

    def test_rebase_continues_through_the_same_checks_and_exact_head_merge(self):
        old, current = 'a' * 40, 'b' * 40
        pull = {'head': {'ref': 'feature'}}
        for final in ('ready', 'checks_failed', 'merge_conflict', 'head_changed', 'pending'):
            with self.subTest(final=final):
                checks = []
                provider = GitHubMergeProvider(
                    'owner/repo', 'token', max_polls=3, sleep=lambda _: None,
                    bring_current=lambda sha, branch: current,
                    check_review=lambda request: checks.append(request.head_sha))
                states = iter(['behind', final, final])

                def readiness(request):
                    state = next(states)
                    if state in ('checks_failed', 'merge_conflict', 'head_changed'):
                        raise MergeError(state)
                    return state, pull

                with patch.object(provider, '_readiness', side_effect=readiness), patch.object(
                    provider, '_merge', return_value=MergeOutcome(7, current, True, 'c' * 40)
                ) as merge:
                    if final == 'ready':
                        outcome = provider.merge(MergeRequest(7, old))
                        self.assertEqual(outcome.head_sha, current)
                        self.assertEqual(merge.call_args.args[0].head_sha, current)
                    else:
                        with self.assertRaises(MergeError) as caught:
                            provider.merge(MergeRequest(7, old))
                        self.assertEqual(caught.exception.code,
                                         'checks_timed_out' if final == 'pending' else final)
                        merge.assert_not_called()
                self.assertEqual(checks, [current])

    def test_changed_patch_after_rebase_stops_for_review(self):
        def refuse(request):
            raise MergeError('review_missing')

        provider = GitHubMergeProvider('owner/repo', 'token',
                                      bring_current=lambda *_: 'b' * 40, check_review=refuse)
        with patch.object(provider, '_readiness', return_value=('behind', {'head': {'ref': 'feature'}})):
            with self.assertRaises(MergeError) as caught:
                provider.merge(MergeRequest(7, 'a' * 40))
        self.assertEqual(caught.exception.code, 'review_required')

    def test_existing_capability_is_the_only_registered_merge_path(self):
        runtime = build_repository_runtime(True, 'owner/repo', 'token', lambda: 'id',
                                           provider=object())
        self.assertEqual(tuple(runtime.executors), (MERGE_PULL_REQUEST,))
        self.assertEqual(len(runtime.definitions), 1)
        from alx.tools.repository import DEFINITION
        self.assertIs(runtime.definitions[0], DEFINITION)


class ReviewEquivalenceTests(unittest.TestCase):
    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.root, check=True,
                              capture_output=True, text=True).stdout.strip()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.git('init', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'code').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-m', 'base')
        self.base = self.git('rev-parse', 'HEAD')
        self.git('switch', '-c', 'feature')
        (self.root / 'code').write_text('reviewed\n')
        self.git('commit', '-am', 'own change')
        self.reviewed = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        (self.root / 'main-only').write_text('upstream\n')
        self.git('add', '.')
        self.git('commit', '-m', 'main advanced')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', 'feature')
        self.git('rebase', 'main')
        self.current = self.git('rev-parse', 'HEAD')
        self.authority = RepositoryAuthority(CanonicalSystem(self.root, 'owner/repo'))

    def reader(self, *, available=True, score=5, number=7, reviewer='greptile'):
        reviewed = self.reviewed

        class Reader:
            def reviewed_heads(inner, pr):
                self.assertEqual(pr, 7)
                return (reviewed,)

            def read(inner, request):
                old = request.head_sha == reviewed
                return ReviewContent(
                    pull_request_number=number, head_sha=request.head_sha,
                    reviewer=reviewer, available=old and available,
                    summary=f'<!-- greptile_confidence_score:{score} -->' if old and available else '',
                    retrieved_at=datetime.now(UTC),
                    submitted_at=datetime.now(UTC) if old and available else None,
                    unavailable_reason='' if old and available else 'no_review_for_revision',
                )
        return Reader()

    def gate(self, reader):
        require_clean_review(reader, MergeRequest(7, self.current),
                             self.authority.own_changes_equal)

    def test_rebased_patch_reuses_review(self):
        self.assertNotEqual(self.current, self.reviewed)
        self.assertTrue(self.authority.own_changes_equal(self.reviewed, self.current))
        self.gate(self.reader())

    def scenario(self, base, reviewed, upstream):
        """Build a same-file review and a clean rebase onto newer main."""
        self.git('switch', 'main')
        (self.root / 'scenario.py').write_text(base)
        self.git('add', '.')
        self.git('commit', '-m', 'scenario base')
        self.git('switch', '-c', 'scenario')
        (self.root / 'scenario.py').write_text(reviewed)
        self.git('commit', '-am', 'reviewed scenario')
        self.reviewed = self.git('rev-parse', 'HEAD')
        self.git('switch', 'main')
        (self.root / 'scenario.py').write_text(upstream)
        self.git('commit', '-am', 'nearby upstream change')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', 'scenario')
        self.git('rebase', 'main')
        self.current = self.git('rev-parse', 'HEAD')

    def test_python_call_moved_outside_permission_check_refuses_review_reuse(self):
        base = 'VERSION = 1\n\ndef perform(allowed):\n    if allowed:\n        audit()\n'
        reviewed = base + '        protected_call()\n'
        self.scenario(base, reviewed, base.replace('VERSION = 1', 'VERSION = 2'))
        (self.root / 'scenario.py').write_text(
            reviewed.replace('VERSION = 1', 'VERSION = 2').replace(
                '        protected_call()', '    protected_call()'))
        self.git('commit', '-am', 'move call outside permission check')
        self.current = self.git('rev-parse', 'HEAD')
        with self.assertRaises(MergeError) as caught:
            self.gate(self.reader())
        self.assertEqual(caught.exception.code, 'review_missing')

    def test_clean_rebase_with_nearby_main_edit_reuses_review(self):
        base = 'UPSTREAM = 1\n\nOWN = 1\n'
        self.scenario(base, base.replace('OWN = 1', 'OWN = 2'),
                      base.replace('UPSTREAM = 1', 'UPSTREAM = 2'))
        self.assertNotEqual(self.current, self.reviewed)
        self.gate(self.reader())

    def test_changed_own_code_still_requires_review(self):
        (self.root / 'code').write_text('different\n')
        self.git('commit', '-am', 'changed own code')
        self.current = self.git('rev-parse', 'HEAD')
        self.assertFalse(self.authority.own_changes_equal(self.reviewed, self.current))
        with self.assertRaises(MergeError) as caught:
            self.gate(self.reader())
        self.assertEqual(caught.exception.code, 'review_missing')

    def test_extra_own_file_requires_review(self):
        (self.root / 'extra').write_text('unreviewed\n')
        self.git('add', '.')
        self.git('commit', '-m', 'extra own edit')
        self.current = self.git('rev-parse', 'HEAD')
        with self.assertRaises(MergeError):
            self.gate(self.reader())

    def test_conflicting_main_change_requires_review_even_after_resolution(self):
        self.git('switch', 'main')
        (self.root / 'code').write_text('conflicting upstream\n')
        self.git('commit', '-am', 'conflicting main change')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.git('switch', 'feature')
        # Record an explicit resolution; the reviewed head still conflicts
        # with main, so its review cannot cover this choice.
        self.git('merge', '-s', 'ours', 'main', '-m', 'resolve conflict')
        self.current = self.git('rev-parse', 'HEAD')
        with self.assertRaises(MergeError):
            self.gate(self.reader())

    def test_unfinished_low_score_foreign_pr_or_unknown_reviewer_refuse(self):
        for options in ({'available': False}, {'score': 4}, {'number': 99},
                        {'reviewer': 'unknown'}):
            with self.subTest(options=options), self.assertRaises(MergeError) as caught:
                self.gate(self.reader(**options))
            self.assertEqual(caught.exception.code, 'review_missing')

    def test_unavailable_history_fails_closed(self):
        self.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertFalse(self.authority.own_changes_equal(self.reviewed, self.current))
        with self.assertRaises(MergeError):
            self.gate(self.reader())
        with patch.object(self.authority, 'own_changes_equal', side_effect=RepositoryAuthorityError('ref_unknown')):
            with self.assertRaises(MergeError):
                self.gate(self.reader())

    def test_binary_changes_do_not_collapse_to_a_placeholder(self):
        (self.root / 'code').write_bytes(b'\x00different binary\xff')
        self.git('commit', '-am', 'binary change')
        current = self.git('rev-parse', 'HEAD')
        self.assertFalse(self.authority.own_changes_equal(self.reviewed, current))

    def test_candidates_are_only_from_reviewer_evidence_on_this_pr(self):
        provider = GitHubReviewProvider('owner/repo', 'token', profile_for(ReviewProvider.GREPTILE))
        calls = []

        def pages(path):
            calls.append(path)
            return [
                {'user': {'login': 'someone'}, 'commit_id': 'f' * 40},
                {'user': {'login': 'greptile-apps[bot]'}, 'commit_id': self.reviewed},
            ]

        with patch.object(provider, '_pages', side_effect=pages):
            self.assertEqual(provider.reviewed_heads(7), (self.reviewed,))
        self.assertEqual(calls, ['/repos/owner/repo/pulls/7/reviews',
                                 '/repos/owner/repo/issues/7/comments'])
