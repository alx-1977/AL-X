"""While background work runs, the status bar shows it and nothing replaces it.

On 2026-10-05 a background coding job was still running when AL/X finished
speaking, and the bar fell back to "Listening". Coding telemetry reached the
browser only through a listener a person turn installed, and the bar was
written by whichever event arrived last. These tests run the real `app.js`
under Node: a running job holds the bar as `Stage · runtime · last activity`,
foreground speech and listening do not replace it, and the bar returns to the
foreground stage only when the job ends.
"""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_voice_playback_serialisation import NODE, run_js  # noqa: E402

import subprocess  # noqa: E402
import tempfile  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from unittest import mock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import CodingTelemetry  # noqa: E402
from alx.interfaces import VoiceActivityStatus, VoiceDiagnosticFeed  # noqa: E402
from alx.providers import coding_process  # noqa: E402

NOW = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)

JOB = """
const job = (overrides) => ({
  type: "diagnostic", code: "coding.status", job_id: "job-1", phase: "execution",
  started_at: new Date(Date.now() - 151000).toISOString(),
  last_activity_at: new Date(Date.now() - 8000).toISOString(),
  terminal: false, ...overrides,
});
"""


@unittest.skipIf(NODE is None, "node is required to execute the client logic")
class BackgroundStatusBarTests(unittest.TestCase):
    def test_a_running_job_holds_the_bar_through_speech_and_listening(self) -> None:
        result = run_js(JOB + textwrap.dedent("""
            handleControl(job({}));
            const coding = diagnosticStage.textContent;
            handleControl({ type: "phase", value: "speaking" });
            const speaking = diagnosticStage.textContent;
            handleControl({ type: "phase", value: "listening" });
            renderStage();
            const listening = diagnosticStage.textContent;
            console.log(JSON.stringify({ coding, speaking, listening }));
        """))
        for key in ("coding", "speaking", "listening"):
            with self.subTest(moment=key):
                self.assertRegex(
                    result[key], r"^Coding · 02:3[01] · last activity 00:0[78]$"
                )

    def test_the_stage_follows_the_job_and_the_bar_returns_when_it_ends(self) -> None:
        result = run_js(JOB + textwrap.dedent("""
            handleControl(job({ phase: "review" }));
            const review = diagnosticStage.textContent;
            handleControl({ type: "phase", value: "listening" });
            handleControl(job({ phase: "commit", terminal: true, outcome: "succeeded" }));
            const after = diagnosticStage.textContent;
            console.log(JSON.stringify({ review, after }));
        """))
        self.assertTrue(result["review"].startswith("Local review · "))
        self.assertEqual(result["after"], "Listening")

    def test_no_liveness_labels_are_shown(self) -> None:
        result = run_js(JOB + textwrap.dedent("""
            handleControl(job({ stalled: true, in_flight: true, unresponsive: true }));
            console.log(JSON.stringify({ text: diagnosticStage.textContent }));
        """))
        for word in ("STALLED", "ACTIVE", "QUIET", "WAITING", "WORKING"):
            self.assertNotIn(word, result["text"].upper())


@unittest.skipIf(NODE is None, "node is required to execute the client logic")
class ConsoleTidyTests(unittest.TestCase):
    """Friedl's 2026-10-06 console notes, run against the real app.js."""

    def test_red_means_a_failed_job_and_nothing_else(self) -> None:
        result = run_js(textwrap.dedent("""
            console.log(JSON.stringify({
              working: codingTone({ terminal: false }),
              quiet: codingTone({ terminal: false, stalled: true }),
              interrupted: codingTone({ terminal: true, outcome: "interrupted" }),
              cancelled: codingTone({ terminal: true, outcome: "cancelled" }),
              succeeded: codingTone({ terminal: true, outcome: "succeeded" }),
              failed: codingTone({ terminal: true, outcome: "failed" }),
            }));
        """))
        self.assertEqual(result, {
            "working": "active", "quiet": "warn", "interrupted": "warn",
            "cancelled": "warn", "succeeded": "active", "failed": "error",
        })

    def test_an_external_review_is_named_as_such_on_the_bar(self) -> None:
        result = run_js(textwrap.dedent("""
            handleControl({
              type: "diagnostic", code: "task.status", task_id: "task-1",
              state: "waiting_for_result", service: "coderabbit", subject: "PR 109",
              elapsed_seconds: 5, at: new Date().toISOString(),
            });
            renderStage();
            console.log(JSON.stringify({ text: diagnosticStage.textContent }));
        """))
        self.assertTrue(result["text"].startswith("External review · "), result["text"])

    def test_a_log_line_has_no_subsystem_column(self) -> None:
        result = run_js(textwrap.dedent("""
            const tags = [];
            document.createElement = (tag) => { tags.push(tag); return element(); };
            diagnostic("Reasoning started", "info", "SYSTEM", { subsystem: "CORE" });
            console.log(JSON.stringify({ tags }));
        """))
        self.assertNotIn("b", result["tags"])
        self.assertEqual(result["tags"], ["div", "time", "span"])

    def test_the_bar_lets_go_of_a_step_once_it_ends(self) -> None:
        """On 2026-10-06 it showed a finished step for four minutes."""
        result = run_js(textwrap.dedent("""
            setPhase("listening");
            const step = (status) => handleControl({
              type: "diagnostic", code: "trace", subsystem: "thoughts",
              label: "Withdraw carried thought", status, at: new Date().toISOString(),
            });
            step("started");
            const during = diagnosticStage.textContent;
            step("completed");
            const after = diagnosticStage.textContent;
            console.log(JSON.stringify({ during, after }));
        """))
        self.assertEqual(result["during"], "THOUGHTS · Withdraw carried thought")
        self.assertEqual(result["after"], "Listening")

    def test_the_console_has_no_stop_coding_control(self) -> None:
        """Stopping a job is AL/X's stop_coding_job, not a console button."""
        assets = Path(__file__).resolve().parents[1] / "src/alx/interfaces/assets"
        for name in ("index.html", "app.js", "app.css"):
            with self.subTest(asset=name):
                text = (assets / name).read_text()
                self.assertNotIn("coding-cancel", text)
                self.assertNotIn("coding.cancel", text)

class LiveCodingStatusTests(unittest.TestCase):
    """The server side: background coding reaches every console, live."""

    def _telemetry(self, **changes) -> CodingTelemetry:
        values = dict(job_id="job-1", phase="execution", started_at=NOW - timedelta(minutes=2),
                      phase_started_at=NOW - timedelta(minutes=1),
                      last_activity_at=NOW - timedelta(seconds=8), in_flight=True)
        values.update(changes)
        return CodingTelemetry(**values)

    def test_coding_status_is_published_without_any_person_turn(self) -> None:
        feed = VoiceDiagnosticFeed()
        received: list[dict] = []
        feed.subscribe(lambda _owner, event: received.append(event))
        VoiceActivityStatus(feed, clock=lambda: NOW).publish_coding(self._telemetry())
        (event,) = received
        self.assertEqual(event["code"], "coding.status")
        self.assertEqual(event["phase"], "execution")
        self.assertEqual(event["started_at"], (NOW - timedelta(minutes=2)).isoformat(timespec="milliseconds"))
        self.assertEqual(event["last_activity_at"], (NOW - timedelta(seconds=8)).isoformat(timespec="milliseconds"))

    def test_a_reconnecting_console_sees_a_running_job_but_not_a_finished_one(self) -> None:
        feed = VoiceDiagnosticFeed()
        activity = VoiceActivityStatus(feed, clock=lambda: NOW)
        activity.publish_coding(self._telemetry())
        _unsubscribe, replay = feed.subscribe(lambda _owner, _event: None)
        self.assertEqual([event["job_id"] for _owner, event in replay], ["job-1"])
        activity.publish_coding(self._telemetry(phase="complete", terminal=True, outcome="succeeded"))
        _unsubscribe, replay = feed.subscribe(lambda _owner, _event: None)
        self.assertEqual(replay, ())

    def test_session_activity_reaches_the_job_throttled(self) -> None:
        """Output from the running command is the activity the bar shows."""
        reports: list[float] = []
        script = "import time\nfor _ in range(12):\n    print('x', flush=True); time.sleep(0.1)\n"
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(coding_process, "ACTIVITY_REPORT_SECONDS", 0.5):
            cancellation = coding_process.CodingCancellation(
                on_activity=lambda: reports.append(1.0))
            cancellation.run(
                subprocess.run, [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=30, inactivity_timeout=10,
                activity_root=directory, stdin=subprocess.DEVNULL, shell=False, check=False,
            )
        # About 1.2 s of output at a 0.5 s throttle: reported, but not per line.
        self.assertGreaterEqual(len(reports), 1)
        self.assertLessEqual(len(reports), 4)


if __name__ == "__main__":
    unittest.main()
