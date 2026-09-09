"""Grok CLI subscription coding transport: fails closed, never bills xAI."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.coding import build_coding_runtime  # noqa: E402
from alx.bootstrap.providers import build_runtime_providers  # noqa: E402
from alx.capabilities import CapabilityBroker, CapabilityRegistry  # noqa: E402
from alx.config.settings import RuntimeSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttemptDisposition,
    CodingSessionResult,
    CapabilityCall,
    CapabilityResultState,
    ModelMessage,
    ModelRequest,
    ModelRole,
)
from alx.providers import (  # noqa: E402
    ClaudeSubscriptionReasoningModel,
    GrokSubscriptionReasoningModel,
    OpenAIReasoningModel,
    XAIReasoningModel,
)
from alx.providers.errors import ProviderError  # noqa: E402
from alx.providers.grok_subscription import PROVIDER_NAME  # noqa: E402
from alx.providers.coding_agent import PLAN_SCHEMA  # noqa: E402
from alx.safety import AuthorityContext, SafetyGate  # noqa: E402
from alx.tools.coding import RUN_CODING_TASK  # noqa: E402
from datetime import UTC, datetime  # noqa: E402


NOW = datetime(2026, 9, 9, tzinfo=UTC)

DECISION = {
    "decision": "finish",
    "action_kind": "none",
    "path": "",
    "content": "",
    "command": [],
    "summary": "nothing to do",
    "unresolved_issues": [],
    "external_review_recommended": False,
    "status": "blocked",
}


def _request() -> ModelRequest:
    return ModelRequest(
        (
            ModelMessage(ModelRole.SYSTEM, "bounded coding worker"),
            ModelMessage(ModelRole.USER, json.dumps({"task": "inspect"})),
        ),
        "alx_coding_decision",
        {
            "type": "object",
            "properties": {"decision": {"type": "string"}},
            "required": ["decision"],
        },
        kind="coding",
    )


def _plan_request() -> ModelRequest:
    return ModelRequest(
        (
            ModelMessage(ModelRole.SYSTEM, "PLAN mode"),
            ModelMessage(ModelRole.USER, json.dumps({"phase": "planning"})),
        ),
        "alx_coding_plan",
        PLAN_SCHEMA,
        kind="coding",
    )


class _Recorder:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[dict] = []
        self.fail: BaseException | None = None

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        if self.fail is not None:
            raise self.fail
        return subprocess.CompletedProcess(
            command, self.returncode, self.stdout, self.stderr
        )


def _envelope(structured, **extra) -> str:
    envelope = {
        "text": "done",
        "stopReason": "EndTurn",
        "structured_output": structured,
        "sessionId": "session-1",
        "usage": {"input_tokens": 10, "output_tokens": 4},
        "model": "grok-4.6",
        "is_error": False,
    }
    envelope.update(extra)
    return json.dumps(envelope)


class _StubSession:
    """Stands in for the native coding session in transport-level tests."""

    def __init__(self, worktree: Path) -> None:
        self.worktree = worktree
        self.calls: list[str] = []

    def run_session(self, request, briefing):
        self.calls.append(briefing)
        (Path(request.worktree) / "app.py").write_text(
            "changed\n", encoding="utf-8"
        )
        return CodingSessionResult(
            completed=True, report="applied the bounded change", turns=4
        )


def _environment(**changes: str) -> dict[str, str]:
    values = {
        "ALX_REASONING_PROVIDER": "claude_subscription",
        "ALX_REASONING_MODEL": "opus",
        "ALX_STT_PROVIDER": "cartesia",
        "ALX_STT_MODEL": "stt-model",
        "ALX_STT_API_KEY": "stt-secret",
        "ALX_STT_API_VERSION": "stt-version",
        "ALX_STT_TURN_START_THRESHOLD": "0.7",
        "ALX_STT_TURN_EAGER_END_THRESHOLD": "0.5",
        "ALX_STT_TURN_END_THRESHOLD": "0.4",
        "ALX_STT_TURN_END_TIMEOUT_MS": "4500",
        "ALX_TTS_PROVIDER": "none",
        "ALX_TTS_MODEL": "none",
        "ALX_TTS_API_KEY": "none",
        "ALX_TTS_VOICE_ID": "none",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_ID": "none",
        "ALX_TTS_PRONUNCIATION_DICTIONARY_VERSION_ID": "none",
        "ALX_CODING_ENABLED": "true",
        "ALX_CODING_PROVIDER": "grok_subscription",
        "ALX_CODING_MODEL": "grok-4.6",
    }
    values.update(changes)
    return values


class GrokSubscriptionTransportTests(unittest.TestCase):
    def test_child_environment_strips_xai_api_key(self) -> None:
        model = GrokSubscriptionReasoningModel(
            "grok-4.6",
            30,
            environment={
                "PATH": "/usr/bin",
                "HOME": "/Users/friedl",
                "XAI_API_KEY": "must-not-reach-the-child",
                "OPENAI_API_KEY": "also-not",
                "ANTHROPIC_API_KEY": "nor-this",
            },
        )
        child = model.child_environment()
        self.assertNotIn("XAI_API_KEY", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("ANTHROPIC_API_KEY", child)
        self.assertEqual(child["PATH"], "/usr/bin")
        self.assertEqual(child["GROK_SUBAGENTS"], "0")

    def test_planning_command_is_headless_and_structured(self) -> None:
        """This transport carries the planning turn, not the coding session."""
        runner = _Recorder(_envelope({"decision": "finish"}))
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/usr/bin"}
        )
        model.complete(_request())
        command = runner.calls[0]["command"]
        self.assertEqual(command[0], "grok")
        self.assertIn("--prompt-file", command)
        self.assertIn("--json-schema", command)
        self.assertIn("--no-subagents", command)
        self.assertIn("--disable-web-search", command)
        # The planning turn runs in an empty temporary directory, so it needs
        # no tool grants at all. Execution is a separate native session.
        self.assertNotIn("--tools", command)
        self.assertNotIn("--max-turns", command)
        self.assertNotIn("--sandbox", command)
        self.assertNotIn("-c", command)
        self.assertNotIn("--continue", command)
        self.assertNotIn("--always-approve", command)
        self.assertNotIn("--worktree", command)
        self.assertNotIn("git", command)
        self.assertIs(runner.calls[0]["shell"], False)

    def test_xai_api_key_in_parent_does_not_reach_the_cli(self) -> None:
        runner = _Recorder(_envelope({"decision": "finish"}))
        model = GrokSubscriptionReasoningModel(
            "grok-4.6",
            30,
            runner=runner,
            environment={"PATH": "/bin", "HOME": "/tmp", "XAI_API_KEY": "secret"},
        )
        model.complete(_request())
        self.assertNotIn("XAI_API_KEY", runner.calls[0]["env"])
        self.assertTrue(runner.calls[0]["env"]["GROK_HOME"])

    def test_cli_failure_does_not_construct_or_call_an_api_client(self) -> None:
        runner = _Recorder()
        runner.fail = FileNotFoundError("grok")
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.provider, PROVIDER_NAME)
        self.assertEqual(raised.exception.reason, "cli_not_installed")

    def test_non_zero_exit_is_a_provider_failure(self) -> None:
        runner = _Recorder(stderr="not logged in", returncode=1)
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.reason, "subscription_unauthenticated")

    def test_nonzero_exit_reports_cli_failed_with_exit_status(self) -> None:
        runner = _Recorder(stderr="command failed", returncode=2)
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.reason, "cli_failed")
        self.assertEqual(raised.exception.details.get("exit_status"), 2)
        self.assertEqual(raised.exception.details.get("stderr_characters"), len("command failed"))
        self.assertNotIn("command failed", str(raised.exception.details))

    def test_scripted_cli_json_drives_a_coding_job(self) -> None:
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name)
        (root / "app.py").write_text("ok\n", encoding="utf-8")
        runner = _Recorder(_envelope(DECISION))
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        runtime = build_coding_runtime(
            True, model, lambda: "call-1", session=_StubSession(root)
        )
        broker = CapabilityBroker(
            CapabilityRegistry(runtime.definitions),
            SafetyGate(runtime.policies),
            runtime.executors,
        )
        attempt = broker.dispatch(
            CapabilityCall(
                "call-1",
                RUN_CODING_TASK,
                {"task": "inspect", "worktree": str(root)},
            ),
            AuthorityContext("friedl", runtime.permissions, NOW),
        )
        self.assertIs(attempt.disposition, CapabilityAttemptDisposition.EXECUTED)
        self.assertIs(attempt.result.state, CapabilityResultState.FAILED)
        self.assertEqual(attempt.result.values["status"], "failed")
        self.assertTrue(runner.calls)
        self.assertEqual(runner.calls[0]["command"][0], "grok")

    def test_installed_cli_help_documents_the_headless_contract(self) -> None:
        import os

        if os.environ.get("ALX_GROK_CLI_HELP_PROBE", "").strip().lower() not in {
            "1", "true", "yes",
        }:
            self.skipTest("set ALX_GROK_CLI_HELP_PROBE=1 to probe the installed CLI")
        executable = shutil.which("grok")
        if executable is None:
            self.skipTest("unverified on this CLI: grok is not installed")
        model = GrokSubscriptionReasoningModel("grok-4.6", 30)
        with tempfile.TemporaryDirectory(prefix="alx-grok-help-") as cwd:
            result = subprocess.run(
                [executable, "--help"],
                cwd=cwd,
                env=model.child_environment(),
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
        help_text = result.stdout.lower()
        self.assertIn("--single", help_text)
        self.assertIn("--json-schema", help_text)
        self.assertIn("--prompt-file", help_text)
        self.assertIn("--tools", help_text)
        self.assertIn("--disallowed-tools", help_text)
        self.assertNotIn("XAI_API_KEY", result.stdout)

    def test_live_cli_envelope_uses_camelcase_structured_output(self) -> None:
        """Observed grok 1.0.24 `--output-format json --json-schema` envelope."""
        observed = {
            "modelUsage": {},
            "num_turns": 1,
            "requestId": "req-1",
            "sessionId": "session-1",
            "stopReason": "end_turn",
            "structuredOutput": {
                "status": "probe_ok",
                "summary": "live grok subscription probe.",
            },
            "text": '{"status":"probe_ok","summary":"live grok subscription probe."}',
            "thought": "",
            "total_cost_usd": 0,
            "total_cost_usd_ticks": 0,
            "usage": {
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            },
        }
        runner = _Recorder(json.dumps(observed))
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        completion = model.complete(_request())
        self.assertEqual(completion.output["status"], "probe_ok")
        self.assertEqual(
            completion.output["summary"], "live grok subscription probe."
        )
        # Prefer the structured field over prose `text` when they diverge.
        diverged = dict(observed)
        diverged["text"] = '{"status":"wrong","summary":"from text"}'
        runner = _Recorder(json.dumps(diverged))
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        completion = model.complete(_request())
        self.assertEqual(completion.output["status"], "probe_ok")

    def test_real_grok_envelope_shape_preserves_a_plan_object(self) -> None:
        plan = {
            "problem_understanding": "inspect the bounded task",
            "hypotheses": ["the continuation is incomplete"],
            "inspection_targets": ["src/alx/core"],
            "intended_changes": ["correct the continuation state"],
            "verification": ["run targeted tests"],
            "risks_constraints": ["no shell authority"],
            "more_context_required": False,
        }
        runner = _Recorder(json.dumps({
            "structuredOutput": plan,
            "text": json.dumps(plan),
            "is_error": False,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }))
        model = GrokSubscriptionReasoningModel(
            "grok-4.6", 30, runner=runner, environment={"PATH": "/bin"}
        )
        output = model.complete(_plan_request()).output
        self.assertEqual(json.loads(json.dumps(dict(output))), plan)

    def test_one_subprocess_runner_binding(self) -> None:
        source = (
            REPOSITORY_ROOT / "src" / "alx" / "providers" / "grok_subscription.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        runs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"
            and node.attr == "run"
        ]
        self.assertEqual(len(runs), 1)
        self.assertNotIn("XAIReasoningModel", source)
        self.assertNotIn("OpenAIReasoningModel", source)
        self.assertNotIn("api.x.ai", source)


class GrokCodingCompositionTests(unittest.TestCase):
    def test_core_can_be_claude_while_coding_is_grok_cli(self) -> None:
        with patch(
            "alx.bootstrap.providers.subscription_cli_present", return_value=True
        ):
            providers = build_runtime_providers(
                RuntimeSettings.from_environment(
                    _environment(XAI_API_KEY="must-not-select-xai-for-coding")
                )
            )
        self.assertIsInstance(providers.reasoning, ClaudeSubscriptionReasoningModel)
        self.assertIsInstance(providers.coding, GrokSubscriptionReasoningModel)
        self.assertNotIsInstance(providers.coding, XAIReasoningModel)
        self.assertNotIsInstance(providers.coding, OpenAIReasoningModel)
        self.assertIsNone(providers.specialist)

    def test_coding_does_not_require_an_xai_api_key(self) -> None:
        environment = _environment()
        self.assertNotIn("XAI_API_KEY", environment)
        with patch(
            "alx.bootstrap.providers.subscription_cli_present", return_value=True
        ):
            providers = build_runtime_providers(
                RuntimeSettings.from_environment(environment)
            )
        self.assertIsInstance(providers.coding, GrokSubscriptionReasoningModel)
        self.assertNotIn("XAI_API_KEY", providers.coding.child_environment())

    def test_a_present_xai_key_does_not_become_the_coding_transport(self) -> None:
        with patch(
            "alx.bootstrap.providers.subscription_cli_present", return_value=True
        ):
            providers = build_runtime_providers(
                RuntimeSettings.from_environment(
                    _environment(XAI_API_KEY="metered-secret")
                )
            )
        self.assertNotIsInstance(providers.coding, XAIReasoningModel)
        with patch.dict("os.environ", {"XAI_API_KEY": "metered-secret"}):
            self.assertNotIn("XAI_API_KEY", providers.coding.child_environment())

    def test_openai_coding_provider_is_selected_without_changing_core(self) -> None:
        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            providers = build_runtime_providers(RuntimeSettings.from_environment(
                _environment(
                    ALX_CODING_PROVIDER="openai",
                    ALX_CODING_MODEL="codex-test",
                    ALX_CODING_API_KEY="coding-key",
                )
            ))
        self.assertIsInstance(providers.reasoning, ClaudeSubscriptionReasoningModel)
        self.assertIsInstance(providers.coding, OpenAIReasoningModel)
        self.assertEqual(providers.coding._api_key, "coding-key")

    def test_claude_coding_provider_is_explicit_and_independent(self) -> None:
        with patch("alx.bootstrap.providers.subscription_cli_present", return_value=True):
            providers = build_runtime_providers(RuntimeSettings.from_environment(
                _environment(
                    ALX_CODING_PROVIDER="claude_subscription",
                    ALX_CODING_MODEL="claude-test",
                )
            ))
        self.assertIsInstance(providers.reasoning, ClaudeSubscriptionReasoningModel)
        self.assertIsInstance(providers.coding, ClaudeSubscriptionReasoningModel)


if __name__ == "__main__":
    unittest.main()
