"""`ask_web_page` through the broker, and the trust boundary around what it returns.

Nothing here reaches a network: the fetch provider is a stub returning fixed
pages, because what is under test is the capability contract, the provenance
it stamps, the state it makes durable, and the fact that a retrieved page
never becomes an instruction.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import (
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    ContentOrigin,
    WebPage,
    WebRetrievalError,
)
from alx.bootstrap.web import WEB_READ_PERMISSION, build_web_runtime
from alx.safety import AuthorityContext, SafetyGate
from alx.tools import ASK_WEB_PAGE
from alx.tools.web import build_web_executors


NOW = datetime(2026, 9, 4, 10, 30, tzinfo=UTC)


def a_page(**overrides) -> WebPage:
    values = {
        "requested_url": "https://example.com/report",
        "final_url": "https://example.com/report",
        "source_domain": "example.com",
        "retrieved_at": NOW,
        "http_status": 200,
        "content": "The reservoir stood at sixty percent on Tuesday.",
        "title": "Reservoir Report",
        "publisher": "Example News",
    }
    values.update(overrides)
    return WebPage(**values)


class StubFetcher:
    def __init__(self, page=None, error=None) -> None:
        self._page = page
        self._error = error
        self.calls: list[tuple[str, int]] = []

    def fetch(self, url: str, max_characters: int) -> WebPage:
        self.calls.append((url, max_characters))
        if self._error is not None:
            raise self._error
        return self._page or a_page()


def execute(fetcher, arguments, call_id="call-1"):
    executors = build_web_executors(fetcher, lambda: call_id)
    return executors[ASK_WEB_PAGE](arguments)


class ResultShapeTests(unittest.TestCase):
    def test_a_retrieved_page_carries_its_provenance(self) -> None:
        result = execute(StubFetcher(), {"page_id": "p1", "url": "https://example.com/report"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["final_url"], "https://example.com/report")
        self.assertEqual(result.values["source_domain"], "example.com")
        self.assertEqual(result.values["retrieved_at"], NOW.isoformat())
        self.assertEqual(result.values["title"], "Reservoir Report")
        self.assertEqual(result.values["publisher"], "Example News")
        self.assertEqual(result.values["http_status"], 200)

    def test_both_urls_are_kept_through_a_redirect(self) -> None:
        """What was asked for and what answered are different facts."""
        result = execute(
            StubFetcher(a_page(
                requested_url="https://example.com/old",
                final_url="https://example.com/new",
            )),
            {"page_id": "p1", "url": "https://example.com/old"},
        )
        self.assertEqual(result.values["requested_url"], "https://example.com/old")
        self.assertEqual(result.values["final_url"], "https://example.com/new")

    def test_a_complete_read_reports_no_omission(self) -> None:
        result = execute(StubFetcher(), {"page_id": "p1", "url": "https://example.com/"})
        self.assertNotIn("content_omitted_characters", result.values)

    def test_a_partial_read_reports_the_shortfall(self) -> None:
        result = execute(
            StubFetcher(a_page(content="abc", content_omitted_characters=4200)),
            {"page_id": "p1", "url": "https://example.com/"},
        )
        self.assertEqual(result.values["content_omitted_characters"], 4200)

    def test_an_absent_title_is_omitted_rather_than_invented(self) -> None:
        result = execute(
            StubFetcher(a_page(title=None, publisher=None)),
            {"page_id": "p1", "url": "https://example.com/"},
        )
        self.assertNotIn("title", result.values)
        self.assertNotIn("publisher", result.values)


class ProvenanceTests(unittest.TestCase):
    def test_web_content_is_external(self) -> None:
        result = execute(StubFetcher(), {"page_id": "p1", "url": "https://example.com/"})
        self.assertIn(ContentOrigin.EXTERNAL, result.provenance.origins)

    def test_web_content_carries_no_mail_expiry(self) -> None:
        """D-013 governs mail. A web page is not mail-derived."""
        result = execute(StubFetcher(), {"page_id": "p1", "url": "https://example.com/"})
        self.assertIsNone(result.provenance.content_expires_at)
        self.assertFalse(result.provenance.governed_by_retention())

    def test_the_retrieval_time_is_the_provenance_time(self) -> None:
        result = execute(StubFetcher(), {"page_id": "p1", "url": "https://example.com/"})
        self.assertEqual(result.provenance.recorded_at, NOW)


class DurableStateTests(unittest.TestCase):
    def test_the_page_body_never_becomes_durable(self) -> None:
        """Goal state records what was read, not the whole of what it said."""
        result = execute(
            StubFetcher(a_page(content="x" * 5000)),
            {"page_id": "p1", "url": "https://example.com/"},
        )
        self.assertNotIn("content", result.durable_values)
        self.assertNotIn("x" * 5000, str(result.durable_values))

    def test_durable_state_keeps_enough_to_cite_after_a_restart(self) -> None:
        result = execute(StubFetcher(), {"page_id": "p1", "url": "https://example.com/report"})
        for field in ("requested_url", "final_url", "source_domain", "retrieved_at"):
            self.assertIn(field, result.durable_values)

    def test_the_content_still_reaches_the_core_in_full(self) -> None:
        result = execute(
            StubFetcher(a_page(content="the readable part")),
            {"page_id": "p1", "url": "https://example.com/"},
        )
        self.assertEqual(result.values["content"], "the readable part")

    def test_the_url_is_durable_input_so_a_restart_knows_what_was_asked(self) -> None:
        from alx.tools.web import DEFINITION

        self.assertEqual(DEFINITION.durable_input_fields, ("page_id", "url"))


class FailureTests(unittest.TestCase):
    def test_each_refusal_keeps_its_own_code(self) -> None:
        """Blocked, missing and too large are different facts about the world."""
        for code in ("url_not_public", "retrieval_blocked", "retrieval_timeout",
                     "unsupported_content_type", "unsupported_dynamic_page",
                     "page_too_large", "provider_failed"):
            with self.subTest(code=code):
                result = execute(
                    StubFetcher(error=WebRetrievalError(code)),
                    {"page_id": "p1", "url": "https://example.com/"},
                )
                self.assertIs(result.state, CapabilityResultState.FAILED)
                self.assertEqual(result.failure["code"], code)

    def test_an_undeclared_error_becomes_provider_failed(self) -> None:
        result = execute(
            StubFetcher(error=RuntimeError("secret internal detail")),
            {"page_id": "p1", "url": "https://example.com/"},
        )
        self.assertEqual(result.failure["code"], "provider_failed")
        self.assertNotIn("secret internal detail", str(result.failure))

    def test_unusable_arguments_are_refused_before_any_fetch(self) -> None:
        fetcher = StubFetcher()
        for arguments in (
            {"page_id": "p1"},
            {"page_id": "p1", "url": "https://example.com/", "max_characters": 0},
            {"page_id": "p1", "url": "https://example.com/", "max_characters": -5},
            {"page_id": "p1", "url": "https://example.com/", "max_characters": "many"},
        ):
            with self.subTest(arguments=arguments):
                result = execute(fetcher, arguments)
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(fetcher.calls, [])

    def test_a_failure_carries_no_page_content(self) -> None:
        result = execute(
            StubFetcher(error=WebRetrievalError("page_too_large", "some page text")),
            {"page_id": "p1", "url": "https://example.com/"},
        )
        self.assertEqual(set(result.failure), {"code"})


class BrokerTests(unittest.TestCase):
    """The capability is reachable only the way every other one is."""

    def setUp(self) -> None:
        self.runtime = build_web_runtime(True, lambda: "call-1")
        self.registry = CapabilityRegistry(self.runtime.definitions)
        self.fetcher = StubFetcher()
        self.broker = CapabilityBroker(
            self.registry,
            SafetyGate(self.runtime.policies),
            build_web_executors(self.fetcher, lambda: "call-1"),
        )

    def authority(self, permissions):
        return AuthorityContext("friedl", frozenset(permissions), NOW)

    def test_the_capability_runs_with_web_read(self) -> None:
        attempt = self.broker.dispatch(
            CapabilityCall("call-1", ASK_WEB_PAGE,
                           {"page_id": "p1", "url": "https://example.com/"}),
            self.authority({WEB_READ_PERMISSION}),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)

    def test_without_web_read_nothing_is_fetched(self) -> None:
        attempt = self.broker.dispatch(
            CapabilityCall("call-1", ASK_WEB_PAGE,
                           {"page_id": "p1", "url": "https://example.com/"}),
            self.authority(set()),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(self.fetcher.calls, [])

    def test_research_spend_does_not_grant_web_access(self) -> None:
        """Buying model tokens is not permission to reach the network."""
        attempt = self.broker.dispatch(
            CapabilityCall("call-1", ASK_WEB_PAGE,
                           {"page_id": "p1", "url": "https://example.com/"}),
            self.authority({"research.spend"}),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(self.fetcher.calls, [])

    def test_a_malformed_call_is_rejected_by_schema(self) -> None:
        attempt = self.broker.dispatch(
            CapabilityCall("call-1", ASK_WEB_PAGE, {"page_id": "p1"}),
            self.authority({WEB_READ_PERMISSION}),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)
        self.assertEqual(attempt.reason_code, "input_invalid")

    def test_an_undeclared_argument_is_rejected(self) -> None:
        attempt = self.broker.dispatch(
            CapabilityCall("call-1", ASK_WEB_PAGE, {
                "page_id": "p1", "url": "https://example.com/", "method": "POST",
            }),
            self.authority({WEB_READ_PERMISSION}),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.REJECTED)

    def test_every_declared_failure_code_is_accepted_by_the_broker(self) -> None:
        for code in ("url_not_public", "retrieval_blocked", "page_too_large",
                     "unsupported_dynamic_page", "provider_failed"):
            with self.subTest(code=code):
                broker = CapabilityBroker(
                    self.registry,
                    SafetyGate(self.runtime.policies),
                    build_web_executors(
                        StubFetcher(error=WebRetrievalError(code)), lambda: "call-1"
                    ),
                )
                attempt = broker.dispatch(
                    CapabilityCall("call-1", ASK_WEB_PAGE,
                                   {"page_id": "p1", "url": "https://example.com/"}),
                    self.authority({WEB_READ_PERMISSION}),
                )
                self.assertIs(
                    attempt.disposition, CapabilityAttemptDisposition.EXECUTED
                )
                self.assertEqual(attempt.result.failure["code"], code)


class RuntimeAvailabilityTests(unittest.TestCase):
    def test_a_runtime_never_told_it_may_read_has_no_capability(self) -> None:
        """Absent, not merely failing."""
        self.assertIsNone(build_web_runtime(False, lambda: "call-1"))

    def test_the_enabled_runtime_registers_exactly_one_capability(self) -> None:
        runtime = build_web_runtime(True, lambda: "call-1")
        self.assertEqual(
            [item.capability_id for item in runtime.definitions], [ASK_WEB_PAGE]
        )
        self.assertEqual(set(runtime.executors), {ASK_WEB_PAGE})
        runtime.provider.close()

    def test_web_read_is_its_own_permission(self) -> None:
        runtime = build_web_runtime(True, lambda: "call-1")
        self.assertEqual(runtime.permissions, frozenset({WEB_READ_PERMISSION}))
        self.assertNotIn("research.spend", runtime.permissions)
        runtime.provider.close()

    def test_reading_needs_no_approval_ceremony(self) -> None:
        runtime = build_web_runtime(True, lambda: "call-1")
        self.assertFalse(runtime.policies[ASK_WEB_PAGE].approval_required)
        runtime.provider.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
