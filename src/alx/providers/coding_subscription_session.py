"""Run one coding-agent session on a subscription CLI, whichever CLI it is.

The Coding Agent's execution half was written against one CLI, and roughly
seventy per cent of it turned out to be about *running a subscription coding
CLI under containment* rather than about that particular CLI. This module is
that part, extracted so a second adapter is a handful of small methods rather
than a parallel copy of the orchestration.

What lives here is what was already proven neutral by being true of any such
CLI: per-job temporary state, a briefing handed over as a file, the subprocess
lifecycle and its error mapping, an environment allowlist that strips every
metered credential, copying a login file rather than a key, and the failure
classification that distinguishes containment refusing to start from an
exhausted subscription from an unauthenticated one.

What stays with each adapter is the CLI's own vocabulary: its executable, its
flags, where it keeps its login, what it calls its tools, and the key
spellings in the envelope it returns.

The boundary is deliberately drawn at behaviour, not convenience. A subclass
cannot reach around this class to launch a process differently: `run_session`
is final in practice, and every provider-specific decision it needs arrives
through a named hook. That is what keeps one execution route under Law 0 while
allowing more than one CLI behind it.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess  # noqa: S404 - the coding-session launch site
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from alx.contracts.coding import (
    CodingError,
    CodingRequest,
    CodingSessionResult,
)


LOGGER = logging.getLogger(__name__)

# The most session output spooled to disk before the session is stopped as
# session_output_limit. A size bound, not a time bound: a session may run as
# long as it stays active, but it may not fill the disk.
MAX_SESSION_OUTPUT_BYTES = 512 * 1024 * 1024
# How much of that output is read back into memory to form the result.
MAX_RETAINED_OUTPUT_CHARACTERS = 1_000_000
MAX_RETAINED_STDERR_CHARACTERS = 64_000


# The longest spooled line read back whole. Anything longer is never decoded:
# only a short prefix is kept, so the adapter can tell what kind it was.
MAX_EVENT_LINE_CHARACTERS = 1_000_000
OVERSIZED_PREFIX_CHARACTERS = 256


class OversizedLine(str):
    """The prefix of a spooled line too long to read back whole."""


def bounded_lines(handle) -> Iterator[str]:
    """Each line of a spooled file, none read whole past the bound."""
    while True:
        line = handle.readline(MAX_EVENT_LINE_CHARACTERS + 1)
        if not line:
            return
        if len(line) > MAX_EVENT_LINE_CHARACTERS and not line.endswith("\n"):
            prefix = line[:OVERSIZED_PREFIX_CHARACTERS]
            # Skip the rest of the line without keeping it.
            while True:
                rest = handle.readline(MAX_EVENT_LINE_CHARACTERS)
                if not rest or rest.endswith("\n"):
                    break
            yield OversizedLine(prefix)
            continue
        yield line


def _tail(path: Path, characters: int) -> str:
    """The last `characters` of a spooled file, read without loading all of it."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - characters * 4))
            return handle.read().decode("utf-8", errors="replace")[-characters:]
    except OSError:
        return ""

# Process basics only. Anything not named here does not reach the child, so a
# credential the host happens to export cannot be inherited by accident.
ALLOWED_ENVIRONMENT_KEYS = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "WINDIR",
})

# Metered credentials, removed rather than merely absent. A session runs on a
# subscription login; if that subscription is exhausted the job must fail
# rather than quietly continue as billed API usage. Keys for every provider
# are stripped regardless of which CLI is running, because the point is that
# no metered key reaches a coding session at all.
METERED_ENVIRONMENT_KEYS = frozenset({
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
})

# Compared against, rather than iterated: see `child_environment`.
_METERED_KEYS_FOLDED = frozenset(
    key.upper() for key in METERED_ENVIRONMENT_KEYS
)

USAGE_LIMIT_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "quota",
    "too many requests", "resets at", "upgrade to",
)
AUTHENTICATION_MARKERS = (
    "not logged in", "log in", "login", "unauthenticated", "unauthorized",
    "authentication", "invalid api key", "oauth", "credentials", "auth.json",
)
SANDBOX_MARKERS = (
    "sandbox could not be applied",
    "refusing to start",
    "sandbox profile",
)


def classify_failure(stderr: str, stdout: str) -> tuple[str, str]:
    """Why a subscription coding CLI failed, as a declared failure code.

    Containment refusing to start is checked first and deliberately: it is the
    containment working, and the job must fail rather than run with its
    protections missing. An exhausted subscription and an unauthenticated one
    are distinguished because they need different answers from AL/X.
    """
    text = f"{stderr}\n{stdout}".lower()
    if any(marker in text for marker in SANDBOX_MARKERS):
        return "sandbox_unusable", "sandbox_not_applied"
    if any(marker in text for marker in USAGE_LIMIT_MARKERS):
        return "session_failed", "subscription_usage_exhausted"
    if any(marker in text for marker in AUTHENTICATION_MARKERS):
        return "session_failed", "subscription_unauthenticated"
    return "session_failed", "cli_failed"


class SubscriptionCodingSession:
    """One contained coding session per job, on a subscription CLI.

    Concrete adapters supply the CLI's vocabulary through the hooks below.
    They do not supply a second way to start a process: the lifecycle, the
    containment setup and the error mapping are here and are the only route.
    """

    #: The provider identity carried in session diagnostics.
    provider_name: str = ""
    #: Short name used for this job's temporary directory.
    session_label: str = "coding"
    #: Environment variable naming the CLI's home directory, if it has one.
    home_environment_key: str = ""
    #: The login file copied into the per-job home, if the CLI keeps one.
    auth_file_name: str = ""

    def __init__(
        self,
        model: str,
        *,
        executable: str,
        stall_seconds: int | float,
        max_turns: int = 60,
        runner: "Callable[..., subprocess.CompletedProcess] | None" = None,
        environment: Mapping[str, str] | None = None,
        effort: str = "",
    ) -> None:
        # The only bound on a session: silence. There is no absolute ceiling;
        # a session may run as long as it keeps showing real activity.
        if isinstance(stall_seconds, bool) or not stall_seconds > 0:
            raise ValueError("stall_seconds must be positive")
        if max_turns <= 1:
            # One turn is what broke the previous execution model: an agent
            # that cannot take a second turn cannot act on what it just read.
            raise ValueError("a native session needs more than one turn")
        if not model.strip():
            raise ValueError("model must not be blank")
        self._model = model
        self._stall_seconds = stall_seconds
        self._executable = executable
        self._max_turns = max_turns
        self._runner = runner or subprocess.run
        self._environment = os.environ if environment is None else environment
        self._effort = effort.strip()

    @property
    def model_name(self) -> str:
        """Configured session model, for runtime diagnostics only."""
        return self._model

    # --- provider-specific hooks -------------------------------------------

    def command(self, prompt_path: Path, worktree: Path) -> list[str]:
        """The exact argument vector for one session. Adapter-specific."""
        raise NotImplementedError

    def provider_environment(self, home: Path) -> dict[str, str]:
        """CLI-specific environment on top of the allowlist. Adapter-specific."""
        raise NotImplementedError

    def prepare_home(self, home: Path, worktree: Path, request: CodingRequest) -> None:
        """Install containment and anything else the CLI needs in its home.

        Called before the process starts, with a private directory this job
        owns. An adapter that cannot install its containment must raise rather
        than continue: a session running without its protections is the one
        outcome this whole path exists to prevent.
        """
        raise NotImplementedError

    def read_result(self, stdout: str) -> CodingSessionResult:
        """Read the CLI's envelope. Adapter-specific key spellings."""
        raise NotImplementedError

    def retained_output(self, lines: Iterable[str]) -> str:
        """What of the spooled stdout is kept to read the session's result.

        All of it by default, within MAX_RETAINED_OUTPUT_CHARACTERS. An adapter
        whose stream also carries what the agent read keeps only the events its
        result is read from.
        """
        kept: list[str] = []
        size = 0
        for line in lines:
            if isinstance(line, OversizedLine):
                # Unknown content that cannot be read back: refused rather
                # than silently left out of the result.
                raise CodingError("session_failed", reason_code="session_event_oversized")
            size += len(line)
            if size > MAX_RETAINED_OUTPUT_CHARACTERS:
                break
            kept.append(line.rstrip("\n"))
        return "\n".join(kept)

    def failure_output(self, stdout: str) -> str:
        """The part of stdout that may explain a failed exit.

        The whole of it by default. An adapter whose stdout streams what the
        agent read returns only the CLI's own error reports: file contents
        that happen to mention a usage limit must not classify the failure.
        """
        return stdout

    # --- neutral orchestration ---------------------------------------------

    def child_environment(self, home: Path) -> dict[str, str]:
        """Process basics and this job's own CLI home. Never a metered key.

        The adapter's own variables are merged and then held to the same rule
        as the host's. Filtering only what is copied from the host would make
        the guarantee depend on every future adapter's restraint: an adapter
        naming a metered key among its "required" variables would put a billed
        credential straight back into the subprocess after the host's copy had
        been stripped. Found in the PR #33 review and reproduced.

        This refuses rather than silently dropping the key. An adapter asking
        for a metered credential has misunderstood what a subscription session
        is, and a job that fails closed is a better answer than one that runs
        with the credential quietly removed and fails somewhere less legible.
        """
        environment = {
            key: value
            for key, value in self._environment.items()
            if key in ALLOWED_ENVIRONMENT_KEYS
            and key not in METERED_ENVIRONMENT_KEYS
        }
        provider = self.provider_environment(home)
        # Compared case-insensitively and whitespace-stripped. Environment
        # variable names are case-insensitive on macOS and Windows, so an exact
        # set-membership test is defeated by spelling the key in lower case:
        # `anthropic_api_key` reached the subprocess as the same variable the
        # check was written to refuse. Found by attacking this check after the
        # PR #33 review reported it resolved.
        metered = sorted(
            key for key in provider
            if key.strip().upper() in _METERED_KEYS_FOLDED
        )
        if metered:
            raise CodingError(
                "session_failed",
                reason_code="provider_environment_carries_metered_key",
                metered_count=len(metered),
            )
        environment.update(provider)
        return environment

    def run_session(
        self, request: CodingRequest, briefing: str
    ) -> CodingSessionResult:
        """Run one session in the assigned worktree and return its account.

        The report that comes back is the agent's own account, never evidence.
        AL/X verifies the repository and the tests separately.
        """
        worktree = Path(request.worktree).expanduser().resolve()
        prefix = f"alx-{self.session_label}-session-"
        with TemporaryDirectory(prefix=prefix) as working_directory:
            home = Path(working_directory) / "home"
            home.mkdir(mode=0o700)
            self.install_subscription_auth(home)
            self.prepare_home(home, worktree, request)
            prompt_path = Path(working_directory) / "briefing.txt"
            prompt_path.write_text(briefing, encoding="utf-8")
            command = self.command(prompt_path, worktree)
            # Output is spooled to this job's own directory, never held in
            # memory: with no deadline, a long and talkative session would
            # otherwise grow the runtime's memory for as long as it ran.
            stdout_path = Path(working_directory) / "session.stdout"
            stderr_path = Path(working_directory) / "session.stderr"
            stdout_path.touch()
            stderr_path.touch()
            try:
                from alx.providers.coding_process import run_coding_subprocess
                completed = run_coding_subprocess(self._runner,
                    command,
                    # No deadline: inactivity is the session's only bound.
                    # Relayed output is activity, and its size is bounded
                    # where it is written instead of its duration.
                    timeout=None,
                    inactivity_timeout=self._stall_seconds,
                    activity_root=worktree,
                    activity_blocked_paths=request.blocked_paths,
                    spool_to=(stdout_path, stderr_path),
                    output_limit_bytes=MAX_SESSION_OUTPUT_BYTES,
                    env=self.child_environment(home),
                    cwd=str(worktree),
                    stdin=subprocess.DEVNULL,
                    shell=False,
                    check=False,
                )
            except FileNotFoundError as error:
                raise CodingError(
                    "coding_unavailable", reason_code="cli_not_installed"
                ) from error
            except OSError as error:
                raise CodingError(
                    "coding_unavailable", reason_code="cli_unavailable"
                ) from error
            # A substituted runner may still answer with text directly.
            if isinstance(completed.stdout, str):
                stdout = self.retained_output(completed.stdout.splitlines())
                stderr = (completed.stderr or "")[-MAX_RETAINED_STDERR_CHARACTERS:]
            else:
                with open(stdout_path, encoding="utf-8", errors="replace") as spooled:
                    stdout = self.retained_output(bounded_lines(spooled))
                stderr = _tail(stderr_path, MAX_RETAINED_STDERR_CHARACTERS)

        if completed.returncode != 0:
            code, reason = classify_failure(stderr, self.failure_output(stdout))
            raise CodingError(
                code,
                reason_code=reason,
                exit_status=completed.returncode,
                stderr_characters=len(stderr),
            )
        return self.read_result(stdout)

    def install_subscription_auth(self, home: Path) -> None:
        """Copy the CLI login file only. Never copy an API key.

        The real home is never symlinked: a CLI can refuse a symlinked home
        under a write-denying sandbox, and a link would expose the rest of that
        directory to the session. Adapters that keep no login file leave
        `auth_file_name` blank and nothing is copied.
        """
        if not self.auth_file_name:
            return
        source = self.auth_home() / self.auth_file_name
        if not source.is_file():
            return
        destination = home / self.auth_file_name
        shutil.copy2(source, destination)
        destination.chmod(0o600)

    def auth_home(self) -> Path:
        """Where this CLI keeps its real login, honouring its home variable."""
        configured = (
            self._environment.get(self.home_environment_key, "")
            if self.home_environment_key
            else ""
        )
        if configured:
            return Path(configured)
        return self.default_auth_home()

    def default_auth_home(self) -> Path:
        """The CLI's login directory when its home variable is unset."""
        raise NotImplementedError

    @staticmethod
    def stopped_cleanly(stop_reason: str) -> bool:
        """Whether the agent chose to stop, rather than being cut off.

        Anything other than an end-of-turn is a failure to complete, even if
        files changed: a session cut off mid-repair has not done the job.
        """
        return stop_reason.strip().lower() in ("", "end_turn", "endturn")


def subscription_cli_present(executable: str) -> bool:
    """Whether a subscription coding CLI can be found at all."""
    return shutil.which(executable) is not None


__all__ = [
    "ALLOWED_ENVIRONMENT_KEYS",
    "AUTHENTICATION_MARKERS",
    "METERED_ENVIRONMENT_KEYS",
    "SANDBOX_MARKERS",
    "USAGE_LIMIT_MARKERS",
    "SubscriptionCodingSession",
    "classify_failure",
    "subscription_cli_present",
]
