"""Codex CLI subscription transport for the advisory Coding Agent reviewer.

This is deliberately a one-turn structured reasoning adapter, not native
Coding Agent execution.  The Codex CLI authenticates through Friedl's existing
ChatGPT login; API-key environment variables are removed from its child
process, so selecting this provider can neither bill the OpenAI API nor fall
back to another provider.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - governed subscription reasoning transport
from collections.abc import Callable, Mapping
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from alx.contracts import ModelCompletion, ModelRequest, ModelRole, normalise_usage
from alx.providers.errors import raise_provider_failure


LOGGER = logging.getLogger(__name__)
PROVIDER_NAME = "codex_subscription"

_METERED_ENVIRONMENT_KEYS = frozenset(
    {"OPENAI_API_KEY", "CODEX_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"}
)
_AUTHENTICATION_MARKERS = (
    "not logged in", "log in", "login", "unauthenticated", "unauthorized",
    "authentication", "chatgpt", "credentials", "auth",
)
_USAGE_LIMIT_MARKERS = (
    "usage limit", "rate limit", "quota", "too many requests", "upgrade",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


class _CodexProtocolError(ValueError):
    def __init__(self, code: str, **details: object) -> None:
        self.code = code
        self.details = dict(details)
        super().__init__(code)


class CodexSubscriptionReasoningModel:
    """Complete one schema-constrained reviewer request through Codex CLI."""

    supports_bounded_research = False

    def __init__(
        self,
        model: str,
        timeout_seconds: int,
        *,
        executable: str = "codex",
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        runner: "Callable[..., subprocess.CompletedProcess] | None" = None,
        environment: Mapping[str, str] | None = None,
        effort: str = "",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not model.strip():
            raise ValueError("model must not be blank")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._executable = executable
        self._telemetry_sink = telemetry_sink
        self._runner = runner or subprocess.run
        self._environment = os.environ if environment is None else environment
        self._effort = effort.strip()

    def child_environment(self) -> dict[str, str]:
        """Keep Codex's subscription login, never a metered API credential."""
        allowed = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
            "CODEX_HOME", "SYSTEMROOT", "WINDIR",
        }
        return {
            key: value
            for key, value in self._environment.items()
            if key in allowed and key not in _METERED_ENVIRONMENT_KEYS
        }

    def command(self, request: ModelRequest, schema_path: str, cwd: str) -> list[str]:
        command = [
            self._executable, "exec", "--json", "--ephemeral", "--ignore-rules",
            "--sandbox", "read-only", "--skip-git-repo-check", "--model", self._model,
            "--output-schema", schema_path, "--cd", cwd,
        ]
        if self._effort:
            command.extend(["--config", f'model_reasoning_effort="{self._effort}"'])
        # A dash makes Codex read the turn from stdin. Never put review context
        # or model-supplied file content in the process argument vector.
        command.append("-")
        return command

    @staticmethod
    def _prompt(request: ModelRequest) -> str:
        system = [message.content for message in request.messages if message.role is ModelRole.SYSTEM]
        content = [message.content for message in request.messages if message.role is not ModelRole.SYSTEM]
        if not content:
            raise _CodexProtocolError("request_without_turn_content")
        return "\n\n".join((*system, *content))

    def complete(self, request: ModelRequest) -> ModelCompletion:
        started_at = monotonic()
        details: dict[str, object] = {}
        try:
            with TemporaryDirectory(prefix="alx-codex-review-") as working_directory:
                root = os.fspath(working_directory)
                schema_path = os.path.join(root, "schema.json")
                with open(schema_path, "w", encoding="utf-8") as schema_file:
                    json.dump(_json_value(request.output_schema), schema_file, sort_keys=True)
                completed = self._runner(
                    self.command(request, schema_path, root),
                    input=self._prompt(request),
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    env=self.child_environment(),
                    cwd=root,
                    shell=False,
                    check=False,
                )
            if completed.returncode != 0:
                raise _CodexProtocolError(
                    self._failure_code(completed.stderr, completed.stdout),
                    exit_status=completed.returncode,
                )
            output, usage = self._parse(completed.stdout)
            completion = ModelCompletion(PROVIDER_NAME, self._model, output, usage)
            self._emit(request, "reasoning.completed", started_at, usage)
            return completion
        except subprocess.TimeoutExpired:
            details = {"reason_code": "reasoning_timeout"}
            error_code = "reasoning_timeout"
        except FileNotFoundError:
            error_code = "cli_not_installed"
        except OSError:
            error_code = "cli_unavailable"
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            error_code = error.code if isinstance(error, _CodexProtocolError) else "response_invalid"
            details = dict(error.details) if isinstance(error, _CodexProtocolError) else {}
        self._emit(request, "reasoning.failed", started_at, {})
        raise_provider_failure(PROVIDER_NAME, error_code, **details)

    @staticmethod
    def _parse(stdout: str) -> tuple[dict[str, Any], dict[str, int]]:
        message: str | None = None
        usage: Mapping[str, Any] | None = None
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, Mapping):
                raise _CodexProtocolError("response_event_invalid")
            if event.get("type") == "item.completed":
                item = event.get("item")
                if isinstance(item, Mapping) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        message = text
            if event.get("type") == "turn.completed" and isinstance(event.get("usage"), Mapping):
                usage = event["usage"]
        if message is None:
            raise _CodexProtocolError("structured_output_missing")
        value = json.loads(message)
        if not isinstance(value, Mapping):
            raise _CodexProtocolError("structured_output_not_object")
        return dict(value), normalise_usage(usage)

    @staticmethod
    def _failure_code(stderr: str, stdout: str) -> str:
        text = f"{stderr}\n{stdout}".lower()
        if any(marker in text for marker in _USAGE_LIMIT_MARKERS):
            return "subscription_usage_exhausted"
        if any(marker in text for marker in _AUTHENTICATION_MARKERS):
            return "subscription_unauthenticated"
        return "cli_failed"

    def _emit(
        self, request: ModelRequest, code: str, started_at: float, usage: Mapping[str, int]
    ) -> None:
        if self._telemetry_sink is None or request.affinity_key is None:
            return
        try:
            self._telemetry_sink(request.affinity_key, {
                "code": code, "provider": PROVIDER_NAME, "model": self._model,
                "duration_ms": round((monotonic() - started_at) * 1000), **usage,
            })
        except Exception:
            LOGGER.info("Codex subscription telemetry sink failed")


def subscription_cli_present(executable: str = "codex") -> bool:
    return shutil.which(executable) is not None
