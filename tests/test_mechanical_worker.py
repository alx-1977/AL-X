"""D-070: Opus decides; a lighter worker carries out one brief of mechanical work.

On 2026-11-05 a board clean-up took 65 Opus sessions, mostly 112 program runs
and the reviews of their results. The worker runs those on its own small
context, through the same broker and safety gate, and reports once.
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities.broker import CapabilityBroker  # noqa: E402
from alx.capabilities.registry import CapabilityRegistry  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ModelCompletion, SideEffect, StructuredSchema, ValueKind,
)
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate  # noqa: E402
from alx.specialists.worker import MechanicalWorker, WorkerError  # noqa: E402
from alx.tools.worker import (  # noqa: E402
    DEFINITION, DELEGATE_MECHANICAL_WORK, WORKER_CAPABILITIES, build_worker_executors,
)

ROOT = Path(__file__).resolve().parents[1]
OBJECT = StructuredSchema(ValueKind.OBJECT)
RUN = CapabilityDefinition("run_program", "Run a program", OBJECT, OBJECT, SideEffect.EFFECTFUL,
                           ("program_unavailable",))
SEND = CapabilityDefinition("send_mail_reply", "Send a reply", OBJECT, OBJECT,
                            SideEffect.EFFECTFUL)
REPORT = {"status": "done", "results": [{"name": "RF1", "value": "44 ohm",
                                         "how_checked": "ngspice"}],
          "open_points": [], "files": []}


def tool(capability_id: str, arguments: dict | str) -> dict:
    return {"name": "call_capability", "input": {
        "capability_id": capability_id,
        "arguments_json": arguments if isinstance(arguments, str) else json.dumps(arguments)}}


class ScriptedModel:
    def __init__(self, steps: list, final: dict = REPORT) -> None:
        self.steps, self.final = list(steps), final
        self.requests, self.abandoned = [], 0

    def complete(self, request):
        self.requests.append(request)
        if self.steps:
            return ModelCompletion("fake", "sonnet", {}, {}, tool_use=self.steps.pop(0))
        return ModelCompletion("fake", "sonnet", self.final, {})

    def abandon_session(self) -> None:
        self.abandoned += 1


class WorkerLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dispatched = []

    def dispatch(self, call):
        self.dispatched.append(call)
        return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                 CapabilityResult(call.call_id, call.capability_id,
                                                  CapabilityResultState.SUCCEEDED,
                                                  {"stdout": "Z = 44 ohm"}))

    def test_a_brief_runs_its_calls_and_reports_once(self) -> None:
        model = ScriptedModel([tool("run_program", {"argv": ["ngspice"]}),
                               tool("send_mail_reply", {}), tool("run_program", "not json")])
        outcome = MechanicalWorker(model, self.dispatch, (RUN,)).run("Simulate RF1.", "job", 10)
        self.assertEqual(outcome["status"], "done")
        self.assertEqual(outcome["report"]["results"][0]["value"], "44 ohm")
        # Only the listed, well-formed call reached the broker.
        self.assertEqual([(c.call_id, c.capability_id) for c in self.dispatched],
                         [("job-1", "run_program")])
        self.assertEqual([c["outcome"] for c in outcome["calls"]],
                         ["succeeded", "not_listed", "arguments_unusable"])
        answers = [json.loads(r.tool_result) for r in model.requests[1:]]
        self.assertEqual(answers[0]["values"]["stdout"], "Z = 44 ohm")
        self.assertEqual(answers[1]["refused"], "capability_not_listed")
        # Its own small context: its rules, the listed capabilities, the brief.
        first = model.requests[0]
        self.assertEqual(first.kind, "worker")
        self.assertEqual(first.messages[1].content, "Simulate RF1.")
        self.assertIn('"id": "run_program"', first.messages[0].content)
        self.assertNotIn("send_mail_reply", first.messages[0].content)

    def test_a_run_that_reaches_its_call_limit_stops_without_a_report(self) -> None:
        model = ScriptedModel([tool("run_program", {})] * 5)
        outcome = MechanicalWorker(model, self.dispatch, (RUN,)).run("Loop.", "job", 2)
        self.assertEqual((outcome["status"], outcome["stop_reason"]), ("stopped", "call_limit"))
        self.assertIsNone(outcome["report"])
        self.assertEqual(len(self.dispatched), 2)
        self.assertEqual(model.abandoned, 1)

    def test_a_failure_after_completed_calls_returns_them(self) -> None:
        """Review of #207: an edit done before a timeout must not be hidden."""
        class FailsSecond(ScriptedModel):
            def complete(self, request):
                if request.tool_result is not None:
                    raise RuntimeError("reasoning_timeout")
                return super().complete(request)
        model = FailsSecond([tool("run_program", {})])
        with self.assertRaises(WorkerError) as raised:
            MechanicalWorker(model, self.dispatch, (RUN,)).run("Edit.", "job", 5)
        self.assertEqual(raised.exception.code, "worker_failed")
        self.assertEqual([c["call_id"] for c in raised.exception.calls], ["job-1"])
        self.assertEqual(model.abandoned, 1)
        result = build_worker_executors(
            MechanicalWorker(FailsSecond([tool("run_program", {})]), self.dispatch, (RUN,)),
            lambda: "d")[DELEGATE_MECHANICAL_WORK]({"brief": "Edit."})
        self.assertEqual(result.failure["code"], "worker_failed")
        self.assertEqual([c["call_id"] for c in result.failure["calls"]], ["d-1"])

    def test_the_time_limit_is_checked_after_every_call(self) -> None:
        """Review of #207: a long call must not carry the run into another request."""
        model = ScriptedModel([tool("run_program", {}), tool("run_program", {})])
        outcome = MechanicalWorker(model, self.dispatch, (RUN,), max_seconds=-1).run(
            "Slow.", "job", 5)
        self.assertEqual((outcome["status"], outcome["stop_reason"]), ("stopped", "time_limit"))
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.abandoned, 1)

    def test_a_malformed_report_is_a_failure_not_a_result(self) -> None:
        model = ScriptedModel([], final={"status": "maybe"})
        with self.assertRaises(WorkerError):
            MechanicalWorker(model, self.dispatch, (RUN,)).run("Go.", "job", 5)

    def test_one_brief_at_a_time(self) -> None:
        release = threading.Event()

        class Blocking(ScriptedModel):
            def complete(self, request):
                release.wait(5)
                return super().complete(request)

        worker = MechanicalWorker(Blocking([]), self.dispatch, (RUN,))
        first = threading.Thread(target=worker.run, args=("A.", "a", 5))
        first.start()
        try:
            with self.assertRaises(WorkerError) as raised:
                for _ in range(100):
                    worker.run("B.", "b", 5)
                    threading.Event().wait(0.01)
            self.assertEqual(raised.exception.code, "worker_busy")
        finally:
            release.set()
            first.join(5)

    def test_the_worker_has_no_approvals_through_the_real_gate(self) -> None:
        """Anything needing her approval is refused to the worker, not worked around."""
        registry = CapabilityRegistry((RUN, SEND))
        broker = CapabilityBroker(registry, SafetyGate({
            "run_program": AuthorityPolicy(frozenset({"self.read"})),
            "send_mail_reply": AuthorityPolicy(frozenset({"self.read"}), approval_required=True),
        }), {
            "run_program": lambda arguments: CapabilityResult(
                "job-2", "run_program", CapabilityResultState.SUCCEEDED, {}),
            "send_mail_reply": lambda arguments: self.fail("sent without approval"),
        })

        def dispatch(call):
            return broker.dispatch(call, AuthorityContext(
                "friedl", frozenset({"self.read"}), datetime.now(UTC)))

        model = ScriptedModel([tool("send_mail_reply", {}), tool("run_program", {})])
        outcome = MechanicalWorker(model, dispatch, (RUN, SEND)).run("Try.", "job", 5)
        self.assertEqual([c["outcome"] for c in outcome["calls"]],
                         ["approval_required", "succeeded"])


class DelegateCapabilityTests(unittest.TestCase):
    def executor(self, worker):
        return build_worker_executors(worker, lambda: "delegate-1")[DELEGATE_MECHANICAL_WORK]

    def test_the_report_returns_to_her_as_evidence(self) -> None:
        worker = MechanicalWorker(ScriptedModel([]), lambda call: None, (RUN,))
        result = self.executor(worker)({"brief": "Measure RF1."})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.call_id, "delegate-1")
        self.assertEqual(result.values["report"]["status"], "done")
        self.assertTrue(DEFINITION.output_schema.accepts(result.values))

    def test_unusable_arguments_and_failures_return_codes(self) -> None:
        worker = MechanicalWorker(ScriptedModel([]), lambda call: None, (RUN,))
        for arguments in ({"brief": " "}, {"brief": "x", "max_calls": 0},
                          {"brief": "x", "max_calls": 81}, {"brief": "x", "max_calls": True}):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.executor(worker)(arguments).failure["code"],
                                 "arguments_unusable")

        class Broken(ScriptedModel):
            def complete(self, request):
                raise RuntimeError("cli_failed")
        broken = MechanicalWorker(Broken([]), lambda call: None, (RUN,))
        self.assertEqual(self.executor(broken)({"brief": "x"}).failure["code"], "worker_failed")

    def test_the_worker_list_names_no_capability_that_sends_spends_or_merges(self) -> None:
        from alx.tools import mail, program, repository, research, review, web
        forbidden = {
            mail.SEND_MAIL_REPLY, mail.MOVE_MAIL_MESSAGE_TO_TRASH, repository.MERGE_PULL_REQUEST,
            research.ASK_RESEARCH_QUESTION, review.REQUEST_EXTERNAL_REVIEW,
            web.ASK_WEB_SEARCH, web.ASK_WEB_PAGE, "write_back_kicad_board",
            "export_kicad_manufacturing", "flash_bench_reader_usb", DELEGATE_MECHANICAL_WORK,
        }
        self.assertFalse(forbidden & set(WORKER_CAPABILITIES))
        # Review of #207: run_program runs any command as Friedl's user, so
        # no fixed list could bound what the worker did with it.
        self.assertNotIn(program.RUN_PROGRAM, WORKER_CAPABILITIES)

    def test_the_runtime_builds_one_worker_through_her_broker(self) -> None:
        source = (ROOT / "src/alx/bootstrap/live_voice.py").read_text(encoding="utf-8")
        calls = [node.func.id for node in ast.walk(ast.parse(source))
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                 and node.func.id in ("MechanicalWorker", "build_worker_executors")]
        self.assertEqual(sorted(calls), ["MechanicalWorker", "build_worker_executors"])
        # Her permissions, none of her approvals or standing scopes.
        body = source.split("def worker_dispatch")[1].split("worker_definitions =")[0]
        self.assertIn("granted_permission_references=frozenset(permissions)", body)
        self.assertNotIn("approvals", body)
        self.assertNotIn("standing_scopes", body)


if __name__ == "__main__":
    unittest.main()
