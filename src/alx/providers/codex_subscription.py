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

from alx.contracts import ModelCompletion, ModelRequest, ModelRole, normalise_usage, usage_telemetry
from alx.providers.subscription_retry import SubscriptionRetryPolicy, cli_failure_message, error_event_message, retry_subscription
from alx.providers.errors import raise_provider_failure


LOGGER = logging.getLogger(__name__)
PROVIDER_NAME = "codex_subscription"

_METERED_ENVIRONMENT_KEYS = frozenset(
    {"OPENAI_API_KEY", "CODEX_API_KEY", "XAI_API_KEY", "ANTHROPIC_API_KEY"}
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
        retry_policy: SubscriptionRetryPolicy | None = None,
        effort: str = "",
        launcher: "Callable[..., Any] | None" = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not model.strip():
            raise ValueError("model must not be blank")
        self._retry_policy = retry_policy or SubscriptionRetryPolicy()
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._executable = executable
        self._telemetry_sink = telemetry_sink
        self._runner = runner or subprocess.run
        self._environment = os.environ if environment is None else environment
        self._effort = effort.strip()
        # D-083a: the one launcher a native agent session uses (as D-065 for Claude).
        self._launcher = launcher or subprocess.Popen

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

    @retry_subscription
    def complete(self, request: ModelRequest) -> ModelCompletion:
        started_at = monotonic()
        details: dict[str, object] = {}
        try:
            with TemporaryDirectory(prefix="alx-codex-review-") as working_directory:
                root = os.fspath(working_directory)
                schema_path = os.path.join(root, "schema.json")
                with open(schema_path, "w", encoding="utf-8") as schema_file:
                    json.dump(_json_value(request.output_schema), schema_file, sort_keys=True)
                from alx.providers.coding_process import run_coding_subprocess
                completed = run_coding_subprocess(self._runner,
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
                    "cli_failed",
                    service_message=cli_failure_message(completed.stderr, completed.stdout),
                    exit_status=completed.returncode,
                    stdout_characters=len(completed.stdout or ""),
                    stderr_characters=len(completed.stderr or ""),
                )
            try:
                output, usage, reported = self._parse(completed.stdout)
            except _CodexProtocolError as error:
                error.details.setdefault("stdout_characters", len(completed.stdout or ""))
                error.details.setdefault("stderr_characters", len(completed.stderr or ""))
                raise
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
                raise _CodexProtocolError(
                    "response_invalid",
                    stdout_characters=len(completed.stdout or ""),
                    stderr_characters=len(completed.stderr or ""),
                ) from error
            completion = ModelCompletion(PROVIDER_NAME, self._model, output, usage)
            self._emit(request, "reasoning.completed", started_at, usage, reported)
            return completion
        except subprocess.TimeoutExpired as error:
            details = {"service_message": error.stderr or error.stdout or "reasoning_timeout"}
            error_code = "reasoning_timeout"
        except FileNotFoundError as error:
            details = {"service_message": str(error)}
            error_code = "cli_not_installed"
        except OSError as error:
            details = {"service_message": str(error)}
            error_code = "cli_unavailable"
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            error_code = error.code if isinstance(error, _CodexProtocolError) else "response_invalid"
            details = dict(error.details) if isinstance(error, _CodexProtocolError) else {}
        self._emit(request, "reasoning.failed", started_at, {})
        raise_provider_failure(PROVIDER_NAME, error_code, **details)

    @staticmethod
    def _parse(stdout: str) -> tuple[dict[str, Any], dict[str, int], Any]:
        message: str | None = None
        usage: Mapping[str, Any] | None = None
        for line in stdout.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if not isinstance(event, Mapping):
                raise _CodexProtocolError("response_event_invalid")
            if event.get("type") in ("error", "turn.failed"):
                raise _CodexProtocolError(
                    "cli_failed", service_message=error_event_message(event)
                )
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
        # The raw report travels too, so telemetry can tell a breakdown the
        # CLI never reported from one it reported as zero.
        return dict(value), normalise_usage(usage), usage


    def _emit(
        self, request: ModelRequest, code: str, started_at: float,
        usage: Mapping[str, int], reported: Any = None,
    ) -> None:
        if self._telemetry_sink is None or request.affinity_key is None:
            return
        try:
            self._telemetry_sink(request.affinity_key, {
                "code": code, "provider": PROVIDER_NAME, "model": self._model,
                "duration_ms": round((monotonic() - started_at) * 1000),
                **(usage_telemetry(usage, reported) if code == "reasoning.completed" else {}),
                **({"purpose": request.purpose} if request.purpose else {}),
            })
        except Exception:
            LOGGER.info("Codex subscription telemetry sink failed")


# Codex's own tools an agent of AL/X's never needs: it acts only through her tools,
# which reach it through the bridge and her broker (16 Nov, Friedl: the board agent on
# Astra). Code mode stays, since it is how Codex reaches MCP tools; the read-only
# sandbox holds apply_patch. Probed 16 Nov: no file, shell or network from code mode.
_AGENT_FEATURES_OFF = (
    "shell_tool", "unified_exec", "view_image", "apps", "plugins", "browser_use",
    "browser_use_external", "computer_use", "image_generation", "multi_agent", "multi_agent_v2",
    "goals", "tool_suggest", "in_app_browser", "memories",
)


class CodexSubscriptionAgentModel(CodexSubscriptionReasoningModel):
    """A Codex model (Astra) as an agent of AL/X's: it calls her tools natively.

    The same bridge and pause as the Claude agent (D-065): the CLI runs with the bridge
    as its only MCP server, offering exactly the request's tools; a call waits at the
    bridge until the Core answers, so the run keeps its conversation for the stretch.
    Requests without tools are one-turn reviews, as before.
    """

    PAUSE_LIMIT_SECONDS = 3600

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._session: Any = None
        self._bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)), "native_tool_bridge.py")

    def native_command(self, request: ModelRequest, socket_path: str, tools_path: str) -> list[str]:
        import sys
        folder = os.path.dirname(tools_path)
        schema_path = os.path.join(folder, "schema.json")
        with open(schema_path, "w", encoding="utf-8") as schema_file:
            json.dump(_json_value(request.output_schema), schema_file, sort_keys=True)
        command = [
            self._executable, "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "--skip-git-repo-check", "--model", self._model,
            "--output-schema", schema_path, "--cd", folder,
            "--config", 'web_search="disabled"',
            "--config", "mcp_servers.alx.command=%s" % json.dumps(sys.executable),
            "--config", "mcp_servers.alx.args=%s" % json.dumps(["-I", self._bridge, socket_path, tools_path]),
            "--config", "mcp_servers.alx.tool_timeout_sec=%d" % self.PAUSE_LIMIT_SECONDS,
            "--config", 'mcp_servers.alx.default_tools_approval_mode="approve"',
        ]
        for feature in _AGENT_FEATURES_OFF:
            command.extend(["--disable", feature])
        if self._effort:
            command.extend(["--config", f'model_reasoning_effort="{self._effort}"'])
        command.append("-")
        return command

    def abandon_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.close()

    def _native_event(self, request: ModelRequest) -> tuple[str, Any]:
        from alx.providers.claude_subscription import _NativeSession
        session = self._session
        if request.tool_result is not None and session is not None and session.alive:
            session.answer(request.tool_result, request.tool_images)
        else:
            self.abandon_session()
            session = _NativeSession(
                self._launcher, lambda path, tools: self.native_command(request, path, tools),
                self._prompt(request), self.child_environment(), self.PAUSE_LIMIT_SECONDS, request.tools)
            self._session = session
        if request.on_dispatched is not None:
            request.on_dispatched()
        try:
            event = session.next_event(self._timeout_seconds)
        except BaseException:
            self._session = None
            session.close()
            raise
        if event[0] == "exit":
            self._session = None
            session.close()
        return event

    @retry_subscription
    def complete(self, request: ModelRequest) -> ModelCompletion:
        if not request.tools:
            return CodexSubscriptionReasoningModel.complete.__wrapped__(self, request)
        started_at = monotonic()
        details: dict[str, object] = {}
        try:
            kind, payload = self._native_event(request)
            if kind == "tool":
                return ModelCompletion(PROVIDER_NAME, self._model, {}, {"input_tokens": 0, "output_tokens": 0},
                                       tool_use=payload)
            returncode, stdout, stderr = payload
            if returncode != 0:
                raise _CodexProtocolError("cli_failed", service_message=cli_failure_message(stderr, stdout),
                                          exit_status=returncode)
            output, usage, reported = self._parse(stdout)
            self._emit(request, "reasoning.completed", started_at, usage, reported)
            return ModelCompletion(PROVIDER_NAME, self._model, output, usage)
        except subprocess.TimeoutExpired as error:
            details = {"service_message": "reasoning_timeout"}
            error_code = "reasoning_timeout"
            del error
        except FileNotFoundError as error:
            details = {"service_message": str(error)}
            error_code = "cli_not_installed"
        except OSError as error:
            details = {"service_message": str(error)}
            error_code = "cli_unavailable"
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            error_code = error.code if isinstance(error, _CodexProtocolError) else "response_invalid"
            details = dict(error.details) if isinstance(error, _CodexProtocolError) else {}
        self._emit(request, "reasoning.failed", started_at, {})
        raise_provider_failure(PROVIDER_NAME, error_code, **details)


def subscription_cli_present(executable: str = "codex") -> bool:
    return shutil.which(executable) is not None
