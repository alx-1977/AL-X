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

    def test_edit_crops_reach_the_next_model_request(self):
        pictures = {"sheet": b"sheet PNG", "board": b"board PNG"}
        def dispatch(call):
            result = CapabilityResult(call.call_id, "kicad", CapabilityResultState.SUCCEEDED,
                                      {"action": "edit", "result": {}, "image_sha256s": list(pictures)})
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True, result)
        model = SessionModel([call({"action":"edit","script":"pass","intent":[]}), report("step_ready","schematic")])
        agent = BoardJobAgent(model, dispatch, (KICAD,), Path(self.folder.name), image_source=pictures.get)
        agent.work(job_id=None, brief="Draw a sheet", message=None, max_calls=4)
        self.assertEqual([image.data for image in model.requests[-1].tool_images], list(pictures.values()))

    def test_each_stretch_is_a_fresh_session_started_from_the_journal_and_his_preferences(self) -> None:
        prefs = Path(self.folder.name) / "prefs.md"
        prefs.write_text("- GND ports face down.")
        model = SessionModel([call({"action": "edit"}), call({"action": "render"}), report("step_ready", "schematic"),
                              call({"action": "edit"}), report("step_ready", "placement")])
        agent = BoardJobAgent(model, self.dispatch, (KICAD,), Path(self.folder.name), preferences=prefs)
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
        self.assertEqual(model.requests[0].kind, "board_agent")

    def test_a_job_remembers_what_it_read_across_stretches(self) -> None:
        """19 Nov 2026: 405 calls in one job, 108 exact repeats; each stretch read the same
        datasheets again. An identical unchanging read is answered from the first one, and the
        next stretch is told what was read."""
        def dispatch(item):
            self.dispatched.append(item)
            return CapabilityAttempt(item, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult(
                item.call_id, item.capability_id, CapabilityResultState.SUCCEEDED,
                {"action": item.arguments["action"], "result": {"path": "/ds/TPT75176B.pdf", "pages": "1-4"}}))
        datasheet = {"action": "fetch_datasheet", "part_number": "TPT75176B-DF6R"}
        reading = {"action": "read_reference", "path": "/ds/TPT75176B.pdf", "offset": 0}
        copy_read = {"action": "read_reference", "path": "/x/.alx/runtime/kicad/abc/project/a.kicad_sch"}
        model = SessionModel([call(datasheet), call(reading), call(datasheet), call(copy_read), call(copy_read),
                              report("step_ready", "schematic"), call(reading), report("step_ready", "placement")])
        agent = BoardJobAgent(model, dispatch, (KICAD,), Path(self.folder.name))
        first = agent.work(job_id=None, brief="Add the RS485 LEDs.", message=None, max_calls=20)
        self.assertEqual(len(first["calls"]), 5)
        # The datasheet was fetched once; work-copy reads always run (edits change them).
        actions = [item.arguments["action"] for item in self.dispatched]
        self.assertEqual(actions.count("fetch_datasheet"), 1)
        self.assertEqual(sum(1 for item in self.dispatched if item.arguments.get("path", "").endswith("a.kicad_sch")), 2)
        self.assertTrue(first["calls"][2].get("repeated_read"))
        second = agent.work(job_id=first["job_id"], brief=None, message="Approved.", max_calls=20)
        self.assertEqual(second["status"], "step_ready")
        history = json.loads(model.requests[6].messages[-1].content)
        asked = [note["asked"].get("action") for note in history["already_read"]]
        self.assertIn("fetch_datasheet", asked)
        self.assertIn("/ds/TPT75176B.pdf", json.dumps(history["already_read"]))
        # Across stretches the identical datasheet read is still answered from the first.
        self.assertEqual(sum(1 for item in self.dispatched if item.arguments == reading), 1)

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

    def test_every_call_is_journalled_and_a_retry_carries_on_from_it_unless_stopped(self) -> None:
        """15 Nov: a retried session started again from the first brief and made a second copy."""
        def dispatch(item):
            return CapabilityAttempt(item, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult(
                item.call_id, item.capability_id, CapabilityResultState.SUCCEEDED,
                {"action": "check", "result": {"work_copy_id": "c" * 32, "work_copy_revision": "r7"}}))
        model = SessionModel([call({"action": "check", "project_dir": "/p"}), call({"action": "edit"}),
                              report("step_ready", "schematic")])
        agent = BoardJobAgent(model, dispatch, (KICAD,), Path(self.folder.name))
        outcome = agent.work(job_id=None, brief="Swap U6.", message=None, max_calls=10)
        journal = json.loads((Path(self.folder.name) / (outcome["job_id"] + ".json")).read_text())
        self.assertEqual(journal["work_copy"], {"work_copy_id": "c" * 32, "work_copy_revision": "r7"})
        self.assertEqual(len(journal["receipts"]), 2)
        # A retry starts from the journal as it is, with the work done so far.
        renewed = model.requests[-1].on_retry(RuntimeError("cli_failed"))
        latest = json.loads(renewed.messages[-1].content)
        self.assertEqual(latest["work_copy"]["work_copy_id"], "c" * 32)
        self.assertEqual(len(latest["latest_calls"]), 2)
        # After a stop, a retry is refused instead of starting the agent again.
        agent._stop.set()
        from alx.specialists.board_agent import BoardJobStopped
        with self.assertRaises(BoardJobStopped):
            model.requests[-1].on_retry(RuntimeError("cli_failed"))

    def test_a_stop_that_meets_a_retry_ends_the_stretch_as_stopped(self) -> None:
        from alx.specialists.board_agent import BoardJobStopped

        class RetriedModel(SessionModel):
            def complete(inner, request):
                if len(inner.requests) == 1:
                    inner.requests.append(request)
                    agent.stop()
                    request.on_retry(RuntimeError("cli_failed"))
                return super().complete(request)
        model = RetriedModel([call({"action": "edit"}), call({"action": "edit"})])
        agent = self.agent(model)
        agent._running = "x"
        outcome = agent.work(job_id=None, brief="Route.", message=None, max_calls=10)
        self.assertEqual((outcome["status"], outcome["stop_reason"]), ("stopped", "stopped_by_alx"))
        self.assertIsNotNone(BoardJobStopped)

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
        for action in ("approve_step", "write_back", "export", "open_in_kicad", "confirm_layout_readings"):
            refused = board_agent_call_refusal(CapabilityCall("c", "kicad", {"action": action}))
            self.assertEqual(refused.reason_code, "board_agent_action_not_allowed", action)
        for action in BOARD_AGENT_KICAD_ACTIONS:
            self.assertIsNone(board_agent_call_refusal(CapabilityCall("c", "kicad", {"action": action})))
        self.assertIsNone(board_agent_call_refusal(CapabilityCall(
            "c", "kicad", {"action": "edit", "script": "b = load()", "intent": []})))
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

    def test_a_window_that_never_opened_is_a_failure(self):
        """18 Nov review: a failed launch returned an empty viewer and counted as shown."""
        from types import SimpleNamespace
        from alx.providers import coding_process
        failed = (lambda argv, **kwargs: SimpleNamespace(returncode=1 if argv[0] == "/usr/bin/open" else 0,
                                                         stdout="", stderr="no application"))
        with self.assertRaisesRegex(OSError, "did not open"):
            coding_process.start_kicad_viewer("/Apps/pcbnew.app", "/tmp/x.kicad_pcb", runner=failed, settle_seconds=1)
        silent = (lambda argv, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""))
        with self.assertRaisesRegex(OSError, "no KiCad window appeared"):
            coding_process.start_kicad_viewer("/Apps/pcbnew.app", "/tmp/x.kicad_pcb", runner=silent, settle_seconds=0.3)


if __name__ == "__main__":
    unittest.main()
