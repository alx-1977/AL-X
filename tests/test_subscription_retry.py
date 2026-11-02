"""Subscription failures share one bounded policy and retain their diagnostics."""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ModelMessage, ModelRequest, ModelRole
from alx.contracts.coding import CodingError, CodingRequest
from alx.providers.claude_subscription import ClaudeSubscriptionReasoningModel
from alx.providers.codex_subscription import CodexSubscriptionReasoningModel
from alx.providers.grok_subscription import GrokSubscriptionReasoningModel
from alx.providers.coding_subscription_session import SubscriptionCodingSession
from alx.providers.errors import ProviderError
from alx.providers.subscription_retry import SubscriptionRetryPolicy, retry_delay


MODELS = (ClaudeSubscriptionReasoningModel, CodexSubscriptionReasoningModel,
          GrokSubscriptionReasoningModel)
RESET_MESSAGE = "You've hit your 5-hour limit · resets 6:30pm"
LOCAL = ZoneInfo("Africa/Johannesburg")


@pytest.fixture(autouse=True)
def no_real_login_files(monkeypatch):
    monkeypatch.setattr(GrokSubscriptionReasoningModel, "_install_subscription_auth",
                        lambda self, home: None)



def request():
    return ModelRequest((ModelMessage(ModelRole.USER, "structured request"),),
                        "subscription-test", {"type": "object"})


@pytest.mark.parametrize("model_type", MODELS)
def test_every_failure_retries_and_preserves_the_final_message(model_type):
    waits, calls = [], []
    final = "  Unexpected failure\n" + "word " * 400 + "\n  "

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", final if len(calls) == 3 else "earlier")

    model = model_type("model", 30, runner=runner, environment={},
                       retry_policy=SubscriptionRetryPolicy(wait=waits.append))
    with pytest.raises(ProviderError) as raised:
        model.complete(request())
    assert len(calls) == 3
    assert waits == [1, 2]
    assert raised.value.reason == "cli_failed"
    assert raised.value.details["service_message"] == final
    assert raised.value.details["retry_exhausted"] is True
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("model_type", MODELS)
@pytest.mark.parametrize("hour,delay", [(17, 5400), (19, 84600)])
def test_exact_claude_reset_message_uses_local_clock(model_type, hour, delay):
    waits = []
    policy = SubscriptionRetryPolicy(
        wait=waits.append, now=lambda: datetime(2026, 11, 2, hour, tzinfo=LOCAL))
    model = model_type("model", 30, environment={}, retry_policy=policy,
                       runner=lambda command, **kwargs: subprocess.CompletedProcess(
                           command, 1, "", RESET_MESSAGE))
    with pytest.raises(ProviderError) as raised:
        model.complete(request())
    assert waits == [delay, delay]
    assert raised.value.details["service_message"] == RESET_MESSAGE


@pytest.mark.parametrize("clock", ["resets 25:30", "resets 6:90pm", "resets 0pm",
                                  "resets 6:300pm", "resets 6:30xyz", "no clock"])
def test_missing_or_invalid_reset_clock_uses_backoff(clock):
    assert [retry_delay(clock, attempt) for attempt in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30]


@pytest.mark.parametrize("model_type", MODELS)
def test_a_successful_retry_returns_normally(model_type):
    calls, waits = [], []

    def runner(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return subprocess.CompletedProcess(command, 1, "", "arbitrary failure")
        output = (json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "{}"}})
            if model_type is CodexSubscriptionReasoningModel else
            json.dumps({"structured_output": {}}))
        return subprocess.CompletedProcess(command, 0, output, "")

    model = model_type("model", 30, runner=runner, environment={},
                       retry_policy=SubscriptionRetryPolicy(wait=waits.append))
    assert dict(model.complete(request()).output) == {}
    assert len(calls) == 2
    assert waits == [1]


@pytest.mark.parametrize("model_type", (ClaudeSubscriptionReasoningModel, GrokSubscriptionReasoningModel))
@pytest.mark.parametrize("exit_status", [0, 1])
def test_error_envelope_preserves_message_and_reset(model_type, exit_status):
    waits = []
    model = model_type("model", 30, environment={}, retry_policy=SubscriptionRetryPolicy(
        wait=waits.append, now=lambda: datetime(2026, 11, 2, 18, tzinfo=LOCAL)),
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, exit_status, json.dumps({"is_error": True, "result": RESET_MESSAGE}), "warning"))
    with pytest.raises(ProviderError) as raised:
        model.complete(request())
    assert waits == [1800, 1800]
    assert raised.value.details["service_message"] == RESET_MESSAGE


class FakeSession(SubscriptionCodingSession):
    def command(self, prompt_path, worktree):
        return ["fake"]

    def provider_environment(self, home):
        return {}

    def prepare_home(self, home, worktree, request):
        pass


def test_coding_session_uses_same_reset_policy_without_truncation(tmp_path):
    calls, waits = [], []
    message = " " + RESET_MESSAGE + "\n" + "details " * 10000 + "\n  "

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", message)

    session = FakeSession("model", executable="fake", stall_seconds=30, runner=runner,
                          retry_policy=SubscriptionRetryPolicy(wait=waits.append,
                              now=lambda: datetime(2026, 11, 2, 18, tzinfo=LOCAL)))
    with pytest.raises(CodingError) as raised:
        session.run_session(CodingRequest(task="task", job_id="job", worktree=str(tmp_path)), "briefing")
    assert len(calls) == 3
    assert waits == [1800, 1800]
    assert raised.value.details["service_message"] == message


def test_masking_is_the_only_change_to_failure_text():
    model = ClaudeSubscriptionReasoningModel("model", 30, environment={},
        retry_policy=SubscriptionRetryPolicy(wait=lambda seconds: None),
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", "  failure\npassword=secret\n  "))
    with pytest.raises(ProviderError) as raised:
        model.complete(request())
    assert raised.value.details["service_message"] == "  failure\npassword=[masked]\n  "


def test_reset_waits_still_have_a_finite_attempt_bound():
    now = [datetime(2026, 11, 2, 18, tzinfo=LOCAL)]
    waits = []

    def wait(seconds):
        waits.append(seconds)
        now[0] = datetime.fromtimestamp(now[0].timestamp() + seconds + 1, LOCAL)

    model = ClaudeSubscriptionReasoningModel("model", 30, environment={},
        retry_policy=SubscriptionRetryPolicy(wait=wait, now=lambda: now[0]),
        runner=lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, "", RESET_MESSAGE))
    with pytest.raises(ProviderError):
        model.complete(request())
    assert waits == [1800, 86399]


def test_zero_exit_codex_failure_event_keeps_the_exact_message():
    waits = []
    model = CodexSubscriptionReasoningModel("model", 30, environment={},
        retry_policy=SubscriptionRetryPolicy(wait=waits.append,
            now=lambda: datetime(2026, 11, 2, 18, tzinfo=LOCAL)),
        runner=lambda command, **kwargs: subprocess.CompletedProcess(command, 0,
            json.dumps({"type": "turn.failed", "error": {"message": RESET_MESSAGE}}), ""))
    with pytest.raises(ProviderError) as raised:
        model.complete(request())
    assert waits == [1800, 1800]
    assert raised.value.details["service_message"] == RESET_MESSAGE


def test_unsuccessful_session_result_retries_and_keeps_its_interface(tmp_path):
    from alx.contracts.coding import CodingSessionResult
    waits, calls = [], []

    class IncompleteSession(FakeSession):
        def read_result(self, stdout):
            return CodingSessionResult(False, "  incomplete account\n", failure_code="session_failed")

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    session = IncompleteSession("model", executable="fake", stall_seconds=30,
        runner=runner, retry_policy=SubscriptionRetryPolicy(wait=waits.append))
    result = session.run_session(CodingRequest("task", "job", str(tmp_path)), "briefing")
    assert not result.completed
    assert result.failure_code == "session_failed"
    assert result.diagnostics["service_message"] == "  incomplete account\n"
    assert len(calls) == 3
    assert waits == [1, 2]


def test_cancellation_does_not_start_another_session(tmp_path):
    waits, calls = [], []

    def runner(command, **kwargs):
        calls.append(command)
        raise CodingError("coding_cancelled")

    session = FakeSession("model", executable="fake", stall_seconds=30,
        runner=runner, retry_policy=SubscriptionRetryPolicy(wait=waits.append))
    with pytest.raises(CodingError, match="coding_cancelled"):
        session.run_session(CodingRequest("task", "job", str(tmp_path)), "briefing")
    assert len(calls) == 1
    assert waits == []


@pytest.mark.parametrize("research", [False, True])
def test_specialist_consumers_preserve_terminal_message_without_retrying_again(tmp_path, research):
    from alx.contracts import Cognition, ResearchQuestion, SpecialistError, SpecialistQuestion
    from alx.contracts.diagnostics import failure_details
    from alx.observability.pricing import ConfiguredPricingWorstCase
    from alx.observability.research_budget import ResearchBudget, SQLiteResearchLedger
    from alx.providers.claude_subscription import ClaudeSubscriptionResearchModel
    from alx.specialists.research import ResearchSpecialist, ResearchTierModel
    from alx.specialists.runner import ModelSpecialist

    message = "  CLI failure\n" + "detail " * 400 + "\n "
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, "", message)

    model = ClaudeSubscriptionResearchModel("claude-haiku-5-5", 30, environment={},
        runner=runner, retry_policy=SubscriptionRetryPolicy(wait=lambda seconds: None))
    question = SpecialistQuestion("q", "instruction", "material", {"type": "object"})
    if research:
        ledger = SQLiteResearchLedger(tmp_path / "research.sqlite3", ResearchBudget(
            daily_usd=1, per_request_max_usd=0.1, daily_calls=30))
        specialist = ResearchSpecialist({Cognition.SURVEY: ResearchTierModel(
            "claude_subscription", "claude-haiku-5-5", model)}, ledger,
            ConfiguredPricingWorstCase(), 8000, 1000, 0.1)
        question = ResearchQuestion(question)
    else:
        specialist = ModelSpecialist(model)
    with pytest.raises(SpecialistError) as raised:
        specialist.answer(question)
    assert len(calls) == 3
    assert failure_details(raised.value)["service_message"] == message


def test_removed_classifiers_and_latches_have_no_callable_path():
    from alx.providers import claude_subscription, codex_subscription, grok_subscription
    from alx.providers import coding_subscription_session, coding_session
    for module in (claude_subscription, codex_subscription, grok_subscription,
                   coding_subscription_session):
        assert not any("MARKERS" in name for name in vars(module))
    assert not hasattr(coding_subscription_session, "classify_failure")
    assert not hasattr(coding_session.GrokCodingSession, "_failure")
    for model_type in MODELS:
        assert not hasattr(model_type, "_failure_code")
    assert not hasattr(claude_subscription, "_COOLDOWN_CODES")
