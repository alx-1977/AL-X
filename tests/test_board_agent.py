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

    def test_a_job_pauses_at_each_step_and_carries_on_in_the_same_session(self) -> None:
        model = SessionModel([call({"action": "edit"}), call({"action": "render"}), report("step_ready", "schematic"),
                              call({"action": "edit"}), report("step_ready", "placement")])
        agent = self.agent(model)
        first = agent.work(job_id=None, brief="On P0009, replace the two RF switches.", message=None, max_calls=10)
        self.assertEqual((first["status"], first["report"]["step"], len(first["calls"])), ("step_ready", "schematic", 2))
        second = agent.work(job_id=first["job_id"], brief=None, message="Approved.", max_calls=10)
        self.assertEqual((second["status"], second["report"]["step"], second["session"]),
                         ("step_ready", "placement", "continued"))
        # One session for both steps: his answer went in as the paused call's result.
        self.assertEqual(model.sessions, 1)
        self.assertEqual(json.loads(model.requests[3].tool_result), {"from_alx": "Approved."})
        self.assertEqual(model.requests[0].kind, "board_agent")

    def test_a_job_whose_session_ended_carries_on_from_its_journal(self) -> None:
        first_model = SessionModel([report("question", "schematic")])
        first = self.agent(first_model).work(job_id=None, brief="Swap U6 and U7.", message=None, max_calls=5)
        # A restarted runtime has a new agent and no paused session.
        model = SessionModel([report("step_ready", "schematic")])
        second = self.agent(model).work(job_id=first["job_id"], brief=None,
                                        message="RFC2 and RFC4 are used.", max_calls=5)
        self.assertEqual(second["session"], "new")
        history = json.loads(model.requests[0].messages[-1].content)
        self.assertEqual(history["transport_fact"], "earlier_in_this_job")
        self.assertEqual(history["events"][-1], {"alx": "RFC2 and RFC4 are used."})
        self.assertEqual(history["events"][0]["report"]["status"], "question")

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


if __name__ == "__main__":
    unittest.main()
