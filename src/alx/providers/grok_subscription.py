"""Grok CLI subscription transport for the coding model's planning turn.

This carries one structured reasoning turn — the coding job's PLAN — on the
user's Grok login rather than a metered `XAI_API_KEY`. One prompt goes in, one
JSON object matching the caller's schema comes back, and the process exits.

It is not where the coding job is carried out. Execution is a native
coding-agent session with its own tools and multi-turn loop, launched by
`alx.providers.coding_session`. That separation is the point: a planning turn
needs no repository access, so it is given none, while the session that does
need it runs under a kernel-enforced sandbox instead.

Why a subprocess rather than the xAI HTTP client: the subscription credential
is the CLI login stored in `~/.grok/auth.json`. The documented API-key path
(`XAI_API_KEY`) is a metered fallback the CLI will use when no session token
is active. This adapter must not take that fallback. `XAI_API_KEY` is stripped
from the child environment rather than omitted, so a key present for specialist
or research work cannot silently become what pays for a coding turn.

**No metered fallback exists here.** If the CLI is missing, unauthenticated or
exhausted, this raises a provider failure. It never constructs an xAI, OpenAI
or Claude HTTP client.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - the governed coding-model transport, not an execution site
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from alx.contracts import (
    ModelCompletion,
    ModelRequest,
    ModelRole,
    normalise_usage,
)
from alx.providers.errors import ProviderError, raise_provider_failure


LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "grok_subscription"

_METERED_ENVIRONMENT_KEYS = frozenset(
    {
        "XAI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "KIMI_API_KEY",
        "GROK_CLI_CHAT_PROXY_BASE_URL",
        "GROK_MODELS_BASE_URL",
        "GROK_DEPLOYMENT_KEY",
        "XAI_API_BASE_URL",
        "GROK_AUTH_PROVIDER_COMMAND",
    }
)

_USAGE_LIMIT_MARKERS = (
    "usage limit",
    "rate limit",
    "rate_limit",
    "quota",
    "too many requests",
    "resets at",
    "upgrade to",
)
_AUTHENTICATION_MARKERS = (
    "not logged in",
    "log in",
    "login",
    "unauthenticated",
    "unauthorized",
    "authentication",
    "invalid api key",
    "oauth",
    "credentials",
    "auth.json",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _request_telemetry(request: ModelRequest) -> dict[str, Any]:
    return {
        "kind": request.kind,
        "tier": request.tier,
        "reservation_id": request.reservation_id,
        "reserved_usd": request.reserved_usd,
    }


class _GrokProtocolError(ValueError):
    def __init__(self, code: str, **details: object) -> None:
        self.code = code
        self.details = dict(details)
        super().__init__(code)


class GrokSubscriptionReasoningModel:
    """Reason through the Grok CLI on the user's subscription.

    Satisfies the `ReasoningModel` port with one call per structured
    reasoning turn. The caller's JSON schema is handed to `--json-schema`,
    matching the CLI's documented structured-output flag.
    """

    supports_bounded_research = False

    def __init__(
        self,
        model: str,
        timeout_seconds: int,
        *,
        executable: str = "grok",
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
        """Process basics and a Grok home path, never a metered API key.

        `XAI_API_KEY` is removed rather than left absent. The CLI documentation
        states that an API key is used when no session token is active; a key
        inherited from AL/X would turn this subscription path into billed xAI
        credit while still reporting `grok_subscription`.
        """
        allowed = {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "SYSTEMROOT",
            "WINDIR",
        }
        environment = {
            key: value
            for key, value in self._environment.items()
            if key in allowed and key not in _METERED_ENVIRONMENT_KEYS
        }
        environment["GROK_SUBAGENTS"] = "0"
        environment["GROK_WEB_FETCH"] = "0"
        environment["GROK_MEMORY"] = "0"
        return environment

    def command(self, request: ModelRequest, prompt_path: str, cwd: str) -> list[str]:
        """The exact argument vector for one coding-model turn.

        Headless mode is `-p` / `--single` in V1 and on the installed CLI;
        `--prompt-file` is the current CLI's documented way to supply that
        prompt from a file so the job text is not an argv argument, and
        `--json-schema` is the structured-output flag. The planning turn runs
        in an empty temporary directory with no repository, so it needs no
        tool grants; web search and subagents are refused outright.
        """
        command = [
            self._executable,
            "--prompt-file",
            prompt_path,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_json_value(request.output_schema), sort_keys=True),
            "--model",
            self._model,
            "--system-prompt-override",
            self._system_prompt(request),
            "--disable-web-search",
            "--no-subagents",
            "--no-plan",
            "--no-leader",
            "--verbatim",
            "--cwd",
            cwd,
        ]
        if self._effort:
            command.extend(["--reasoning-effort", self._effort])
        return command

    @staticmethod
    def _system_prompt(request: ModelRequest) -> str:
        parts = [
            message.content
            for message in request.messages
            if message.role is ModelRole.SYSTEM
        ]
        return "\n\n".join(parts)

    @staticmethod
    def _user_prompt(request: ModelRequest) -> str:
        parts = [
            message.content
            for message in request.messages
            if message.role is not ModelRole.SYSTEM
        ]
        if not parts:
            raise _GrokProtocolError("request_without_turn_content")
        return "\n\n".join(parts)

    def complete(self, request: ModelRequest) -> ModelCompletion:
        started_at = monotonic()
        LOGGER.info("Coding model provider request started")
        try:
            prompt = self._user_prompt(request)
            try:
                with TemporaryDirectory(prefix="alx-grok-") as working_directory:
                    grok_home = Path(working_directory) / "grok-home"
                    grok_home.mkdir(mode=0o700)
                    self._install_subscription_auth(grok_home)
                    prompt_path = Path(working_directory) / "prompt.txt"
                    prompt_path.write_text(prompt, encoding="utf-8")
                    cwd = Path(working_directory) / "cwd"
                    cwd.mkdir(mode=0o700)
                    environment = self.child_environment()
                    environment["GROK_HOME"] = str(grok_home)
                    command = self.command(request, str(prompt_path), str(cwd))
                    completed = self._runner(
                        command,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout_seconds,
                        env=environment,
                        cwd=str(cwd),
                        stdin=subprocess.DEVNULL,
                        shell=False,
                        check=False,
                    )
            except subprocess.TimeoutExpired as error:
                raise _GrokProtocolError("reasoning_timeout") from error
            except FileNotFoundError as error:
                raise _GrokProtocolError("cli_not_installed") from error
            except OSError as error:
                raise _GrokProtocolError("cli_unavailable") from error

            if completed.returncode != 0:
                raise _GrokProtocolError(
                    self._failure_code(completed.stderr, completed.stdout),
                    exit_status=completed.returncode,
                    stderr_characters=len(completed.stderr or ""),
                    stdout_characters=len(completed.stdout or ""),
                )
            output, model, usage = self._parse(completed.stdout)
            completion = ModelCompletion(
                PROVIDER_NAME, model or self._model, output, usage
            )
            duration = monotonic() - started_at
            self._emit_telemetry(
                request.affinity_key,
                {
                    "code": "reasoning.completed",
                    "provider": PROVIDER_NAME,
                    "model": model or self._model,
                    "duration_ms": round(duration * 1000),
                    **{
                        key: value
                        for key, value in usage.items()
                        if isinstance(value, int)
                    },
                    **_request_telemetry(request),
                },
            )
            LOGGER.info(
                "Coding model provider request completed in %.3f seconds",
                duration,
            )
            return completion
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            error_code = self._safe_error_code(error)
            details = (
                dict(error.details)
                if isinstance(error, _GrokProtocolError)
                else {}
            )
            duration = monotonic() - started_at
            self._emit_telemetry(
                request.affinity_key,
                {
                    "code": "reasoning.failed",
                    "provider": PROVIDER_NAME,
                    "model": self._model,
                    "duration_ms": round(duration * 1000),
                    "error_type": type(error).__name__,
                    "error_code": error_code,
                    **_request_telemetry(request),
                },
            )
            LOGGER.info(
                "Coding model provider request failed after %.3f seconds: %s",
                duration,
                error_code,
            )
        raise_provider_failure(PROVIDER_NAME, error_code, **details)

    def _install_subscription_auth(self, grok_home: Path) -> None:
        """Copy the CLI login file only. Never copy an API key.

        Auth lives in `auth.json` under the user's Grok home. Copying that
        file into an isolated `GROK_HOME` lets the child authenticate the way
        `grok login` already did, without inheriting `config.toml` permission
        modes or a metered key.
        """
        source_root = Path(
            self._environment.get("GROK_HOME") or Path.home() / ".grok"
        )
        source = source_root / "auth.json"
        if not source.is_file():
            return
        shutil.copy2(source, grok_home / "auth.json")

    def _parse(self, stdout: str) -> tuple[dict[str, Any], str, dict[str, int]]:
        """Read one `--output-format json --json-schema` envelope."""
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise _GrokProtocolError("response_json_invalid") from error
        if not isinstance(envelope, Mapping):
            raise _GrokProtocolError("response_envelope_not_object")
        if envelope.get("is_error"):
            raise _GrokProtocolError(
                self._failure_code(str(envelope.get("result", "")), "")
            )
        # The installed CLI (1.0.24) emits camelCase `structuredOutput` on
        # `--output-format json --json-schema`. Docs and Claude-compat envelopes
        # use snake_case. Prefer either structured field over `text`, which can
        # be prose for the same turn.
        output = self._structured_value(envelope)
        if output is None:
            output = self._json_object_field(envelope.get("text"))
        if output is None:
            output = self._json_object_field(envelope.get("result"))
        if output is None:
            raise _GrokProtocolError("structured_output_missing")
        if not isinstance(output, Mapping):
            raise _GrokProtocolError("structured_output_not_object")
        model = envelope.get("model")
        return (
            dict(output),
            model if isinstance(model, str) else "",
            normalise_usage(envelope.get("usage")),
        )

    @classmethod
    def _structured_value(cls, envelope: Mapping[str, Any]) -> dict[str, Any] | None:
        for key in ("structured_output", "structuredOutput"):
            value = cls._json_object_field(envelope.get(key))
            if value is not None:
                return value
        return None

    @staticmethod
    def _json_object_field(value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, Mapping):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as error:
                raise _GrokProtocolError("structured_json_invalid") from error
            if not isinstance(parsed, Mapping):
                raise _GrokProtocolError("structured_output_not_object")
            return dict(parsed)
        return None

    @staticmethod
    def _failure_code(stderr: str, stdout: str) -> str:
        text = f"{stderr}\n{stdout}".lower()
        if any(marker in text for marker in _USAGE_LIMIT_MARKERS):
            return "subscription_usage_exhausted"
        if any(marker in text for marker in _AUTHENTICATION_MARKERS):
            return "subscription_unauthenticated"
        return "cli_failed"

    @staticmethod
    def _safe_error_code(error: Exception) -> str:
        if isinstance(error, _GrokProtocolError):
            return error.code
        if isinstance(error, json.JSONDecodeError):
            return "response_json_invalid"
        if isinstance(error, KeyError):
            return "response_field_missing"
        if isinstance(error, TypeError):
            return "response_type_invalid"
        return "response_value_invalid"

    def _emit_telemetry(
        self, affinity_key: str | None, values: Mapping[str, Any]
    ) -> None:
        if affinity_key is None or self._telemetry_sink is None:
            return
        try:
            self._telemetry_sink(affinity_key, values)
        except Exception:
            LOGGER.info("Coding model telemetry sink failed")


def subscription_cli_present(executable: str = "grok") -> bool:
    """Whether the Grok CLI can be found at all."""
    return shutil.which(executable) is not None
