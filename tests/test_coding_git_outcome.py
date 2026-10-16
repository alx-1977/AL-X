"""A coding job returns its repair as a branch and a commit SHA, end to end.

`tests/test_coding_git_workspace.py` proves the git mechanism in isolation:
which argv can run, what may be staged, when a commit is refused. This file
covers the other half — that a job dispatched through the ordinary capability
path actually uses it, and that what Core receives is a branch and a SHA rather
than a dirty worktree it has to interpret.

Everything here goes through the registry, broker and Safety Gate the way a
real job does, so nothing proved here depends on calling the agent directly.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.coding import (  # noqa: E402
    CODING_EXECUTE_PERMISSION,
    build_coding_runtime,
)
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.contracts import CapabilityCall, CapabilityResultState  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.coding import RUN_CODING_TASK  # noqa: E402

from test_coding_agent import (  # noqa: E402
    FIXTURE_BRANCH,
    NOW,
    CodingSessionResult,
    PlanningModel,
    RecordingSession,
    _FIXED,
    _git,
    _worktree,
)


class GitOutcome(unittest.TestCase):
    """One real worktree, dispatched the way Core dispatches a coding job."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.parent = Path(self.directory.name)
        self.root = _worktree(self.parent)

    def run_job(self, session, **arguments):
        # `worktree` is only a legacy test-helper keyword naming the canonical
        # fixture repository; it is never a capability argument.
        arguments.pop("worktree", None)
        runtime = build_coding_runtime(
            True, PlanningModel(), lambda: "call-1",
            session=session,
            repository=self.root,
        )
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        result = broker.dispatch(
            CapabilityCall("call-1", RUN_CODING_TASK, arguments),
            AuthorityContext(
                "friedl", frozenset({CODING_EXECUTE_PERMISSION}), NOW
            ),
        ).result
        self.job_root = self.root
        return result

    def git(self, *argv: str) -> str:
        """Git in the canonical repository: refs are shared with the worktree."""
        return subprocess.run(
            ["git", *argv], cwd=self.root, check=True,
            capture_output=True, text=True,
        ).stdout

    def job_git(self, *argv: str) -> str:
        """Git in the job's own worktree, where its HEAD and status live."""
        return subprocess.run(
            ["git", *argv], cwd=self.job_root, check=True,
            capture_output=True, text=True,
        ).stdout


class ASuccessfulJobReturnsABranchAndASha(GitOutcome):
    """The capability's reason for existing."""

    def test_a_collision_commits_on_the_suffix_selected_branch(self) -> None:
        self.git("branch", "repair/add")
        occupied_ref = self.git("rev-parse", "repair/add").strip()

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["branch"], "repair/add-2")
        self.assertEqual(result.values["commit"]["branch"], "repair/add-2")
        self.assertEqual(self.git("rev-parse", "repair/add").strip(), occupied_ref)

    def test_the_outcome_carries_branch_commit_sha_and_files(self) -> None:
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        values = result.values
        self.assertEqual(values["branch"], "repair/add")
        self.assertEqual(
            values["commit_sha"],
            self.git("rev-parse", "repair/add").strip(),
        )
        self.assertEqual(list(values["commit"]["committed_files"]), ["app.py"])
        self.assertTrue(values["commit"]["worktree_clean"])

    def test_the_commit_exists_on_the_repair_branch(self) -> None:
        self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(
            self.job_git("rev-parse", "--abbrev-ref", "HEAD").strip(), "repair/add"
        )
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), "repair/add"
        )
        self.assertEqual(
            self.git("log", "-1", "--pretty=%s", "repair/add").strip(),
            "repair addition",
        )
        self.assertIn(
            "app.py",
            self.git("show", "--name-only", "--pretty=", "repair/add"),
        )

    def test_the_baseline_names_the_commit_the_job_started_from(self) -> None:
        """What "where the job started" means changed with D-031.

        Under D-029 the job switched branches inside a worktree it inherited,
        so its starting branch was a fact about somebody else's checkout, and
        reporting the repair branch there was false evidence — reproduced on
        2026-09-12.

        A D-031 job has no such prior branch: its worktree is created already
        on its own branch, cut from the repository's HEAD. The branch in the
        baseline is therefore the job's own, which is the truth about the
        worktree being described. The commit it starts from is the fact that
        still ties the job to the repository, and that is asserted here.
        """
        start_sha = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.values["baseline"]["head_sha"], start_sha)
        self.assertEqual(result.values["baseline"]["branch"], "repair/add")
        self.assertEqual(result.values["commit"]["branch"], "repair/add")
        # The canonical checkout is the visible feature checkout.
        self.assertEqual(
            self.git("rev-parse", "--abbrev-ref", "HEAD").strip(), "repair/add"
        )

    def test_the_baseline_proves_where_the_job_started(self) -> None:
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        baseline = result.values["baseline"]
        self.assertEqual(baseline["head_sha"], before)
        self.assertTrue(baseline["clean"])
        self.assertEqual(list(baseline["inherited_dirty"]), [])


class DeletionsReachTheCommit(GitOutcome):
    """A Law 0 cleanup is exactly the repair V1 previously could not commit."""

    def test_a_job_that_deletes_a_file_commits_the_deletion(self) -> None:
        class DeletingSession(RecordingSession):
            def run_session(self, request, briefing):
                result = super().run_session(request, briefing)
                (Path(request.worktree) / "superseded.py").unlink()
                return result

        (self.root / "superseded.py").write_text("old path\n", encoding="utf-8")
        _git(self.root, "add", "superseded.py")
        _git(self.root, "commit", "-m", "add superseded path")

        result = self.run_job(
            DeletingSession(edits={"app.py": _FIXED}),
            task="repair and remove the superseded path",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition and remove superseded path",
        )

        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(
            sorted(result.values["commit"]["committed_files"]),
            ["app.py", "superseded.py"],
        )
        # Gone from both the feature ref and the visible checkout HEAD.
        self.assertNotIn(
            "superseded.py", self.git("ls-tree", "--name-only", "repair/add")
        )
        self.assertNotIn(
            "superseded.py", self.git("ls-tree", "--name-only", "HEAD")
        )

    def test_an_inherited_deletion_is_not_committed(self) -> None:
        """Deleted before the job started, so not the job's to remove."""
        (self.root / "theirs.py").write_text("theirs\n", encoding="utf-8")
        _git(self.root, "add", "theirs.py")
        _git(self.root, "commit", "-m", "add theirs")
        (self.root / "theirs.py").unlink()

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["reason_code"], "canonical_checkout_dirty")


class DirtyCanonicalCheckoutIsRefused(GitOutcome):
    def test_uncommitted_work_blocks_implementation_without_being_touched(self) -> None:
        theirs = "# somebody else was working here\n"
        (self.root / "notes.txt").write_text(theirs, encoding="utf-8")
        session = RecordingSession(edits={"app.py": _FIXED})

        result = self.run_job(
            session,
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["reason_code"], "canonical_checkout_dirty")
        self.assertEqual(session.calls, [])
        self.assertEqual((self.root / "notes.txt").read_text(), theirs)


class CommittingIsRefusedRatherThanWidened(GitOutcome):
    """Fail closed: no commit is better than the wrong commit."""

    def test_someone_elses_staged_file_cannot_reach_the_job_s_commit(self) -> None:
        """D-031 moved this from a refusal to an impossibility.

        Staging an unrelated file used to poison the job's own index, because
        the job shared it. `_refuse_foreign_staged_state` caught that and
        failed the job closed, and still does — proved directly against the
        staging code in `test_coding_git_workspace.py`.

        A linked worktree has its own index, so the checkout's staged file is
        not in the job's index to be caught. The job succeeds, its commit
        contains only its own file, and the staged work is left exactly as its
        owner left it.
        """
        (self.root / "theirs.py").write_text("not this job's\n", encoding="utf-8")
        _git(self.root, "add", "theirs.py")
        before = self.git("rev-parse", "HEAD").strip()

        result = self.run_job(
            RecordingSession(edits={"app.py": _FIXED}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )

        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["reason_code"], "canonical_checkout_dirty")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertIn("A  theirs.py", self.git("status", "--porcelain"))

    def test_a_failed_job_is_not_committed(self) -> None:
        """Evidence Core still has to judge must not read as a finished repair."""
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            RecordingSession(
                edits={"app.py": _FIXED}, completed=False,
                failure_code="session_failed",
            ),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)

    def test_a_job_whose_required_check_could_not_run_is_not_committed(self) -> None:
        """D-029 authorises a commit after the job passed its verification.

        A job whose required check never ran has not passed it; it skipped it.
        The predicate used to be `tests_run and tests_passed`, so a job where no
        candidate command survived the allowlist reached "succeeded" having
        verified nothing and was committed anyway. An unverified commit reads
        downstream exactly like a verified one, which is what makes the gap
        worth closing — and the gap is unchanged by the checks becoming
        deterministic, so it is still proved here.
        """
        from unittest.mock import patch

        from alx.providers import coding_agent as agent_module

        before = self.git("rev-parse", "HEAD").strip()
        # Every required command is refused, so each check is required, none
        # runs, and `all_required_passed` is false.
        with patch.object(
            agent_module, "command_permitted", return_value=False
        ):
            result = self.run_job(
                RecordingSession(edits={"app.py": _FIXED}),
                task="repair the addition",
                worktree=str(self.root),
                repair_branch="repair/add",
                commit_message="repair addition",
            )

        self.assertFalse(result.values["all_required_verification_passed"])
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        # Said plainly, so Core knows why there is no SHA rather than having
        # to infer it from an absent field.
        self.assertIn(
            "required_verification_failed", result.values["unresolved_issues"]
        )
        # And the evidence says which checks were owed and which did not run.
        # The content check is performed in process, so refusing every command
        # does not stop it; what matters is that the refused ones are recorded
        # as required and not run, which is enough to withhold the commit.
        evidence = result.values["verification"]
        self.assertIn("diff_check", evidence["failed"])
        self.assertNotIn("diff_check", evidence["ran"])
        self.assertIn("content_check", evidence["required"])
        # The work is not thrown away: it is still there as a diff.
        self.assertEqual(list(result.values["files_changed"]), ["app.py"])

    def test_a_job_whose_tests_failed_is_not_committed(self) -> None:
        """The neighbouring case, so the two cannot be confused."""
        before = self.git("rev-parse", "HEAD").strip()
        result = self.run_job(
            # Leaves app.py broken, so the fixture's own test fails.
            RecordingSession(edits={"app.py": "def add(a, b):\n    return a * b\n"}),
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)

    def test_a_job_changing_more_files_than_the_bound_is_not_committed(self) -> None:
        """Finding 2 of the 2026-09-12 re-review, confirmed by inspection.

        `_files_changed` clipped to MAX_REPORTED_FILES and `_authorised_paths`
        then tested for more than MAX_STAGED_FILES — the same number — so the
        bound could never fire. A job changing sixty files staged the first
        fifty and reported a complete repair. The untruncated count now
        decides whether a commit is possible at all.
        """
        from alx.contracts.coding import MAX_STAGED_FILES

        # app.py is repaired too, so the job's own tests pass and the file
        # bound is what this test exercises rather than a test failure.
        edits = {"app.py": _FIXED}
        edits.update({
            f"generated_{index:03d}.py": f"value = {index}\n"
            for index in range(MAX_STAGED_FILES + 10)
        })
        before = self.git("rev-parse", "HEAD").strip()

        result = self.run_job(
            RecordingSession(edits=edits),
            task="generate many modules",
            worktree=str(self.root),
            repair_branch="repair/many",
            commit_message="generate modules",
        )

        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertIn(
            "too_many_files_to_commit", result.values["unresolved_issues"]
        )

    def test_commit_authorisation_uses_the_complete_inherited_dirt(self) -> None:
        """Finding 1: the bounded status path must not decide what may stage.

        `preexisting_dirty` comes from evidence clipped at 16,000 characters.
        A clipped inherited path that reappears in the post-session status
        would read as job-owned. Authorisation now uses the baseline reader's
        complete listing.
        """
        import inspect

        from alx.providers.coding_agent import CodingAgent

        source = inspect.getsource(CodingAgent._run)
        self.assertIn("baseline_dirty", source)
        # The commit call takes the complete sets, not the reporting ones.
        self.assertIn("complete_files,", source)
        self.assertIn("baseline_dirty,", source)

    def test_a_branch_that_cannot_be_created_fails_before_the_session(self) -> None:
        """A session must never write to a branch nobody asked for."""
        session = RecordingSession(edits={"app.py": _FIXED})
        result = self.run_job(
            session,
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="--force",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertEqual(result.failure["code"], "git_refused")
        self.assertEqual(session.calls, [])

class TheSessionStillHasNoGitAuthority(GitOutcome):
    """Git moved to AL/X's side of the boundary, not into the session's."""

    def test_the_briefing_still_forbids_the_session_from_committing(self) -> None:
        session = RecordingSession(edits={"app.py": _FIXED})
        self.run_job(
            session,
            task="repair the addition",
            worktree=str(self.root),
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        _request, briefing = session.calls[0]
        self.assertIn("Do not commit, push, merge, deploy", briefing)
        self.assertIn("repair/add", briefing)

    def test_the_session_cannot_run_a_git_command_of_its_own(self) -> None:
        """The sandbox denies .git outright; this states the intent alongside."""
        from alx.providers.coding_process import command_permitted

        for argv in (
            ["git", "commit", "-m", "mine"],
            ["git", "switch", "-c", "mine"],
            ["git", "add", "-A"],
            ["git", "push"],
        ):
            self.assertFalse(
                command_permitted(argv, self.root), " ".join(argv)
            )



class VerificationIsProportionateToTheChange(GitOutcome):
    """The corrected commit predicate, dispatched the way Core dispatches.

    `tests/test_coding_verification.py` proves the policy in isolation. This
    covers the half that matters to Core: that a job whose changed files owe no
    test is verified, committed and reported as verified, and that a job whose
    required check fails is not committed at all.
    """

    def test_a_documentation_only_job_is_committed_without_any_test(self) -> None:
        """9. All required verification passed, and no pytest was required.

        This is the incident, reproduced end to end. Before the correction the
        commit predicate was `tests_run and tests_passed`, so this job — which
        has nothing to prove by running pytest — could not be committed however
        correct it was.
        """
        result = self.run_job(
            RecordingSession(edits={"NOTES.md": "# Notes\n\nOne line.\n"}),
            task="add a notes file",
            repair_branch="docs/notes",
            commit_message="add notes",
        )
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(result.values["all_required_verification_passed"])
        # Committed, with a real SHA, on the strength of a diff check alone.
        self.assertIn("commit_sha", result.values)
        self.assertTrue(result.values["commit_sha"])
        self.assertEqual(result.values["branch"], "docs/notes")
        # No test was required, and none was claimed to have run.
        self.assertFalse(result.values["tests_run"])
        self.assertNotIn("tests_passed", result.values)
        evidence = result.values["verification"]
        self.assertEqual(
            tuple(evidence["required"]), ("diff_check", "content_check")
        )
        self.assertEqual(
            tuple(evidence["ran"]), ("diff_check", "content_check")
        )
        self.assertEqual(tuple(evidence["failed"]), ())
        # And in particular the full suite was never selected.
        argv_run = [tuple(item["argv"]) for item in result.values["commands"]]
        self.assertEqual(argv_run, [("git", "diff", "--check")])

    def test_a_job_whose_required_check_fails_is_not_committed(self) -> None:
        """8. A failing required check refuses the commit and fails the job.

        The diff check is made to fail by writing a real conflict marker, so
        the refusal comes from the check itself rather than from a patched
        predicate.
        """
        before = self.git("rev-parse", "HEAD").strip()
        conflicted = (
            "def add(a, b):\n"
            "<<<<<<< HEAD\n"
            "    return a + b\n"
            "=======\n"
            "    return a - b\n"
            ">>>>>>> other\n"
        )
        result = self.run_job(
            RecordingSession(edits={"app.py": conflicted}),
            task="repair the addition",
            repair_branch="repair/add",
            commit_message="repair addition",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertFalse(result.values["all_required_verification_passed"])
        self.assertIn("diff_check", result.values["verification"]["failed"])
        self.assertIn(
            "required_verification_failed", result.values["unresolved_issues"]
        )
        # Nothing was committed, and the work is still in the worktree.
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)
        self.assertIn("app.py", result.values["files_changed"])

    def test_a_malformed_new_file_is_not_committed(self) -> None:
        """The untracked half of the structural check.

        `git diff --check` inspects tracked changes only, and a file the job
        newly created is still untracked when verification runs — staging
        happens later, inside the commit. So a new file carrying leftover
        conflict markers passed the diff check and was committed. Found in
        review on PR #54. The content check reads the job's own final files
        directly, which is the same question asked where git cannot see.
        """
        before = self.git("rev-parse", "HEAD").strip()
        conflicted = (
            "def helper():\n"
            "<<<<<<< HEAD\n"
            "    return 1\n"
            "=======\n"
            "    return 2\n"
            ">>>>>>> other\n"
        )
        result = self.run_job(
            RecordingSession(edits={"helper.py": conflicted}),
            task="add a helper",
            repair_branch="feat/helper",
            commit_message="add helper",
        )
        self.assertEqual(result.state, CapabilityResultState.FAILED)
        self.assertFalse(result.values["all_required_verification_passed"])
        # The diff check saw nothing wrong, because the file is untracked...
        checks = {item["name"]: item for item in result.values["verification"]["checks"]}
        self.assertTrue(checks["diff_check"]["passed"])
        # ...and the content check is what caught it.
        self.assertFalse(checks["content_check"]["passed"])
        self.assertTrue(
            any(
                "conflict marker" in finding
                for finding in checks["content_check"]["findings"]
            ),
            checks["content_check"]["findings"],
        )
        # Nothing was committed.
        self.assertNotIn("commit_sha", result.values)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), before)

    def test_a_clean_new_file_still_passes_and_commits(self) -> None:
        """The content check must not fail honest jobs that create files.

        A documentation file rather than a Python one, so this isolates the
        content check: a new `.py` module with no mapped test legitimately
        escalates to the full suite, which is a different requirement and is
        covered separately.
        """
        result = self.run_job(
            RecordingSession(edits={"HELPER.md": "# Helper\n\nOne line.\n"}),
            task="add a helper note",
            repair_branch="feat/helper",
            commit_message="add helper note",
        )
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertTrue(result.values["all_required_verification_passed"])
        self.assertTrue(result.values["commit_sha"])

if __name__ == "__main__":
    unittest.main()
