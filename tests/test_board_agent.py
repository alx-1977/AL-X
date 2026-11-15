"""D-083: the board agent does a whole board job in one session, paused between steps.

On 15 Nov AL/X spent 132 reasoning rounds and 15 fresh sessions to reach the
schematic step of a two-part swap. The agent keeps one session, works in big
steps, and waits at each step for Friedl's answer through her.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityCall, CapabilityDefinition,
    CapabilityResult, CapabilityResultState, ModelCompletion, SideEffect, StructuredSchema, ValueKind,
)
from alx.specialists.board_agent import REPORT_TOOL, BoardJobAgent  # noqa: E402
from alx.tools.board_agent import (  # noqa: E402
    BOARD_AGENT_KICAD_ACTIONS, DEFINITION, board_agent_call_refusal, build_board_agent_executors,
)

OBJECT = StructuredSchema(ValueKind.OBJECT)
KICAD = CapabilityDefinition("kicad", "KiCad", OBJECT, OBJECT, SideEffect.EFFECTFUL)


def call(arguments: dict) -> dict:
    return {"name": "call_capability", "input": {"capability_id": "kicad",
                                                 "arguments_json": json.dumps(arguments)}}


def report(status: str, step: str) -> dict:
    return {"name": REPORT_TOOL, "input": {
        "status": status, "step": step, "summary": step + " ready", "work_copy_id": "w" * 32,
        "work_copy_revision": "r1", "checks": ["nothing new against the original"],
        "questions": [], "open_points": []}}


class SessionModel:
    """A native session: each request either starts one or answers its pending call."""

    def __init__(self, steps: list) -> None:
        self.steps = list(steps)
        self.requests, self.abandoned, self.sessions = [], 0, 0

    def complete(self, request):
        self.requests.append(request)
        if request.tool_result is None:
            self.sessions += 1
        return ModelCompletion("fake", "opus", {}, {}, tool_use=self.steps.pop(0))

    def abandon_session(self) -> None:
        self.abandoned += 1


class BoardAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.dispatched = []

    def dispatch(self, item):
        self.dispatched.append(item)
        return CapabilityAttempt(item, CapabilityAttemptDisposition.EXECUTED, True,
                                 CapabilityResult(item.call_id, item.capability_id,
                                                  CapabilityResultState.SUCCEEDED, {"ok": True}))

    def agent(self, model):
        return BoardJobAgent(model, self.dispatch, (KICAD,), Path(self.folder.name))

    def test_each_stretch_is_a_fresh_session_started_from_the_journal_and_his_preferences(self) -> None:
        prefs = Path(self.folder.name) / "prefs.md"
        prefs.write_text("- GND ports face down.")
        model = SessionModel([call({"action": "edit"}), call({"action": "render"}), report("step_ready", "schematic"),
                              call({"action": "edit"}), report("step_ready", "placement")])
        agent = BoardJobAgent(model, self.dispatch, (KICAD,), Path(self.folder.name), preferences=prefs,
                              manual="add_track(board, net, points, width): A track.")
        first = agent.work(job_id=None, brief="On P0009, replace the two RF switches.", message=None, max_calls=10)
        self.assertEqual((first["status"], first["report"]["step"], len(first["calls"])), ("step_ready", "schematic", 2))
        second = agent.work(job_id=first["job_id"], brief=None, message="Approved.", max_calls=10)
        self.assertEqual((second["status"], second["report"]["step"]), ("step_ready", "placement"))
        # A new session for the second stretch, from the journal: the old pictures and results stay behind.
        self.assertEqual(model.sessions, 2)
        self.assertGreaterEqual(model.abandoned, 2)
        fresh = model.requests[3]
        self.assertIsNone(fresh.tool_result)
        history = json.loads(fresh.messages[-1].content)
        self.assertEqual([list(event)[0] for event in history["events"]], ["report", "alx"])
        self.assertEqual(history["events"][-1], {"alx": "Approved."})
        self.assertIn("GND ports face down.", fresh.messages[0].content)
        self.assertIn("add_track(board, net, points, width): A track.", fresh.messages[0].content)
        self.assertEqual(model.requests[0].kind, "board_agent")

    def test_alx_can_stop_a_running_stretch(self) -> None:
        agent_box = {}

        class StoppingModel(SessionModel):
            def complete(inner, request):
                result = super().complete(request)
                if len(inner.requests) == 2:
                    self.assertTrue(agent_box["agent"].stop(agent_box["job"]))
                return result
        model = StoppingModel([call({"action": "edit"}), call({"action": "edit"}), call({"action": "edit"})])
        agent = self.agent(model)
        agent_box["agent"] = agent
        original = agent._save

        def save(job_id, journal):
            agent_box["job"] = job_id
            original(job_id, journal)
        agent._save = save
        outcome = agent.work(job_id=None, brief="Route it.", message=None, max_calls=10)
        self.assertEqual((outcome["status"], outcome["stop_reason"]), ("stopped", "stopped_by_alx"))
        self.assertEqual(len(outcome["calls"]), 1)
        self.assertFalse(agent.stop(agent_box["job"]))
        executor = build_board_agent_executors(agent, lambda: "c")["delegate_board_job"]
        self.assertEqual(executor({"stop": True, "job_id": agent_box["job"]}).values["status"], "not_running")
        self.assertEqual(executor({"stop": True}).failure["code"], "arguments_unusable")

    def test_a_stretch_stops_at_its_bound_and_an_unknown_job_is_refused(self) -> None:
        model = SessionModel([call({"action": "edit"})] * 5)
        outcome = self.agent(model).work(job_id=None, brief="Route it.", message=None, max_calls=2)
        self.assertEqual((outcome["status"], outcome["stop_reason"], len(outcome["calls"])),
                         ("stopped", "call_limit", 2))
        executor = build_board_agent_executors(self.agent(SessionModel([])), lambda: "c")["delegate_board_job"]
        self.assertEqual(executor({"job_id": "nope", "message": "go"}).failure["code"], "job_unknown")
        self.assertEqual(executor({"brief": "x", "job_id": "y"}).failure["code"], "arguments_unusable")
        self.assertEqual(executor({}).failure["code"], "arguments_unusable")

    def test_the_agent_cannot_approve_write_back_or_open_windows(self) -> None:
        for action in ("approve_step", "write_back", "export", "open_in_kicad", "confirm_layout_readings",
                       "copy_library"):
            refused = board_agent_call_refusal(CapabilityCall("c", "kicad", {"action": action}))
            self.assertEqual(refused.reason_code, "board_agent_action_not_allowed", action)
        for action in BOARD_AGENT_KICAD_ACTIONS:
            self.assertIsNone(board_agent_call_refusal(CapabilityCall("c", "kicad", {"action": action})))
        self.assertIsNone(board_agent_call_refusal(CapabilityCall("c", "look", {"path": "x"})))
        self.assertIn("open_in_kicad", DEFINITION.purpose)



class ViewerTests(unittest.TestCase):
    def test_a_viewer_opens_in_front_through_launch_services_and_is_found_by_its_process(self) -> None:
        from types import SimpleNamespace
        from unittest.mock import patch
        from alx.providers import coding_process
        calls, found = [], iter(["", "", "4242\n"])

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=next(found) if argv[0] == "/usr/bin/pgrep" else "",
                                   stderr="")
        viewer = coding_process.start_kicad_viewer("/Apps/eeschema.app", "/tmp/x.kicad_sch", runner=runner,
                                                   settle_seconds=5)
        self.assertEqual(calls[1], ["/usr/bin/open", "-n", "-a", "/Apps/eeschema.app", "/tmp/x.kicad_sch"])
        self.assertEqual(viewer.pids, (4242,))
        with patch.object(coding_process.os, "kill") as kill:
            viewer.terminate()
        kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
