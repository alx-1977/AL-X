"""Claude Code subscription transport behind the provider-neutral model port.

This adapter exists to remove AL/X Core's dependency on metered API credit. It
reasons through Friedl's Claude subscription, authenticated the way the Claude
Code CLI already authenticates itself, and it never holds or forwards an API
key.

**It is a reasoning transport, not an agent.** The CLI is capable of running an
agent loop with tools of its own; that would be a second reasoning path beside
the one Law 1 places in AL/X, so every one of those capabilities is refused
here. One prompt goes in, one JSON decision comes back, and the process exits.
Nothing is resumed, nothing is remembered on the far side, and nothing out
there may act.

Why a subprocess rather than an HTTP client: the subscription credential is
held by the Claude Code installation - in its OAuth login or the system
keychain - and the supported way to reason on it is the CLI's own headless
mode. There is no endpoint AL/X could call with a token of her own without
reintroducing exactly the metered path this replaces.

**No metered fallback exists here.** `ANTHROPIC_API_KEY` is stripped from the
child environment rather than merely omitted, so a key present in AL/X's own
environment cannot silently become the thing that pays. If the subscription is
unavailable, exhausted, or unauthenticated, this raises a provider failure and
the turn does not happen. It never reaches for another provider: selecting one
is configuration, and a transport that chose its own replacement would be
spending Friedl's money on a decision he did not make.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - the governed reasoning transport, not an execution site
from collections.abc import Callable, Mapping
from tempfile import TemporaryDirectory
from threading import Lock
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

PROVIDER_NAME = "claude_subscription"

# Failures that will not resolve by being retried in the same runtime. A
# subscription that is exhausted or unauthenticated stays that way until
# Friedl acts, so the adapter latches and refuses subsequent turns rather than
# starting a process per turn to be told the same thing.
_TERMINAL_CODES = frozenset(
    {
        "subscription_usage_exhausted",
        "subscription_unauthenticated",
        "cli_not_installed",
    }
)

# Exhaustion clears on its own when the subscription's window rolls over, so it
# is latched with an expiry rather than permanently. Authentication does not:
# that one needs Friedl, and re-checking it on a timer would be a process spawn
# per turn that can only fail.
_COOLDOWN_CODES = frozenset({"subscription_usage_exhausted"})

# A structured answer that cannot be read. One is a bad turn; a run of them is
# a provider that is not going to produce a decision, and the Core step loop
# will keep asking - eleven calls in five minutes, on the first live test.
# Subscription allowance is spent either way, so the run is bounded here rather
# than left to the loop that cannot see the pattern.
_MALFORMED_CODES = frozenset(
    {
        "structured_json_invalid",
        "structured_output_not_object",
        "structured_output_missing",
        "response_result_missing",
        "response_json_invalid",
        "response_envelope_not_object",
        "decision_schema_unsatisfied",
    }
)

# How many consecutive malformed answers end the run. Two, because one is
# noise and the third would be paid for to learn what the second already said.
MAX_CONSECUTIVE_MALFORMED = 2

TERMINAL_FAILURE_COOLDOWN_SECONDS = 900.0

# Read from the CLI's own reporting, which is untrusted external text. Matched
# case-insensitively against the message it returns, and used only to choose a
# sanitised code - never rendered to Friedl and never treated as instruction.
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
    "please run /login",
    "credentials",
)


def _json_value(value: Any) -> Any:
    """Plain JSON from AL/X's frozen structured data."""
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


class _ClaudeProtocolError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ClaudeSubscriptionReasoningModel:
    """Reason through the Claude Code CLI on Friedl's subscription.

    Satisfies the `ReasoningModel` port with one stateless call per Core turn.
    The Core's own strict JSON schema is handed to the CLI, so the structured
    decision comes back validated by the model rather than parsed out of prose.
    """

    # Research spends against a dollar ceiling and settles from measured token
    # usage. The CLI reports no usage this adapter can price, so it must not be
    # offered for bounded research: a reservation settled from an unmeasured
    # call would record a cost that was never established.
    supports_bounded_research = False

    def __init__(
        self,
        model: str,
        timeout_seconds: int,
        *,
        executable: str = "claude",
        telemetry_sink: Callable[[str, Mapping[str, Any]], None] | None = None,
        terminal_failure_cooldown_seconds: float = TERMINAL_FAILURE_COOLDOWN_SECONDS,
        clock: Callable[[], float] = monotonic,
        runner: "Callable[..., subprocess.CompletedProcess] | None" = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if terminal_failure_cooldown_seconds <= 0:
            raise ValueError("terminal failure cooldown must be positive")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._executable = executable
        self._telemetry_sink = telemetry_sink
        self._terminal_failure_cooldown_seconds = terminal_failure_cooldown_seconds
        self._clock = clock
        # Injected so the process boundary is fakeable in tests, exactly as the
        # HTTP adapters inject a client. Production passes nothing.
        self._runner = runner or subprocess.run
        self._environment = os.environ if environment is None else environment
        self._availability_lock = Lock()
        self._terminal_failure: tuple[str, float | None] | None = None
        # Consecutive malformed answers. Reset by any completed turn, so a
        # single bad decision between good ones never latches.
        self._consecutive_malformed = 0

    def ensure_available(self) -> None:
        """Refuse a known-terminal provider before another turn is attempted.

        Same contract the metered adapters offer, and the Core already calls
        it. Without this an exhausted subscription would be rediscovered once
        per turn, spawning a process each time to learn nothing new.
        """
        with self._availability_lock:
            failure = self._terminal_failure
            if (
                failure is not None
                and failure[1] is not None
                and self._clock() >= failure[1]
            ):
                self._terminal_failure = None
                failure = None
        if failure:
            raise_provider_failure(PROVIDER_NAME, failure[0])

    def child_environment(self) -> dict[str, str]:
        """The environment the CLI is given: the subscription, never a key.

        `ANTHROPIC_API_KEY` is removed rather than left absent, because AL/X's
        own process may legitimately hold one for a provider Friedl selected
        elsewhere. Inheriting it here would let this path bill metered credit
        while reporting itself as the subscription, which is the exact failure
        this adapter exists to make impossible.

        `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` go for the same reason:
        each redirects the CLI onto a billed or third-party route. What remains
        is the CLI's own subscription authentication - its OAuth login, the
        system keychain, or `CLAUDE_CODE_OAUTH_TOKEN` where Friedl has issued
        one with `claude setup-token`.

        Exposed as a method so a test can assert the exact mapping rather than
        infer it from behaviour.
        """
        # Allow only process basics and subscription authentication. In
        # particular, no provider keys, cloud credentials, CLI routing toggles,
        # or injected runtime options are inherited from the host.
        allowed = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
            "SYSTEMROOT", "WINDIR", "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
        return {
            key: value for key, value in self._environment.items() if key in allowed
        }

    def command(self, request: ModelRequest) -> list[str]:
        """The exact argument vector for one turn.

        A method so a test can assert the flags rather than trust a comment.

        `--print` is one-shot headless mode. `--tools ""` removes built-in
        tools; an explicit empty MCP configuration and strict mode remove MCP
        servers. No continuation flags appear: AL/X owns durable continuity.
        """
        return [
            self._executable,
            "--print",
            "--output-format",
            "json",
            "--model",
            self._model,
            # The Core's own strict schema. The decision is validated where it
            # is generated rather than hopefully parsed here.
            "--json-schema",
            json.dumps(_json_value(request.output_schema), sort_keys=True),
            "--system-prompt",
            self._system_prompt(request),
            # Disable host settings and all built-in/MCP tools.
            "--mcp-config",
            '{"mcpServers": {}}',
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--tools",
            "",
        ]

    @staticmethod
    def _system_prompt(request: ModelRequest) -> str:
        """Every SYSTEM message, in order, as the one system prompt.

        The Core composes the Laws, her identity, the protocol and the
        capability catalogue as separate system messages. The CLI takes one, so
        they are joined in the order the Core built them; nothing is dropped,
        reordered or summarised, because each is part of who is reasoning.
        """
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
            raise _ClaudeProtocolError("request_without_turn_content")
        return "\n\n".join(parts)

    def complete(self, request: ModelRequest) -> ModelCompletion:
        self.ensure_available()
        started_at = monotonic()
        LOGGER.info("Reasoning provider request started")
        try:
            prompt = self._user_prompt(request)
            command = self.command(request)
            try:
                with TemporaryDirectory(prefix="alx-claude-") as working_directory:
                    completed = self._runner(
                        command,
                        input=prompt,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout_seconds,
                        env=self.child_environment(),
                        cwd=working_directory,
                        # Nothing on stdin beyond the prompt, and no shell.
                        shell=False,
                        check=False,
                    )
            except subprocess.TimeoutExpired as error:
                # The CLI is killed by `subprocess.run` when the deadline
                # passes, so the turn cannot outlive its own timeout.
                raise _ClaudeProtocolError("reasoning_timeout") from error
            except FileNotFoundError as error:
                raise _ClaudeProtocolError("cli_not_installed") from error
            except OSError as error:
                raise _ClaudeProtocolError("cli_unavailable") from error

            if completed.returncode != 0:
                raise _ClaudeProtocolError(
                    self._failure_code(completed.stderr, completed.stdout)
                )
            output, model, usage = self._parse(completed.stdout)
            self._require_schema(output, request)
            completion = ModelCompletion(
                PROVIDER_NAME, model or self._model, output, usage
            )
            with self._availability_lock:
                self._consecutive_malformed = 0
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
                "Reasoning provider request completed in %.3f seconds", duration
            )
            return completion
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            error_code = self._safe_error_code(error)
            if error_code in _MALFORMED_CODES:
                with self._availability_lock:
                    self._consecutive_malformed += 1
                    exhausted = (
                        self._consecutive_malformed >= MAX_CONSECUTIVE_MALFORMED
                    )
                    if exhausted:
                        # Latched without an expiry: nothing about this
                        # runtime will change on a timer, and the next attempt
                        # would spend allowance to be told the same thing.
                        # Friedl restarts once the cause is fixed.
                        self._terminal_failure = (
                            "provider_output_unusable",
                            None,
                        )
                if exhausted:
                    LOGGER.warning(
                        "Claude subscription reasoning latched after %d "
                        "consecutive malformed responses",
                        MAX_CONSECUTIVE_MALFORMED,
                    )
            if error_code in _TERMINAL_CODES:
                expires_at = (
                    self._clock() + self._terminal_failure_cooldown_seconds
                    if error_code in _COOLDOWN_CODES
                    else None
                )
                with self._availability_lock:
                    self._terminal_failure = (error_code, expires_at)
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
                "Reasoning provider request failed after %.3f seconds: %s",
                duration,
                error_code,
            )
        # Raised after the handler has exited so the prompt cannot be reached
        # from the failure. See `providers/errors.py`.
        raise_provider_failure(PROVIDER_NAME, error_code)

    @staticmethod
    def _require_schema(output: Mapping[str, Any], request: ModelRequest) -> None:
        """Check the decision carries what the Core's schema requires.

        Not a full JSON Schema implementation: the model is asked for the
        schema and generally honours it, and re-validating every constraint
        here would put a second interpretation of the Core's contract in a
        provider. What this catches is the case that actually breaks the Core -
        a top-level key it subscripts being absent - so a missing field becomes
        a named provider failure instead of a KeyError raised deeper in
        reasoning, where it reads as a defect in AL/X rather than in the answer
        she was given.
        """
        schema = request.output_schema
        if not isinstance(schema, Mapping):
            return
        required = schema.get("required")
        if not isinstance(required, (list, tuple)):
            return
        missing = [
            name
            for name in required
            if isinstance(name, str) and name not in output
        ]
        if missing:
            raise _ClaudeProtocolError("decision_schema_unsatisfied")

    def _parse(self, stdout: str) -> tuple[dict[str, Any], str, dict[str, int]]:
        """Read one `--output-format json` envelope and its decision."""
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise _ClaudeProtocolError("response_json_invalid") from error
        if not isinstance(envelope, Mapping):
            raise _ClaudeProtocolError("response_envelope_not_object")
        if envelope.get("is_error"):
            raise _ClaudeProtocolError(
                self._failure_code(str(envelope.get("result", "")), "")
            )
        if envelope.get("subtype") not in (None, "success"):
            raise _ClaudeProtocolError(
                f"response_{str(envelope.get('subtype'))[:40]}"
            )
        # The schema-conformant decision arrives in its own field. `result`
        # carries the assistant's prose for the same turn, which is not the
        # decision and does not parse as one: reading `result` first made every
        # Core turn fail as malformed while the structured answer sat unread
        # beside it.
        if "structured_output" in envelope:
            output: Any = envelope.get("structured_output")
            if output is None:
                raise _ClaudeProtocolError("structured_output_missing")
        else:
            # No structured field at all. An older or differently configured
            # CLI may still put the decision in `result`, so that is read as a
            # fallback rather than failing outright.
            result = envelope.get("result")
            if result is None:
                raise _ClaudeProtocolError("response_result_missing")
            if isinstance(result, str):
                try:
                    output = json.loads(result)
                except json.JSONDecodeError as error:
                    raise _ClaudeProtocolError(
                        "structured_json_invalid"
                    ) from error
            else:
                output = result
        if not isinstance(output, Mapping):
            raise _ClaudeProtocolError("structured_output_not_object")
        model = self._answering_model(envelope)
        return (
            dict(output),
            model if isinstance(model, str) else "",
            self._usage(envelope),
        )

    @staticmethod
    def _answering_model(envelope: Mapping[str, Any]) -> str:
        """Which model actually answered, from the CLI's per-model usage.

        There is no top-level model field. `modelUsage` can name more than one:
        the CLI uses a small helper model for its own bookkeeping alongside the
        one that did the reasoning. Taking the first key would sometimes record
        the helper as the model that answered, so the one that generated the
        most output is reported instead.
        """
        usage = envelope.get("modelUsage")
        if not isinstance(usage, Mapping) or not usage:
            return ""
        def _output(entry: Any) -> int:
            if not isinstance(entry, Mapping):
                return 0
            value = entry.get("outputTokens")
            return value if isinstance(value, int) and not isinstance(value, bool) else 0
        best = max(usage.items(), key=lambda item: _output(item[1]))
        return best[0] if isinstance(best[0], str) else ""

    @staticmethod
    def _usage(envelope: Mapping[str, Any]) -> dict[str, int]:
        """Token counts when the CLI reports them, zeros otherwise.

        These are informational only. This path spends no metered credit, so
        nothing settles a reservation from them, and `normalise_usage` reports
        an unmeasured call rather than inventing a figure.
        """
        return normalise_usage(envelope.get("usage"))

    @staticmethod
    def _failure_code(stderr: str, stdout: str) -> str:
        """Classify a CLI failure into one sanitised code.

        The CLI's own text is external untrusted data. It is inspected only to
        choose which of these fixed codes to raise; none of it is returned, and
        it never becomes wording AL/X speaks.
        """
        text = f"{stderr}\n{stdout}".lower()
        if any(marker in text for marker in _USAGE_LIMIT_MARKERS):
            return "subscription_usage_exhausted"
        if any(marker in text for marker in _AUTHENTICATION_MARKERS):
            return "subscription_unauthenticated"
        return "cli_failed"

    @staticmethod
    def _safe_error_code(error: Exception) -> str:
        if isinstance(error, _ClaudeProtocolError):
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
            LOGGER.info("Reasoning telemetry sink failed")


def subscription_cli_present(executable: str = "claude") -> bool:
    """Whether the Claude Code CLI can be found at all.

    Used by configuration to refuse at startup rather than at the first turn.
    """
    return shutil.which(executable) is not None
