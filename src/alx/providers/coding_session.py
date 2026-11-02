"""Launch one native Grok coding-agent session inside an assigned worktree.

This is the Grok adapter for `SubscriptionCodingSession`, which holds the
orchestration every subscription coding CLI shares. What remains here is
Grok's own vocabulary: its executable, its flags, where it keeps its login,
what it calls its tools, and the key spellings in the envelope it returns.

It starts the Grok CLI as a real agent: its own tools, its own multi-turn
loop, its own context, with the assigned worktree as its actual working
directory. That is what V1 did and what made it work.

D-063 permits terminal and network access. The generated profile retains
explicit blocked paths and unrelated file protections. Metered API keys remain
stripped from the subscription transport; access does not approve spending.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path

from alx.contracts.coding import CodingError, CodingRequest, CodingSessionResult
from alx.providers.coding_containment import PROFILE_NAME, write_profile
from alx.providers.subscription_retry import error_event_message
from alx.providers.coding_subscription_session import (
    MAX_RETAINED_OUTPUT_CHARACTERS,
    OversizedLine,
    SubscriptionCodingSession,
    subscription_cli_present,
)


LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "grok_subscription"

EXECUTABLE = "grok"

# Subagent delegation is outside this single coding-session outcome.
WITHHELD_TOOLS: tuple[str, ...] = ("Agent", "task", "spawn_subagent")

# The native file tools the agent works with. Named so a CLI change that
# renames or drops one is visible here rather than silently disabling editing.
NATIVE_TOOLS: tuple[str, ...] = (
    "read_file",
    "list_dir",
    "grep",
    "search_replace",
    "create_file",
    "edit_file",
    "todo_write",
)


# Stream events that carry what the agent read or thought, never the result.
# Only these may be dropped when a line is too long to decode.
_BULK_EVENTS = frozenset({
    "tool_call", "tool_call_update", "thought", "usage", "available_commands",
})


def _event_kind(prefix: str) -> str:
    """The `type` an NDJSON event's prefix declares, read structurally.

    The CLI writes `type` as each event's first key; whitespace between the
    tokens is tolerated. Anything else declares no kind and is not dropped.
    """
    rest = prefix.lstrip()
    for token in ("{", '"type"', ":", '"'):
        rest = rest.lstrip()
        if not rest.startswith(token):
            return ""
        rest = rest[len(token):]
    end = rest.find('"')
    return rest[:end] if end > 0 else ""


class GrokCodingSession(SubscriptionCodingSession):
    """One native coding-agent session per job, contained by the kernel."""

    provider_name = PROVIDER_NAME
    session_label = "grok"
    home_environment_key = "GROK_HOME"
    auth_file_name = "auth.json"
    has_terminal = True

    def __init__(
        self,
        model: str,
        *,
        executable: str = EXECUTABLE,
        **keywords: object,
    ) -> None:
        super().__init__(model, executable=executable, **keywords)

    def default_auth_home(self) -> Path:
        return Path.home() / ".grok"

    def provider_environment(self, home: Path) -> dict[str, str]:
        """This job's own Grok home, with the CLI's own extras disabled."""
        return {
            "GROK_HOME": str(home),
            "GROK_SUBAGENTS": "0",
            "GROK_WEB_FETCH": "0",
            "GROK_MEMORY": "0",
        }

    def prepare_home(
        self, home: Path, worktree: Path, request: CodingRequest
    ) -> None:
        """Install the generated sandbox profile this job will run under."""
        write_profile(home, worktree, tuple(request.blocked_paths))

    def command(self, prompt_path: Path, worktree: Path) -> list[str]:
        """The exact argument vector for one native coding session.

        `--sandbox` names the generated custom profile, which fails closed.
        Terminal and network access are available under D-063.
        """
        command = [
            self._executable,
            "--prompt-file", str(prompt_path),
            # Streamed, one line per agent update. The single end-of-session
            # envelope of `json` left the stall watchdog nothing to observe
            # but file writes, so a session that was reading and reasoning
            # through a large repair looked silent and was stopped as stalled
            # after ten minutes, twice, on 2026-10-05.
            "--output-format", "streaming-json",
            "--model", self._model,
            "--cwd", str(worktree),
            "--sandbox", PROFILE_NAME,
            "--disallowed-tools", ",".join(WITHHELD_TOOLS),
            "--no-subagents",
            "--max-turns", str(self._max_turns),
            "--verbatim",
            "--always-approve",
        ]
        if self._effort:
            command.extend(["--reasoning-effort", self._effort])
        return command

    def read_result(self, stdout: str) -> CodingSessionResult:
        """Read the streamed session. Its report is an account, not evidence.

        The stream carries the agent's text as deltas and ends with one `end`
        event holding the stop reason and turn count. A stream without one did
        not finish, whatever its exit status said.
        """
        events = self._events(stdout)
        errors = [event for event in events if event.get("type") == "error"]
        if errors:
            raise CodingError(
                "session_failed", reason_code="cli_failed",
                service_message=self._error_text(errors),
            )
        ends = [event for event in events if event.get("type") == "end"]
        if not ends:
            raise CodingError("session_failed", reason_code="session_end_missing")
        envelope = ends[-1]
        report = "".join(
            str(event.get("data") or "") for event in events
            if event.get("type") == "text"
        )
        stop_reason = str(envelope.get("stopReason") or "")
        turns = envelope.get("num_turns")
        diagnostics: dict[str, object] = {
            "provider": PROVIDER_NAME,
            "stop_reason": stop_reason,
        }
        completed_cleanly = self.stopped_cleanly(stop_reason)
        if not completed_cleanly:
            diagnostics["unresolved_issues"] = [f"session_stopped:{stop_reason}"]
        return CodingSessionResult(
            completed=completed_cleanly,
            report=report,
            turns=turns if isinstance(turns, int) and turns >= 0 else 0,
            failure_code="" if completed_cleanly else "session_failed",
            diagnostics=diagnostics,
        )

    def retained_output(self, lines) -> str:
        """Only the events the result is read from: text, end and error.

        The stream also carries every file the agent read; none of that is
        needed to read the result, and keeping it would put a long session's
        reading back into memory. Text is bounded; end and error are kept.
        """
        kept: list[str] = []
        text_size = 0
        control_size = 0
        for line in lines:
            if isinstance(line, OversizedLine):
                # Too long to decode. Bulk reading events are dropped by the
                # kind their prefix names; anything that could carry the result
                # is refused rather than accepted truncated.
                if _event_kind(line) not in _BULK_EVENTS:
                    raise CodingError("session_failed", reason_code="session_event_oversized")
                continue
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line.rstrip("\n"))  # left for _events to refuse
                continue
            kind = event.get("type") if isinstance(event, Mapping) else None
            if kind in ("end", "error"):
                control_size += len(line)
                if control_size > MAX_RETAINED_OUTPUT_CHARACTERS:
                    raise CodingError("session_failed", reason_code="session_event_oversized")
                kept.append(line.rstrip("\n"))
            elif kind == "text":
                text_size += len(line)
                if text_size <= MAX_RETAINED_OUTPUT_CHARACTERS:
                    kept.append(line.rstrip("\n"))
        return "\n".join(kept)

    def failure_output(self, stdout: str) -> str:
        """Only the CLI's own error events, never what the agent read."""
        try:
            events = self._events(stdout)
        except CodingError:
            return ""
        return self._error_text([e for e in events if e.get("type") == "error"])

    @staticmethod
    def _events(stdout: str) -> list[Mapping[str, object]]:
        """Each NDJSON line of the stream, or a declared refusal."""
        events: list[Mapping[str, object]] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise CodingError(
                    "session_failed", reason_code="response_json_invalid"
                ) from error
            if not isinstance(event, Mapping):
                raise CodingError(
                    "session_failed", reason_code="response_envelope_not_object"
                )
            events.append(event)
        return events

    @staticmethod
    def _error_text(errors: list[Mapping[str, object]]) -> str:
        """The CLI's error messages, without rewriting their content."""
        return "\n".join(error_event_message(event) for event in errors)

    def _read_result(self, stdout: str) -> CodingSessionResult:
        return self.read_result(stdout)


def coding_cli_present(executable: str = EXECUTABLE) -> bool:
    """Whether the Grok CLI can be found at all."""
    return subscription_cli_present(executable)


__all__ = [
    "EXECUTABLE",
    "NATIVE_TOOLS",
    "PROVIDER_NAME",
    "WITHHELD_TOOLS",
    "GrokCodingSession",
    "coding_cli_present",
]
