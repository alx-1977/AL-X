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

Claude Code does not currently expose a mode that both retains subscription
OAuth/keychain authentication and suppresses organization-managed hooks.
`--setting-sources ""` excludes the user, project, and local sources the CLI
allows a caller to select, but managed policy remains authoritative. Therefore
this transport must run under an unmanaged subscription account, or under
managed policy that sets `disableAllHooks: true`; the process command cannot
enforce that administrative boundary itself.

The supported subscription transport is the CLI's own headless mode, using
its saved OAuth login or an explicitly configured setup-token credential.

**No metered fallback exists here.** `ANTHROPIC_API_KEY` is stripped from the
child environment rather than merely omitted, so a key present in AL/X's own
environment cannot silently become the thing that pays. If the subscription is
unavailable, exhausted, or unauthenticated, this raises a provider failure and
the turn does not happen. It never reaches for another provider: selecting one
is configuration, and a transport that chose its own replacement would be
spending Friedl's money on a decision he did not make.

**AL/X's own identity.** The Core reasons as a dedicated Claude account, held
in its own Claude Code configuration directory (`ALX_CLAUDE_CONFIG_DIR`). The
child is given exactly that directory, and any inherited `CLAUDE_CONFIG_DIR`
is dropped. Only a setup token verified through Anthropic's OAuth profile for
alx@fire-fli.co.za on a supported subscription may override the saved login;
otherwise it is excluded and a diagnostic returns to the Core.
Before the first turn the directory's login is verified through the CLI's machine-readable
`auth status --json`: signed in, through a claude.ai subscription login, at
that directory, and as the expected account when one is configured. Anything
else refuses the attempt; there is no fallback identity.
"""

from __future__ import annotations

from uuid import uuid4

import json
import http.client
import logging
import os
import shutil
import sys
import subprocess  # noqa: S404 - the governed reasoning transport, not an execution site
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from typing import Any

from alx.contracts import (
    ModelCompletion,
    ModelRequest,
    ModelRole,
    normalise_usage,
    usage_telemetry,
)
from alx.providers.subscription_retry import SubscriptionRetryPolicy, cli_failure_message, retry_subscription
from alx.providers.errors import raise_provider_failure


LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "claude_subscription"

# Why the dedicated identity could not be confirmed. Each needs Friedl: none
# clears on a timer, and none may be answered by trying another account.
_IDENTITY_CODES = frozenset(
    {
        "subscription_config_missing",
        "subscription_unauthenticated",
        "subscription_login_not_claude_ai",
        "subscription_config_mismatch",
        "subscription_account_mismatch",
        "subscription_identity_unverifiable",
        "subscription_config_personal",
    }
)

# How long the CLI's local login identity check may take.
IDENTITY_CHECK_TIMEOUT_SECONDS = 30


@dataclass(frozen=True, slots=True)
class _SetupTokenProfile:
    token: str = field(repr=False)
    subscription: str | None = None
    refusal: str | None = None
    notice_id: str = field(default_factory=lambda: uuid4().hex)

    @property
    def notice(self) -> str | None:
        if self.refusal is None:
            return None
        return (
            "The Claude setup token was refused because " + self.refusal
            + "; the normal dedicated Claude login is being used."
        )


_setup_token_profile: _SetupTokenProfile | None = None
_setup_token_lock = Lock()
_setup_token_notice_acknowledged: str | None = None


def _unique_profile_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("ambiguous account profile")
        fields[key] = value
    return fields


def _lookup_setup_token(token: str) -> _SetupTokenProfile:
    """One fixed OAuth endpoint, without redirects, proxies or HTTP diagnostics.

    Claude Code 2.1.316's embedded iv/Xs/cmr functions use this endpoint,
    these headers and organization_type values (not the API-key profile).
    Never retain an external exception or response in a refusal.
    """
    connection = None
    reason = "Anthropic's account lookup could not be reached"
    try:
        connection = http.client.HTTPSConnection("api.anthropic.com", timeout=10)
        connection.set_debuglevel(0)
        connection.request("GET", "/api/oauth/profile", headers={
            "User-Agent": "claude-code/2.1.316",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        })
        response = connection.getresponse()
        if response.status != 200:
            reason = "Anthropic did not approve the account lookup"
        else:
            reason = "Anthropic's account details were incomplete or unreadable"
            body = response.read(65_537)
            if len(body) <= 65_536:
                profile = json.loads(body, object_pairs_hook=_unique_profile_fields)
                account = profile.get("account") if isinstance(profile, dict) else None
                organization = profile.get("organization") if isinstance(profile, dict) else None
                if isinstance(account, dict) and isinstance(organization, dict):
                    email = account.get("email")
                    plan = organization.get("organization_type")
                    if isinstance(email, str) and isinstance(plan, str):
                        if email != "alx@fire-fli.co.za":
                            reason = "it belongs to a different account"
                        elif (plan not in {"claude_pro", "claude_max", "claude_team"}
                              or organization.get("billing_type") not in {
                                  None, "stripe_subscription", "stripe_subscription_contracted",
                                  "apple_subscription", "google_play_subscription",
                              }):
                            reason = "it does not confirm a supported Claude subscription"
                        else:
                            return _SetupTokenProfile(token, subscription=plan)
    except TimeoutError:
        reason = "Anthropic's account lookup timed out"
    except Exception:
        # Even malformed headers and unexpected HTTP/parser errors fail closed;
        # their messages can contain the request credential or response body.
        pass
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    return _SetupTokenProfile(token, refusal=reason)


def _validated_setup_token(token: str) -> _SetupTokenProfile:
    global _setup_token_profile
    with _setup_token_lock:
        if _setup_token_profile is None or _setup_token_profile.token != token:
            _setup_token_profile = _lookup_setup_token(token)
        return _setup_token_profile


@dataclass(frozen=True, slots=True)
class SubscriptionIdentity:
    """Who the Core reasons as, as the CLI itself reports it. No credentials."""

    config_dir: str
    account: str
    auth_method: str
    subscription_type: str


class SubscriptionIdentityError(Exception):
    """The dedicated identity is missing, invalid, or not the expected one."""

    def __init__(self, code: str) -> None:
        if code not in _IDENTITY_CODES:
            raise ValueError("identity failures must be declared")
        self.code = code
        super().__init__(code)



def _json_value(value: Any) -> Any:
    """Plain JSON from AL/X's frozen structured data."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


def _request_telemetry(request: ModelRequest) -> dict[str, Any]:
    values: dict[str, Any] = {
        "kind": request.kind,
        "tier": request.tier,
        "reservation_id": request.reservation_id,
        "reserved_usd": request.reserved_usd,
    }
    if request.purpose:
        values["purpose"] = request.purpose
    return values


# Service tiers Anthropic reports in usage. The value is external text, so it
# reaches telemetry only when it is one of these; anything else is omitted.
_SERVICE_TIERS = frozenset({"standard", "priority", "batch"})


def _envelope_metrics(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Measurements the CLI itself reported, and nothing it did not.

    `duration_api_ms` is the time the CLI spent waiting on the API, as distinct
    from the subprocess wall time this adapter measures. The CLI reports no
    first-event or generation timing, so none is published: a zero there
    would claim the model answered instantly.
    """
    metrics: dict[str, Any] = {}
    api_ms = envelope.get("duration_api_ms")
    if isinstance(api_ms, int) and not isinstance(api_ms, bool) and api_ms >= 0:
        metrics["api_duration_ms"] = api_ms
    usage = envelope.get("usage")
    tier = usage.get("service_tier") if isinstance(usage, Mapping) else None
    if isinstance(tier, str) and tier in _SERVICE_TIERS:
        metrics["service_tier"] = tier
    return metrics


class _ClaudeProtocolError(ValueError):
    def __init__(self, code: str, **details: object) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


# The bridge is run by path, with `-I`, so nothing on the CLI's path or in its
# working directory can stand in for it.
_BRIDGE_SCRIPT = Path(__file__).resolve().with_name("native_tool_bridge.py")


class _NativeSession:
    """One Claude CLI run that may pause on native tool calls (D-065).

    The CLI is started with the tool bridge as its only MCP server. When the
    model calls `call_capability`, the bridge connects to this session's socket
    and waits; the session hands the call to the Core and holds the connection
    until the Core answers. The CLI keeps its conversation, and its cache, for
    the whole turn. Nothing here runs a capability.
    """

    def __init__(self, launcher: Callable[..., Any], argv_for: Callable[[str], list[str]],
                 prompt: str, environment: Mapping[str, str], idle_seconds: float) -> None:
        import socket
        import tempfile
        import threading
        # A short path: unix socket paths are limited to about 100 bytes.
        self._directory = tempfile.mkdtemp(prefix="alx-native-", dir="/tmp")
        self._socket_path = os.path.join(self._directory, "s")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self._socket_path)
        self._server.listen(8)
        self._server.settimeout(0.2)
        self._pending: Any = None
        # The request that started the run and when, for its record.
        self.request: Any = None
        self.started_at = monotonic()
        self._idle_seconds = idle_seconds
        self._idle_timer: Any = None
        self._stdout: list[str] = []
        self._stderr: list[str] = []
        try:
            self._process = launcher(
                argv_for(self._socket_path), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, cwd=self._directory, env=dict(environment),
                start_new_session=True, shell=False,
            )
        except BaseException:
            # Never stored, so never closed later: release what was made here.
            self._server.close()
            import shutil as _shutil
            _shutil.rmtree(self._directory, ignore_errors=True)
            raise
        self._readers = [
            threading.Thread(target=self._drain, args=(self._process.stdout, self._stdout),
                             daemon=True),
            threading.Thread(target=self._drain, args=(self._process.stderr, self._stderr),
                             daemon=True),
        ]
        for reader in self._readers:
            reader.start()
        # Written off this thread, so a CLI that stalls before reading a large
        # prompt cannot hold the turn past next_event's deadline, which kills it.
        threading.Thread(target=self._deliver, args=(prompt,), daemon=True).start()

    def _deliver(self, prompt: str) -> None:
        try:
            self._process.stdin.write(prompt)
            self._process.stdin.close()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _drain(stream: Any, sink: list[str]) -> None:
        for chunk in iter(lambda: stream.read(65536), ""):
            sink.append(chunk)

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def next_event(self, timeout_seconds: float) -> tuple[str, Any]:
        """Wait for a native call, ("tool", input), or the end, ("exit", (code, out, err))."""
        import socket
        deadline = monotonic() + timeout_seconds
        while monotonic() < deadline:
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                connection = None
            if connection is not None:
                connection.settimeout(timeout_seconds)
                received = b""
                while not received.endswith(b"\n"):
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    received += chunk
                try:
                    call = json.loads(received.decode("utf-8"))["input"]
                except (ValueError, KeyError):
                    connection.close()
                    continue
                self._pending = connection
                self._arm_idle_timer()
                return "tool", call
            if self._process.poll() is not None:
                for reader in self._readers:
                    reader.join(5)
                return "exit", (self._process.returncode, "".join(self._stdout),
                                "".join(self._stderr))
        self.close()
        raise subprocess.TimeoutExpired("claude", timeout_seconds)

    def answer(self, text: str) -> None:
        """Return the Core's result to the paused call."""
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        connection, self._pending = self._pending, None
        if connection is None:
            raise _ClaudeProtocolError("native_call_not_pending")
        try:
            connection.sendall((json.dumps({"text": text}) + "\n").encode("utf-8"))
        finally:
            connection.close()

    def _arm_idle_timer(self) -> None:
        # A paused session nobody continues, because the turn ended, must not
        # outlive its own timeout.
        import threading
        self._idle_timer = threading.Timer(self._idle_seconds, self.close)
        self._idle_timer.daemon = True
        self._idle_timer.start()

    def close(self) -> None:
        import shutil as _shutil
        import signal
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        if self._pending is not None:
            try:
                self._pending.close()
            except OSError:
                pass
            self._pending = None
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                pass
        try:
            self._server.close()
        except OSError:
            pass
        _shutil.rmtree(self._directory, ignore_errors=True)


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
        runner: "Callable[..., subprocess.CompletedProcess] | None" = None,
        environment: Mapping[str, str] | None = None,
        config_dir: str | None = None,
        expected_account: str | None = None,
        oauth_token: str | None = None,
        retry_policy: SubscriptionRetryPolicy | None = None,
        launcher: "Callable[..., subprocess.Popen] | None" = None,
        native_tools: bool = False,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._retry_policy = retry_policy or SubscriptionRetryPolicy()
        # D-065: the one other way this transport starts the CLI, for a native
        # session that pauses. Bound here once; used only by _NativeSession.
        self._launcher = launcher or subprocess.Popen
        # Whether the Core may call capabilities natively through this transport.
        self.native_tools_enabled = native_tools
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._executable = executable
        self._telemetry_sink = telemetry_sink
        # Injected so the process boundary is fakeable in tests, exactly as the
        # HTTP adapters inject a client. Production passes nothing.
        self._runner = runner or subprocess.run
        self._environment = os.environ if environment is None else environment
        self._oauth_token = oauth_token or None
        # The dedicated Claude configuration this Core reasons through. None
        # only for callers outside the Core, which keep the CLI's own default.
        if config_dir is not None and not os.path.isabs(config_dir):
            raise ValueError("config_dir must be an absolute path")
        if config_dir is not None and not (expected_account or "").strip():
            # A dedicated directory proves nothing about who signed into it.
            raise ValueError("a dedicated config_dir requires its expected account")
        self._config_dir = config_dir
        self._expected_account = (
            None if expected_account is None else expected_account.strip().casefold()
        )
        self._identity: SubscriptionIdentity | None = None
        self._availability_lock = Lock()
        # D-065: the one session paused on a native call, if any.
        self._session: _NativeSession | None = None

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
        if self._config_dir is not None:
            # Ambient overrides cannot replace the explicitly configured identity.
            allowed -= {"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN"}
        if self._oauth_token is not None:
            allowed.discard("CLAUDE_CODE_OAUTH_TOKEN")
        environment = {
            key: value for key, value in self._environment.items() if key in allowed
        }
        if self._config_dir is not None:
            environment["CLAUDE_CONFIG_DIR"] = self._config_dir
        if (self._oauth_token is not None
                and _validated_setup_token(self._oauth_token).subscription is not None):
            environment["CLAUDE_CODE_OAUTH_TOKEN"] = self._oauth_token
        return environment

    @staticmethod
    def setup_token_notice_facts() -> dict[str, str]:
        """Read the pending diagnostic without consuming its delivery receipt."""
        with _setup_token_lock:
            if (_setup_token_profile is None
                    or _setup_token_notice_acknowledged == _setup_token_profile.notice_id
                    or _setup_token_profile.refusal is None):
                return {}
            return {
                "authentication_notice": _setup_token_profile.notice,
                "authentication_notice_id": _setup_token_profile.notice_id,
            }

    @staticmethod
    def acknowledge_setup_token_notice(facts: Mapping[str, Any]) -> None:
        """An old request can acknowledge only the notice it actually carried."""
        global _setup_token_notice_acknowledged
        with _setup_token_lock:
            if (_setup_token_profile is not None
                    and _setup_token_profile.notice is not None
                    and facts.get("authentication_notice") == _setup_token_profile.notice
                    and facts.get("authentication_notice_id") == _setup_token_profile.notice_id):
                _setup_token_notice_acknowledged = _setup_token_profile.notice_id

    @property
    def executable(self) -> str:
        return self._executable

    @property
    def config_dir(self) -> str | None:
        return self._config_dir

    def verify_identity(self) -> SubscriptionIdentity:
        """Confirm the dedicated configuration is AL/X's subscription login.

        Setup tokens use the cached Anthropic profile lookup; a refused token
        uses the normal `claude auth status --json` saved-login check. Raises
        `SubscriptionIdentityError` rather than letting any other identity
        answer.
        """
        if self._oauth_token is not None:
            profile = _validated_setup_token(self._oauth_token)
            if profile.subscription is not None:
                return SubscriptionIdentity(
                    self._config_dir or "", "alx@fire-fli.co.za", "claude.ai",
                    profile.subscription,
                )
        if self._config_dir is None:
            raise SubscriptionIdentityError("subscription_config_missing")
        if not os.path.isdir(self._config_dir):
            # Checked before the CLI runs: given a missing directory it would
            # create an empty one and report a login that was never made.
            raise SubscriptionIdentityError("subscription_config_missing")
        home = self._environment.get("HOME", "")
        personal = {os.path.realpath(home), os.path.realpath(os.path.join(home, ".claude"))}
        if home and os.path.realpath(self._config_dir) in personal:
            # However it is named, it must not resolve to the person's login.
            raise SubscriptionIdentityError("subscription_config_personal")
        failed = False
        try:
            with TemporaryDirectory(prefix="alx-claude-") as working_directory:
                from alx.providers.coding_process import run_coding_subprocess
                completed = run_coding_subprocess(
                    self._runner,
                    [self._executable, "auth", "status", "--json"],
                    capture_output=True, text=True,
                    timeout=IDENTITY_CHECK_TIMEOUT_SECONDS,
                    env=self.child_environment(), cwd=working_directory,
                    shell=False, check=False,
                )
        except (OSError, subprocess.SubprocessError):
            failed = True
        if failed:
            # Outside the handler: no exception context can retain CLI output.
            raise SubscriptionIdentityError("subscription_identity_unverifiable")
        try:
            status = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            failed = True
        if failed:
            raise SubscriptionIdentityError("subscription_identity_unverifiable")
        if not isinstance(status, Mapping):
            raise SubscriptionIdentityError("subscription_identity_unverifiable")
        if status.get("loggedIn") is not True:
            raise SubscriptionIdentityError("subscription_unauthenticated")
        if status.get("authMethod") != "claude.ai" or status.get("apiProvider") != "firstParty":
            # An API key or a third-party route would bill, which is the one
            # thing this transport exists not to do.
            raise SubscriptionIdentityError("subscription_login_not_claude_ai")
        reported = status.get("configDirectory")
        if not isinstance(reported, str) or (
            os.path.realpath(reported) != os.path.realpath(self._config_dir)
        ):
            raise SubscriptionIdentityError("subscription_config_mismatch")
        account = status.get("email")
        if not isinstance(account, str) or not account.strip():
            raise SubscriptionIdentityError("subscription_identity_unverifiable")
        if account.strip().casefold() != self._expected_account:
            raise SubscriptionIdentityError("subscription_account_mismatch")
        subscription = status.get("subscriptionType")
        identity = SubscriptionIdentity(
            self._config_dir, account.strip(), "claude.ai",
            subscription if isinstance(subscription, str) else "",
        )
        with self._availability_lock:
            changed = identity != self._identity
            self._identity = identity
        if changed:
            # Once per identity, not once per turn: the account and the
            # directory, never a credential or any configuration content.
            LOGGER.info(
                "AL/X Core Claude identity verified: config_dir=%s account=%s "
                "auth=%s subscription=%s",
                identity.config_dir, identity.account, identity.auth_method,
                identity.subscription_type or "unknown",
            )
        return identity

    def _require_identity(self) -> None:
        """Before every turn: the dedicated identity, verified again.

        A login can change while the runtime runs, so a check at startup
        alone would not hold. One local status read per turn.
        """
        if self._config_dir is None and self._oauth_token is None:
            return
        try:
            self.verify_identity()
        except SubscriptionIdentityError as error:
            LOGGER.warning("AL/X Core Claude identity refused: %s", error.code)
            raise_provider_failure(PROVIDER_NAME, error.code)

    def native_command(self, request: ModelRequest, socket_path: str) -> list[str]:
        """The argument vector for a native-tool session (D-065).

        As `command`, with the tool bridge as the only MCP server and its one
        tool the only tool allowed. Built-in tools stay removed.
        """
        bridge = {"mcpServers": {"alx": {
            "command": sys.executable,
            "args": ["-I", str(_BRIDGE_SCRIPT), socket_path],
        }}}
        argv = self.command(request)
        index = argv.index("--mcp-config")
        argv[index + 1] = json.dumps(bridge)
        return [*argv, "--allowedTools", "mcp__alx__call_capability"]

    def command(self, request: ModelRequest) -> list[str]:
        """The exact argument vector for one turn.

        A method so a test can assert the flags rather than trust a comment.

        `--print` is one-shot headless mode and `--no-session-persistence`
        prevents the CLI from writing a resumable transcript. `--tools ""`
        removes built-in tools; an explicit empty MCP configuration and strict
        mode remove MCP servers. No continuation flags appear: AL/X owns
        durable continuity.
        """
        return [
            self._executable,
            "--print",
            "--no-session-persistence",
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

    def abandon_session(self) -> None:
        """End a paused native session the Core will not continue.

        The run did reason, so it is recorded like any other call, with its
        usage marked unknown: the CLI reports usage only when it finishes.
        """
        session, self._session = self._session, None
        if session is None:
            return
        session.close()
        request, started_at = session.request, session.started_at
        if request is None:
            return
        self._emit_telemetry(request.affinity_key, {
            "code": "reasoning.failed",
            "outcome": "abandoned",
            "provider": PROVIDER_NAME,
            "model": self._model,
            "duration_ms": round((monotonic() - started_at) * 1000),
            "error_code": "native_session_abandoned",
            "usage_unknown": True,
            **_request_telemetry(request),
        })

    def _native_event(self, request: ModelRequest) -> tuple[str, Any]:
        """Start or continue the native session and wait for its next event.

        A result for a session that is gone starts a fresh one from the full
        request: the Core has already recorded the result there, so nothing is
        lost, only the cache.
        """
        session = self._session
        if request.tool_result is not None and session is not None and session.alive:
            session.answer(request.tool_result)
        else:
            self.abandon_session()
            prompt = self._user_prompt(request)
            session = _NativeSession(
                self._launcher, lambda path: self.native_command(request, path), prompt,
                self.child_environment(), self._timeout_seconds,
            )
            # Kept for the record of a run that is abandoned rather than finished.
            session.request, session.started_at = request, monotonic()
            self._session = session
        if request.on_dispatched is not None:
            request.on_dispatched()
        try:
            event = session.next_event(self._timeout_seconds)
        except BaseException:
            # The run is over and reported by complete(); nothing may report it again.
            self._session = None
            session.close()
            raise
        if event[0] == "exit":
            self._session = None
            session.close()
        return event

    def _completion(self, request: ModelRequest, completed: Any,
                    started_at: float) -> ModelCompletion:
        """Read a finished CLI run into the Core's decision, with telemetry."""
        if completed.returncode != 0:
            raise _ClaudeProtocolError(
                "cli_failed", service_message=cli_failure_message(completed.stderr, completed.stdout)
            )
        output, model, usage, envelope = self._parse(completed.stdout)
        self._require_schema(output, request)
        completion = ModelCompletion(
            PROVIDER_NAME, model or self._model, output,
            usage_telemetry(usage, envelope.get("usage")) if request.kind == "research" else usage,
        )
        duration = monotonic() - started_at
        self._emit_telemetry(
            request.affinity_key,
            {
                "code": "reasoning.completed",
                "provider": PROVIDER_NAME,
                "model": model or self._model,
                "duration_ms": round(duration * 1000),
                **_envelope_metrics(envelope),
                **usage_telemetry(usage, envelope.get("usage")),
                **_request_telemetry(request),
            },
        )
        LOGGER.info(
            "Reasoning provider request completed in %.3f seconds", duration
        )
        return completion

    @retry_subscription
    def complete(self, request: ModelRequest) -> ModelCompletion:
        self._require_identity()
        started_at = monotonic()
        LOGGER.info("Reasoning provider request started")
        try:
            if request.native_tools:
                try:
                    kind, payload = self._native_event(request)
                except subprocess.TimeoutExpired as error:
                    raise _ClaudeProtocolError(
                        "reasoning_timeout", service_message="reasoning_timeout") from error
                except OSError as error:
                    raise _ClaudeProtocolError("cli_unavailable", service_message=str(error)) from error
                if kind == "tool":
                    LOGGER.info("Reasoning provider paused on a native call after %.3f seconds",
                                monotonic() - started_at)
                    # Answered: the model reasoned and chose a call. Its usage
                    # is reported once, with the session's final answer.
                    return ModelCompletion(PROVIDER_NAME, self._model, {},
                                           {"input_tokens": 0, "output_tokens": 0},
                                           tool_use=payload)
                returncode, stdout, stderr = payload
                completed = subprocess.CompletedProcess([], returncode, stdout, stderr)
            else:
                prompt = self._user_prompt(request)
                command = self.command(request)
                try:
                    with TemporaryDirectory(prefix="alx-claude-") as working_directory:
                        from alx.providers.coding_process import run_coding_subprocess
                        completed = run_coding_subprocess(self._runner,
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
                        if request.on_dispatched is not None:
                            request.on_dispatched()
                except subprocess.TimeoutExpired as error:
                    if request.on_dispatched is not None:
                        request.on_dispatched()
                    # The CLI is killed by `subprocess.run` when the deadline
                    # passes, so the turn cannot outlive its own timeout.
                    raise _ClaudeProtocolError(
                        "reasoning_timeout", service_message=error.stderr or error.stdout or "reasoning_timeout"
                    ) from error
                except FileNotFoundError as error:
                    raise _ClaudeProtocolError("cli_not_installed", service_message=str(error)) from error
                except OSError as error:
                    raise _ClaudeProtocolError("cli_unavailable", service_message=str(error)) from error
            return self._completion(request, completed, started_at)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            error_code = self._safe_error_code(error)
            details = dict(error.details) if isinstance(error, _ClaudeProtocolError) else {}
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
        raise_provider_failure(PROVIDER_NAME, error_code, **details)

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

    def _parse(
        self, stdout: str
    ) -> tuple[dict[str, Any], str, dict[str, int], Mapping[str, Any]]:
        """Read one `--output-format json` envelope and its decision."""
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise _ClaudeProtocolError("response_json_invalid") from error
        if not isinstance(envelope, Mapping):
            raise _ClaudeProtocolError("response_envelope_not_object")
        if envelope.get("is_error"):
            raise _ClaudeProtocolError(
                "cli_failed", service_message=str(envelope.get("result", ""))
            )
        if envelope.get("subtype") not in (None, "success"):
            raise _ClaudeProtocolError(
                "cli_failed", service_message=str(envelope.get("result", ""))
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
            envelope,
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


class ClaudeSubscriptionResearchModel(ClaudeSubscriptionReasoningModel):
    """The same transport, offered for research on AL/X's own subscription.

    D-023, amended 2026-10-14. A subscription call has no per-token price, so
    research on it is priced at zero and bounded instead by the daily call cap
    the research ledger enforces. Settings refuse a subscription research tier
    without that cap, which is what makes the bound declared here true.
    """

    supports_bounded_research = True


def subscription_cli_present(executable: str = "claude") -> bool:
    """Whether the Claude Code CLI can be found at all.

    Used by configuration to refuse at startup rather than at the first turn.
    """
    return shutil.which(executable) is not None
