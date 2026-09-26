"""Codex ChatGPT-subscription reviewer transport stays separate from OpenAI API."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
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


def _codex(directory: Path, body: str) -> Path:
    """A scripted `codex` child; the transport starts it like the real CLI."""
    script = directory / "codex"
    script.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"record = {str(directory / 'launch.json')!r}\n"
        "json.dump({'argv': sys.argv, 'env': dict(os.environ),"
        " 'stdin': sys.stdin.read()}, open(record, 'w'))\n" + body,
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


class CodexSubscriptionTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def _model(self, body: str, **options) -> CodexSubscriptionReasoningModel:
        return CodexSubscriptionReasoningModel(
            "gpt-5.6-luna", 30, executable=str(_codex(self.directory, body)),
            **options,
        )

    def test_subscription_reviewer_uses_codex_cli_without_an_api_key(self) -> None:
        events = "\n".join((
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"findings": []}'},
            }),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
        ))
        model = self._model(
            f"print({events!r})\n",
            environment={"PATH": os.environ.get("PATH", ""), "HOME": "/home/friedl",
                         "OPENAI_API_KEY": "must-not-pass"},
            effort="high",
        )
        completion = model.complete(_request())

        self.assertEqual(completion.provider, "codex_subscription")
        self.assertEqual(dict(completion.output), {"findings": ()})
        launch = json.loads((self.directory / "launch.json").read_text())
        self.assertNotIn("OPENAI_API_KEY", launch["env"])
        self.assertEqual(launch["argv"][1:4], ["exec", "--json", "--ephemeral"])
        self.assertIn('model_reasoning_effort="high"', launch["argv"])
        self.assertNotIn("openai", " ".join(launch["argv"][1:]).lower())
        # The turn travels on stdin, never in the argument vector.
        self.assertEqual(launch["argv"][-1], "-")
        self.assertIn('{"candidate": "review"}', launch["stdin"])
        self.assertEqual(
            CodexSubscriptionReasoningModel("gpt-5.6-luna", 30).command(
                _request(), "schema.json", "."
            )[0],
            "codex",
        )

    def test_unavailable_subscription_auth_fails_closed(self) -> None:
        model = self._model(
            "sys.stderr.write('Not logged in to ChatGPT')\nsys.exit(1)\n"
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.provider, "codex_subscription")
        self.assertEqual(raised.exception.reason, "subscription_unauthenticated")

    def test_nonzero_exit_retains_only_bounded_process_metadata(self) -> None:
        model = self._model(
            "sys.stdout.write('ignored')\n"
            "sys.stderr.write('credential=must-not-survive')\nsys.exit(17)\n"
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.reason, "cli_failed")
        self.assertEqual(raised.exception.details, {
            "exit_status": 17, "stdout_characters": 7, "stderr_characters": 27,
        })

    def test_parser_failure_retains_lengths_without_response_content(self) -> None:
        model = self._model(
            "sys.stdout.write('not-json')\nsys.stderr.write('cookie=must-not-survive')\n"
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.reason, "response_invalid")
        self.assertEqual(raised.exception.details, {
            "stdout_characters": 8, "stderr_characters": 23,
        })


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
