"""Regression coverage for self-repair observability gaps 5 and 6."""
from datetime import UTC, datetime, timedelta
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.contracts import CapabilityCall, GoalState, Objective, SuccessCriterion
from alx.contracts.diagnostics import MAX_SERVICE_MESSAGE, service_message
from alx.contracts.web import WebRetrievalError
from alx.goals import SQLiteGoalStore
from alx.observability.self_record import RuntimeLogHandler, read_log, runtime_log_paths
from alx.providers.web_fetch import HttpWebFetchProvider
from alx.providers.llamaparse import _json_object
from alx.contracts import SpecialistError
from alx.safety import AuthorityContext, SafetyGate
from alx.bootstrap.web import build_web_runtime, WEB_READ_PERMISSION
from alx.tools.web import build_web_executors
from alx.tools.goal_history import READ_OWN_GOALS, build_goal_history_executors
from alx.tools.self_diagnosis import LOG_DEFINITION, READ_OWN_LOG, build_self_diagnosis_executors

ROOT = Path(__file__).resolve().parents[1]


class ServiceFailureTests(unittest.TestCase):
    def test_service_message_is_masked_bounded_stored_and_read_back(self):
        message = "Service quota exhausted; api_key=sk-test-abcdefghijk " + "quota exceeded " * 200
        response = httpx.Response(429, json={"error": {"message": message}})
        provider = HttpWebFetchProvider()
        with self.assertRaises(WebRetrievalError) as caught:
            provider._page("https://example.com", None, response, 100, 0)
        error = caught.exception
        self.assertIn("Service quota exhausted", error.service_message)
        self.assertNotIn("sk-test-abcdefghijk", error.service_message)
        self.assertIn("[masked]", error.service_message)
        self.assertLessEqual(len(error.service_message), MAX_SERVICE_MESSAGE)

        class Fetcher:
            def fetch(self, *args):
                raise error

        runtime = build_web_runtime(True, lambda: "call")
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions), SafetyGate(runtime.policies),
            build_web_executors(Fetcher(), lambda: "call"))
        now = datetime.now(UTC)
        attempt = broker.dispatch(
            CapabilityCall("call", "ask_web_page", {"page_id": "p", "url": "https://example.com"}),
            AuthorityContext("friedl", frozenset({WEB_READ_PERMISSION}), now))
        self.assertEqual(attempt.result.failure["service_message"], error.service_message)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "goals.sqlite3"
            store = SQLiteGoalStore(path)
            store.create(GoalState("g", Objective("turn:1", "Inspect failure"),
                                  (SuccessCriterion("seen", "Failure inspected"),),
                                  attempts=(attempt,)), "conversation", now + timedelta(days=30))
            store.close()
            self.assertNotIn(b"sk-test-abcdefghijk", path.read_bytes())
            store = SQLiteGoalStore(path)
            try:
                result = build_goal_history_executors(store, lambda: "read")[READ_OWN_GOALS]({"goal_id": "g"})
                self.assertIn(error.service_message, str(result.values))
                self.assertEqual(store.load("g").state.attempts[0].result.failure, attempt.result.failure)
            finally:
                store.close()

    def test_masking_happens_before_length_bound_and_only_error_fields_survive(self):
        value = "quota " * 164 + "sk-test-" + "a" * 200
        self.assertNotIn("sk-test", service_message(value))
        response = httpx.Response(400, json={
            "error": {"message": "Invalid extraction; token=private-token"},
            "request": {"document": "PRIVATE DOCUMENT"}})
        with self.assertRaises(SpecialistError) as caught:
            _json_object(response)
        self.assertEqual(caught.exception.service_message, "Invalid extraction; token=[masked]")
        self.assertNotIn("PRIVATE DOCUMENT", str(vars(caught.exception)))

    def test_xero_refusal_keeps_the_sanitized_service_message(self):
        from alx.providers.xero import _raise_clean, _validation_messages
        from alx.contracts import XeroAccessError
        from alx.tools.xero import build_xero_executors, SEARCH_XERO_CONTACTS
        message = "Due Date is required; api_key=private-key"
        response = Mock()
        response.json.return_value = {"Elements": [{"ValidationErrors": [{"Message": message}]}]}
        with self.assertRaises(XeroAccessError) as caught:
            _raise_clean("request_rejected", "; ".join(_validation_messages(response)))
        account = Mock()
        account.search_contacts.side_effect = caught.exception
        result = build_xero_executors(account, Mock(), lambda: "call")[SEARCH_XERO_CONTACTS](
            {"search_term": "supplier"})
        self.assertEqual(result.failure["service_message"],
                         "Due Date is required; api_key=[masked]")

    def test_smtp_refusal_keeps_only_the_sanitized_reply_without_the_exception_chain(self):
        import smtplib
        from alx.providers.icloud_mail_send import ICloudMailSender
        from alx.contracts import MailSendError
        sender = ICloudMailSender("smtp.example.com", 465, "me@example.com", "unused")
        connection = Mock()
        connection.send_message.side_effect = smtplib.SMTPDataError(
            550, b"Mailbox unavailable; token=private-token")
        with patch.object(sender, "_open", return_value=connection), patch.object(
                sender, "build_message", return_value=Mock()):
            reply = Mock(to=("you@example.com",), carbon_copy=())
            with self.assertRaises(MailSendError) as caught:
                sender.send_reply(reply)
        self.assertEqual(caught.exception.service_message,
                         "Mailbox unavailable; token=[masked]")
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("private-token", str(vars(caught.exception)))

    def test_quoted_credentials_are_masked(self):
        self.assertEqual(service_message('{"token": "private-token"}'),
                         '{"token": "[masked]"}')


class RetainedLogTests(unittest.TestCase):
    def test_thirty_day_window_and_cleanup_on_startup_or_rotation_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "live_voice.log"
            now = datetime.now().astimezone()
            for age in (0, 1, 29, 30, 31):
                archived = root / ("live_voice.log." + str(age))
                at = now - timedelta(days=age) + timedelta(seconds=5)
                archived.write_text(at.strftime("%Y-%m-%d %H:%M:%S") +
                                    ",000 ERROR alx.test: day " + str(age) + "\n")
                os.utime(archived, (at.timestamp(), at.timestamp()))
            handler = RuntimeLogHandler(path)
            try:
                self.assertFalse((root / "live_voice.log.31").exists())
                self.assertTrue((root / "live_voice.log.30").exists())
                reader = lambda *args: read_log(runtime_log_paths(path), *args)
                capability = build_self_diagnosis_executors(None, reader, lambda: "read", clock=lambda: now)
                result = capability[READ_OWN_LOG]({
                    "from": (now - timedelta(days=30)).isoformat(),
                    "to": (now + timedelta(minutes=1)).isoformat()})
                self.assertEqual(len(result.values["entries"]), 4)
                with patch.object(handler, "prune", wraps=handler.prune) as prune, patch.object(
                        Path, "glob", side_effect=AssertionError("directory scan during emit")):
                    for number in range(10):
                        handler.emit(logging.LogRecord("test", logging.ERROR, __file__, 1,
                                                       "record %d", (number,), None))
                    prune.assert_not_called()
                with patch.object(handler, "prune", wraps=handler.prune) as prune:
                    handler.doRollover()
                    prune.assert_called_once()
                self.assertTrue(any(p.name.startswith("live_voice.log.20") for p in runtime_log_paths(path)))
            finally:
                handler.close()

    def test_process_capture_archive_is_retained_readable_and_masked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now(UTC)
            for age in (29, 31):
                path = root / f"live_voice.process.log.{age}"
                path.write_text("before logging\nTraceback (most recent call last):\nRuntimeError: token=secret\n")
                at = (now - timedelta(days=age)).timestamp()
                os.utime(path, (at, at))
            handler = RuntimeLogHandler(root / "live_voice.log")
            handler.close()
            entries, _ = read_log(runtime_log_paths(root / "live_voice.log"),
                                  now - timedelta(days=30), now, "WARNING")
            self.assertEqual(len(entries), 1)
            self.assertIn("before logging", entries[0]["message"])
            self.assertIn("RuntimeError: token=[masked]", entries[0]["message"])
            self.assertEqual(entries[0]["timestamp_source"], "file_mtime")
            self.assertFalse((root / "live_voice.process.log.31").exists())


    def test_late_tracebacks_remain_readable_after_large_process_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live_voice.process.log"
            path.write_text(("startup diagnostic\n" * 500) +
                            "Traceback (most recent call last):\nRuntimeError: late failure\n")
            now = datetime.now(UTC)
            entries, _ = read_log((path,), now - timedelta(minutes=1),
                                  now + timedelta(minutes=1), contains="late failure")
            self.assertEqual(len(entries), 1)
            self.assertIn("RuntimeError: late failure", entries[0]["message"])

    def test_launcher_captures_output_before_logging_and_uncaught_traceback(self):
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src/alx/bootstrap"
            package.mkdir(parents=True)
            for parent in (package, package.parent):
                (parent / "__init__.py").write_text("")
            (package / "live_voice.py").write_text(
                'print("before logging", flush=True)\nraise RuntimeError("uncaught startup failure")\n')
            script = f"""
source {shlex.quote(str(ROOT / 'scripts/alx'))}
LOG={shlex.quote(str(root / 'live_voice.log'))}
CAPTURE={shlex.quote(str(root / 'live_voice.process.log'))}
STAMP={shlex.quote(str(root / 'config'))}
ROOT={shlex.quote(str(root))}
PYTHON={shlex.quote(sys.executable)}
pids() {{ :; }}
healthy() {{ return 1; }}
runtime_code() {{ echo "$ROOT"; }}
record_config() {{ :; }}
sleep() {{ command sleep 0.02; }}
start
wait
"""
            subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
            entries, _ = read_log(runtime_log_paths(root / "live_voice.log"),
                                  datetime.now(UTC) - timedelta(minutes=1),
                                  datetime.now(UTC) + timedelta(minutes=1), "WARNING")
            text = str(entries)
            self.assertIn("before logging", text)
            self.assertIn("Traceback", text)
            self.assertIn("RuntimeError: uncaught startup failure", text)
            # Python logs exclusively to the rotating file; no duplicate stream copy.
            source = (ROOT / "src/alx/bootstrap/live_voice.py").read_text()
            self.assertIn('handlers=[RuntimeLogHandler(', source)

    def test_log_capability_description_is_clean_and_matches_retention(self):
        self.assertIn("Logs are kept for 30 days.", LOG_DEFINITION.purpose)
        self.assertIn("The default window is the last 24 hours.", LOG_DEFINITION.purpose)
        self.assertIn("Untimestamped process output uses the file modification time", LOG_DEFINITION.purpose)
        self.assertNotIn("the current run and the one before", LOG_DEFINITION.purpose)

