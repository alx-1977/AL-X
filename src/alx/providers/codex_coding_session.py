"""Launch one native Codex coding-agent session inside an assigned worktree.

The Codex adapter for `SubscriptionCodingSession` (D-052). The orchestration
is the shared one; what lives here is Codex's vocabulary: its executable and
flags, its permission profile, and the events it streams.

Containment is Codex's own kernel sandbox, configured per job as a named
permission profile passed on the command line, never read from Friedl's
`~/.codex/config.toml` (which is ignored, so nothing there can widen it):

- reads are denied across the disk except the system paths tools need, the
  Python installation that runs AL/X's tests, and the worktree itself;
- writes are allowed in the worktree and a temporary directory this job owns;
- the worktree's git metadata, every credential shape, and every path AL/X
  blocked for the job are denied for read and write;
- network is off for every command the agent runs.

Measured against codex-cli 0.157.1 before this was written: outside files,
`.env`, nested `.env.*`, `.git` and `~/.ssh` were all refused, the network was
unreachable, and AL/X's own tests ran inside the profile.

Unlike the Grok session, this one has a terminal. Codex reads and searches
files with shell commands, so withholding the shell would leave it unable to
work; the terminal runs inside the profile above, and only AL/X's own
verification afterwards counts.

The login is the real Codex home's, not a copy. A copied login refreshes its
token in the copy and leaves the original holding a refresh token the
provider has already rotated away, which signs the CLI out.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

from alx.contracts.coding import CodingError, CodingRequest, CodingSessionResult
from alx.providers.coding_containment import CREDENTIAL_DENY_GLOBS, deny_entries
from alx.providers.coding_subscription_session import (
    MAX_RETAINED_OUTPUT_CHARACTERS,
    OversizedLine,
    SubscriptionCodingSession,
    classify_failure,
)


LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "codex_subscription"
EXECUTABLE = "codex"
PROFILE_NAME = "alx_coding_job"
# Written by `prepare_home` and read back by `command`, both inside this job's
# private directory, so two jobs never share a profile.
PROFILE_FILE = "permissions.json"

# Events whose bulk is what the agent ran or read, never its result.
_BULK_ITEMS = frozenset({"command_execution", "file_change", "reasoning", "todo_list"})


def _toml(value: object) -> str:
    """A TOML inline value. JSON strings are valid TOML basic strings."""
    if isinstance(value, Mapping):
        return "{" + ", ".join(
            f"{json.dumps(str(key))}={_toml(item)}" for key, item in value.items()
        ) + "}"
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(str(value))


def permission_profile(
    worktree: Path, blocked_paths: tuple[str, ...], temporary: Path,
    runtime: Path, cli: tuple[Path, ...] = (),
) -> dict[str, object]:
    """The exact permission profile one coding job runs under.

    `deny_entries` is the same list the Grok profile denies: the worktree's
    git metadata, every credential glob, and each blocked path anchored to
    this worktree. Absolute entries are denied as paths; the credential globs
    are denied inside the workspace root. `cli` is the Codex executable
    itself, which Codex runs inside the sandbox as its own file helper.
    """
    filesystem: dict[str, object] = {
        ":root": "deny",
        ":minimal": "read",
        ":slash_tmp": "deny",
        str(runtime): "read",
        str(temporary): "write",
        **{str(path): "read" for path in cli},
    }
    workspace: dict[str, str] = {}
    for entry in deny_entries(worktree, blocked_paths):
        if entry in CREDENTIAL_DENY_GLOBS:
            workspace[entry] = "deny"
        else:
            filesystem[entry] = "deny"
    filesystem[":workspace_roots"] = workspace
    return {
        "extends": ":workspace",
        "filesystem": filesystem,
        "network": {"enabled": False},
    }


class CodexCodingSession(SubscriptionCodingSession):
    """One native Codex session per job, contained by its own sandbox."""

    provider_name = PROVIDER_NAME
    session_label = "codex"
    home_environment_key = "CODEX_HOME"
    # Never copied: see the module docstring. Only its presence is checked.
    auth_file_name = ""
    login_file_name = "auth.json"
    has_terminal = True

    def __init__(
        self,
        model: str,
        *,
        executable: str = EXECUTABLE,
        runtime_prefix: Path | None = None,
        **keywords: object,
    ) -> None:
        super().__init__(model, executable=executable, **keywords)
        # The Python that runs AL/X runs her tests, so the agent may read it.
        self._runtime = Path(runtime_prefix or sys.base_prefix).resolve()

    def default_auth_home(self) -> Path:
        home = self._environment.get("HOME", "")
        return (Path(home) if home else Path.home()) / ".codex"

    def _cli_paths(self) -> tuple[Path, ...]:
        """The directories holding the executable and the installation it links to.

        Directories, because the sandbox refuses to exec through a symlink
        granted only as a file (measured: execvp is refused with the link
        and its target granted, and allowed with their directories).
        """
        found = shutil.which(self._executable)
        if found is None:
            raise CodingError("coding_unavailable", reason_code="cli_not_installed")
        link = Path(found).absolute()
        target = link.resolve()
        installation = target.parent.parent if target.parent.name == "bin" else target.parent
        return tuple(dict.fromkeys((link.parent, installation)))

    def provider_environment(self, home: Path) -> dict[str, str]:
        """The real Codex home for the login; this job's own temporary dir."""
        environment = {"TMPDIR": str(home / "tmp")}
        configured = self._environment.get(self.home_environment_key, "")
        if configured:
            environment[self.home_environment_key] = configured
        return environment

    def prepare_home(
        self, home: Path, worktree: Path, request: CodingRequest
    ) -> None:
        """Write this job's permission profile. Refuses rather than run open."""
        temporary = home / "tmp"
        temporary.mkdir(mode=0o700)
        profile = permission_profile(
            worktree, tuple(request.blocked_paths), temporary, self._runtime,
            self._cli_paths(),
        )
        (home / PROFILE_FILE).write_text(json.dumps(profile), encoding="utf-8")

    def command(self, prompt_path: Path, worktree: Path) -> list[str]:
        """The exact argument vector for one native coding session.

        The briefing arrives on stdin through the trailing `-`, so task text
        never enters the argument vector. `--ignore-user-config` keeps Friedl's own Codex
        configuration, including any older `sandbox_mode` that would
        override a permission profile, out of the job.
        """
        home = prompt_path.parent / "home"
        try:
            profile = json.loads((home / PROFILE_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CodingError(
                "sandbox_unusable", reason_code="sandbox_not_applied"
            ) from error
        command = [
            self._executable, "exec",
            "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--model", self._model,
            "--cd", str(worktree),
            "--config", f"permissions.{PROFILE_NAME}={_toml(profile)}",
            "--config", f'default_permissions="{PROFILE_NAME}"',
            "--config", 'approval_policy="never"',
            "--config", 'web_search="disabled"',
        ]
        if self._effort:
            command.extend(["--config", f'model_reasoning_effort="{self._effort}"'])
        command.append("-")
        return command

    def briefing_on_stdin(self) -> bool:
        return True

    def read_result(self, stdout: str) -> CodingSessionResult:
        """Read the streamed session. Its report is an account, not evidence.

        A turn that completed ends with `turn.completed`; one that failed
        ends with `turn.failed` or an `error` event. A stream with neither did
        not finish, whatever its exit status said.
        """
        events = self._events(stdout)
        failures = [
            event for event in events
            if event.get("type") in ("error", "turn.failed")
        ]
        if failures:
            code, reason = classify_failure(self._error_text(failures), "")
            raise CodingError(code, reason_code=reason)
        completed = [event for event in events if event.get("type") == "turn.completed"]
        if not completed:
            raise CodingError("session_failed", reason_code="session_end_missing")
        messages = [
            str(item.get("text") or "")
            for event in events
            if event.get("type") == "item.completed"
            and isinstance(item := event.get("item"), Mapping)
            and item.get("type") == "agent_message"
        ]
        turns = sum(1 for event in events if event.get("type") == "turn.started")
        return CodingSessionResult(
            completed=True,
            report=messages[-1] if messages else "",
            turns=turns,
            diagnostics={"provider": PROVIDER_NAME, "stop_reason": "turn_completed"},
        )

    def retained_output(self, lines) -> str:
        """Only what the result is read from: messages, turn ends and errors.

        Command output and file changes are what the agent did, and the
        repository diff and AL/X's own verification are the evidence for
        that; keeping them would hold a long session's output in memory.
        """
        kept: list[str] = []
        size = 0
        for line in lines:
            if isinstance(line, OversizedLine):
                # Too long to decode. Only command output or a file change can
                # be that large; anything else could carry the result.
                if not any(f'"{kind}"' in line for kind in _BULK_ITEMS):
                    raise CodingError("session_failed", reason_code="session_event_oversized")
                continue
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line.rstrip("\n"))  # left for _events to refuse
                continue
            if not isinstance(event, Mapping):
                kept.append(line.rstrip("\n"))
                continue
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") in _BULK_ITEMS:
                continue
            size += len(line)
            if size > MAX_RETAINED_OUTPUT_CHARACTERS:
                raise CodingError("session_failed", reason_code="session_event_oversized")
            kept.append(line.rstrip("\n"))
        return "\n".join(kept)

    def failure_output(self, stdout: str) -> str:
        """Only the CLI's own error reports, never what the agent read."""
        try:
            events = self._events(stdout)
        except CodingError:
            return ""
        return self._error_text([
            event for event in events if event.get("type") in ("error", "turn.failed")
        ])

    @staticmethod
    def _events(stdout: str) -> list[Mapping[str, object]]:
        """Each JSONL line of the stream, or a declared refusal."""
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
        """Error events as text, read only to choose a declared failure code."""
        return "\n".join(json.dumps(event, sort_keys=True) for event in errors)


__all__ = [
    "EXECUTABLE",
    "PROFILE_NAME",
    "PROVIDER_NAME",
    "CodexCodingSession",
    "permission_profile",
]
