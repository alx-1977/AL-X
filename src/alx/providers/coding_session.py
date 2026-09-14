"""Launch one native Grok coding-agent session inside an assigned worktree.

This is the Grok adapter for `SubscriptionCodingSession`, which holds the
orchestration every subscription coding CLI shares. What remains here is
Grok's own vocabulary: its executable, its flags, where it keeps its login,
what it calls its tools, and the key spellings in the envelope it returns.

It starts the Grok CLI as a real agent: its own tools, its own multi-turn
loop, its own context, with the assigned worktree as its actual working
directory. That is what V1 did and what made it work.

What is different from V1 is containment. The session runs under a generated
custom sandbox profile, so git metadata, credential files and every blocked
path are refused by the kernel rather than by the agent's cooperation, and a
profile that cannot be applied stops the job instead of running unenforced.
That profile is Grok-specific and so is installed here rather than in the
shared base: another CLI contains its sessions its own way, and pretending
otherwise would put a containment decision behind an abstraction.

Two things are deliberately withheld in this iteration:

- **the terminal.** `run_terminal_cmd` and its aliases are removed, so the
  agent cannot run commands or tests. The CLI's deny-by-default permission mode
  proved not to be one on the installed build, and its hooks fail open, so the
  only reliable way to withhold shell today is not to hand it over. AL/X runs
  the tests afterwards through its own allowlisted executor;
- **metered credit.** The child receives the subscription login and nothing
  else. `XAI_API_KEY` and every other metered key are removed rather than left
  absent, so an exhausted subscription fails the job instead of quietly
  becoming billed API usage.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from alx.contracts.coding import CodingError, CodingRequest, CodingSessionResult
from alx.providers.coding_containment import PROFILE_NAME, write_profile
from alx.providers.coding_subscription_session import (
    SubscriptionCodingSession,
    classify_failure,
    subscription_cli_present,
)


LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "grok_subscription"

EXECUTABLE = "grok"

# Every tool that can start a process, reach the network, or spawn a second
# agent. The terminal is withheld for this iteration; web access and subagents
# are withheld because neither is part of editing an assigned worktree.
WITHHELD_TOOLS: tuple[str, ...] = (
    "run_terminal_cmd",
    "run_terminal_command",
    "Bash",
    "bash",
    "shell",
    "web_search",
    "web_fetch",
    "Agent",
    "task",
    "spawn_subagent",
)

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


class GrokCodingSession(SubscriptionCodingSession):
    """One native coding-agent session per job, contained by the kernel."""

    provider_name = PROVIDER_NAME
    session_label = "grok"
    home_environment_key = "GROK_HOME"
    auth_file_name = "auth.json"

    def __init__(
        self,
        model: str,
        timeout_seconds: int,
        *,
        executable: str = EXECUTABLE,
        **keywords: object,
    ) -> None:
        super().__init__(
            model, timeout_seconds, executable=executable, **keywords
        )

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
        `--disallowed-tools` withholds the terminal; it wins over `--tools` on
        the installed CLI, so the two are consistent rather than contradictory.
        """
        command = [
            self._executable,
            "--prompt-file", str(prompt_path),
            "--output-format", "json",
            "--model", self._model,
            "--cwd", str(worktree),
            "--sandbox", PROFILE_NAME,
            "--disallowed-tools", ",".join(WITHHELD_TOOLS),
            "--disable-web-search",
            "--no-subagents",
            "--max-turns", str(self._max_turns),
            "--verbatim",
            "--always-approve",
        ]
        if self._effort:
            command.extend(["--reasoning-effort", self._effort])
        return command

    def read_result(self, stdout: str) -> CodingSessionResult:
        """Read the session envelope. Its report is an account, not evidence."""
        envelope = self.envelope_object(stdout)
        if envelope.get("is_error"):
            code, reason = classify_failure(str(envelope.get("result", "")), "")
            raise CodingError(code, reason_code=reason)
        report = envelope.get("text")
        if not isinstance(report, str):
            report = str(envelope.get("result") or "")
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

    # Retained because the behaviour probe and existing tests name it. It is
    # the shared classification, not a Grok-specific second implementation.
    @staticmethod
    def _failure(stderr: str, stdout: str) -> tuple[str, str]:
        return classify_failure(stderr, stdout)

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
