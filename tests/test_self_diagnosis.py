"""AL/X reads her own trace and runtime log (D-050).

Friedl, 2026-10-15: two of her notebook searches were refused, the only record
was the trace on his screen, and asked whether anything had gone wrong she
said she could see nothing. "ALX should be self sustaining."
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS  # noqa: E402
from alx.observability.self_record import SQLiteTraceRecord, _mask, read_log  # noqa: E402
from alx.tools.self_diagnosis import (  # noqa: E402
    READ_OWN_LOG,
    READ_OWN_TRACE,
    build_self_diagnosis_executors,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def event(at, status="completed", label="Search research", reason=None, subsystem="notebook"):
    values = {"code": "trace", "at": at.isoformat(), "subsystem": subsystem,
              "status": status, "label": label, "reference": "search_research"}
    if reason:
        values["reason_code"] = reason
    return values


class TraceRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.record = SQLiteTraceRecord(Path(directory.name) / "trace.sqlite3")
        self.addCleanup(self.record.close)

    def test_she_can_find_her_refused_searches(self) -> None:
        self.record.record(event(NOW - timedelta(minutes=5)))
        self.record.record(event(NOW - timedelta(minutes=4), "failed", reason="arguments_unusable"))
        self.record.record({"code": "phase", "value": "listening"})  # not a trace step
        steps, truncated = self.record.between(NOW - timedelta(hours=1), NOW, problems_only=True)
        self.assertFalse(truncated)
        self.assertEqual([(step["status"], step["reason_code"]) for step in steps],
                         [("failed", "arguments_unusable")])
        everything, _ = self.record.between(NOW - timedelta(hours=1), NOW)
        self.assertEqual(len(everything), 2)

    def test_times_come_back_local_with_their_offset(self) -> None:
        """2026-10-17: her 10:09 own time reached her as 08:09+00:00, and she
        told Friedl it had not fired."""
        from datetime import timezone
        sast = timezone(timedelta(hours=2))
        own_time = datetime(2026, 10, 17, 8, 9, 39, tzinfo=UTC)
        self.record.record(event(own_time, label="Own time", subsystem="core"))
        steps, _ = self.record.between(own_time - timedelta(minutes=1),
                                       own_time + timedelta(minutes=1), zone=sast)
        self.assertEqual(steps[0]["at"], "2026-10-17T10:09:39+02:00")
        self.assertEqual(datetime.fromisoformat(steps[0]["at"]), own_time)

    def test_a_subsystem_and_a_limit_narrow_it(self) -> None:
        for minute in range(5):
            self.record.record(event(NOW - timedelta(minutes=minute), subsystem="mail"))
        self.record.record(event(NOW - timedelta(minutes=1)))
        steps, truncated = self.record.between(NOW - timedelta(hours=1), NOW, subsystem="mail", limit=3)
        self.assertEqual((len(steps), truncated), (3, True))


class LogTests(unittest.TestCase):
    def test_warnings_and_their_tracebacks_in_the_window(self) -> None:
        local = NOW.astimezone()
        stamp = lambda minutes: (local + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"{stamp(-120)},000 ERROR alx.old: before the window",
            f"{stamp(-10)},000 INFO alx.x: fine",
            f"{stamp(-5)},123 ERROR websockets.server: connection handler failed",
            "Traceback (most recent call last):",
            '  File "/lib/python3.13/site-packages/websockets/server.py", line 376',
            f"{stamp(-4)},000 WARNING alx.y: token=abcdef1234 refused",
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live_voice.log"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            entries, truncated = read_log((path,), NOW - timedelta(hours=1), NOW, "WARNING")
        self.assertFalse(truncated)
        self.assertEqual([entry["level"] for entry in entries], ["ERROR", "WARNING"])
        # Local, with the offset stated, the same instant the log wrote.
        stated = datetime.fromisoformat(entries[0]["at"])
        self.assertIsNotNone(stated.tzinfo)
        self.assertEqual(stated.replace(tzinfo=None),
                         (local + timedelta(minutes=-5)).replace(tzinfo=None, microsecond=0))
        self.assertIn("Traceback", entries[0]["message"])
        self.assertIn("site-packages/websockets/server.py", entries[0]["message"])
        self.assertIn("token=[masked]", entries[1]["message"])

    def test_only_the_newest_are_held_and_a_traceback_is_bounded(self) -> None:
        local = NOW.astimezone()
        stamp = (local - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"{stamp},000 ERROR alx.z: failure {n}" for n in range(10)]
        lines += [f"{stamp},000 ERROR alx.z: long", *["  frame line " + "x" * 300] * 100]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live_voice.log"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            entries, truncated = read_log((path,), NOW - timedelta(hours=1), NOW, "ERROR", limit=3)
        self.assertTrue(truncated)
        self.assertEqual([entry["message"][:9] for entry in entries[:2]], ["failure 8", "failure 9"])
        self.assertLessEqual(len(entries[-1]["message"]), 4000)

    def test_keys_are_masked_and_paths_are_not(self) -> None:
        self.assertEqual(_mask("Authorization: Bearer abc.def"), "Authorization: [masked] [masked]")
        self.assertIn("[masked]", _mask('api_key="sk-ant-api03-abcdefghijklmnop"'))
        path = "/Library/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages"
        self.assertEqual(_mask(path), path)


class CapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.record = SQLiteTraceRecord(Path(directory.name) / "trace.sqlite3")
        self.addCleanup(self.record.close)
        self.record.record(event(NOW - timedelta(minutes=4), "failed", reason="arguments_unusable"))
        self.seen = []

        def reader(start, end, level, contains, limit):
            self.seen.append((level, contains, limit))
            return [{"at": "x", "level": "ERROR", "source": "s", "message": "m"}], False

        self.run_ = build_self_diagnosis_executors(self.record, reader, lambda: "call-1",
                                                   clock=lambda: NOW)

    def test_she_reads_her_problems(self) -> None:
        result = self.run_[READ_OWN_TRACE]({"problems_only": True})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["entries"][0]["reason_code"], "arguments_unusable")

    def test_the_log_defaults_to_warnings(self) -> None:
        result = self.run_[READ_OWN_LOG]({})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(self.seen[0], ("WARNING", "", 500))

    def test_a_bad_window_says_why(self) -> None:
        result = self.run_[READ_OWN_TRACE]({"from": NOW.isoformat(), "to": (NOW - timedelta(hours=1)).isoformat()})
        self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertIn("later than", result.failure["reason"])

    def test_the_log_is_read_where_the_launcher_writes_it(self) -> None:
        root = Path(__file__).resolve().parents[1]
        launcher = (root / "scripts/alx").read_text()
        self.assertIn('LOG="$ROOT/.alx/runtime/live_voice.log"', launcher)
        composition = (root / "src/alx/bootstrap/live_voice.py").read_text()
        self.assertIn('repository_root / ".alx/runtime/live_voice.log"', composition)

    def test_she_knows_to_look(self) -> None:
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("read_own_trace lists every step you took", text)
        self.assertIn("look there before you answer", text)
        self.assertIn("Both give times in local time with their offset", text)

    def test_what_she_states_comes_from_what_she_checked(self) -> None:
        """Friedl, 2026-10-17: "when she gives me info, I do not want to have
        to double check that info, else I could have just done it." """
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("Friedl relies on what you tell him without checking it himself", text)
        self.assertIn("you say where you looked", text)
        self.assertIn("never fill a gap with what seems likely", text)


class PollingNoiseTests(unittest.TestCase):
    """The page's five-second polls are not written to her log (2026-10-16)."""

    def _record(self, status: int, phrase: str):
        import logging
        return logging.LogRecord("websockets.server", logging.INFO, __file__, 1,
                                 "connection rejected (%d %s)", (status, phrase), None)

    def test_an_answered_poll_is_dropped_and_a_real_refusal_is_kept(self) -> None:
        from alx.bootstrap.live_voice import _AnsweredPageRequests

        answered = _AnsweredPageRequests()
        self.assertFalse(answered.filter(self._record(200, "OK")))
        self.assertTrue(answered.filter(self._record(403, "Forbidden")))
        self.assertTrue(answered.filter(self._record(500, "Internal Server Error")))


if __name__ == "__main__":
    unittest.main()
