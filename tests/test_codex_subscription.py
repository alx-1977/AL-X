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
from alx.providers.codex_coding_session import (  # noqa: E402
    CodexCodingSession,
    permission_profile,
)
from alx.providers.coding_process import (  # noqa: E402
    CodingCancellation,
    _deliver_input,
    bind_cancellation,
    reset_cancellation,
)
from alx.providers.errors import ProviderError  # noqa: E402

# Larger than any default pipe capacity (16 KiB on macOS, 64 KiB on Linux), so
# the child must be reading before it all fits.
_LARGE_INPUT = "x" * (256 * 1024)

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

    def test_input_the_pipe_cannot_encode_fails_instead_of_arriving_short(self) -> None:
        reader = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
        with self.assertRaises(UnicodeEncodeError):
            # A lone surrogate has no encoding in any codec with strict errors.
            self._run(CodingCancellation(), reader, input="\udcff", timeout=30)

    def test_the_writer_stops_when_nobody_ever_reads(self) -> None:
        # A descendant that left the process group can hold the read end open
        # without reading. The writer must end when told, not block forever.
        read_end, write_end = os.pipe()
        stdin = os.fdopen(write_end, "wb")
        stop = threading.Event()
        writer = threading.Thread(
            target=_deliver_input, args=(stdin, _LARGE_INPUT.encode(), stop)
        )
        try:
            writer.start()
            time.sleep(0.3)
            self.assertTrue(writer.is_alive())
            stop.set()
            writer.join(5)
            self.assertFalse(writer.is_alive())
            self.assertTrue(stdin.closed)
        finally:
            os.close(read_end)

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


class CodexCodingCompositionTests(unittest.TestCase):
    def test_codex_coding_needs_no_openai_api_key_or_openai_adapter(self) -> None:
        """D-052: Codex plans and codes; nothing else reviews."""
        environment = {
            "ALX_REASONING_PROVIDER": "claude_subscription",
            "ALX_CLAUDE_ACCOUNT": "core@example.invalid",
            "ALX_REASONING_MODEL": "claude-haiku-4-5",
            "ALX_CODING_ENABLED": "true",
            "ALX_CODING_PROVIDER": "codex_subscription",
            "ALX_CODING_MODEL": "gpt-5.6-luna",
            "ALX_CODING_EFFORT": "high",
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
        self.assertIsInstance(providers.coding, CodexSubscriptionReasoningModel)
        self.assertNotIsInstance(providers.coding, OpenAIReasoningModel)
        self.assertEqual(providers.coding._model, "gpt-5.6-luna")
        self.assertEqual(providers.coding._effort, "high")
        self.assertIsInstance(providers.coding_session, CodexCodingSession)
        self.assertTrue(providers.coding_session.has_terminal)
        self.assertEqual(providers.coding_session.model_name, "gpt-5.6-luna")
        self.assertFalse(hasattr(providers, "coding_reviewer"))


class CodexCodingSessionTests(unittest.TestCase):
    """D-052: Codex as the coding session, inside its own kernel sandbox."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name).resolve()
        self.worktree = self.parent / "repo"
        self.worktree.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.worktree, check=True)
        self.home = self.parent / "job" / "home"
        self.home.mkdir(parents=True)
        self.session = CodexCodingSession(
            "gpt-5.6-luna", stall_seconds=60, effort="high",
            environment={"PATH": "/bin", "HOME": str(self.parent),
                         "OPENAI_API_KEY": "metered", "CODEX_API_KEY": "metered"},
        )

    def _prepare(self, blocked=()):
        from types import SimpleNamespace
        with patch("alx.providers.codex_coding_session.shutil.which",
                   return_value="/opt/tool/bin/codex"):
            self.session.prepare_home(
                self.home, self.worktree, SimpleNamespace(blocked_paths=blocked))
        return json.loads((self.home / "permissions.json").read_text())

    def test_the_profile_denies_git_credentials_blocked_paths_and_network(self) -> None:
        profile = self._prepare(blocked=("secrets/vault.txt",))
        filesystem = profile["filesystem"]
        self.assertEqual(profile["extends"], ":workspace")
        self.assertEqual(filesystem[":root"], "deny")
        self.assertEqual(filesystem[str(self.worktree / ".git")], "deny")
        self.assertEqual(filesystem[str(self.worktree / "secrets/vault.txt")], "deny")
        self.assertEqual(filesystem[":workspace_roots"]["**/.env"], "deny")
        self.assertEqual(filesystem[":workspace_roots"]["**/auth.json"], "deny")
        self.assertEqual(filesystem[str(self.home / "tmp")], "write")
        self.assertEqual(profile["network"], {"enabled": False})
        # Nothing outside the job, the runtime and the CLI is granted.
        granted = {key for key, value in filesystem.items()
                   if value in ("read", "write") and key.startswith("/")}
        self.assertEqual(granted, {
            str(self.home / "tmp"), str(self.session._runtime),
            "/opt/tool/bin", "/opt/tool",
        })

    def test_the_command_ignores_personal_config_and_keeps_the_task_off_argv(self) -> None:
        self._prepare()
        argv = self.session.command(self.home.parent / "briefing.txt", self.worktree)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn('default_permissions="alx_coding_job"', argv)
        self.assertIn('approval_policy="never"', argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertEqual(argv[-1], "-")
        self.assertTrue(self.session.briefing_on_stdin())
        self.assertTrue(self.session.has_terminal)

    def test_a_missing_profile_refuses_rather_than_running_open(self) -> None:
        with self.assertRaises(CodingError) as raised:
            self.session.command(self.home.parent / "briefing.txt", self.worktree)
        self.assertEqual(raised.exception.code, "sandbox_unusable")

    def test_no_metered_key_reaches_the_session(self) -> None:
        environment = self.session.child_environment(self.home)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("CODEX_API_KEY", environment)
        self.assertEqual(environment["TMPDIR"], str(self.home / "tmp"))

    def test_the_stream_is_read_for_its_last_message(self) -> None:
        stream = "\n".join(json.dumps(event) for event in (
            {"type": "thread.started"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "x" * 50}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed the date."}},
            {"type": "turn.completed", "usage": {}},
        ))
        kept = self.session.retained_output(stream.splitlines())
        self.assertNotIn("command_execution", kept)
        result = self.session.read_result(kept)
        self.assertTrue(result.completed)
        self.assertEqual(result.report, "Fixed the date.")

    def test_an_unfinished_or_failed_stream_is_a_failed_session(self) -> None:
        with self.assertRaises(CodingError) as unfinished:
            self.session.read_result(json.dumps({"type": "turn.started"}))
        self.assertEqual(unfinished.exception.details["reason_code"], "session_end_missing")
        with self.assertRaises(CodingError) as limited:
            self.session.read_result(json.dumps(
                {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}))
        self.assertEqual(limited.exception.details["reason_code"], "subscription_usage_exhausted")

    def test_a_missing_login_is_named_at_start_up(self) -> None:
        self.assertEqual(self.session.login_problem(), "subscription_unauthenticated")
        (self.parent / ".codex").mkdir()
        (self.parent / ".codex" / "auth.json").write_text("{}")
        self.assertEqual(self.session.login_problem(), "")
