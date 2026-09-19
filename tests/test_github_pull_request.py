"""Reading and revising a pull request, against the shapes GitHub really sends.

The pull request is where the work is proposed, reviewed and answered, so these
are part of AL/X's repository authority rather than a separate one. What is
asserted here is mostly about not mistaking one answer for another: an empty
page for a complete read, a resolved thread for an outstanding one, a
malformed response for a pull request with nothing to say.

That distinction is the whole value of the review-thread read. Branch
protection can require every thread resolved before a merge, so AL/X asks this
before merging and acts on the count. An answer that undercounts is worse than
no answer, because no answer is visibly no answer.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts.github_pull_request import PullRequestError  # noqa: E402
from alx.providers import github_pull_request as module  # noqa: E402
from alx.providers.github_pull_request import GitHubPullRequests  # noqa: E402


def _thread(identifier: str, resolved: bool = False) -> dict:
    return {
        "id": identifier,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "src/alx/thing.py",
        "line": 12,
        "comments": {"nodes": [{"author": {"login": "coderabbitai[bot]"},
                                "body": "a finding"}]},
    }


class _Response:
    def __init__(self, payload, status: int = 200) -> None:
        self.status_code = status
        self.headers: dict = {}
        self._payload = payload

    def json(self):
        return self._payload


class ReviewThreadTests(unittest.TestCase):
    """Every unresolved thread, or an honest failure. Never a short count."""

    def transport(self, pages: list[dict]):
        """A GitHub that answers with these pages in order."""
        self.requests: list[dict] = []
        remaining = list(pages)

        def request(method, url, **keywords):
            self.requests.append(keywords.get("json") or {})
            return _Response({"data": {"repository": {"pullRequest": {
                "reviewThreads": remaining.pop(0),
            }}}})

        original = module.httpx.request
        module.httpx.request = request
        self.addCleanup(setattr, module.httpx, "request", original)
        return GitHubPullRequests("owner/repo", "token")

    def page(self, nodes: list[dict], more: bool = False, cursor: str = "") -> dict:
        return {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": more, "endCursor": cursor or None},
        }

    def test_a_single_page_returns_its_unresolved_threads(self) -> None:
        provider = self.transport([self.page([
            _thread("T1"), _thread("T2", resolved=True), _thread("T3"),
        ])])
        threads = provider.review_threads(48)
        self.assertEqual([item["id"] for item in threads], ["T1", "T3"])

    def test_threads_beyond_the_first_page_are_still_counted(self) -> None:
        """A truncated read would report "none remain" while some do.

        The query asks for a hundred at a time, so a pull request with more
        threads than that returned only what the first page happened to hold —
        and AL/X reads this count to decide whether anything is outstanding.
        """
        provider = self.transport([
            self.page([_thread("T1"), _thread("T2", resolved=True)],
                      more=True, cursor="CURSOR1"),
            self.page([_thread("T3", resolved=True)], more=True, cursor="CURSOR2"),
            self.page([_thread("T4")]),
        ])
        threads = provider.review_threads(48)
        self.assertEqual([item["id"] for item in threads], ["T1", "T4"])
        # Each page was asked for with the cursor the last one gave.
        cursors = [item["variables"]["after"] for item in self.requests]
        self.assertEqual(cursors, [None, "CURSOR1", "CURSOR2"])

    def test_a_resolved_pull_request_reports_nothing_outstanding(self) -> None:
        provider = self.transport([self.page([
            _thread("T1", resolved=True), _thread("T2", resolved=True),
        ])])
        self.assertEqual(provider.review_threads(48), ())

    def test_a_thread_without_an_identity_is_not_counted(self) -> None:
        """Counting it would make "threads remain" true for something
        nobody can address."""
        provider = self.transport([self.page([
            {"isResolved": False}, _thread(""), _thread("T1"),
        ])])
        self.assertEqual([item["id"] for item in provider.review_threads(48)],
                         ["T1"])

    def test_a_missing_cursor_is_unreadable_rather_than_truncated(self) -> None:
        """Another page exists and there is no way to ask for it."""
        provider = self.transport([
            self.page([_thread("T1")], more=True, cursor=""),
        ])
        with self.assertRaises(PullRequestError) as caught:
            provider.review_threads(48)
        self.assertEqual(caught.exception.code, "pull_request_unavailable")

    def test_an_unreadable_response_is_never_an_empty_answer(self) -> None:
        for payload in (
            None,
            {"data": {"repository": None}},
            {"data": {"repository": {"pullRequest": {
                "reviewThreads": {"nodes": None, "pageInfo": {}}}}}},
        ):
            with self.subTest(payload=payload):
                def request(method, url, **keywords):
                    return _Response(payload)

                original = module.httpx.request
                module.httpx.request = request
                self.addCleanup(setattr, module.httpx, "request", original)
                provider = GitHubPullRequests("owner/repo", "token")
                with self.assertRaises(PullRequestError):
                    provider.review_threads(48)

    def test_the_walk_is_bounded(self) -> None:
        """A server that always claims another page does not loop forever."""
        endless = [
            self.page([_thread(f"T{index}")], more=True, cursor=f"C{index}")
            for index in range(module.MAX_THREAD_PAGES + 2)
        ]
        provider = self.transport(endless)
        with self.assertRaises(PullRequestError):
            provider.review_threads(48)
        self.assertEqual(len(self.requests), module.MAX_THREAD_PAGES)


class MalformedResponseTests(unittest.TestCase):
    """A response that cannot be read stays a declared failure.

    `_outcome` builds the record every caller returns, so a value GitHub
    should never send has to be refused there rather than reaching
    `PullRequestOutcome` and raising something the capability boundary does
    not catch. The broker would report an executor fault, which says the
    system broke rather than that GitHub answered strangely.
    """

    def provider(self, payload):
        def request(method, url, **keywords):
            return _Response(payload)

        original = module.httpx.request
        module.httpx.request = request
        self.addCleanup(setattr, module.httpx, "request", original)
        return GitHubPullRequests("owner/repo", "token")

    def test_an_unusable_pull_request_number_is_a_declared_failure(self) -> None:
        """`bool` is an `int`, so `True` passed the old check."""
        for number in (True, False, 0, -1, "7", None):
            with self.subTest(number=number):
                provider = self.provider({
                    "number": number,
                    "state": "open",
                    "head": {"ref": "fix/thing", "sha": "a" * 40},
                    "base": {"ref": "main"},
                })
                with self.assertRaises(PullRequestError) as caught:
                    provider.update(7, title="Revised")
                self.assertEqual(
                    caught.exception.code, "pull_request_unavailable"
                )

    def test_a_usable_response_still_builds_the_record(self) -> None:
        provider = self.provider({
            "number": 48,
            "state": "open",
            "head": {"ref": "fix/thing", "sha": "a" * 40},
            "base": {"ref": "main"},
        })
        outcome = provider.update(48, title="Revised")
        self.assertEqual(outcome.pull_request_number, 48)
        self.assertEqual(outcome.branch, "fix/thing")


if __name__ == "__main__":
    unittest.main()
