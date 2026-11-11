"""The web, paid questions and her notebook as one capability (D-074).

Eleven capabilities (ask_web_page, ask_web_search, ask_research_question and
eight notebook jobs) became `research`, with an action per job. Each action
keeps its own contract, permission and approval rule; the old capabilities
are removed, not hidden (Law 0).
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.config import WebSearchSettings  # noqa: E402
from alx.config.settings import APPROVED_SEARCH_USD_PER_REQUEST  # noqa: E402
from alx.contracts import (  # noqa: E402
    RESEARCH, CapabilityResultState, ResearchAction, SideEffect, WebPage,
)
from alx.core.model_reasoner import _one_line  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.research import CONTRACTS, build_research_executors  # noqa: E402
from alx.tools.web import build_page_reader  # noqa: E402
from alx.tools.worker import WORKER_CAPABILITIES  # noqa: E402
from research_actions import (  # noqa: E402
    CORRECT_ENTRY, DELETE, NOTEBOOK, OPEN_THREAD, READ_PAGE, READ_THREAD, SEARCH_NOTEBOOK,
    definition, research_call, research_runtime,
)

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
FORMER = (
    "ask_web_page", "ask_web_search", "ask_research_question",
    "open_research_thread", "record_research_entry", "revise_research_entry",
    "correct_research_entry", "search_research", "read_research_thread",
    "set_research_status", "delete_research",
)


class Fetcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str, max_characters: int) -> WebPage:
        self.calls.append(url)
        return WebPage(requested_url=url, final_url=url, source_domain="example.com",
                       retrieved_at=NOW, http_status=200, content="text", title="T")


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self, **options):
        runtime = research_runtime(self.directory.name, lambda: "call-1", **options)
        self.addCleanup(runtime.close)
        return runtime


class OneCapabilityTests(Fixture):
    def test_one_capability_carries_every_offered_job(self) -> None:
        settings = WebSearchSettings(True, "key", APPROVED_SEARCH_USD_PER_REQUEST, 30, 0.15)
        runtime = self.runtime(web_read_enabled=True, search_settings=settings)
        self.assertEqual([item.capability_id for item in runtime.definitions], [RESEARCH])
        self.assertEqual(set(runtime.executors), {RESEARCH})
        self.assertEqual(set(runtime.policies), {RESEARCH})
        self.assertEqual(runtime.offered,
                         (ResearchAction.READ_PAGE, ResearchAction.SEARCH_WEB, *NOTEBOOK))

    def test_an_action_without_its_authority_is_absent_everywhere(self) -> None:
        runtime = self.runtime()
        self.assertEqual(runtime.offered, NOTEBOOK)
        purpose = runtime.definitions[0].purpose
        for action in ("read_page", "search_web", "ask_model"):
            self.assertNotIn(f"[{action}]", purpose)
        line = _one_line(purpose)
        self.assertNotIn("web page", line)
        self.assertNotIn("paid model", line)
        self.assertEqual(runtime.permissions, frozenset({"research.notebook"}))

    def test_the_catalogue_line_names_every_job_within_the_cut(self) -> None:
        line = _one_line(definition().purpose)
        self.assertLessEqual(len(line), 200)
        self.assertFalse(line.endswith("..."))
        for job in ("web page", "search the web", "paid model", "notebook"):
            self.assertIn(job, line)

    def test_each_action_keeps_its_own_effect_class(self) -> None:
        one = definition()
        for action, contract in CONTRACTS.items():
            with self.subTest(action=action.value):
                self.assertIs(one.effect_for({"action": action.value}), contract.side_effect)
        self.assertIs(one.effect_for({"action": SEARCH_NOTEBOOK}), SideEffect.NONE)
        self.assertIs(one.effect_for({"action": READ_PAGE}), SideEffect.EFFECTFUL)
        self.assertFalse(one.transmits_authored_text)

    def test_notebook_text_stays_out_of_the_goal_audit(self) -> None:
        durable = set(definition().durable_input_fields)
        for field in ("content", "reason", "question", "interest"):
            self.assertNotIn(field, durable)
        for field in ("action", "entry_id", "thread_id", "url", "subject", "instruction"):
            self.assertIn(field, durable)

    def test_the_worker_holds_no_research(self) -> None:
        self.assertNotIn(RESEARCH, WORKER_CAPABILITIES)


class ApprovalTests(Fixture):
    def test_only_friedls_correction_needs_his_approval(self) -> None:
        runtime = self.runtime(web_read_enabled=True)
        gate = SafetyGate(runtime.policies)
        context = AuthorityContext("friedl", runtime.permissions, NOW)
        for action in runtime.offered:
            with self.subTest(action=action.value):
                decision = gate.evaluate(research_call("c", action.value, {}), context)
                expected = "approval_required" if action is ResearchAction.CORRECT_ENTRY else "allowed"
                self.assertEqual(decision.state.value, expected)

    def test_deleting_her_own_notebook_needs_none(self) -> None:
        """D-051, unchanged by the merge."""
        runtime = self.runtime()
        decision = SafetyGate(runtime.policies).evaluate(
            research_call("c", DELETE, {"record_id": "t", "kind": "thread"}),
            AuthorityContext("friedl", runtime.permissions, NOW))
        self.assertEqual(decision.state.value, "allowed")


class DispatchTests(Fixture):
    def broker(self, runtime, executors=None):
        return CapabilityBroker(CapabilityRegistry(runtime.definitions),
                                SafetyGate(runtime.policies), executors or runtime.executors)

    def authority(self, runtime):
        return AuthorityContext("friedl", runtime.permissions, NOW)

    def test_an_unoffered_action_runs_nothing_and_names_the_offered(self) -> None:
        runtime = self.runtime()
        # The gate already stops it: an unoffered action is not exempt from
        # approval, so nothing could run it unasked.
        attempt = self.broker(runtime).dispatch(
            research_call("call-1", READ_PAGE, {}), self.authority(runtime))
        self.assertFalse(attempt.implementation_invoked)
        # And the capability itself refuses it, naming what is offered.
        result = runtime.executors[RESEARCH]({"action": READ_PAGE})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("open_thread", result.failure["reason"])
        self.assertNotIn("read_page", result.failure["reason"])

    def test_a_field_of_another_action_is_refused(self) -> None:
        runtime = self.runtime(web_read_enabled=True)
        attempt = self.broker(runtime).dispatch(
            research_call("call-1", READ_THREAD, {"thread_id": "t", "url": "https://example.com"}),
            self.authority(runtime))
        self.assertEqual(attempt.result.failure["code"], "arguments_unusable")
        self.assertIn("read_thread does not take url", attempt.result.failure["reason"])

    def test_a_notebook_write_and_read_round_trip_under_one_id(self) -> None:
        runtime = self.runtime()
        broker = self.broker(runtime)
        opened = broker.dispatch(research_call("call-1", OPEN_THREAD, {
            "thread_id": "t1", "question": "Why?", "interest": "Curious."}), self.authority(runtime))
        self.assertIs(opened.result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(opened.result.capability_id, RESEARCH)
        # The receipt keeps identity only, as before the merge.
        self.assertEqual(dict(opened.result.durable_values), {"thread_id": "t1", "status": "open"})
        read = broker.dispatch(research_call("call-1", READ_THREAD, {"thread_id": "t1"}),
                               self.authority(runtime))
        self.assertEqual(read.result.values["question"], "Why?")

    def test_a_page_result_keeps_its_provenance(self) -> None:
        fetcher = Fetcher()
        executors = build_research_executors(
            {ResearchAction.READ_PAGE: build_page_reader(fetcher, lambda: "c")}, lambda: "c")
        result = executors[RESEARCH]({"action": READ_PAGE, "page_id": "p",
                                      "url": "https://example.com/a"})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.capability_id, RESEARCH)
        self.assertIsNotNone(result.provenance)
        self.assertNotIn("content", result.durable_values)
        self.assertEqual(fetcher.calls, ["https://example.com/a"])


class OnePathTests(unittest.TestCase):
    """Law 0: the eleven former capabilities are gone, not hidden."""

    def test_no_former_id_remains_in_production_code(self) -> None:
        for path in sorted((ROOT / "src" / "alx").rglob("*.py")):
            text = path.read_text()
            for name in FORMER:
                self.assertNotIn(f'"{name}"', text, f"{name} in {path}")

    def test_start_up_composes_research_once(self) -> None:
        source = (ROOT / "src/alx/bootstrap/live_voice.py").read_text()
        calls = [node.func.id for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        self.assertEqual(calls.count("build_research_runtime"), 1)
        for gone in ("build_web_runtime", "build_notebook_runtime"):
            self.assertNotIn(gone, source)
        for module in ("web.py", "notebook.py"):
            self.assertFalse((ROOT / "src/alx/bootstrap" / module).exists())

    def test_restoring_a_former_capability_is_caught(self) -> None:
        from dataclasses import replace
        registry = CapabilityRegistry((definition(),))
        registry.register(replace(CONTRACTS[ResearchAction.READ_PAGE], capability_id="ask_web_page"))
        identifiers = {item.capability_id for item in registry.list_definitions()}
        with self.assertRaises(AssertionError):
            self.assertEqual(identifiers, {RESEARCH})

    def test_no_action_contract_is_registered_on_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = research_runtime(directory, lambda: "c", web_read_enabled=True)
            try:
                registered = {item.capability_id for item in runtime.definitions}
            finally:
                runtime.close()
        self.assertFalse(registered & {action.value for action in CONTRACTS})
        self.assertFalse(registered & set(FORMER))


if __name__ == "__main__":
    unittest.main()
