"""Regression coverage for self-repair observability gaps 5 and 6."""
from datetime import UTC, datetime, timedelta
import logging
import os
import socket
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
from alx.contracts.diagnostics import MAX_SERVICE_MESSAGE, service_message, service_response_message
from alx.contracts.repository_authority import RepositoryAuthorityError
from alx.contracts.web import WebRetrievalError
from alx.goals import SQLiteGoalStore
from alx.observability.self_record import RuntimeLogHandler, read_log, runtime_log_paths
from alx.providers.web_fetch import HttpWebFetchProvider
from alx.providers.llamaparse import _json_object
from alx.contracts import SpecialistError
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.tools.web import build_web_executors
from alx.tools.goal_history import READ_OWN_GOALS, build_goal_history_executors
from alx.tools.self_diagnosis import LOG_DEFINITION, READ_OWN_LOG, build_self_diagnosis_executors

ROOT = Path(__file__).resolve().parents[1]


class ServiceFailureTests(unittest.TestCase):
    def test_service_message_is_masked_bounded_stored_and_read_back(self):
        message = "Service quota exhausted; api_key=sk-test-abcdefghijk " + "quota exceeded " * 200
        from alx.providers.xero import _raise_clean, _validation_messages
        from alx.contracts import XeroAccessError
        from alx.tools.xero import build_xero_executors, SEARCH_CONTACTS_DEFINITION, SEARCH_XERO_CONTACTS
        response = httpx.Response(400, json={
            "Elements": [{"ValidationErrors": [{"Message": message}]}]})
        with self.assertRaises(XeroAccessError) as caught:
            _raise_clean("request_rejected", "; ".join(_validation_messages(response)))
        error = caught.exception
        self.assertIn("Service quota exhausted", error.service_message)
        self.assertNotIn("sk-test-abcdefghijk", error.service_message)
        self.assertIn("[masked]", error.service_message)
        self.assertLessEqual(len(error.service_message), MAX_SERVICE_MESSAGE)

        account = Mock()
        account.search_contacts.side_effect = error
        broker = CapabilityBroker(
            CapabilityRegistry((SEARCH_CONTACTS_DEFINITION,)),
            SafetyGate({SEARCH_XERO_CONTACTS: AuthorityPolicy(frozenset({"xero.read"}))}),
            build_xero_executors(account, Mock(), lambda: "call"))
        now = datetime.now(UTC)
        attempt = broker.dispatch(
            CapabilityCall("call", SEARCH_XERO_CONTACTS, {"search_term": "supplier"}),
            AuthorityContext("friedl", frozenset({"xero.read"}), now))
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

    def test_web_http_failures_never_read_or_return_page_content(self):
        distinctive = b"UNTRUSTED PAGE TEXT MUST NEVER REACH FAILURES"

        class PageStream(httpx.SyncByteStream):
            reads = 0

            def __iter__(self):
                self.reads += 1
                yield distinctive

        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                streams = []

                def respond(request):
                    stream = PageStream()
                    streams.append(stream)
                    return httpx.Response(status, stream=stream)

                def resolver(host, port):
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                             "", ("93.184.216.34", port))]

                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    provider = HttpWebFetchProvider(client=client, resolver=resolver)
                    with self.assertRaises(WebRetrievalError) as caught:
                        provider.fetch("https://example.com")
                    error = caught.exception
                    code = "retrieval_blocked" if status in (401, 403, 429) else "provider_failed"
                    self.assertEqual(error.code, code)
                    self.assertEqual(error.detail, f"status {status}")
                    self.assertEqual(error.service_message, f"status {status}")
                    self.assertNotIn(distinctive.decode(), str(vars(error)))
                    result = build_web_executors(provider, lambda: "call")["ask_web_page"](
                        {"page_id": "p", "url": "https://example.com"})
                    self.assertEqual(result.failure, {"code": code, "service_message": f"status {status}"})
                    self.assertNotIn(distinctive.decode(), str(result.failure))
                    self.assertEqual([stream.reads for stream in streams], [0, 0])

    def test_web_service_message_allows_only_http_status_text(self):
        for detail in ("redirect loop", "status 404 untrusted content", "page body"):
            with self.subTest(detail=detail):
                self.assertEqual(WebRetrievalError("provider_failed", detail).service_message, "")

    def test_non_json_markup_is_not_a_service_message(self):
        for body in ("  <div>failure</div>", "proxy error <HTML>failure</HTML>",
                     "proxy error <!DOCTYPE html>", b"\n<html>failure</html>"):
            with self.subTest(body=body):
                self.assertEqual(service_response_message(body), "")
        self.assertEqual(service_response_message("Service unavailable"), "Service unavailable")
        self.assertEqual(service_response_message('{"error":{"message":"quota exceeded"}}'),
                         "quota exceeded")

    def test_repository_refusal_preserves_sha_in_detail_and_exception(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        detail = f"Refusing revision {sha}; token=private-token " + "reason " * 200
        error = RepositoryAuthorityError("operation_refused", detail)
        self.assertEqual(error.detail, detail)
        self.assertEqual(str(error), detail)
        self.assertNotIn(sha, error.service_message)
        self.assertNotIn("private-token", error.service_message)
        self.assertIn("[masked]", error.service_message)
        self.assertLessEqual(len(error.service_message), MAX_SERVICE_MESSAGE)
        self.assertEqual(str(RepositoryAuthorityError("operation_refused")), "operation_refused")

    def test_imap_select_and_fetch_failure_replies_are_decoded(self):
        from alx.contracts import MailAccessError, MailReference
        from alx.providers.icloud_mail import ICloudMailAdapter
        reply = [b"Mailbox unavailable", b"bad byte: \xff", "try later"]
        adapter = ICloudMailAdapter("imap.example.com", 993, "me@example.com", "unused", Mock(), 30)
        reference = MailReference("INBOX", "1", "2")
        operations = (
            lambda: adapter.scan(),
            lambda: adapter._read_parsed(reference),
            lambda: adapter.search(Mock(mailbox_id="INBOX")),
            lambda: adapter.file_message(reference, "Archive"),
            lambda: adapter.move_to_trash(reference),
            lambda: adapter.mark_seen(reference),
            lambda: adapter.set_flag_colour(reference, "orange"),
            lambda: adapter.flagged_messages("INBOX", "orange"),
        )
        for operation in operations:
            connection = Mock()
            connection.select.return_value = ("NO", reply)
            with self.subTest(operation=operation), patch.object(adapter, "_open", return_value=connection):
                with self.assertRaises(MailAccessError) as caught:
                    operation()
                self.assertEqual(caught.exception.service_message,
                                 "Mailbox unavailable; bad byte: \ufffd; try later")
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.response.return_value = ("OK", [b"1"])
        connection.uid.return_value = ("NO", reply)
        with patch.object(adapter, "_open", return_value=connection):
            with self.assertRaises(MailAccessError) as caught:
                adapter._read_parsed(reference)
        self.assertEqual(caught.exception.service_message,
                         "Mailbox unavailable; bad byte: \ufffd; try later")


class RetainedLogTests(unittest.TestCase):
    def test_newest_matching_entries_are_kept_in_time_order_with_stable_ties(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime.now().astimezone().replace(microsecond=0)
            paths = (root / "new.log", root / "old.log")
            for path, records in zip(paths, (
                    ((4, "newest"), (2, "earlier tie"), (3, "middle")),
                    ((0, "oldest"), (2, "later tie"), (1, "older")))):
                path.write_text("".join(
                    (now + timedelta(seconds=offset)).strftime("%Y-%m-%d %H:%M:%S")
                    + f",000 ERROR alx.test: {message}\n" for offset, message in records))
            for limit, expected in (
                    (0, []), (3, ["later tie", "middle", "newest"]),
                    (4, ["earlier tie", "later tie", "middle", "newest"]),
                    (6, ["oldest", "older", "earlier tie", "later tie", "middle", "newest"])):
                with self.subTest(limit=limit):
                    entries, truncated = read_log(paths, now - timedelta(seconds=1),
                                                  now + timedelta(seconds=5), limit=limit)
                    self.assertEqual([entry["message"] for entry in entries], expected)
                    self.assertEqual(truncated, 6 > limit)
            with self.assertRaises(ValueError):
                read_log(paths, now, now + timedelta(seconds=5), limit=-1)

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
            launched = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
            self.assertIn(f"process capture {root / 'live_voice.process.log'} and runtime log "
                          f"{root / 'live_voice.log'}:", launched.stdout)
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
        self.assertIn("Process output is shown at ERROR level.", LOG_DEFINITION.purpose)
        self.assertNotIn("the current run and the one before", LOG_DEFINITION.purpose)

