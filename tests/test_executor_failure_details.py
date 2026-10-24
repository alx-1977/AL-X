"""Provider diagnostics survive the production capability boundary."""
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (
    CapabilityCall, GoalState, MailAccessError, MailAttachment, Objective,
    SpecialistError, SuccessCriterion, XeroAccessError,
)
from alx.contracts.diagnostics import MAX_SERVICE_MESSAGE, failure_details
from alx.contracts.dhl import DhlDocumentError
from alx.contracts.pull_request_checks import CheckReadError
from alx.contracts.readers import ReaderAccessError
from alx.contracts.review_content import ReviewReadError
from alx.contracts.sandbox import SandboxError
from alx.contracts.web import WebSearchError
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.goals import SQLiteGoalStore
from alx.providers.llamaparse import LlamaParseExtractor
from alx.safety import AuthorityContext, AuthorityPolicy, SafetyGate
from alx.tools.documents import DEFINITION, build_document_executors
from alx.tools.dhl import build_dhl_executors
from alx.tools.particle import build_particle_executors
from alx.tools.pull_request_checks import build_pull_request_checks_executors
from alx.tools.readers import build_reader_executors
from alx.tools.research import build_research_executors
from alx.tools.review_content import build_review_content_executors
from alx.tools.sandbox import build_sandbox_executors
from alx.tools.web import build_web_search_executors
from alx.tools.ship24 import build_ship24_executors

NOW = datetime.now(UTC)
SOURCE = dict(mailbox_id="INBOX", uid_validity="1", uid="2",
              attachment_id="3", expected_sha256="abc")
MESSAGE = "Service refused; token=private-token " + "quota exceeded " * 200


def mail_account():
    account = Mock()
    account.read_attachment.return_value = (
        MailAttachment("3", "invoice.pdf", "application/pdf", 4, "abc", ""), b"%PDF")
    return account


def diagnostic(error):
    # Code-only provider contracts may explicitly supply diagnostics too.
    error.service_message = MESSAGE
    return error


@pytest.mark.parametrize("stage", ["upload", "extraction", "mail"])
def test_document_provider_diagnostic_is_stored_through_production_executor(stage):
    mail = mail_account()
    client = Mock()
    extractor = LlamaParseExtractor("key", data_schema={"type": "object"},
                                   instruction="Extract fields", client=client)
    if stage == "mail":
        error = MailAccessError("connection_failed", MESSAGE)
        mail.read_attachment.side_effect = error
    else:
        error = SpecialistError("provider_failed", MESSAGE)
        client.request.side_effect = (
            [httpx.Response(400, json={"error": {"message": MESSAGE}})]
            if stage == "upload" else [
                httpx.Response(200, json={"id": "file"}),
                httpx.Response(200, json={"id": "job"}),
                httpx.Response(200, json={"status": "FAILED", "error": {"message": MESSAGE}})])
    executors = build_document_executors(mail, extractor.read, lambda: "call")
    broker = CapabilityBroker(CapabilityRegistry((DEFINITION,)),
                              SafetyGate({"read_mail_document": AuthorityPolicy(frozenset())}),
                              executors)
    attempt = broker.dispatch(CapabilityCall("call", "read_mail_document", SOURCE),
                              AuthorityContext("friedl", frozenset(), NOW))
    assert attempt.result.failure == {"code": error.code, **failure_details(error)}
    assert "[masked]" in attempt.result.failure["service_message"]
    assert "private-token" not in str(attempt.result.failure)
    assert len(attempt.result.failure["service_message"]) <= MAX_SERVICE_MESSAGE
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "goals.sqlite3"
        store = SQLiteGoalStore(path)
        store.create(GoalState("g", Objective("turn:1", "Inspect failure"),
                               (SuccessCriterion("seen", "Failure inspected"),),
                               attempts=(attempt,)), "conversation", NOW + timedelta(days=30))
        store.close()
        store = SQLiteGoalStore(path)
        try:
            assert store.load("g").state.attempts[0].result.failure == attempt.result.failure
        finally:
            store.close()
        assert b"private-token" not in path.read_bytes()


@pytest.mark.parametrize("error_type", [MailAccessError, DhlDocumentError, XeroAccessError])
def test_dhl_provider_failures_keep_details_and_classified_documents(error_type):
    error = diagnostic(error_type("connection_failed" if error_type is MailAccessError
                                  else "worksheet_invalid" if error_type is DhlDocumentError
                                  else "provider_failed"))
    mail, analyzer = mail_account(), Mock()
    if error_type is MailAccessError:
        mail.read_attachment.side_effect = error
    else:
        analyzer.classify.side_effect = error
    result = build_dhl_executors(mail, analyzer, Mock(), lambda: "call", "1", "2", "3")[
        "process_dhl_import"]({"documents": [SOURCE]})
    assert result.failure == {"code": error.code, **failure_details(error)}
    if error_type is DhlDocumentError:
        assert result.values["documents"] == ({"attachment_id": "3", "kind": "unreadable"},)


@pytest.mark.parametrize("capability,method,arguments", [
    ("particle_api_request", "api", {"method": "POST", "path": "/v1/devices"}),
    ("read_particle_usage", "usage", {"start": "2026-10-01", "end": "2026-10-02"}),
])
def test_particle_provider_failures_keep_details(capability, method, arguments):
    error = diagnostic(ReaderAccessError("connection_failed"))
    particle = Mock()
    getattr(particle, method).side_effect = error
    result = build_particle_executors(particle, Mock(), lambda: "call")[capability](arguments)
    assert result.failure == {"code": error.code, **failure_details(error)}


@pytest.mark.parametrize("stage", ["fleet", "config", "send"])
def test_reader_provider_failures_keep_details(stage):
    error = diagnostic(ReaderAccessError("connection_failed"))
    fleet, configs, calendar, control = Mock(), Mock(), Mock(), Mock()
    device = Mock(reader_uid="12345678", device_id="a" * 24, product_id=1,
                  online=True, last_heard="")
    device.name = "Reader"
    fleet.devices.return_value = [device]
    reader = dict(reader_uid="12345678", device_id="a" * 24, product_id=1, mode=0, room="Room")
    calendar.snapshot.return_value = (NOW.isoformat(), (), (reader,), ())
    if stage == "fleet":
        fleet.devices.side_effect = error
    elif stage == "config":
        configs.config.side_effect = error
    else:
        control.call_function.side_effect = error
    executors = build_reader_executors(fleet, configs, calendar, (1,), lambda: "call",
                                       clock=lambda: NOW, control=control)
    result = executors["send_reader_schedule" if stage == "send" else "refresh_reader_calendar"](
        {"reader_uid": "12345678"})
    if stage == "config":
        assert result.values["readers"][0]["service_message"] == failure_details(error)["service_message"]
    else:
        assert result.failure["service_message"] == failure_details(error)["service_message"]


@pytest.mark.parametrize("kind", ["research", "sandbox", "review", "checks", "changed_head", "search", "ship24"])
def test_other_code_only_provider_handlers_keep_explicit_diagnostics(kind):
    arguments = {"pull_request_number": 179, "head_sha": "a" * 40}
    if kind == "research":
        error = SpecialistError("provider_failed", MESSAGE)
        provider = Mock()
        provider.answer.side_effect = error
        executors = build_research_executors(provider, lambda: "call")
        arguments = dict(question_id="q", instruction="Inspect", material="Evidence")
    elif kind == "sandbox":
        error = diagnostic(SandboxError("sandbox_unavailable"))
        executors = build_sandbox_executors(Mock(side_effect=error), lambda: "call", lambda: "run")
        arguments = dict(experiment_id="e", session_id="s", source="print(1)")
    elif kind == "review":
        error = diagnostic(ReviewReadError("review_unavailable"))
        executors = build_review_content_executors(Mock(side_effect=error), lambda: "call")
    elif kind in ("checks", "changed_head"):
        error = diagnostic(CheckReadError("head_changed" if kind == "changed_head" else "permission_denied",
                                         **({"actual_head": "b" * 40} if kind == "changed_head" else {})))
        executors = build_pull_request_checks_executors(Mock(side_effect=error), lambda: "call")
    elif kind == "ship24":
        from alx.providers.ship24 import Ship24Error
        error = diagnostic(Ship24Error("provider_failed"))
        provider = Mock()
        provider.track.side_effect = error
        executors = build_ship24_executors(provider, lambda: "call")
        arguments = dict(tracking_number="123456")
    else:
        error = diagnostic(WebSearchError("search_provider_failed", "status 500"))
        provider = Mock()
        provider.search.side_effect = error
        executors = build_web_search_executors(provider, Mock(), lambda: "call", "search")
        arguments = dict(search_id="s", subject="subject")
    result = next(iter(executors.values()))(arguments)
    assert result.failure["service_message"] == failure_details(error)["service_message"]
    assert "private-token" not in str(result.failure)
    if kind == "changed_head":
        assert result.failure["actual_head"] == "b" * 40


@pytest.mark.parametrize("kind", ["ordinary", "research"])
def test_specialist_wrappers_preserve_explicit_provider_diagnostics(kind):
    from alx.contracts import Cognition, SpecialistQuestion
    from alx.specialists.runner import ModelSpecialist
    from alx.specialists.research import ResearchSpecialist, ResearchTierModel

    error = diagnostic(RuntimeError("raw exception text must not travel"))
    transport = Mock(supports_bounded_research=True)
    transport.complete.side_effect = error
    if kind == "ordinary":
        specialist = ModelSpecialist(transport)
        question = SpecialistQuestion("q", "Inspect", "Evidence", {"type": "object"})
        with pytest.raises(SpecialistError) as caught:
            specialist.answer(question)
        assert failure_details(caught.value) == failure_details(error)
    else:
        ledger, pricing = Mock(), Mock()
        ledger.overrun_usd.return_value = 0
        ledger.remaining_usd.return_value = 1
        ledger.reserve.return_value = Mock(reservation_id="reservation", reserved_usd=0.01)
        pricing.worst_case_usd.return_value = 0.01
        specialist = ResearchSpecialist(
            {Cognition.SURVEY: ResearchTierModel("provider", "model", transport)},
            ledger, pricing, 8000, 1000, 0.1)
        result = build_research_executors(specialist, lambda: "call")["ask_research_question"](
            dict(question_id="q", instruction="Inspect", material="Evidence"))
        transport.complete.assert_called_once()
        assert result.failure == {"code": "provider_failed", **failure_details(error)}
