"""D-026: proving an accepted independent reviewer saw this exact diff.

The requirement is a property, not a product. Someone other than the agent
that wrote the change must examine the whole proposed diff and report what
they found. These tests drive the real verifier over the shapes GitHub
actually returns, because every way this can be wrong is a way an unreviewed
change reaches `main`.

They are deliberately adversarial about the ways a review can look valid
without being one: covering an earlier head, coming from the implementer,
coming from a reviewer nobody accepted, or carrying a verdict with no report.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from check_independent_review import (  # noqa: E402
    ReviewEvidenceError,
    exception_covers,
    is_substantive,
    load_accepted_reviewers,
    verify,
)


HEAD = "a" * 40
OLDER = "b" * 40
REVIEWER_ID = 165735046
IMPLEMENTER_ID = 299481958
ACCEPTED = {REVIEWER_ID: "greptile-apps[bot]"}

# Long enough to be a report rather than a verdict, and phrased as one.
CLEAN_REPORT = (
    "Reviewed the complete diff across 4 source files and 2 test files. "
    "Checked the single-path property for the new capability, the durable "
    "goal transitions, the spend ceiling arithmetic and the mutation "
    "coverage behind the new gate. No correctness, regression, architecture "
    "or economic-boundary defects found. Test enforcement was inspected and "
    "the new suite is collected by the CI runner."
)


def review(
    *,
    user_id: int = REVIEWER_ID,
    sha: str = HEAD,
    body: str = CLEAN_REPORT,
    state: str = "COMMENTED",
) -> dict:
    return {
        "user": {"id": user_id, "login": f"reviewer-{user_id}"},
        "commit_id": sha,
        "body": body,
        "state": state,
    }


def finding(*, user_id: int = REVIEWER_ID, sha: str = HEAD) -> dict:
    return {
        "user": {"id": user_id, "login": f"reviewer-{user_id}"},
        "commit_id": sha,
        "path": "src/alx/core/loop.py",
        "body": "This branch can leave the goal without recorded evidence.",
    }


class AcceptedReviewTests(unittest.TestCase):
    """What a valid independent review looks like."""

    def test_a_substantive_review_of_this_head_is_accepted(self) -> None:
        evidence = verify(
            [review()], [], {IMPLEMENTER_ID}, HEAD, ACCEPTED
        )
        self.assertIn("reviewed", evidence)

    def test_a_clean_review_needs_no_invented_defect(self) -> None:
        """Finding nothing is a legitimate outcome of a real review.

        Requiring a finding would reward manufacturing one, which is worse
        than the absence it was meant to prevent.
        """
        evidence = verify([review()], [], {IMPLEMENTER_ID}, HEAD, ACCEPTED)
        self.assertIn("0 anchored finding", evidence)

    def test_anchored_findings_alone_are_substantive(self) -> None:
        """A reviewer who commented on files has reported, whatever the body."""
        evidence = verify(
            [review(body="")],
            [finding(), finding()],
            {IMPLEMENTER_ID},
            HEAD,
            ACCEPTED,
        )
        self.assertIn("2 anchored finding", evidence)


class ReviewStateTests(unittest.TestCase):
    """A review that was withdrawn is not evidence that one stands."""

    def test_a_dismissed_review_cannot_satisfy_verification(self) -> None:
        """Dismissal is someone with write access retracting the review.

        It keeps its commit id and its body, so every other check here would
        pass it. Only the state says it no longer stands.
        """
        with self.assertRaises(ReviewEvidenceError) as caught:
            verify(
                [review(state="DISMISSED")],
                [],
                {IMPLEMENTER_ID},
                HEAD,
                ACCEPTED,
            )
        self.assertIn("dismissed", str(caught.exception))

    def test_a_dismissed_review_with_anchored_findings_still_fails(self) -> None:
        """Findings do not survive the review being withdrawn."""
        with self.assertRaises(ReviewEvidenceError):
            verify(
                [review(state="DISMISSED", body="")],
                [finding(), finding()],
                {IMPLEMENTER_ID},
                HEAD,
                ACCEPTED,
            )

    def test_dismissal_is_matched_regardless_of_case(self) -> None:
        for state in ("dismissed", "Dismissed", "DISMISSED"):
            with self.subTest(state=state):
                with self.assertRaises(ReviewEvidenceError):
                    verify(
                        [review(state=state)], [], {IMPLEMENTER_ID}, HEAD, ACCEPTED
                    )

    def test_a_standing_review_beside_a_dismissed_one_still_counts(self) -> None:
        """Dismissing one review does not retract another that still stands."""
        evidence = verify(
            [review(state="DISMISSED"), review(state="COMMENTED")],
            [],
            {IMPLEMENTER_ID},
            HEAD,
            ACCEPTED,
        )
        self.assertIn("reviewed", evidence)

    def test_changes_requested_is_still_a_review(self) -> None:
        """A reviewer who found problems reviewed; that is the evidence.

        Rejecting this state would mean a review only counts when it finds
        nothing. The corrective commits that follow move the head, and the
        staleness check already requires a fresh review of it.
        """
        evidence = verify(
            [review(state="CHANGES_REQUESTED")],
            [],
            {IMPLEMENTER_ID},
            HEAD,
            ACCEPTED,
        )
        self.assertIn("reviewed", evidence)

    def test_the_states_real_reviews_carry_are_accepted(self) -> None:
        """Observed on this repository: Greptile submits COMMENTED."""
        for state in ("COMMENTED", "APPROVED", "CHANGES_REQUESTED"):
            with self.subTest(state=state):
                self.assertIn(
                    "reviewed",
                    verify(
                        [review(state=state)], [], {IMPLEMENTER_ID}, HEAD, ACCEPTED
                    ),
                )


class RejectedReviewTests(unittest.TestCase):
    """Every way a review can look valid without being one."""

    def _refused(self, reviews, comments=(), authors=frozenset({IMPLEMENTER_ID})):
        with self.assertRaises(ReviewEvidenceError) as caught:
            verify(list(reviews), list(comments), set(authors), HEAD, ACCEPTED)
        return str(caught.exception)

    def test_a_review_of_an_earlier_head_does_not_count(self) -> None:
        """The exact drift seen on PR #14: reviewed 16bf2d9, merged 86c7e2a."""
        self.assertIn("reviewed", self._refused([review(sha=OLDER)]))

    def test_an_unaccepted_reviewer_does_not_count(self) -> None:
        self.assertIn("id 424242", self._refused([review(user_id=424242)]))

    def test_the_implementing_agent_cannot_review_itself(self) -> None:
        """Even on the allowlist, an author is not independent of its own work."""
        reason = self._refused(
            [review(user_id=REVIEWER_ID)], authors={REVIEWER_ID}
        )
        self.assertIn("authored commits", reason)

    def test_a_bare_verdict_does_not_count(self) -> None:
        for verdict in ("LGTM", "Approved.", "looks good to me", "+1", "No issues found"):
            with self.subTest(verdict=verdict):
                self.assertIn(
                    "without a report", self._refused([review(body=verdict)])
                )

    def test_an_empty_review_does_not_count(self) -> None:
        self.assertIn("without a report", self._refused([review(body="")]))

    def test_no_review_at_all_does_not_count(self) -> None:
        self.assertIn("no review", self._refused([]))

    def test_a_finding_on_an_earlier_head_does_not_make_a_bare_verdict_valid(
        self,
    ) -> None:
        """Anchoring must be to this diff, not to a previous one."""
        self.assertIn(
            "without a report",
            self._refused([review(body="LGTM")], [finding(sha=OLDER)]),
        )


class ExceptionEscapeTests(unittest.TestCase):
    """An approved exception carries a merge no reviewer could reach.

    This is the mechanism EX-002 and EX-003 already used. It is reused rather
    than reinvented, and it must not become a way past the gate for anything
    Friedl has not approved for that exact head.
    """

    APPROVED = (
        "## EX-004 — an approved example\n\n"
        f"- **Scope:** head `{HEAD}`.\n"
        "- **Approval date:** 2026-09-05.\n"
    )

    def test_an_approved_exception_naming_this_head_is_honoured(self) -> None:
        self.assertTrue(exception_covers(self.APPROVED, HEAD))

    def test_an_exception_for_another_head_is_not_honoured(self) -> None:
        self.assertFalse(exception_covers(self.APPROVED, OLDER))

    def test_a_pending_exception_is_not_honoured(self) -> None:
        """A draft is not an approval, however complete it looks."""
        draft = self.APPROVED.replace("2026-09-05", "pending")
        self.assertFalse(exception_covers(draft, HEAD))

    def test_an_exception_without_an_approval_date_is_not_honoured(self) -> None:
        malformed = f"## EX-005\n\n- **Scope:** head `{HEAD}`.\n"
        self.assertFalse(exception_covers(malformed, HEAD))

    def test_an_abbreviated_sha_is_not_enough(self) -> None:
        """A prefix could match a commit nobody approved."""
        self.assertFalse(exception_covers(self.APPROVED, HEAD[:12]))

    def test_a_later_exception_does_not_cover_an_earlier_heads_sha(self) -> None:
        """The approval date must sit in the section naming the head."""
        split = (
            f"## EX-006 — no date here\n\n- **Scope:** head `{HEAD}`.\n\n"
            "## EX-007 — dated, different head\n\n"
            "- **Approval date:** 2026-09-05.\n"
        )
        self.assertFalse(exception_covers(split, HEAD))


class AcceptedReviewerConfigurationTests(unittest.TestCase):
    """The allowlist is what makes a reviewer acceptable, so it is governed."""

    def test_the_committed_allowlist_loads(self) -> None:
        accepted = load_accepted_reviewers(REPOSITORY_ROOT)
        self.assertIn(REVIEWER_ID, accepted)

    def test_identity_is_the_immutable_id_not_the_login(self) -> None:
        """A login can be renamed or re-registered; a numeric id cannot.

        The verifier must never match on the human-readable name, or a
        recreated account could inherit an accepted reviewer's standing.
        """
        renamed = {REVIEWER_ID: "something-else[bot]"}
        self.assertIn(
            "reviewed", verify([review()], [], {IMPLEMENTER_ID}, HEAD, renamed)
        )
        with self.assertRaises(ReviewEvidenceError):
            verify([review(user_id=1)], [], {IMPLEMENTER_ID}, HEAD, renamed)

    def test_a_malformed_allowlist_is_refused(self) -> None:
        for document in (
            {"reviewers": []},
            {"reviewers": [{"login": "x"}]},
            {"reviewers": [{"id": "165735046", "login": "x"}]},
            {"reviewers": [{"id": True, "login": "x"}]},
            {"reviewers": [{"id": 1}]},
        ):
            with self.subTest(document=document):
                with tempfile.TemporaryDirectory() as name:
                    root = Path(name)
                    (root / "review").mkdir()
                    (root / "review/accepted_reviewers.json").write_text(
                        json.dumps(document), encoding="utf-8"
                    )
                    with self.assertRaises(ReviewEvidenceError):
                        load_accepted_reviewers(root)

    def test_a_missing_allowlist_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            with self.assertRaises(ReviewEvidenceError):
                load_accepted_reviewers(Path(name))


class SubstanceThresholdTests(unittest.TestCase):
    """Where a report stops being a token."""

    def test_a_terse_but_real_report_passes(self) -> None:
        self.assertTrue(is_substantive(CLEAN_REPORT, 0))

    def test_padding_a_verdict_with_punctuation_does_not_pass(self) -> None:
        for disguised in ("LGTM!!!", "  approved  ", "*LGTM*", "__ok__"):
            with self.subTest(disguised=disguised):
                self.assertFalse(is_substantive(disguised, 0))

    def test_a_verdict_padded_past_the_length_threshold_does_not_pass(self) -> None:
        """Length alone cannot be the test, or padding defeats it.

        A verdict decorated until it is long enough is still a verdict. This
        is the case the bare-verdict list exists for: without it, the length
        threshold is the only check and a wall of punctuation passes.
        """
        padded = "LGTM" + ("!" * 300)
        self.assertGreater(len(padded), 200)
        self.assertFalse(is_substantive(padded, 0))
        self.assertFalse(
            is_substantive("~*~ " * 40 + "approved" + " ~*~" * 40, 0)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class NetworkAndCommandLineTests(unittest.TestCase):
    """The layer that decides whether a failure blocks a merge.

    `verify` is pure and thoroughly exercised above, but it is `main` that
    turns a verdict into an exit code, and `check_pull_request` that decides
    what an unreachable GitHub means. That is where the fail-closed guarantee
    lives, and until now nothing proved it: a refactor could have made a
    network error look like a passing review and no test would have noticed.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "review").mkdir()
        (self.root / "review/accepted_reviewers.json").write_text(
            json.dumps({"reviewers": [{"id": REVIEWER_ID, "login": "reviewer"}]}),
            encoding="utf-8",
        )
        (self.root / "governance").mkdir()
        (self.root / "governance/EXCEPTIONS.md").write_text("", encoding="utf-8")

    def run_main(self, *arguments: str, responses=None, token: str = "t"):
        """Drive the command line with GitHub replaced, never called for real.

        The gate's own reporting is captured rather than printed: it belongs
        to the run under test, and letting it reach the suite's output would
        bury a real failure among a dozen deliberate ones.
        """
        import check_independent_review as module

        environment = dict(os.environ)
        environment["GITHUB_TOKEN"] = token
        self.output = io.StringIO()
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(self.output):
                if responses is None:
                    return module.main(list(arguments))
                with unittest.mock.patch.object(
                    module, "_paged", side_effect=responses
                ):
                    return module.main(list(arguments))

    def _arguments(self, *extra: str) -> tuple[str, ...]:
        return (
            "--root",
            str(self.root),
            "--repository",
            "owner/repo",
            "--pull-request",
            "1",
            "--head-sha",
            HEAD,
            *extra,
        )

    def test_a_network_failure_reports_but_does_not_block_in_warn_only_mode(
        self,
    ) -> None:
        code = self.run_main(
            *self._arguments(),
            responses=urllib.error.URLError("unreachable"),
        )
        self.assertEqual(code, 0)
        # It must say so rather than passing silently, or a warn-only run
        # would be indistinguishable from a verified one.
        self.assertIn("NOT VERIFIED", self.output.getvalue())

    def test_a_network_failure_fails_closed_under_enforce(self) -> None:
        """The property the whole promotion path depends on.

        An unreachable GitHub must never be indistinguishable from a verified
        review. Under enforcement it blocks.
        """
        code = self.run_main(
            *self._arguments("--enforce"),
            responses=urllib.error.URLError("unreachable"),
        )
        self.assertEqual(code, 1)
        self.assertIn("NOT VERIFIED", self.output.getvalue())

    def test_an_http_error_also_fails_closed_under_enforce(self) -> None:
        failure = urllib.error.HTTPError(
            "https://api.github.com", 403, "Forbidden", {}, None
        )
        self.assertEqual(
            self.run_main(*self._arguments("--enforce"), responses=failure), 1
        )
        self.assertEqual(self.run_main(*self._arguments(), responses=failure), 0)

    def test_a_missing_review_reports_in_warn_only_and_blocks_under_enforce(
        self,
    ) -> None:
        empty = ([], [], [])
        self.assertEqual(
            self.run_main(*self._arguments(), responses=lambda *a, **k: []), 0
        )
        self.assertEqual(
            self.run_main(
                *self._arguments("--enforce"), responses=lambda *a, **k: []
            ),
            1,
        )
        del empty

    def test_a_qualifying_review_passes_under_enforce(self) -> None:
        def responses(url, token):
            if url.endswith("reviews") or "/reviews?" in url:
                return [review()]
            return []

        self.assertEqual(
            self.run_main(*self._arguments("--enforce"), responses=responses), 0
        )

    def test_a_self_review_blocks_under_enforce(self) -> None:
        def responses(url, token):
            if "/reviews?" in url:
                return [review()]
            if "/commits?" in url:
                return [{"author": {"id": REVIEWER_ID}}]
            return []

        self.assertEqual(
            self.run_main(*self._arguments("--enforce"), responses=responses), 1
        )

    def test_no_pull_request_context_exits_successfully(self) -> None:
        """A push to an already-merged branch has no review to read."""
        for arguments in (
            ("--root", str(self.root)),
            ("--root", str(self.root), "--enforce"),
            ("--root", str(self.root), "--enforce", "--event-name", "push"),
            ("--root", str(self.root), "--enforce", "--event-name", "schedule"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_main(*arguments), 0)

    def test_a_pull_request_event_without_its_number_fails_closed(self) -> None:
        """The one way a required check could pass while verifying nothing.

        A push has no pull request and legitimately passes. A pull_request
        event that arrived without its number is a broken invocation, and
        treating the two alike would let a workflow edit satisfy branch
        protection with no review at all.
        """
        def refuse(*arguments, **keywords):
            raise AssertionError("GitHub must not be consulted without a pull request")

        for event in ("pull_request", "pull_request_target"):
            with self.subTest(event=event):
                self.assertEqual(
                    self.run_main(
                        "--root", str(self.root),
                        "--enforce", "--event-name", event,
                        responses=refuse,
                    ),
                    1,
                )
                # Reporting mode still passes: it blocks nothing by design.
                self.assertEqual(
                    self.run_main(
                        "--root", str(self.root),
                        "--event-name", event,
                        responses=refuse,
                    ),
                    0,
                )

    def test_the_workflow_enforces_and_supplies_the_event_name(self) -> None:
        """The guard is worthless if the workflow never passes the event.

        `--event-name` is what separates "no pull request here" from "a pull
        request whose number went missing", so a workflow that enforced without
        it would reinstate the vacuous pass this test exists to prevent.
        """
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/law-gates.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("independent-review", workflow)
        self.assertIn("--enforce", workflow)
        self.assertIn("--event-name", workflow)
        self.assertIn("github.event_name", workflow)
        # The enforcing invocation must carry both, not one of them.
        step = workflow.split("--enforce", 1)[1]
        self.assertIn("--event-name", step.split("--head-sha", 1)[0])

    def test_a_short_head_sha_cannot_satisfy_enforcement(self) -> None:
        """An abbreviation names a commit the gate cannot pin down."""
        def refuse(*arguments, **keywords):
            raise AssertionError(
                "GitHub must not be consulted for a head the gate cannot pin down"
            )

        for sha in ("", HEAD[:12], HEAD[:39]):
            with self.subTest(sha=sha):
                arguments = (
                    "--root",
                    str(self.root),
                    "--repository",
                    "owner/repo",
                    "--pull-request",
                    "1",
                    "--head-sha",
                    sha,
                )
                # The refusal happens before any request, so a removed length
                # guard shows up as a call rather than only as an exit code.
                self.assertEqual(
                    self.run_main(*arguments, "--enforce", responses=refuse), 1
                )
                self.assertEqual(self.run_main(*arguments, responses=refuse), 0)

    def test_a_missing_token_cannot_satisfy_enforcement(self) -> None:
        def refuse(*arguments, **keywords):
            raise AssertionError("GitHub must not be consulted without a token")

        self.assertEqual(
            self.run_main(*self._arguments("--enforce"), responses=refuse, token=""),
            1,
        )
        self.assertEqual(
            self.run_main(*self._arguments(), responses=refuse, token=""), 0
        )

    def test_a_word_containing_pending_does_not_void_an_exception(self) -> None:
        """"depending" is not "pending", and the difference is load-bearing.

        The unapproved marker was matched as a substring, so any exception
        whose prose contained "depending", "appending", "impending" or
        "spending" was silently ignored while reading as approved. EX-004 and
        EX-005 both contain such a word. Nothing reports this: the exception
        simply stops working.
        """
        for word in ("depending", "appending", "impending", "spending"):
            with self.subTest(word=word):
                (self.root / "governance/EXCEPTIONS.md").write_text(
                    f"## EX-009\n\n- **Scope:** `{HEAD}`.\n"
                    f"- **Approval date:** 2026-09-05.\n"
                    f"- **Necessity:** it exists to stop {word} on one provider.\n",
                    encoding="utf-8",
                )

                def refuse(*arguments, **keywords):
                    raise AssertionError(
                        "GitHub must not be called once an exception covers the head"
                    )

                self.assertEqual(
                    self.run_main(*self._arguments("--enforce"), responses=refuse), 0
                )

    def test_a_standalone_pending_still_voids_an_exception(self) -> None:
        """The guard must keep doing the job it was added for."""
        for marker in ("pending", "Pending", "PENDING", "approval pending."):
            with self.subTest(marker=marker):
                (self.root / "governance/EXCEPTIONS.md").write_text(
                    f"## EX-009\n\n- **Scope:** `{HEAD}`.\n"
                    f"- **Approval date:** {marker}\n",
                    encoding="utf-8",
                )
                self.assertEqual(
                    self.run_main(
                        *self._arguments("--enforce"), responses=lambda *a, **k: []
                    ),
                    1,
                )

    def test_an_approved_exception_is_honoured_without_calling_github(self) -> None:
        (self.root / "governance/EXCEPTIONS.md").write_text(
            f"## EX-009\n\n- **Scope:** `{HEAD}`.\n- **Approval date:** 2026-09-05.\n",
            encoding="utf-8",
        )

        def refuse(*arguments, **keywords):
            raise AssertionError("GitHub must not be called once an exception covers the head")

        self.assertEqual(
            self.run_main(*self._arguments("--enforce"), responses=refuse), 0
        )


IMPLEMENTATION = "b" * 40
UNRELATED = "c" * 40


class SelfReferentialExceptionTests(unittest.TestCase):
    """The narrow B-prime path, and the seven ways it must refuse.

    An exception recorded inside the pull request it covers cannot name its own
    head: a commit cannot contain its own SHA. So the approved SHA sits below
    the tip and the commits above it must be the register entry and nothing
    else.

    This is deliberately not "any approved ancestor makes the head valid". Each
    test below removes exactly one condition and proves the path closes.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "review").mkdir()
        (self.root / "review/accepted_reviewers.json").write_text(
            json.dumps({"reviewers": [{"id": REVIEWER_ID, "login": "reviewer"}]}),
            encoding="utf-8",
        )
        (self.root / "governance").mkdir()
        self._write_exception(IMPLEMENTATION, pull_request=1)

    def _write_exception(
        self, sha: str, pull_request: int, approval: str = "2026-09-06."
    ) -> None:
        (self.root / "governance/EXCEPTIONS.md").write_text(
            f"## EX-005 — bootstrap\n\n"
            f"- **Scope:** pull request #{pull_request} at `{sha}`, plus the "
            f"single commit adding this record.\n"
            f"- **Approval date:** {approval}\n"
            f"- **Necessity:** it exists to stop depending on one provider.\n",
            encoding="utf-8",
        )

    def _run(self, commits, files_by_sha, head=HEAD, pull_request="1"):
        """Drive main() with both GitHub accessors replaced."""
        import check_independent_review as module

        def paged(url, token):
            if url.endswith("/commits"):
                return commits
            return []

        def request(url, token):
            sha = url.rsplit("/", 1)[-1]
            if sha not in files_by_sha:
                raise urllib.error.URLError("no such commit")
            return {"files": [{"filename": name} for name in files_by_sha[sha]]}

        environment = dict(os.environ)
        environment["GITHUB_TOKEN"] = "t"
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                with unittest.mock.patch.object(module, "_paged", side_effect=paged):
                    with unittest.mock.patch.object(
                        module, "_request", side_effect=request
                    ):
                        code = module.main(
                            [
                                "--root", str(self.root),
                                "--repository", "owner/repo",
                                "--pull-request", pull_request,
                                "--head-sha", head,
                                "--enforce",
                            ]
                        )
        self.output = captured.getvalue()
        return code

    # 1. The case this exists for.
    def test_implementation_plus_record_only_commit_passes(self) -> None:
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
            files_by_sha={HEAD: ["governance/EXCEPTIONS.md"]},
        )
        self.assertEqual(code, 0)
        self.assertIn("only its own record sits above it", self.output)

    # 2. Source code above the approved SHA.
    def test_a_code_change_above_the_approved_sha_fails(self) -> None:
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
            files_by_sha={
                HEAD: ["governance/EXCEPTIONS.md", "src/alx/tools/sandbox.py"]
            },
        )
        self.assertEqual(code, 1)

    # 3. The verifier or the workflow above the approved SHA.
    def test_a_workflow_or_verifier_change_above_the_approved_sha_fails(self) -> None:
        for smuggled in (
            ".github/workflows/law-gates.yml",
            "scripts/check_independent_review.py",
            "review/accepted_reviewers.json",
            "governance/DECISIONS.md",
        ):
            with self.subTest(file=smuggled):
                code = self._run(
                    commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
                    files_by_sha={HEAD: ["governance/EXCEPTIONS.md", smuggled]},
                )
                self.assertEqual(code, 1)

    # 4. An exception approved for another pull request.
    def test_an_exception_for_a_different_pull_request_fails(self) -> None:
        self._write_exception(IMPLEMENTATION, pull_request=99)
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
            files_by_sha={HEAD: ["governance/EXCEPTIONS.md"]},
        )
        self.assertEqual(code, 1)

    # 5. An approved SHA that is not in this pull request at all.
    def test_an_unrelated_approved_sha_fails(self) -> None:
        self._write_exception(UNRELATED, pull_request=1)
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
            files_by_sha={HEAD: ["governance/EXCEPTIONS.md"]},
        )
        self.assertEqual(code, 1)

    # 6. Approval still pending.
    def test_a_pending_exception_fails(self) -> None:
        self._write_exception(IMPLEMENTATION, pull_request=1, approval="pending")
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
            files_by_sha={HEAD: ["governance/EXCEPTIONS.md"]},
        )
        self.assertEqual(code, 1)

    # 7. No exception at all, and a head that is not a descendant.
    def test_an_absent_or_non_ancestor_exception_fails(self) -> None:
        (self.root / "governance/EXCEPTIONS.md").write_text("", encoding="utf-8")
        self.assertEqual(
            self._run(
                commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
                files_by_sha={HEAD: ["governance/EXCEPTIONS.md"]},
            ),
            1,
        )
        # And an approved SHA that sits *above* the head is not an ancestor.
        self._write_exception(IMPLEMENTATION, pull_request=1)
        self.assertEqual(
            self._run(
                commits=[{"sha": HEAD}, {"sha": IMPLEMENTATION}],
                files_by_sha={HEAD: ["governance/EXCEPTIONS.md"]},
            ),
            1,
        )

    def test_two_approved_ancestors_are_ambiguous_and_refuse(self) -> None:
        """Ambiguity must not resolve itself into permission.

        If two commits in the pull request each carry an approved exception,
        there is no single approved implementation and the narrow path does not
        apply. Picking the first would let an unrelated approved commit
        authorise a head nobody assessed.
        """
        second = "e" * 40
        (self.root / "governance/EXCEPTIONS.md").write_text(
            f"## EX-005 — bootstrap\n\n"
            f"- **Scope:** pull request #1 at `{IMPLEMENTATION}`.\n"
            f"- **Approval date:** 2026-09-06.\n\n"
            f"## EX-006 — another\n\n"
            f"- **Scope:** pull request #1 at `{second}`.\n"
            f"- **Approval date:** 2026-09-06.\n",
            encoding="utf-8",
        )
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": second}, {"sha": HEAD}],
            files_by_sha={
                second: ["governance/EXCEPTIONS.md"],
                HEAD: ["governance/EXCEPTIONS.md"],
            },
        )
        self.assertEqual(code, 1)

    # Extra: an unreadable commit must not pass.
    def test_an_unreadable_commit_above_the_approved_sha_fails(self) -> None:
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": HEAD}],
            files_by_sha={},  # /commits/<sha> raises
        )
        self.assertEqual(code, 1)

    # Extra: two record commits are fine; three files in one are not.
    def test_several_record_only_commits_are_allowed(self) -> None:
        middle = "d" * 40
        code = self._run(
            commits=[{"sha": IMPLEMENTATION}, {"sha": middle}, {"sha": HEAD}],
            files_by_sha={
                middle: ["governance/EXCEPTIONS.md"],
                HEAD: ["governance/EXCEPTIONS.md"],
            },
        )
        self.assertEqual(code, 0)
