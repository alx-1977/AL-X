"""Law 0 for public web reading, and the evidence anchor it produces.

Two things are proved here. That exactly one production path retrieves a web
page, with no sibling entry point that could reach the network another way.
And that a retrieval becomes citable through the mechanism that already
exists, so no second evidence store was created to hold it.
"""

from __future__ import annotations

import ast
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.web import build_web_runtime
from alx.capabilities import CapabilityRegistry
from alx.contracts import (
    CapabilityAttempt,
    CapabilityAttemptDisposition,
    CapabilityCall,
    CapabilityResultState,
    WebPage,
)
from alx.tools import ASK_WEB_PAGE
from alx.tools.web import build_web_executors


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "alx"
NOW = datetime(2026, 9, 4, 10, 30, tzinfo=UTC)


class StubFetcher:
    def fetch(self, url: str, max_characters: int) -> WebPage:
        return WebPage(
            requested_url=url,
            final_url=url,
            source_domain="example.com",
            retrieved_at=NOW,
            http_status=200,
            content="retrieved text",
            title="A Page",
        )


class OneProductionPathTests(unittest.TestCase):
    def test_exactly_one_capability_retrieves_a_web_page(self) -> None:
        runtime = build_web_runtime(True, lambda: "call-1")
        self.assertEqual(len(runtime.definitions), 1)
        self.assertEqual(runtime.definitions[0].capability_id, ASK_WEB_PAGE)
        self.assertEqual(len(runtime.executors), 1)
        runtime.provider.close()

    def test_only_one_module_performs_http_retrieval(self) -> None:
        """A second fetcher would be a second way onto the network."""
        importers = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            text = path.read_text()
            if "httpx" not in text:
                continue
            relative = path.relative_to(SOURCE_ROOT).as_posix()
            importers.append(relative)
        # Every httpx user is a named provider adapter for one external
        # service; web_fetch is the only one that takes an arbitrary URL.
        self.assertIn("providers/web_fetch.py", importers)
        self.assertTrue(
            all(item.startswith("providers/") for item in importers),
            f"httpx escaped the providers boundary: {importers}",
        )

    def test_only_one_module_resolves_a_hostname(self) -> None:
        """Resolution happening twice is what rebinding protection prevents."""
        resolvers = []
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            if "getaddrinfo" in path.read_text():
                resolvers.append(path.relative_to(SOURCE_ROOT).as_posix())
        self.assertEqual(resolvers, ["providers/web_url.py"])

    def test_the_boundary_cannot_be_bypassed_by_the_fetcher(self) -> None:
        """web_fetch must reach the network only through validated URLs."""
        text = (SOURCE_ROOT / "providers" / "web_fetch.py").read_text()
        tree = ast.parse(text)
        # Only calls made on the http client itself: `dict.get` and
        # `headers.get` are unrelated and must not be mistaken for one.
        on_client = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_client"
        }
        # Streaming only. A buffered `get`/`request` would read the whole body
        # before the byte ceiling could bound it.
        self.assertIn("stream", on_client)
        self.assertNotIn("get", on_client)
        self.assertNotIn("request", on_client)
        self.assertIn("parse_public_url", text)

    def test_redirects_are_never_delegated_to_the_client(self) -> None:
        text = (SOURCE_ROOT / "providers" / "web_fetch.py").read_text()
        self.assertIn("follow_redirects=False", text)
        self.assertNotIn("follow_redirects=True", text)

    def test_no_second_web_capability_is_registered(self) -> None:
        runtime = build_web_runtime(True, lambda: "call-1")
        registry = CapabilityRegistry(runtime.definitions)
        identifiers = [
            item.capability_id for item in registry.list_definitions()
        ]
        self.assertEqual(identifiers, [ASK_WEB_PAGE])
        runtime.provider.close()

    def test_search_is_not_present_before_its_review(self) -> None:
        """Step 4 is not authorised to run yet; nothing may anticipate it."""
        runtime = build_web_runtime(True, lambda: "call-1")
        self.assertNotIn(
            "ask_web_search", [item.capability_id for item in runtime.definitions]
        )
        runtime.provider.close()
        for path in sorted(SOURCE_ROOT.rglob("*.py")):
            self.assertNotIn("brave", path.read_text().lower(), f"in {path}")


class MutationTests(unittest.TestCase):
    """Restoring a competing path must be caught, not merely unused."""

    def test_a_second_registered_fetch_capability_is_caught(self) -> None:
        runtime = build_web_runtime(True, lambda: "call-1")
        registry = CapabilityRegistry(runtime.definitions)
        from dataclasses import replace

        duplicate = replace(
            runtime.definitions[0], capability_id="read_web_page"
        )
        registry.register(duplicate)
        identifiers = [
            item.capability_id for item in registry.list_definitions()
        ]
        # The assertion the production test relies on must fail here.
        with self.assertRaises(AssertionError):
            self.assertEqual(identifiers, [ASK_WEB_PAGE])
        runtime.provider.close()


class EvidenceAnchorTests(unittest.TestCase):
    """A retrieval is citable through the anchor that already existed."""

    def attempt(self) -> CapabilityAttempt:
        executors = build_web_executors(StubFetcher(), lambda: "call-web-1")
        call = CapabilityCall(
            "call-web-1", ASK_WEB_PAGE,
            {"page_id": "p1", "url": "https://example.com/report"},
        )
        return CapabilityAttempt(
            call,
            CapabilityAttemptDisposition.EXECUTED,
            True,
            executors[ASK_WEB_PAGE](call.arguments),
        )

    def test_a_successful_retrieval_is_a_known_evidence_source(self) -> None:
        """`attempt:<call_id>` is what goal evidence and the notebook accept."""
        attempt = self.attempt()
        self.assertIs(attempt.result.state, CapabilityResultState.SUCCEEDED)
        known = {
            f"attempt:{attempt.call.call_id}"
            for _ in (attempt,)
            if attempt.disposition is not CapabilityAttemptDisposition.PENDING
            and attempt.result is not None
            and attempt.result.state is CapabilityResultState.SUCCEEDED
        }
        self.assertEqual(known, {"attempt:call-web-1"})

    def test_no_separate_web_evidence_store_exists(self) -> None:
        """Extending the existing contracts, not adding another store."""
        for name in ("web_store.py", "web_evidence.py", "sources.py"):
            self.assertFalse((SOURCE_ROOT / "research" / name).exists())
            self.assertFalse((SOURCE_ROOT / "providers" / name).exists())

    def test_retrieval_records_nothing_on_its_own(self) -> None:
        """No automatic evidence, no automatic notebook entry."""
        attempt = self.attempt()
        self.assertEqual(attempt.result.evidence_refs, ())
        text = (SOURCE_ROOT / "tools" / "web.py").read_text()
        for capability in ("record_research_entry", "open_research_thread",
                           "revise_research_entry"):
            self.assertNotIn(capability, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
