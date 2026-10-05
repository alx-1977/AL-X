"""A coding session that is working is observably working.

On 2026-10-05 two `run_coding_task` sessions on the DHL repair were stopped
as `session_stalled` after exactly ten minutes with no file changed. The
checkout was correct and the session could read and write it. What failed was
observation: the Grok CLI ran with `--output-format json`, which prints one
envelope when the session ends, so while the agent read and reasoned through
large files the watchdog saw neither output nor file writes and stopped a
working session. A planning note that "the session workspace was empty"
described the planner's own deliberately empty scratch directory, not the
session, and led the investigation away from the watchdog.

These tests run a stand-in CLI through the real subprocess watchdog. They
fail if the session goes back to a format that is silent until the end.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import CodingError, CodingRequest  # noqa: E402
from alx.providers import coding_process  # noqa: E402
from alx.providers.coding_session import GrokCodingSession  # noqa: E402


def _fake_cli(directory: Path, body: str) -> Path:
    """An executable that behaves like the CLI for what the watchdog sees."""
    path = directory / "fake-grok"
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# Reads for longer than the stall bound and never writes a file, streaming one
# event per step, then ends: what a session exploring a large repair does.
STREAMING_READER = """
import json, sys, time
fmt = sys.argv[sys.argv.index("--output-format") + 1]
events = []
for step in range(8):
    time.sleep(0.25)
    event = {"type": "tool_call", "toolName": "read_file", "status": "completed"}
    if fmt == "streaming-json":
        print(json.dumps(event), flush=True)
    events.append(event)
end = {"type": "end", "stopReason": "end_turn", "num_turns": 8}
if fmt == "streaming-json":
    print(json.dumps({"type": "text", "data": "read everything"}), flush=True)
    print(json.dumps(end), flush=True)
else:
    print(json.dumps({"text": "read everything", **end}), flush=True)
"""

# Genuinely silent: a hung session must still be stopped.
SILENT = """
import time
time.sleep(30)
"""


class StreamedActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "checkout"
        self.root.mkdir()
        (self.root / "README.md").write_text("# fixture\n", encoding="utf-8")
        self.home = Path(self.directory.name) / "grok-home"
        self.home.mkdir()

    def _session(self, cli: Path) -> GrokCodingSession:
        return GrokCodingSession(
            "grok-test", 30, executable=str(cli), stall_seconds=1,
            environment={"PATH": os.environ.get("PATH", ""), "GROK_HOME": str(self.home)},
        )

    def _request(self) -> CodingRequest:
        return CodingRequest(task="t", job_id="job-1", worktree=str(self.root))

    def test_the_session_asks_the_cli_to_stream(self) -> None:
        command = self._session(Path("grok")).command(self.root / "p.txt", self.root)
        self.assertEqual(command[command.index("--output-format") + 1], "streaming-json")

    def test_a_session_reading_past_the_stall_bound_is_not_stopped(self) -> None:
        """Two seconds of reading, no file written, a one-second stall bound."""
        result = self._session(_fake_cli(Path(self.directory.name), STREAMING_READER)) \
            .run_session(self._request(), "briefing")
        self.assertTrue(result.completed)
        self.assertEqual(result.turns, 8)
        self.assertEqual(result.report, "read everything")

    def test_a_silent_session_is_still_stopped_as_stalled(self) -> None:
        with self.assertRaises(CodingError) as raised:
            self._session(_fake_cli(Path(self.directory.name), SILENT)) \
                .run_session(self._request(), "briefing")
        self.assertEqual(raised.exception.code, "session_interrupted")
        self.assertEqual(raised.exception.details["reason_code"], "session_stalled")


class StreamedResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GrokCodingSession("grok-test", 30)

    @staticmethod
    def _stream(*events) -> str:
        return "\n".join(json.dumps(event) for event in events)

    def test_the_report_is_the_streamed_text_and_the_end_event_decides(self) -> None:
        result = self.session.read_result(self._stream(
            {"type": "tool_call", "toolName": "read_file"},
            {"type": "text", "data": "Changed "},
            {"type": "text", "data": "one file."},
            {"type": "end", "stopReason": "end_turn", "num_turns": 4},
        ))
        self.assertTrue(result.completed)
        self.assertEqual(result.report, "Changed one file.")
        self.assertEqual(result.turns, 4)

    def test_a_stream_without_an_end_did_not_finish(self) -> None:
        with self.assertRaises(CodingError) as raised:
            self.session.read_result(self._stream({"type": "text", "data": "partial"}))
        self.assertEqual(raised.exception.details["reason_code"], "session_end_missing")

    def test_a_streamed_error_is_classified(self) -> None:
        with self.assertRaises(CodingError) as raised:
            self.session.read_result(self._stream(
                {"type": "error", "message": "You have reached your usage limit"},
            ))
        self.assertEqual(
            raised.exception.details["reason_code"], "subscription_usage_exhausted"
        )

    def test_file_contents_in_the_stream_never_classify_a_failed_exit(self) -> None:
        """The agent read a source file that mentions a usage limit."""
        stdout = self._stream(
            {"type": "tool_call_update", "content": "USAGE_LIMIT_MARKERS = ('usage limit',)"},
        )
        self.assertEqual(self.session.failure_output(stdout), "")


class StopReasonSurvivesTests(unittest.TestCase):
    def test_an_eperm_from_killpg_does_not_replace_the_stall(self) -> None:
        """macOS refuses `killpg` once group members have exited unreaped.

        That PermissionError used to escape `_stop`, so a stalled session was
        reported as `cli_unavailable`. The process is signalled directly and
        the stall is what the job reports.
        """
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(
                coding_process.os, "killpg", side_effect=PermissionError(1, "EPERM")
            ):
                with self.assertRaises(CodingError) as raised:
                    coding_process.CodingCancellation().run(
                        subprocess.run, [sys.executable, "-c", "import time; time.sleep(30)"],
                        capture_output=True, text=True, timeout=30,
                        inactivity_timeout=0.5, activity_root=directory,
                        stdin=subprocess.DEVNULL, shell=False, check=False,
                    )
        self.assertEqual(raised.exception.code, "session_interrupted")
        self.assertEqual(raised.exception.details["reason_code"], "session_stalled")


class PlannerScratchDirectoryTests(unittest.TestCase):
    def test_the_planner_is_told_its_empty_directory_is_not_the_checkout(self) -> None:
        """Exact wording for a fixed protocol sentence, not a behaviour proof."""
        from alx.providers.coding_agent import PLAN_INSTRUCTION

        self.assertIn("deliberately empty scratch directory", PLAN_INSTRUCTION)
        self.assertIn(
            "Never describe the empty planning directory as evidence about that checkout",
            PLAN_INSTRUCTION,
        )


if __name__ == "__main__":
    unittest.main()
