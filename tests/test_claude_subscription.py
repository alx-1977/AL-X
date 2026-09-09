"""The Claude subscription reasoner: fails closed, and never bills.

Reasoning calls fake the process boundary. A local help-only probe checks the
installed CLI contract without making a model call or consuming tokens.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.contracts import ModelMessage, ModelRequest, ModelRole  # noqa: E402
from alx.providers.claude_subscription import (  # noqa: E402
    PROVIDER_NAME,
    ClaudeSubscriptionReasoningModel,
)
from alx.providers.errors import ProviderError  # noqa: E402


DECISION = {
    "action": {"type": "respond", "text": "hello"},
    "goal_id": None,
    "goal_update": None,
}


def _request() -> ModelRequest:
    return ModelRequest(
        (
            ModelMessage(ModelRole.SYSTEM, "the laws"),
            ModelMessage(ModelRole.SYSTEM, "the protocol"),
            ModelMessage(ModelRole.USER, "what is the time"),
        ),
        "alx_core_decision",
        {"type": "object", "properties": {"action": {"type": "object"}}},
        "conversation-1",
    )


class _Recorder:
    """A fake `subprocess.run` that records exactly how it was called."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(
            command, self.returncode, self.stdout, self.stderr
        )


def _envelope(structured, prose="Here is my answer.", **extra) -> str:
    """One real `--output-format json --json-schema` envelope.

    Copied from what the CLI actually returns, verified against
    `claude --print --output-format json --json-schema ...` on 2026-09-08:
    the schema-conformant object arrives in `structured_output`, while
    `result` carries the assistant's prose for the same turn. An earlier
    version of this helper put the decision in `result`, which is why the
    suite passed while every live Core turn failed as malformed.
    """
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": prose,
        "structured_output": structured,
        "session_id": "5673292a-8ed7-4535-8409-f65c3c908535",
        "total_cost_usd": 0.0999955,
        "usage": {"input_tokens": 2500, "output_tokens": 94},
        "modelUsage": {
            # The CLI's own helper model appears beside the one that answered.
            "claude-haiku-4-5-20251001": {"inputTokens": 442, "outputTokens": 12},
            "claude-opus-4-8": {"inputTokens": 2500, "outputTokens": 94},
        },
    }
    envelope.update(extra)
    return json.dumps(envelope)


def _legacy_envelope(result) -> str:
    """An envelope with no structured field, decision in `result`."""
    return json.dumps({"subtype": "success", "is_error": False, "result": result})


class SuccessTest(unittest.TestCase):
    def test_a_structured_decision_is_returned(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        completion = model.complete(_request())

        self.assertEqual(completion.provider, PROVIDER_NAME)
        self.assertEqual(completion.output["action"]["type"], "respond")

    def test_the_structured_field_is_read_not_the_prose(self) -> None:
        """The defect the first live test found: `result` is not the decision."""
        runner = _Recorder(
            _envelope(DECISION, prose="Hi Friedl, I am AL/X.")
        )
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        completion = model.complete(_request())

        self.assertEqual(completion.output["action"]["type"], "respond")

    def test_the_answering_model_is_reported_not_the_helper(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        completion = model.complete(_request())

        self.assertEqual(completion.model, "claude-opus-4-8")

    def test_a_decision_in_result_is_read_when_no_structured_field(self) -> None:
        """Fallback for an envelope carrying no structured output."""
        runner = _Recorder(_legacy_envelope(json.dumps(DECISION)))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        completion = model.complete(_request())

        self.assertEqual(completion.output["action"]["type"], "respond")

    def test_the_core_schema_is_handed_to_the_cli(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        request = _request()

        model.complete(request)

        command = runner.calls[0]["command"]
        schema = command[command.index("--json-schema") + 1]
        self.assertEqual(json.loads(schema), dict(request.output_schema))

    def test_every_system_message_reaches_the_system_prompt(self) -> None:
        """The Laws and her identity are who is reasoning; none may be dropped."""
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())

        command = runner.calls[0]["command"]
        prompt = command[command.index("--system-prompt") + 1]
        self.assertIn("the laws", prompt)
        self.assertIn("the protocol", prompt)

    def test_the_turn_content_travels_on_stdin(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())

        self.assertEqual(runner.calls[0]["input"], "what is the time")


class StatelessInvocationTest(unittest.TestCase):
    """AL/X's durable goal state is the only continuity there is."""

    def test_every_turn_disables_cli_session_persistence(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())
        model.complete(_request())

        for call in runner.calls:
            self.assertEqual(call["command"].count("--no-session-persistence"), 1)

    def test_no_conversation_is_continued_or_resumed(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())

        command = runner.calls[0]["command"]
        for flag in ("--continue", "-c", "--resume", "-r", "--session-id"):
            self.assertNotIn(flag, command)

    def test_two_turns_share_no_session(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())
        model.complete(_request())

        self.assertEqual(runner.calls[0]["command"], runner.calls[1]["command"])


class NoClaudeCapabilitiesTest(unittest.TestCase):
    """It reasons. It does not act."""

    def test_command_requests_no_builtin_or_mcp_tools(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())

        command = runner.calls[0]["command"]
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertEqual(json.loads(command[command.index("--mcp-config") + 1]),
                         {"mcpServers": {}})
        self.assertNotIn("--allowed-tools", command)
        self.assertNotIn("--disallowed-tools", command)

    def test_installed_cli_help_documents_isolation_contract(self) -> None:
        executable = shutil.which("claude")
        if executable is None:
            self.skipTest("unverified on this CLI: claude is not installed")
        model = ClaudeSubscriptionReasoningModel("opus", 60)
        with tempfile.TemporaryDirectory(prefix="alx-claude-help-") as cwd:
            result = subprocess.run(
                [executable, "--help"], cwd=cwd, env=model.child_environment(),
                capture_output=True, text=True, timeout=15, check=True,
            )
        def option(flag: str) -> tuple[str, set[str]]:
            lines = result.stdout.splitlines()
            start = next(
                (index for index, line in enumerate(lines)
                 if re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|$)", line)),
                None,
            )
            self.assertIsNotNone(start, f"installed Claude CLI lacks {flag}")
            block = [lines[start]]
            for line in lines[start + 1:]:
                if re.match(r"^  (?:-\w, )?--[a-z]", line):
                    break
                block.append(line)
            description = " ".join(" ".join(block).split()).lower()
            return description, set(re.findall(r"[a-z]+", description))

        tools, tool_words = option("--tools")
        self.assertIn('""', tools)
        self.assertTrue({"disable", "all", "tools"} <= tool_words)
        _strict_mcp, strict_mcp_words = option("--strict-mcp-config")
        self.assertTrue(
            {"only", "mcp", "servers", "ignoring", "other", "configurations"}
            <= strict_mcp_words
        )
        _mcp, mcp_words = option("--mcp-config")
        self.assertTrue({"load", "mcp", "servers", "json", "strings"} <= mcp_words)
        _sources, source_words = option("--setting-sources")
        self.assertTrue({"user", "project", "local"} <= source_words)
        _persistence, persistence_words = option("--no-session-persistence")
        self.assertTrue(
            {"disable", "persistence", "saved", "resumed", "print"}
            <= persistence_words
        )

    def test_private_empty_cwd_is_unique_and_cleaned_on_every_exit(self) -> None:
        directories = []
        for failure in (None, subprocess.TimeoutExpired("claude", 1),
                        FileNotFoundError(), OSError("failed")):
            with self.subTest(failure=failure):
                def runner(command, **kwargs):
                    cwd = Path(kwargs["cwd"])
                    directories.append(cwd)
                    self.assertTrue(cwd.is_dir())
                    self.assertEqual(list(cwd.iterdir()), [])
                    self.assertEqual(cwd.stat().st_mode & 0o777, 0o700)
                    self.assertNotEqual(cwd.resolve(), REPOSITORY_ROOT)
                    self.assertNotEqual(cwd.resolve(), Path.home())
                    (cwd / "child-output").write_text("temporary")
                    if failure is not None:
                        raise failure
                    return subprocess.CompletedProcess(command, 0, _envelope(DECISION), "")
                model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
                if failure is None:
                    model.complete(_request())
                else:
                    with self.assertRaises(ProviderError):
                        model.complete(_request())
                self.assertFalse(directories[-1].exists())
        self.assertEqual(len(set(directories)), len(directories))

    def test_host_settings_are_not_loaded(self) -> None:
        """No CLAUDE.md, agents or plugins become a second instruction source."""
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())

        command = runner.calls[0]["command"]
        self.assertEqual(command[command.index("--setting-sources") + 1], "")

    def test_no_shell_is_used(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        model.complete(_request())

        self.assertIs(runner.calls[0]["shell"], False)


class NoMeteredBillingTest(unittest.TestCase):
    """The requirement this adapter exists for."""

    def test_an_anthropic_api_key_is_stripped_from_the_child(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel(
            "opus",
            60,
            runner=runner,
            environment={
                "ANTHROPIC_API_KEY": "sk-ant-should-never-be-passed",
                "PATH": "/usr/bin",
            },
        )

        model.complete(_request())

        passed = runner.calls[0]["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", passed)
        self.assertNotIn(
            "sk-ant-should-never-be-passed", json.dumps(passed)
        )
        # The rest of the environment survives, so the CLI can still find its
        # own subscription credentials.
        self.assertEqual(passed["PATH"], "/usr/bin")

    def test_every_metered_redirect_is_stripped(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel(
            "opus",
            60,
            runner=runner,
            environment={
                "ANTHROPIC_AUTH_TOKEN": "token",
                "ANTHROPIC_BASE_URL": "https://example.invalid",
                "ANTHROPIC_API_URL": "https://example.invalid",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "CLAUDE_CODE_USE_VERTEX": "1",
                "CLAUDE_CODE_USE_FOUNDRY": "1",
                "HOME": "/Users/friedl",
            },
        )

        model.complete(_request())

        passed = runner.calls[0]["env"]
        for name in (
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_API_URL",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        ):
            self.assertNotIn(name, passed)
        self.assertEqual(passed["HOME"], "/Users/friedl")

    def test_cloud_and_metered_credentials_never_reach_the_child(self) -> None:
        names = (
            "OPENAI_API_KEY", "XAI_API_KEY", "KIMI_API_KEY", "MOONSHOT_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY",
            "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "AZURE_OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
            "AWS_PROFILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_BEARER_TOKEN_BEDROCK",
            "GOOGLE_APPLICATION_CREDENTIALS", "ANTHROPIC_VERTEX_PROJECT_ID",
            "AZURE_CLIENT_SECRET", "AZURE_CLIENT_ID", "AZURE_TENANT_ID",
            "ANTHROPIC_FOUNDRY_API_KEY", "ALX_REASONING_API_KEY",
            "UNKNOWN_FUTURE_PROVIDER_KEY", "NODE_OPTIONS",
        )
        environment = dict.fromkeys(names, "must-not-inherit")
        environment.update(HOME="/subscription/home", PATH="/usr/bin",
                           CLAUDE_CODE_OAUTH_TOKEN="subscription-only")
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner,
                                                 environment=environment)
        model.complete(_request())
        self.assertEqual(runner.calls[0]["env"], {
            "HOME": "/subscription/home", "PATH": "/usr/bin",
            "CLAUDE_CODE_OAUTH_TOKEN": "subscription-only",
        })

    def test_the_subscription_oauth_token_is_preserved(self) -> None:
        """`claude setup-token` is a supported subscription credential."""
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel(
            "opus",
            60,
            runner=runner,
            environment={"CLAUDE_CODE_OAUTH_TOKEN": "oauth-value"},
        )

        model.complete(_request())

        self.assertEqual(
            runner.calls[0]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-value"
        )

    def test_the_adapter_holds_no_api_key_field(self) -> None:
        model = ClaudeSubscriptionReasoningModel("opus", 60)
        self.assertFalse(
            [name for name in vars(model) if "api_key" in name or "token" in name]
        )

    def test_a_failure_never_reaches_another_provider(self) -> None:
        """No fallback: a failed turn raises and stops."""
        runner = _Recorder("", "usage limit reached", returncode=1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.provider, PROVIDER_NAME)
        self.assertEqual(len(runner.calls), 1)

    def test_bounded_research_is_refused(self) -> None:
        """Research settles a dollar reservation from measured usage."""
        model = ClaudeSubscriptionReasoningModel("opus", 60)
        self.assertFalse(model.supports_bounded_research)


class FailClosedTest(unittest.TestCase):
    def test_a_usage_limit_is_a_provider_failure(self) -> None:
        runner = _Recorder("", "Claude usage limit reached. Resets at 5pm", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "subscription_usage_exhausted")

    def test_a_rate_limit_is_a_provider_failure(self) -> None:
        runner = _Recorder("", "429 too many requests", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "subscription_usage_exhausted")

    def test_missing_authentication_is_a_provider_failure(self) -> None:
        runner = _Recorder("", "Not logged in. Please run /login", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "subscription_unauthenticated")

    def test_a_missing_cli_is_a_provider_failure(self) -> None:
        def missing(command, **kwargs):
            raise FileNotFoundError(command[0])

        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=missing)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "cli_not_installed")

    def test_a_timeout_is_a_provider_failure(self) -> None:
        def slow(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=slow)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "reasoning_timeout")

    def test_the_configured_timeout_is_given_to_the_process(self) -> None:
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 45, runner=runner)

        model.complete(_request())

        self.assertEqual(runner.calls[0]["timeout"], 45)

    def test_an_error_envelope_is_a_provider_failure(self) -> None:
        runner = _Recorder(
            json.dumps({"is_error": True, "result": "usage limit reached"})
        )
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "subscription_usage_exhausted")

    def test_a_failure_carries_no_prompt(self) -> None:
        """D-012: a failure must not carry the turn that caused it."""
        runner = _Recorder("", "not logged in", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        try:
            model.complete(_request())
        except ProviderError as error:
            self.assertIsNone(error.__cause__)
            self.assertIsNone(error.__context__)
            self.assertNotIn("what is the time", str(error))
        else:
            self.fail("expected a provider failure")


class MalformedOutputTest(unittest.TestCase):
    def test_output_that_is_not_json_fails(self) -> None:
        runner = _Recorder("I cannot do that")
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "response_json_invalid")

    def test_a_decision_that_is_not_an_object_fails(self) -> None:
        runner = _Recorder(_envelope(["not", "an", "object"]))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "structured_output_not_object")

    def test_a_missing_result_fails(self) -> None:
        runner = _Recorder(json.dumps({"subtype": "success", "is_error": False}))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "response_result_missing")

    def test_unparseable_structured_text_fails(self) -> None:
        runner = _Recorder(_legacy_envelope("{not json"))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "structured_json_invalid")

    def test_a_non_success_subtype_fails(self) -> None:
        runner = _Recorder(
            json.dumps(
                {"subtype": "error_max_turns", "is_error": False, "result": {}}
            )
        )
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "cli_failed")

    def test_a_malicious_subtype_cannot_reach_error_reason_or_logs(self) -> None:
        malicious = "error\nFORGED LOG\x1b[31m\tsecret"
        runner = _Recorder(
            json.dumps(
                {"subtype": malicious, "is_error": False, "result": {}}
            )
        )
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertLogs(
            "alx.providers.claude_subscription", level="INFO"
        ) as logs:
            with self.assertRaises(ProviderError) as caught:
                model.complete(_request())

        self.assertEqual(caught.exception.reason, "cli_failed")
        log_output = "\n".join(logs.output)
        self.assertIn("cli_failed", log_output)
        self.assertNotIn("FORGED LOG", log_output)
        self.assertNotIn("secret", log_output)
        self.assertNotIn("\x1b", caught.exception.reason)


class TerminalLatchingTest(unittest.TestCase):
    """An exhausted subscription must not be re-asked once per turn."""

    def test_exhaustion_latches_and_refuses_without_a_second_process(self) -> None:
        runner = _Recorder("", "usage limit reached", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError):
            model.complete(_request())
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "subscription_usage_exhausted")
        self.assertEqual(len(runner.calls), 1, "the CLI was started again")

    def test_ensure_available_refuses_before_a_turn_is_built(self) -> None:
        runner = _Recorder("", "usage limit reached", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        with self.assertRaises(ProviderError):
            model.complete(_request())

        with self.assertRaises(ProviderError):
            model.ensure_available()

    def test_exhaustion_clears_when_the_window_rolls_over(self) -> None:
        now = [1000.0]
        runner = _Recorder("", "usage limit reached", 1)
        model = ClaudeSubscriptionReasoningModel(
            "opus",
            60,
            runner=runner,
            terminal_failure_cooldown_seconds=900.0,
            clock=lambda: now[0],
        )
        with self.assertRaises(ProviderError):
            model.complete(_request())

        now[0] += 901.0
        runner.returncode = 0
        runner.stdout = _envelope(DECISION)
        runner.stderr = ""

        completion = model.complete(_request())
        self.assertEqual(completion.provider, PROVIDER_NAME)

    def test_an_authentication_failure_does_not_expire(self) -> None:
        """Only Friedl can fix it; retrying on a timer only spawns processes."""
        now = [1000.0]
        runner = _Recorder("", "not logged in", 1)
        model = ClaudeSubscriptionReasoningModel(
            "opus", 60, runner=runner, clock=lambda: now[0]
        )
        with self.assertRaises(ProviderError):
            model.complete(_request())

        now[0] += 100_000.0
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "subscription_unauthenticated")
        self.assertEqual(len(runner.calls), 1)

    def test_an_ordinary_failure_does_not_latch(self) -> None:
        """A malformed answer is not a reason to stop trying to think."""
        runner = _Recorder("garbage")
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        with self.assertRaises(ProviderError):
            model.complete(_request())

        runner.stdout = _envelope(DECISION)
        completion = model.complete(_request())

        self.assertEqual(completion.provider, PROVIDER_NAME)
        self.assertEqual(len(runner.calls), 2)

    def test_no_retry_happens_inside_one_turn(self) -> None:
        runner = _Recorder("", "cli exploded", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError):
            model.complete(_request())

        self.assertEqual(len(runner.calls), 1)


class MalformationLatchTest(unittest.TestCase):
    """A provider that cannot produce a decision must stop being asked.

    The first live test spent eleven Opus calls in five minutes because the
    Core step loop retried a malformed answer it could not diagnose. The loop
    cannot see the pattern; the provider can.
    """

    def test_two_consecutive_malformed_answers_latch(self) -> None:
        runner = _Recorder(_envelope("not an object"))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        with self.assertRaises(ProviderError):
            model.complete(_request())
        with self.assertRaises(ProviderError):
            model.complete(_request())
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "provider_output_unusable")
        self.assertEqual(
            len(runner.calls), 2, "a third subscription call was made"
        )

    def test_the_live_failure_shape_latches(self) -> None:
        """Exactly what the first live test hit: prose where a decision belongs."""
        runner = _Recorder(_legacy_envelope("Hi Friedl, I am AL/X."))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        for _ in range(2):
            with self.assertRaises(ProviderError):
                model.complete(_request())
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "provider_output_unusable")
        self.assertEqual(len(runner.calls), 2)

    def test_a_missing_required_field_is_malformed(self) -> None:
        runner = _Recorder(_envelope({"action": {"type": "respond"}}))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        request = ModelRequest(
            (ModelMessage(ModelRole.USER, "hello"),),
            "alx_core_decision",
            {"type": "object", "required": ["action", "goal_id", "goal_update"]},
        )

        with self.assertRaises(ProviderError) as caught:
            model.complete(request)

        self.assertEqual(caught.exception.reason, "decision_schema_unsatisfied")

    def test_one_malformed_answer_does_not_latch(self) -> None:
        """A single bad turn is noise, not a broken provider."""
        runner = _Recorder(_envelope("not an object"))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        with self.assertRaises(ProviderError):
            model.complete(_request())

        runner.stdout = _envelope(DECISION)
        completion = model.complete(_request())

        self.assertEqual(completion.provider, PROVIDER_NAME)

    def test_a_success_resets_the_counter(self) -> None:
        runner = _Recorder(_envelope("not an object"))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        with self.assertRaises(ProviderError):
            model.complete(_request())

        runner.stdout = _envelope(DECISION)
        model.complete(_request())

        # One more malformed answer must not latch: the run was broken.
        runner.stdout = _envelope("not an object")
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())
        self.assertNotEqual(caught.exception.reason, "provider_output_unusable")

    def test_the_latch_does_not_expire(self) -> None:
        """Nothing changes on a timer; Friedl restarts once it is fixed."""
        now = [1000.0]
        runner = _Recorder(_envelope("not an object"))
        model = ClaudeSubscriptionReasoningModel(
            "opus", 60, runner=runner, clock=lambda: now[0]
        )
        for _ in range(2):
            with self.assertRaises(ProviderError):
                model.complete(_request())

        now[0] += 100_000.0
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())

        self.assertEqual(caught.exception.reason, "provider_output_unusable")
        self.assertEqual(len(runner.calls), 2)

    def test_auth_and_exhaustion_latching_are_unchanged(self) -> None:
        """Fix 3: the existing terminal semantics still hold."""
        runner = _Recorder("", "usage limit reached", 1)
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)
        with self.assertRaises(ProviderError) as caught:
            model.complete(_request())
        self.assertEqual(caught.exception.reason, "subscription_usage_exhausted")

        auth_runner = _Recorder("", "not logged in", 1)
        auth_model = ClaudeSubscriptionReasoningModel(
            "opus", 60, runner=auth_runner
        )
        with self.assertRaises(ProviderError) as caught:
            auth_model.complete(_request())
        self.assertEqual(
            caught.exception.reason, "subscription_unauthenticated"
        )
        # Still latched after one, as before: neither waits for two.
        with self.assertRaises(ProviderError):
            auth_model.complete(_request())
        self.assertEqual(len(auth_runner.calls), 1)


class UsageReportingTest(unittest.TestCase):
    def test_reported_tokens_are_carried_through(self) -> None:
        """The CLI does report tokens, so they are recorded rather than zeroed."""
        runner = _Recorder(_envelope(DECISION))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        completion = model.complete(_request())

        self.assertEqual(completion.usage["input_tokens"], 2500)
        self.assertEqual(completion.usage["output_tokens"], 94)

    def test_an_absent_usage_report_is_unmeasured(self) -> None:
        from alx.contracts.usage import is_measured

        runner = _Recorder(_legacy_envelope(json.dumps(DECISION)))
        model = ClaudeSubscriptionReasoningModel("opus", 60, runner=runner)

        completion = model.complete(_request())

        self.assertFalse(is_measured(completion.usage))

    def test_no_dollar_reservation_can_settle_from_this_path(self) -> None:
        """Tokens are reported, but this path is still refused for research."""
        model = ClaudeSubscriptionReasoningModel("opus", 60)
        self.assertFalse(model.supports_bounded_research)


class ConfigurationTest(unittest.TestCase):
    """Selecting the provider, and refusing a configuration that would bill."""

    BASE = {
        "ALX_REASONING_PROVIDER": "claude_subscription",
        "ALX_REASONING_MODEL": "opus",
        "ALX_STT_PROVIDER": "cartesia",
        "ALX_STT_MODEL": "ink-whisper",
        "ALX_STT_API_KEY": "stt",
        "ALX_STT_API_VERSION": "2024-11-13",
        "ALX_STT_TURN_START_THRESHOLD": "0.7",
        "ALX_STT_TURN_EAGER_END_THRESHOLD": "0.4",
        "ALX_STT_TURN_END_THRESHOLD": "0.1",
        "ALX_STT_TURN_END_TIMEOUT_MS": "1000",
        "ALX_TTS_PROVIDER": "elevenlabs",
        "ALX_TTS_MODEL": "eleven_v3",
        "ALX_TTS_API_KEY": "tts",
        "ALX_TTS_VOICE_ID": "voice",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_ID": "dictionary",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID": "version",
    }

    def _settings(self, **overrides):
        from alx.config.settings import RuntimeSettings

        environment = {**self.BASE, **overrides}
        return RuntimeSettings.from_environment(environment)

    def test_the_core_reasoner_is_selected_by_name(self) -> None:
        settings = self._settings()
        self.assertEqual(settings.reasoning.provider, "claude_subscription")

    def test_no_api_key_or_base_url_is_required_or_held(self) -> None:
        settings = self._settings()
        self.assertEqual(settings.reasoning.api_key, "")
        self.assertEqual(settings.reasoning.base_url, "")

    def test_a_configured_api_key_is_refused(self) -> None:
        """A billable key in this path is a mistake, not something to ignore."""
        from alx.config import ConfigurationError

        with self.assertRaises(ConfigurationError):
            self._settings(ALX_REASONING_API_KEY="sk-ant-leftover")

    def test_the_specialist_defaults_to_absent_not_to_the_subscription(self) -> None:
        """It must not inherit the Core's provider, and must need no key."""
        settings = self._settings()
        self.assertEqual(settings.specialist.provider, "none")
        self.assertEqual(settings.specialist.api_key, "")

    def test_the_specialist_may_not_be_pointed_at_the_subscription(self) -> None:
        """Named explicitly, it is refused rather than silently disabled."""
        from alx.config import ConfigurationError

        with self.assertRaises(ConfigurationError):
            self._settings(
                ALX_SPECIALIST_PROVIDER="claude_subscription",
                ALX_SPECIALIST_MODEL="opus",
            )

    def test_autonomous_cognition_is_untouched(self) -> None:
        """This change is scoped to the conversational Core."""
        settings = self._settings()
        self.assertIsNone(settings.autonomous)

    def test_the_existing_api_providers_remain_selectable(self) -> None:
        settings = self._settings(
            ALX_REASONING_PROVIDER="openai",
            ALX_REASONING_MODEL="gpt-5.6-sol",
            OPENAI_API_KEY="sk-core",
        )
        self.assertEqual(settings.reasoning.provider, "openai")
        self.assertEqual(settings.reasoning.api_key, "sk-core")

    def test_bootstrap_builds_the_subscription_reasoner(self) -> None:
        from alx.bootstrap.providers import build_runtime_providers

        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            providers = build_runtime_providers(self._settings())
        self.assertIsInstance(
            providers.reasoning, ClaudeSubscriptionReasoningModel
        )


class ResearchTierIsolationTest(unittest.TestCase):
    def _settings(self, **overrides):
        from alx.config.settings import RuntimeSettings
        return RuntimeSettings.from_environment({
            **ZeroMeteredApiConfigurationTest.ENVIRONMENT, **overrides,
        })

    def test_enabled_tiers_cannot_inherit_disabled_specialist(self):
        from alx.config import ConfigurationError
        for tier in ("survey", "compare", "judge"):
            with self.subTest(tier=tier), self.assertRaises(ConfigurationError):
                self._settings(ALX_RESEARCH_ENABLED_TIERS=tier)

    def test_disabled_tier_does_not_require_a_usable_transport(self):
        settings = self._settings(
            ALX_RESEARCH_SURVEY_PROVIDER="openai",
            ALX_RESEARCH_SURVEY_MODEL="",
            OPENAI_API_KEY="",
        )

        self.assertEqual(settings.research.enabled_tiers, frozenset())
        self.assertEqual(settings.research.survey.provider, "none")
        self.assertEqual(settings.research.survey.model, "none")
        self.assertEqual(settings.research.survey.api_key, "")

    def test_same_unusable_tier_fails_when_enabled(self):
        from alx.config import ConfigurationError

        with self.assertRaises(ConfigurationError):
            self._settings(
                ALX_RESEARCH_ENABLED_TIERS="survey",
                ALX_RESEARCH_SURVEY_PROVIDER="openai",
                ALX_RESEARCH_SURVEY_MODEL="",
                OPENAI_API_KEY="",
            )

    def test_enabled_valid_tier_keeps_its_configured_transport(self):
        settings = self._settings(
            ALX_RESEARCH_ENABLED_TIERS="survey",
            ALX_RESEARCH_SURVEY_PROVIDER="openai",
            ALX_RESEARCH_SURVEY_MODEL="configured-model",
            OPENAI_API_KEY="fake-key",
        )

        self.assertEqual(settings.research.survey.provider, "openai")
        self.assertEqual(settings.research.survey.model, "configured-model")
        self.assertEqual(settings.research.survey.api_key, "fake-key")

    def test_explicit_provider_requires_model_and_credential(self):
        from alx.config import ConfigurationError
        for tier in ("survey", "compare", "judge"):
            prefix = "ALX_RESEARCH_" + tier.upper()
            base = {"ALX_RESEARCH_ENABLED_TIERS": tier, prefix + "_PROVIDER": "openai"}
            for extra in ({}, {prefix + "_MODEL": "configured-model"},
                          {"OPENAI_API_KEY": "fake-key"},
                          {prefix + "_MODEL": "none", "OPENAI_API_KEY": "fake-key"}):
                with self.subTest(tier=tier, extra=extra), self.assertRaises(ConfigurationError):
                    self._settings(**base, **extra)
            settings = self._settings(**base, **{
                prefix + "_MODEL": "configured-model", "OPENAI_API_KEY": "fake-key"})
            configured = getattr(settings.research, tier)
            self.assertEqual(configured.model, "configured-model")
            self.assertEqual(configured.api_key, "fake-key")

    def test_other_core_provider_does_not_require_claude(self):
        from alx.bootstrap.providers import build_runtime_providers
        settings = self._settings(ALX_REASONING_PROVIDER="openai",
                                  ALX_REASONING_MODEL="configured-model",
                                  OPENAI_API_KEY="fake-key")
        with patch("alx.bootstrap.providers.subscription_cli_present",
                   side_effect=AssertionError("must not inspect Claude")):
            self.assertIsNotNone(build_runtime_providers(settings).reasoning)

    def test_subscription_startup_still_refuses_missing_cli(self):
        from alx.bootstrap.providers import build_runtime_providers
        from alx.config import ConfigurationError
        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=False):
            with self.assertRaises(ConfigurationError):
                build_runtime_providers(self._settings())


class ZeroMeteredApiConfigurationTest(unittest.TestCase):
    """The first live test must be incapable of spending metered credit.

    The whole runtime is composed from an environment holding no API key of
    any kind, and every metered adapter class is asserted absent from what was
    built - not merely unused.
    """

    ENVIRONMENT = {
        "ALX_REASONING_PROVIDER": "claude_subscription",
        "ALX_REASONING_MODEL": "opus",
        "ALX_STT_PROVIDER": "cartesia",
        "ALX_STT_MODEL": "ink-whisper",
        "ALX_STT_API_KEY": "stt-transport-only",
        "ALX_STT_API_VERSION": "2024-11-13",
        "ALX_STT_TURN_START_THRESHOLD": "0.7",
        "ALX_STT_TURN_EAGER_END_THRESHOLD": "0.4",
        "ALX_STT_TURN_END_THRESHOLD": "0.1",
        "ALX_STT_TURN_END_TIMEOUT_MS": "1000",
        "ALX_TTS_PROVIDER": "none",
        "ALX_TTS_MODEL": "none",
        "ALX_TTS_API_KEY": "none",
        "ALX_TTS_VOICE_ID": "none",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_ID": "none",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID": "none",
    }

    def _providers(self):
        from alx.bootstrap.providers import build_runtime_providers
        from alx.config.settings import RuntimeSettings

        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            return build_runtime_providers(
                RuntimeSettings.from_environment(dict(self.ENVIRONMENT))
            )

    def test_the_runtime_starts_with_no_api_key_of_any_kind(self) -> None:
        for name in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "XAI_API_KEY",
            "KIMI_API_KEY",
            "ALX_REASONING_API_KEY",
            "ALX_SPECIALIST_API_KEY",
        ):
            self.assertNotIn(name, self.ENVIRONMENT)
        self.assertIsNotNone(self._providers())

    def test_the_core_reasoner_is_the_subscription(self) -> None:
        self.assertIsInstance(
            self._providers().reasoning, ClaudeSubscriptionReasoningModel
        )

    def test_no_metered_provider_is_constructed(self) -> None:
        from alx.providers import OpenAIReasoningModel, XAIReasoningModel

        providers = self._providers()
        for built in (
            providers.reasoning,
            providers.specialist,
            providers.autonomous,
            providers.coding,
        ):
            self.assertNotIsInstance(built, OpenAIReasoningModel)
            self.assertNotIsInstance(built, XAIReasoningModel)

    def test_the_specialist_is_absent_rather_than_metered(self) -> None:
        self.assertIsNone(self._providers().specialist)

    def test_autonomous_cognition_is_disabled(self) -> None:
        self.assertIsNone(self._providers().autonomous)

    def test_research_is_disabled_and_builds_no_tier(self) -> None:
        """Paid research is off until a tier is named, so none is built."""
        from alx.config.settings import RuntimeSettings

        settings = RuntimeSettings.from_environment(dict(self.ENVIRONMENT))
        self.assertEqual(settings.research.enabled_tiers, frozenset())

    def test_an_ordinary_turn_reaches_the_subscription_provider(self) -> None:
        """The Core's own request, through the real adapter, faked at exec."""
        from alx.bootstrap.providers import build_runtime_providers
        from alx.config.settings import RuntimeSettings

        runner = _Recorder(_envelope(DECISION))
        with (
            patch("alx.bootstrap.providers.subscription_cli_present", return_value=True),
            patch("alx.providers.claude_subscription.subprocess.run", runner),
            patch("alx.providers.claude_subscription.os.environ", {"PATH": "/usr/bin"}),
        ):
            providers = build_runtime_providers(
                RuntimeSettings.from_environment(dict(self.ENVIRONMENT))
            )
            completion = providers.reasoning.complete(_request())

        self.assertEqual(completion.provider, PROVIDER_NAME)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0]["command"][0], "claude")
        self.assertNotIn("ANTHROPIC_API_KEY", runner.calls[0]["env"])


if __name__ == "__main__":
    unittest.main()
