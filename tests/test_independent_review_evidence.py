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

import json
import sys
import tempfile
import unittest
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
    *, user_id: int = REVIEWER_ID, sha: str = HEAD, body: str = CLEAN_REPORT
) -> dict:
    return {
        "user": {"id": user_id, "login": f"reviewer-{user_id}"},
        "commit_id": sha,
        "body": body,
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
