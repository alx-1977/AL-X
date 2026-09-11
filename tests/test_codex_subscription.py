"""Codex ChatGPT-subscription reviewer transport stays separate from OpenAI API."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.providers import build_runtime_providers  # noqa: E402
from alx.config.settings import RuntimeSettings  # noqa: E402
from alx.contracts import ModelMessage, ModelRequest, ModelRole  # noqa: E402
from alx.providers import CodexSubscriptionReasoningModel, OpenAIReasoningModel  # noqa: E402
from alx.providers.errors import ProviderError  # noqa: E402


def _request() -> ModelRequest:
    return ModelRequest(
        (
            ModelMessage(ModelRole.SYSTEM, "Return only the requested JSON."),
            ModelMessage(ModelRole.USER, '{"candidate": "review"}'),
        ),
        "alx_coding_local_review",
        {
            "type": "object",
            "properties": {"findings": {"type": "array"}},
            "required": ["findings"],
            "additionalProperties": False,
        },
        kind="coding",
    )


class _Runner:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[dict] = []

    def __call__(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


class CodexSubscriptionTransportTests(unittest.TestCase):
    def test_subscription_reviewer_uses_codex_cli_without_an_api_key(self) -> None:
        runner = _Runner("\n".join((
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"findings": []}'},
            }),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
        )))
        model = CodexSubscriptionReasoningModel(
            "gpt-5.6-luna", 30, runner=runner,
            environment={"PATH": "/bin", "HOME": "/home/friedl", "OPENAI_API_KEY": "must-not-pass"},
            effort="high",
        )
        completion = model.complete(_request())

        self.assertEqual(completion.provider, "codex_subscription")
        self.assertEqual(dict(completion.output), {"findings": ()})
        call = runner.calls[0]
        self.assertNotIn("OPENAI_API_KEY", call["env"])
        self.assertEqual(call["command"][:4], ["codex", "exec", "--json", "--ephemeral"])
        self.assertIn('model_reasoning_effort="high"', call["command"])
        self.assertNotIn("openai", " ".join(call["command"]).lower())

    def test_unavailable_subscription_auth_fails_closed(self) -> None:
        model = CodexSubscriptionReasoningModel(
            "gpt-5.6-luna", 30,
            runner=_Runner(stderr="Not logged in to ChatGPT", returncode=1),
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.provider, "codex_subscription")
        self.assertEqual(raised.exception.reason, "subscription_unauthenticated")


class CodexSubscriptionReviewerCompositionTests(unittest.TestCase):
    def test_reviewer_selection_needs_no_openai_api_key_or_openai_adapter(self) -> None:
        environment = {
            "ALX_REASONING_PROVIDER": "claude_subscription",
            "ALX_REASONING_MODEL": "claude-haiku-4-5",
            "ALX_CODING_ENABLED": "true",
            "ALX_CODING_PROVIDER": "grok_subscription",
            "ALX_CODING_MODEL": "grok-4.6",
            "ALX_CODING_REVIEWER_PROVIDER": "codex_subscription",
            "ALX_CODING_REVIEWER_MODEL": "gpt-5.6-luna",
            "ALX_CODING_REVIEWER_EFFORT": "high",
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
        }
        with (
            patch("alx.bootstrap.providers.subscription_cli_present", return_value=True),
            patch("alx.bootstrap.providers.codex_subscription_cli_present", return_value=True),
        ):
            providers = build_runtime_providers(RuntimeSettings.from_environment(environment))
        self.assertIsInstance(providers.coding_reviewer, CodexSubscriptionReasoningModel)
        self.assertNotIsInstance(providers.coding_reviewer, OpenAIReasoningModel)
        self.assertEqual(providers.coding_reviewer._model, "gpt-5.6-luna")
        self.assertEqual(providers.coding_reviewer._effort, "high")
