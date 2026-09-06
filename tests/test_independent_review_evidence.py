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


INTRUDER_ID = 424242


class BaseTrustedPolicyTests(unittest.TestCase):
    """The pull request supplies the candidate; the base supplies the judge.

    Every test here points `--root` at a *trusted* directory that is not the
    proposed change, which is what the workflow does by checking out
    `base.sha`. The defects these cover were both found in review: a pull
    request could add its own reviewer, and an accepted reviewer credited as a
    co-author could review its own work.

    The self-referential exception path that used to live here is gone. It
    existed only because an exception recorded inside a pull request could not
    name its own head; once the register is read from the base, an exception
    always names a commit that already exists and the special case has no
    remaining purpose.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.trusted = base / "trusted"
        self.proposed = base / "proposed"
        for root in (self.trusted, self.proposed):
            (root / "review").mkdir(parents=True)
            (root / "governance").mkdir(parents=True)
            (root / "governance/EXCEPTIONS.md").write_text("", encoding="utf-8")
        self._roster(self.trusted, [{"id": REVIEWER_ID, "login": "reviewer"}])
        # The proposed tree adds a reviewer of its own and exempts itself.
        self._roster(
            self.proposed,
            [
                {"id": REVIEWER_ID, "login": "reviewer"},
                {"id": INTRUDER_ID, "login": "intruder[bot]"},
            ],
        )
        (self.proposed / "governance/EXCEPTIONS.md").write_text(
            f"## EX-999\n\n- **Scope:** `{HEAD}`.\n- **Approval date:** 2026-09-06.\n",
            encoding="utf-8",
        )

    @staticmethod
    def _roster(root: Path, reviewers: list) -> None:
        (root / "review/accepted_reviewers.json").write_text(
            json.dumps({"reviewers": reviewers}), encoding="utf-8"
        )

    def _run(self, root: Path, reviews, inline, commits):
        import check_independent_review as module

        def paged(url, token):
            if url.endswith("/reviews"):
                return reviews
            if url.endswith("/comments"):
                return inline
            return commits

        environment = dict(os.environ)
        environment["GITHUB_TOKEN"] = "t"
        with unittest.mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(io.StringIO()) as captured:
                with unittest.mock.patch.object(module, "_paged", side_effect=paged):
                    code = module.main(
                        [
                            "--root", str(root),
                            "--repository", "owner/repo",
                            "--pull-request", "1",
                            "--head-sha", HEAD,
                            "--enforce",
                        ]
                    )
        self.output = captured.getvalue()
        return code

    @staticmethod
    def _review(identity: str | int, login: str = "x"):
        return [
            {
                "user": {"id": identity, "login": login},
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": "A substantive account of what was examined. " + "x" * 220,
            }
        ]

    def test_a_reviewer_the_change_adds_cannot_satisfy_its_own_gate(self) -> None:
        """The defect Qodo found reviewing the change that added it."""
        reviews = self._review(INTRUDER_ID, "intruder[bot]")
        inline = [{"user": {"id": INTRUDER_ID}, "commit_id": HEAD, "path": "f.py"}]
        commits = [{"sha": HEAD, "author": {"id": 12345}, "commit": {"message": "x"}}]

        # Trusted base: the intruder is not on the roster, so it cannot qualify.
        self.assertEqual(self._run(self.trusted, reviews, inline, commits), 1)
        # And the proposed tree would have accepted it, which is the defect.
        self.assertEqual(self._run(self.proposed, reviews, inline, commits), 0)

    def test_an_exception_the_change_writes_cannot_exempt_it(self) -> None:
        commits = [{"sha": HEAD, "author": {"id": 12345}, "commit": {"message": "x"}}]
        self.assertEqual(self._run(self.trusted, [], [], commits), 1)
        # The proposed tree exempts itself; the trusted base does not.
        self.assertEqual(self._run(self.proposed, [], [], commits), 0)

    def test_a_base_roster_reviewer_with_exact_head_evidence_qualifies(self) -> None:
        reviews = self._review(REVIEWER_ID, "reviewer")
        inline = [{"user": {"id": REVIEWER_ID}, "commit_id": HEAD, "path": "f.py"}]
        commits = [{"sha": HEAD, "author": {"id": 12345}, "commit": {"message": "x"}}]
        self.assertEqual(self._run(self.trusted, reviews, inline, commits), 0)

    def test_a_missing_trusted_roster_fails_closed(self) -> None:
        (self.trusted / "review/accepted_reviewers.json").unlink()
        reviews = self._review(REVIEWER_ID, "reviewer")
        commits = [{"sha": HEAD, "author": {"id": 12345}, "commit": {"message": "x"}}]
        self.assertEqual(self._run(self.trusted, reviews, [], commits), 1)

    def test_a_missing_trusted_exception_register_fails_closed(self) -> None:
        """Unverifiable is not the same as empty, and must not pass for free."""
        (self.trusted / "governance/EXCEPTIONS.md").unlink()
        reviews = self._review(REVIEWER_ID, "reviewer")
        inline = [{"user": {"id": REVIEWER_ID}, "commit_id": HEAD, "path": "f.py"}]
        commits = [{"sha": HEAD, "author": {"id": 12345}, "commit": {"message": "x"}}]
        self.assertEqual(self._run(self.trusted, reviews, inline, commits), 1)

    def test_an_unreadable_trusted_roster_fails_closed(self) -> None:
        (self.trusted / "review/accepted_reviewers.json").write_text(
            "{not json", encoding="utf-8"
        )
        commits = [{"sha": HEAD, "author": {"id": 12345}, "commit": {"message": "x"}}]
        self.assertEqual(self._run(self.trusted, self._review(REVIEWER_ID), [], commits), 1)

    def test_the_workflow_runs_the_verifier_from_the_trusted_base(self) -> None:
        """The guard is worthless if the workflow runs the proposed copy."""
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/law-gates.yml"
        ).read_text(encoding="utf-8")
        job = workflow.split("independent-review:", 1)[1]
        self.assertIn("pull_request.base.sha", job)
        self.assertIn("path: trusted", job)
        # Both the script executed and the policy root must be the trusted copy.
        self.assertIn("python trusted/scripts/check_independent_review.py", job)
        self.assertIn("--root trusted", job)


class CoAuthorIndependenceTests(unittest.TestCase):
    """An accepted reviewer that helped write the change is not independent."""

    def test_a_co_author_trailer_is_recognised(self) -> None:
        from check_independent_review import co_authored_ids

        message = (
            "Do a thing\n\n"
            "Co-Authored-By: Bot <151058649+qodo-code-review[bot]@users.noreply.github.com>\n"
        )
        self.assertEqual(co_authored_ids(message), {151058649})

    def test_change_authors_includes_primary_and_co_authors(self) -> None:
        from check_independent_review import change_authors

        commits = [
            {
                "author": {"id": 111},
                "commit": {
                    "message": "x\n\nCo-authored-by: B <222+b@users.noreply.github.com>\n"
                },
            }
        ]
        self.assertEqual(change_authors(commits, {}), {111, 222})

    def test_a_reviewer_credited_as_co_author_cannot_review_its_own_change(self) -> None:
        """The exact bypass reported in review, reproduced then closed."""
        from check_independent_review import verify, ReviewEvidenceError, change_authors

        commits = [
            {
                "author": {"id": 111},
                "commit": {
                    "message": "work\n\nCo-Authored-By: R "
                    f"<{REVIEWER_ID}+reviewer@users.noreply.github.com>\n"
                },
            }
        ]
        reviews = [
            {
                "user": {"id": REVIEWER_ID, "login": "reviewer"},
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": "substantive " + "x" * 250,
            }
        ]
        inline = [{"user": {"id": REVIEWER_ID}, "commit_id": HEAD, "path": "f.py"}]
        with self.assertRaises(ReviewEvidenceError) as caught:
            verify(
                reviews, inline, change_authors(commits, {}), HEAD,
                {REVIEWER_ID: "reviewer"},
            )
        self.assertIn("authored commits", str(caught.exception))

    def test_a_reviewer_who_did_not_write_anything_still_qualifies(self) -> None:
        from check_independent_review import verify, change_authors

        commits = [{"author": {"id": 111}, "commit": {"message": "no trailers here"}}]
        reviews = [
            {
                "user": {"id": REVIEWER_ID, "login": "reviewer"},
                "commit_id": HEAD,
                "state": "COMMENTED",
                "body": "substantive " + "x" * 250,
            }
        ]
        inline = [{"user": {"id": REVIEWER_ID}, "commit_id": HEAD, "path": "f.py"}]
        self.assertIn(
            "reviewer",
            verify(
                reviews, inline, change_authors(commits, {}), HEAD,
                {REVIEWER_ID: "reviewer"},
            ),
        )

    def test_a_malformed_trailer_is_ignored_rather_than_trusted(self) -> None:
        from check_independent_review import co_authored_ids

        for message in (
            "Co-authored-by: nobody\n",
            "Co-authored-by: X <not-an-id@example.com>\n",
            "co authored by: X <1+x@users.noreply.github.com>\n",
        ):
            with self.subTest(message=message):
                self.assertEqual(co_authored_ids(message), set())

    def test_an_id_is_only_trusted_on_github_s_own_noreply_domain(self) -> None:
        """A numeric prefix on any other domain is the attacker's claim.

        Matching the number without anchoring the domain let a trailer reading
        `<151058649+x@evil.example.com>` inject a real reviewer's id from a
        domain nobody controls but whoever wrote the commit. Reported in review.
        """
        from check_independent_review import co_authored_ids, unresolved_co_authors

        hostile = "Co-authored-by: X <151058649+evil@evil.example.com>\n"
        self.assertEqual(co_authored_ids(hostile), set())
        # Seen but unresolvable, rather than silently absent.
        self.assertEqual(unresolved_co_authors(hostile), 1)

        genuine = (
            "Co-authored-by: X <151058649+qodo@users.noreply.github.com>\n"
        )
        self.assertEqual(co_authored_ids(genuine), {151058649})
        self.assertEqual(unresolved_co_authors(genuine), 0)

    def test_address_forms_we_cannot_resolve_are_reported_not_guessed(self) -> None:
        """Resolving a name or address to an id is ambiguous, so it is refused."""
        from check_independent_review import co_authored_ids, unresolved_co_authors

        for message in (
            "Co-authored-by: X <legacy@users.noreply.github.com>\n",
            "Co-authored-by: X <someone@example.com>\n",
        ):
            with self.subTest(message=message):
                self.assertEqual(co_authored_ids(message), set())
                self.assertEqual(unresolved_co_authors(message), 1)

    def test_the_workflow_definition_is_owned(self) -> None:
        """Pinning the verifier does not pin the instruction that runs it.

        A change to the workflow can keep the required job's name and replace
        its command, so enforcement disappears while the check still reports
        success. CODEOWNERS is the control that puts that change in front of
        Friedl; it is recorded here so removing the entry is visible.
        """
        owners = (
            Path(__file__).resolve().parents[1] / ".github/CODEOWNERS"
        ).read_text(encoding="utf-8")
        self.assertIn("/.github/workflows/", owners)
        self.assertIn("/scripts/check_independent_review.py", owners)
        self.assertIn("/review/", owners)
