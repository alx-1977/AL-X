"""Friedl's review allowance, 2026-10-14.

"Three reviews per PR is fine, after that she needs to ask. Then make it 12
for the month (4 x 3) after which she also needs to ask."
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.review import build_review_runtime  # noqa: E402
from alx.observability.review_allowance import SQLiteReviewAllowance  # noqa: E402
from alx.providers import github_review  # noqa: E402
from alx.tools.review import (  # noqa: E402
    REQUEST_EXTERNAL_REVIEW,
    REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE,
)
from tests.review_transcript import (  # noqa: E402
    HEAD,
    install_grace_clock,
    running_round,
    transport,
)

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


class LedgerTests(unittest.TestCase):
    def ledger(self) -> SQLiteReviewAllowance:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return SQLiteReviewAllowance(Path(directory.name) / "reviews.sqlite3")

    def test_three_reviews_of_one_pull_request_reach_it(self) -> None:
        ledger = self.ledger()
        for _ in range(2):
            ledger.record(7, HEAD, NOW)
        self.assertFalse(ledger.state(7, NOW).reached)
        ledger.record(7, HEAD, NOW)
        self.assertTrue(ledger.state(7, NOW).reached)
        # Another pull request still has its own three.
        self.assertFalse(ledger.state(8, NOW).reached)

    def test_twelve_reviews_in_a_month_reach_it(self) -> None:
        ledger = self.ledger()
        for number in range(12):
            ledger.record(100 + number, HEAD, NOW)
        state = ledger.state(200, NOW)
        self.assertEqual(state.month_reviews, 12)
        self.assertTrue(state.reached)
        # A new month starts again.
        self.assertFalse(ledger.state(200, NOW + timedelta(days=31)).reached)

    def test_the_counts_survive_a_restart(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "reviews.sqlite3"
        SQLiteReviewAllowance(path).record(7, HEAD, NOW)
        self.assertEqual(SQLiteReviewAllowance(path).state(7, NOW).pull_request_reviews, 1)


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.allowance = SQLiteReviewAllowance(Path(directory.name) / "reviews.sqlite3")
        original = github_review.httpx.request
        self.addCleanup(setattr, github_review.httpx, "request", original)
        self.statuses: dict[str, list[dict]] = {HEAD: []}
        self.github = transport(head_sha=HEAD, number=42, statuses=self.statuses)
        github_review.httpx.request = self.github
        install_grace_clock(self)
        self.runtime = build_review_runtime(
            True, "owner/repo", "token", lambda: "call-1",
            started=lambda number, sha, at: "completed",
            allowance=self.allowance,
        )

    def ask(self, name: str = REQUEST_EXTERNAL_REVIEW):
        return self.runtime.executors[name]({"pull_request_number": 42})

    def test_the_fourth_review_of_a_pull_request_is_refused_unasked(self) -> None:
        for _ in range(3):
            self.assertEqual(self.ask().state.value, "succeeded")
        result = self.ask()
        self.assertEqual(result.failure["code"], "review_allowance_reached")
        self.assertEqual(result.failure["pull_request_reviews"], 3)
        self.assertTrue(result.failure["requires_judgement"])
        # Three triggers went out, and none for the refused fourth.
        self.assertEqual(len(self.github.posted), 3)

    def test_beyond_the_allowance_needs_friedl_s_approval(self) -> None:
        policy = self.runtime.policies[REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE]
        self.assertTrue(policy.approval_required)
        self.assertFalse(self.runtime.policies[REQUEST_EXTERNAL_REVIEW].approval_required)

    def test_an_approved_review_beyond_the_allowance_goes_out_and_counts(self) -> None:
        for _ in range(3):
            self.ask()
        result = self.ask(REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE)
        self.assertEqual(result.state.value, "succeeded")
        self.assertEqual(len(self.github.posted), 4)
        self.assertEqual(self.allowance.state(42, datetime.now(UTC)).pull_request_reviews, 4)

    def test_joining_a_running_round_asks_for_nothing_and_costs_nothing(self) -> None:
        self.statuses[HEAD] = running_round()
        result = self.ask()
        self.assertFalse(result.values["requested"])
        self.assertEqual(self.github.posted, [])
        self.assertEqual(self.allowance.state(42, datetime.now(UTC)).pull_request_reviews, 0)

    def test_without_an_allowance_no_beyond_capability_is_offered(self) -> None:
        runtime = build_review_runtime(
            True, "owner/repo", "token", lambda: "call-1",
            started=lambda number, sha, at: "completed",
        )
        self.assertNotIn(REQUEST_EXTERNAL_REVIEW_BEYOND_ALLOWANCE, runtime.executors)


if __name__ == "__main__":
    unittest.main()
