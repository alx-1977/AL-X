"""Codex ChatGPT-subscription reviewer transport stays separate from OpenAI API."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from alx.bootstrap.providers import build_runtime_providers  # noqa: E402
from alx.config.settings import RuntimeSettings  # noqa: E402
from alx.contracts import ModelMessage, ModelRequest, ModelRole  # noqa: E402
from alx.providers import CodexSubscriptionReasoningModel, OpenAIReasoningModel  # noqa: E402
from alx.contracts.coding import CodingError  # noqa: E402
from alx.providers.coding_process import (  # noqa: E402
    CodingCancellation,
    bind_cancellation,
    reset_cancellation,
)
from alx.providers.errors import ProviderError  # noqa: E402

# Larger than any pipe buffer, so the child must be reading before it all fits.
_LARGE_INPUT = "x" * (64 * 1024)

# Starts slowly, as `codex exec -` does, then reads stdin to EOF. read() only
# returns at EOF, so an answer at all proves stdin was closed.
_SLOW_READER = (
    "import hashlib, sys, time\n"
    "time.sleep(0.3)\n"
    "data = sys.stdin.buffer.read()\n"
    "sys.stderr.write('read done\\n')\n"
    "print(len(data), hashlib.sha256(data).hexdigest())\n"
)


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

    def test_nonzero_exit_retains_only_bounded_process_metadata(self) -> None:
        model = CodexSubscriptionReasoningModel(
            "gpt-5.6-luna", 30,
            runner=_Runner(stdout="ignored", stderr="credential=must-not-survive", returncode=17),
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.reason, "cli_failed")
        self.assertEqual(raised.exception.details, {
            "exit_status": 17, "stdout_characters": 7, "stderr_characters": 27,
        })

    def test_parser_failure_retains_lengths_without_response_content(self) -> None:
        model = CodexSubscriptionReasoningModel(
            "gpt-5.6-luna", 30,
            runner=_Runner(stdout="not-json", stderr="cookie=must-not-survive"),
        )
        with self.assertRaises(ProviderError) as raised:
            model.complete(_request())
        self.assertEqual(raised.exception.reason, "response_invalid")
        self.assertEqual(raised.exception.details, {
            "stdout_characters": 8, "stderr_characters": 23,
        })


class LargeInputReachesEndOfFileTests(unittest.TestCase):
    """A coding job's real process loop delivers all of stdin, then EOF.

    On 2026-09-27 a 22,138-character review prompt hung `codex exec -` in
    read_to_end until the deadline: the polling loop's first communicate()
    wrote only what fit the pipe, and later calls without input never sent
    the rest or closed stdin. These tests use real child processes because
    an injected runner skips that loop entirely.
    """

    def _run(self, cancellation: CodingCancellation, argv: list[str], **kwargs):
        return cancellation.run(
            subprocess.run, argv, capture_output=True, text=True, check=False, **kwargs
        )

    def test_a_slow_reader_receives_the_whole_input_and_eof(self) -> None:
        started = time.monotonic()
        completed = self._run(
            CodingCancellation(), [sys.executable, "-c", _SLOW_READER],
            input=_LARGE_INPUT, timeout=30,
        )
        elapsed = time.monotonic() - started

        self.assertEqual(completed.returncode, 0)
        expected = hashlib.sha256(_LARGE_INPUT.encode()).hexdigest()
        self.assertEqual(completed.stdout.split(), [str(len(_LARGE_INPUT)), expected])
        self.assertEqual(completed.stderr, "read done\n")
        # Far inside the deadline: completion never waits for the timeout.
        self.assertLess(elapsed, 10)

    def test_cancellation_stays_responsive_while_input_is_undelivered(self) -> None:
        cancellation = CodingCancellation()
        never_reads = [sys.executable, "-c", "import time; time.sleep(60)"]
        threading.Timer(0.3, cancellation.cancel).start()
        started = time.monotonic()
        with self.assertRaises(CodingError) as raised:
            self._run(cancellation, never_reads, input=_LARGE_INPUT, timeout=60)
        self.assertEqual(raised.exception.code, "coding_cancelled")
        self.assertLess(time.monotonic() - started, 10)

    def test_the_overall_deadline_still_applies(self) -> None:
        never_reads = [sys.executable, "-c", "import time; time.sleep(60)"]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self._run(CodingCancellation(), never_reads, input=_LARGE_INPUT, timeout=1)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_large_review_prompt_completes_through_the_codex_adapter(self) -> None:
        # A fake `codex` that behaves like `codex exec -`: slow start, read
        # stdin to EOF, then emit the JSON event stream.
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "codex"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import json, sys, time\n"
                "time.sleep(0.3)\n"
                "received = len(sys.stdin.read())\n"
                "answer = json.dumps({'findings': [], 'received': received})\n"
                "for event in (\n"
                "    {'type': 'thread.started', 'thread_id': 't'},\n"
                "    {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': answer}},\n"
                "    {'type': 'turn.completed', 'usage': {'input_tokens': 1}},\n"
                "):\n"
                "    print(json.dumps(event))\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            model = CodexSubscriptionReasoningModel(
                "gpt-5.6-luna", 30, executable=str(executable),
                environment={"PATH": os.environ.get("PATH", ""), "HOME": directory},
                effort="high",
            )
            request = ModelRequest(
                (
                    ModelMessage(ModelRole.SYSTEM, "Return only the requested JSON."),
                    ModelMessage(ModelRole.USER, _LARGE_INPUT),
                ),
                "alx_coding_local_review",
                _request().output_schema,
                kind="coding",
            )
            token = bind_cancellation(CodingCancellation())
            started = time.monotonic()
            try:
                completion = model.complete(request)
            finally:
                reset_cancellation(token)
            elapsed = time.monotonic() - started

        prompt = CodexSubscriptionReasoningModel._prompt(request)
        self.assertEqual(completion.output["received"], len(prompt))
        self.assertEqual(completion.output["findings"], ())
        self.assertLess(elapsed, 10)


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
