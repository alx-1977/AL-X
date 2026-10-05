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
import time
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
            "grok-test", executable=str(cli), stall_seconds=1,
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

    def test_session_output_is_spooled_to_disk_not_held_in_memory(self) -> None:
        """No deadline, so output must not accumulate in the runtime's memory."""
        seen: dict = {}

        def record(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"type": "end", "stopReason": "end_turn", "num_turns": 1}), "")

        GrokCodingSession("grok-test", stall_seconds=10, runner=record).run_session(
            self._request(), "briefing")
        self.assertNotIn("capture_output", seen)
        self.assertTrue(hasattr(seen["stdout"], "fileno"))
        self.assertTrue(hasattr(seen["stderr"], "fileno"))
        self.assertIsNone(seen["timeout"])

    def test_what_the_agent_read_is_not_kept_to_read_the_result(self) -> None:
        reader = """
import json, sys
for _ in range(50):
    print(json.dumps({"type": "tool_call_update", "content": "x" * 100000}), flush=True)
print(json.dumps({"type": "text", "data": "done"}), flush=True)
print(json.dumps({"type": "end", "stopReason": "end_turn", "num_turns": 3}), flush=True)
"""
        session = self._session(_fake_cli(Path(self.directory.name), reader))
        retained: list[str] = []
        original = session.retained_output
        session.retained_output = lambda lines: retained.append(original(lines)) or retained[-1]
        result = session.run_session(self._request(), "briefing")
        self.assertTrue(result.completed)
        self.assertEqual(result.report, "done")
        self.assertLess(len(retained[0]), 1_000)  # 5 MB of reading was not kept

    def test_output_beyond_the_size_bound_stops_the_session_explicitly(self) -> None:
        """A size bound, not a time bound: activity alone cannot fill the disk."""
        flood = """
import json, time
while True:
    print(json.dumps({"type": "tool_call_update", "content": "x" * 10000}), flush=True)
    time.sleep(0.01)
"""
        from alx.providers import coding_subscription_session as base
        with mock.patch.object(base, "MAX_SESSION_OUTPUT_BYTES", 200_000):
            with self.assertRaises(CodingError) as raised:
                self._session(_fake_cli(Path(self.directory.name), flood)) \
                    .run_session(self._request(), "briefing")
        self.assertEqual(raised.exception.code, "session_interrupted")
        self.assertEqual(raised.exception.details["reason_code"], "session_output_limit")

    def test_output_past_the_bound_fails_even_when_the_session_exits_at_once(self) -> None:
        """Checked on completion too, not only between polls."""
        burst = """
import json, sys
sys.stdout.write(json.dumps({"type": "tool_call_update", "content": "x" * 300000}) + "\\n")
print(json.dumps({"type": "end", "stopReason": "end_turn", "num_turns": 1}), flush=True)
"""
        from alx.providers import coding_subscription_session as base
        with mock.patch.object(base, "MAX_SESSION_OUTPUT_BYTES", 100_000):
            with self.assertRaises(CodingError) as raised:
                self._session(_fake_cli(Path(self.directory.name), burst)) \
                    .run_session(self._request(), "briefing")
        self.assertEqual(raised.exception.details["reason_code"], "session_output_limit")

    def test_an_oversized_reading_line_is_skipped_without_being_read_whole(self) -> None:
        big = """
import json
print(json.dumps({"type": "tool_call_update", "content": "x" * 50000}), flush=True)
print(json.dumps({"type": "text", "data": "done"}), flush=True)
print(json.dumps({"type": "end", "stopReason": "end_turn", "num_turns": 2}), flush=True)
"""
        from alx.providers import coding_subscription_session as base
        with mock.patch.object(base, "MAX_EVENT_LINE_CHARACTERS", 10_000):
            result = self._session(_fake_cli(Path(self.directory.name), big)) \
                .run_session(self._request(), "briefing")
        self.assertTrue(result.completed)
        self.assertEqual(result.report, "done")

    def test_an_oversized_result_line_is_refused_not_truncated(self) -> None:
        big_end = """
import json
print(json.dumps({"type": "text", "data": "done"}), flush=True)
print(json.dumps({"type": "end", "stopReason": "end_turn", "padding": "x" * 50000}), flush=True)
"""
        from alx.providers import coding_subscription_session as base
        with mock.patch.object(base, "MAX_EVENT_LINE_CHARACTERS", 10_000):
            with self.assertRaises(CodingError) as raised:
                self._session(_fake_cli(Path(self.directory.name), big_end)) \
                    .run_session(self._request(), "briefing")
        self.assertEqual(raised.exception.details["reason_code"], "session_event_oversized")

    def test_a_descendant_still_writing_after_the_cli_exits_is_stopped(self) -> None:
        """Monitoring ends with the session, so its leftovers end with it too."""
        pid_file = Path(self.directory.name) / "writer.pid"
        leaver = f"""
import json, subprocess, sys
writer = subprocess.Popen([sys.executable, "-c",
    "import time\\nwhile True:\\n    print('{{\\"type\\": \\"tool_call_update\\"}}', flush=True); time.sleep(0.05)"])
open({str(pid_file)!r}, "w").write(str(writer.pid))
print(json.dumps({{"type": "text", "data": "done"}}), flush=True)
print(json.dumps({{"type": "end", "stopReason": "end_turn", "num_turns": 1}}), flush=True)
"""
        # A roomier stall bound than the shared one: this stand-in starts a
        # second interpreter before it writes anything.
        session = GrokCodingSession(
            "grok-test", executable=str(_fake_cli(Path(self.directory.name), leaver)),
            stall_seconds=5,
            environment={"PATH": os.environ.get("PATH", ""), "GROK_HOME": str(self.home)},
        )
        result = session.run_session(self._request(), "briefing")
        self.assertTrue(result.completed)
        writer = int(pid_file.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(writer, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(writer, signal.SIGKILL)
            self.fail("a descendant kept writing after the session returned")


class GroupCensusTests(unittest.TestCase):
    def test_zombies_are_not_counted_as_live_members(self) -> None:
        listing = "  101   100 S\n  102   100 Z\n  103   100 Z+\n  104   999 S\n"
        with mock.patch.object(coding_process.subprocess, "run", return_value=subprocess.CompletedProcess(
                ["ps"], 0, listing, "")):
            members, counted = coding_process._group_members(100)
        self.assertTrue(counted)
        self.assertEqual(members, (101,))


class StreamedResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = GrokCodingSession("grok-test", stall_seconds=10)

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

    def test_a_descendant_is_stopped_when_the_group_signal_is_refused(self) -> None:
        """A tool process the CLI started must not outlive the session."""
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            script = (
                "import subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            with mock.patch.object(
                coding_process.os, "killpg", side_effect=PermissionError(1, "EPERM")
            ):
                with self.assertRaises(CodingError) as raised:
                    coding_process.CodingCancellation().run(
                        subprocess.run, [sys.executable, "-c", script],
                        capture_output=True, text=True, timeout=30,
                        inactivity_timeout=1.0, activity_root=str(Path(directory) / "none"),
                        stdin=subprocess.DEVNULL, shell=False, check=False,
                    )
            self.assertEqual(raised.exception.details["reason_code"], "session_stalled")
            child = int(pid_file.read_text())
            self.assertTrue(_gone(child), "the CLI's child survived the stop")

    def test_a_descendant_ignoring_sigterm_is_killed_after_the_cli_exits(self) -> None:
        """The CLI exiting does not mean its group stopped."""
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            stubborn = (
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            script = (
                "import subprocess, sys, time\n"
                f"child = subprocess.Popen([sys.executable, '-c', {stubborn!r}])\n"
                "time.sleep(0.3)\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            with self.assertRaises(CodingError) as raised:
                coding_process.CodingCancellation().run(
                    subprocess.run, [sys.executable, "-c", script],
                    capture_output=True, text=True, timeout=30,
                    inactivity_timeout=1.0, activity_root=str(Path(directory) / "none"),
                    stdin=subprocess.DEVNULL, shell=False, check=False,
                )
            self.assertEqual(raised.exception.details["reason_code"], "session_stalled")
            self.assertTrue(
                _gone(int(pid_file.read_text())), "a SIGTERM-ignoring child survived"
            )

    def test_a_failed_census_never_counts_as_an_empty_group(self) -> None:
        """`ps` unavailable: the SIGTERM-ignoring child is still killed."""
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            stubborn = (
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(60)"
            )
            script = (
                "import subprocess, sys, time\n"
                f"child = subprocess.Popen([sys.executable, '-c', {stubborn!r}])\n"
                "time.sleep(0.3)\n"
                f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            with mock.patch.object(
                coding_process, "_group_members", return_value=((), False)
            ):
                with self.assertRaises(CodingError) as raised:
                    coding_process.CodingCancellation().run(
                        subprocess.run, [sys.executable, "-c", script],
                        capture_output=True, text=True, timeout=30,
                        inactivity_timeout=1.0, activity_root=str(Path(directory) / "none"),
                        stdin=subprocess.DEVNULL, shell=False, check=False,
                    )
            self.assertEqual(raised.exception.details["reason_code"], "session_stalled")
            self.assertTrue(
                _gone(int(pid_file.read_text())), "a failed census hid a live child"
            )

    def test_a_process_that_cannot_be_stopped_is_reported_within_a_bound(self) -> None:
        """Both signals refused: a declared failure, not an endless wait."""
        real_kill = os.kill
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "self.pid"
            script = (
                "import os, time\n"
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                "time.sleep(60)\n"
            )
            try:
                with mock.patch.object(
                    coding_process.os, "killpg", side_effect=PermissionError(1, "EPERM")
                ), mock.patch.object(
                    coding_process.os, "kill", side_effect=PermissionError(1, "EPERM")
                ), mock.patch.object(coding_process, "STOP_WAIT_SECONDS", 0.5):
                    with self.assertRaises(CodingError) as raised:
                        coding_process.CodingCancellation().run(
                            subprocess.run, [sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=30,
                            inactivity_timeout=0.5, activity_root=str(Path(directory) / "none"),
                            stdin=subprocess.DEVNULL, shell=False, check=False,
                        )
                self.assertEqual(raised.exception.code, "session_interrupted")
                self.assertEqual(raised.exception.details["reason_code"], "session_unstoppable")
            finally:
                if pid_file.exists():
                    try:
                        real_kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass


def _gone(pid: int, seconds: float = 3.0) -> bool:
    """Whether `pid` no longer exists, allowing a moment for it to exit."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


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
