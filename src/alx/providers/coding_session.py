"""Launch one native Grok coding-agent session inside an assigned worktree.

This is the execution site for a coding job. It starts the Grok CLI as a real
agent: its own tools, its own multi-turn loop, its own context, with the
assigned worktree as its actual working directory. That is what V1 did and what
made it work.

What is different from V1 is containment. The session runs under a generated
custom sandbox profile, so git metadata, credential files and every blocked
path are refused by the kernel rather than by the agent's cooperation, and a
profile that cannot be applied stops the job instead of running unenforced.

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

import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - the coding-session launch site
from collections.abc import Callable, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from alx.contracts.coding import (
    CodingError,
    CodingRequest,
    CodingSessionResult,
)
from alx.providers.coding_containment import PROFILE_NAME, write_profile


LOGGER = logging.getLogger(__name__)

PROVIDER_NAME = "grok_subscription"

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
    "usage limit", "rate limit", "rate_limit", "quota",
    "too many requests", "resets at", "upgrade to",
)
_AUTHENTICATION_MARKERS = (
    "not logged in", "log in", "login", "unauthenticated", "unauthorized",
    "authentication", "invalid api key", "oauth", "credentials", "auth.json",
)
_SANDBOX_MARKERS = (
    "sandbox could not be applied",
    "refusing to start",
    "sandbox profile",
)


class GrokCodingSession:
    """One native coding-agent session per job, contained by the kernel."""

    def __init__(
        self,
        model: str,
        timeout_seconds: int,
        *,
        executable: str = "grok",
        max_turns: int = 60,
        runner: "Callable[..., subprocess.CompletedProcess] | None" = None,
        environment: Mapping[str, str] | None = None,
        effort: str = "",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_turns <= 1:
            # One turn is what broke the previous execution model: an agent
            # that cannot take a second turn cannot act on what it just read.
            raise ValueError("a native session needs more than one turn")
        if not model.strip():
            raise ValueError("model must not be blank")
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._executable = executable
        self._max_turns = max_turns
        self._runner = runner or subprocess.run
        self._environment = os.environ if environment is None else environment
        self._effort = effort.strip()

    def child_environment(self, grok_home: Path) -> dict[str, str]:
        """Process basics and this job's own Grok home. Never a metered key."""
        allowed = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
            "SYSTEMROOT", "WINDIR",
        }
        environment = {
            key: value
            for key, value in self._environment.items()
            if key in allowed and key not in _METERED_ENVIRONMENT_KEYS
        }
        environment["GROK_HOME"] = str(grok_home)
        environment["GROK_SUBAGENTS"] = "0"
        environment["GROK_WEB_FETCH"] = "0"
        environment["GROK_MEMORY"] = "0"
        return environment

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

    def run_session(
        self, request: CodingRequest, briefing: str
    ) -> CodingSessionResult:
        worktree = Path(request.worktree).expanduser().resolve()
        with TemporaryDirectory(prefix="alx-grok-session-") as working_directory:
            grok_home = Path(working_directory) / "grok-home"
            grok_home.mkdir(mode=0o700)
            self._install_subscription_auth(grok_home)
            write_profile(grok_home, worktree, tuple(request.blocked_paths))
            prompt_path = Path(working_directory) / "briefing.txt"
            prompt_path.write_text(briefing, encoding="utf-8")
            command = self.command(prompt_path, worktree)
            try:
                completed = self._runner(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                    env=self.child_environment(grok_home),
                    cwd=str(worktree),
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise CodingError(
                    "session_failed", reason_code="session_timeout"
                ) from error
            except FileNotFoundError as error:
                raise CodingError(
                    "coding_unavailable", reason_code="cli_not_installed"
                ) from error
            except OSError as error:
                raise CodingError(
                    "coding_unavailable", reason_code="cli_unavailable"
                ) from error

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if completed.returncode != 0:
            code, reason = self._failure(stderr, stdout)
            raise CodingError(
                code,
                reason_code=reason,
                exit_status=completed.returncode,
                stderr_characters=len(stderr),
            )
        return self._read_result(stdout)

    def _read_result(self, stdout: str) -> CodingSessionResult:
        """Read the session envelope. Its report is an account, not evidence."""
        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise CodingError(
                "session_failed", reason_code="response_json_invalid"
            ) from error
        if not isinstance(envelope, Mapping):
            raise CodingError(
                "session_failed", reason_code="response_envelope_not_object"
            )
        if envelope.get("is_error"):
            code, reason = self._failure(str(envelope.get("result", "")), "")
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
        # `end_turn` is the agent choosing to stop. Anything else means it was
        # cut off, which is a failure to complete even if files changed.
        completed_cleanly = stop_reason.strip().lower() in (
            "", "end_turn", "endturn",
        )
        if not completed_cleanly:
            diagnostics["unresolved_issues"] = [f"session_stopped:{stop_reason}"]
        return CodingSessionResult(
            completed=completed_cleanly,
            report=report,
            turns=turns if isinstance(turns, int) and turns >= 0 else 0,
            failure_code="" if completed_cleanly else "session_failed",
            diagnostics=diagnostics,
        )

    def _install_subscription_auth(self, grok_home: Path) -> None:
        """Copy the CLI login file only. Never copy an API key.

        The real Grok home is never symlinked: the CLI refuses a symlinked home
        under a write-denying sandbox, and a link would expose the rest of that
        directory to the session.
        """
        source_root = Path(
            self._environment.get("GROK_HOME") or Path.home() / ".grok"
        )
        source = source_root / "auth.json"
        if not source.is_file():
            return
        shutil.copy2(source, grok_home / "auth.json")
        (grok_home / "auth.json").chmod(0o600)

    @staticmethod
    def _failure(stderr: str, stdout: str) -> tuple[str, str]:
        text = f"{stderr}\n{stdout}".lower()
        if any(marker in text for marker in _SANDBOX_MARKERS):
            # The sandbox refusing to start is the containment working. The job
            # fails rather than running with its protections missing.
            return "sandbox_unusable", "sandbox_not_applied"
        if any(marker in text for marker in _USAGE_LIMIT_MARKERS):
            return "session_failed", "subscription_usage_exhausted"
        if any(marker in text for marker in _AUTHENTICATION_MARKERS):
            return "session_failed", "subscription_unauthenticated"
        return "session_failed", "cli_failed"


def coding_cli_present(executable: str = "grok") -> bool:
    """Whether the Grok CLI can be found at all."""
    return shutil.which(executable) is not None
