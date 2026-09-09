"""Allowlisted development commands inside one assigned coding worktree.

This is the one production site that starts a process for a coding job. It is
not the Sandbox, not the Claude subscription transport, and not a generic
shell. Commands are argv lists, never a shell string. Push, merge, deploy and
review invocation are refused here even if a coding model asks for them.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess  # noqa: S404 - the one coding-job process site
import sys

from alx.contracts.coding import (
    DEFAULT_COMMAND_SECONDS,
    MAX_COMMAND_OUTPUT_CHARACTERS,
    MAX_COMMAND_SECONDS,
    MAX_DIFF_CHARACTERS,
    CodingCommandRecord,
    CodingError,
)


_GIT_INSPECT = frozenset({"status", "diff", "log"})
_PYTHON_NAMES = frozenset({"python", "python3"})
_PYTEST_FLAGS = frozenset({
    "-q", "-v", "-x", "-k", "--tb=short", "--tb=line", "--tb=no",
    "-p", "no:cacheprovider",
})


def command_permitted(argv: list[str] | tuple[str, ...]) -> bool:
    """Whether this exact argv is a permitted development command.

    The check is the authority. A coding model cannot widen it, and a missing
    check would let push, merge or a generic shell through.
    """
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        return False
    if any("\x00" in item for item in argv):
        return False
    # A path in argv[0] would run a worktree binary under a permitted name.
    if argv[0] != Path(argv[0]).name:
        return False
    executable = argv[0].lower()
    rest = tuple(argv[1:])
    if executable == "git":
        if not rest or rest[0] not in _GIT_INSPECT:
            return False
        if any(
            item in {"-c", "--exec-path", "--upload-pack", "--receive-pack"}
            or item.startswith("-c")
            for item in rest[1:]
        ):
            return False
        return True
    if executable in _PYTHON_NAMES:
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] in {"pytest", "unittest"}:
            return _pytest_args_permitted(rest[2:])
        return False
    if executable == "pytest":
        return _pytest_args_permitted(rest)
    return False


def _pytest_args_permitted(args: tuple[str, ...]) -> bool:
    expecting_value = False
    for item in args:
        if expecting_value:
            expecting_value = False
            continue
        if item in {"-k", "-p"}:
            expecting_value = True
            continue
        if item in _PYTEST_FLAGS or item.startswith("--tb="):
            continue
        if item.startswith("-"):
            return False
        if Path(item).is_absolute() or item.startswith(".."):
            return False
    return not expecting_value


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _clean_environment() -> dict[str, str]:
    """PATH and locale only. No inherited credentials, tokens or AL/X config."""
    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "HOME")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_permitted_command(
    argv: list[str],
    worktree: Path,
    timeout_seconds: int = DEFAULT_COMMAND_SECONDS,
) -> CodingCommandRecord:
    """Run one allowlisted command with cwd bound to the worktree."""
    if not command_permitted(argv):
        raise CodingError("command_not_permitted")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise CodingError("arguments_unusable")
    if not 1 <= timeout_seconds <= MAX_COMMAND_SECONDS:
        raise CodingError("arguments_unusable")
    bound = list(argv)
    if bound[0].lower() in _PYTHON_NAMES:
        # The allowlist names a Python interpreter, not a particular binary
        # path. Use this runtime's interpreter so a host without `python` on
        # PATH still runs the assigned tests.
        bound[0] = sys.executable
    try:
        completed = subprocess.run(  # noqa: S603 - argv, never shell
            bound,
            cwd=worktree,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return CodingCommandRecord(
            tuple(argv),
            -1,
            _bound(stdout, MAX_COMMAND_OUTPUT_CHARACTERS),
            _bound(stderr, MAX_COMMAND_OUTPUT_CHARACTERS),
            True,
            True,
        )
    except OSError as error:
        raise CodingError("coding_unavailable") from error
    return CodingCommandRecord(
        tuple(argv),
        completed.returncode,
        _bound(completed.stdout or "", MAX_COMMAND_OUTPUT_CHARACTERS),
        _bound(completed.stderr or "", MAX_COMMAND_OUTPUT_CHARACTERS),
        False,
        True,
    )


def inspect_git(worktree: Path) -> tuple[str, str]:
    """Mechanical git status and diff. One correct outcome, so not a model call."""
    status = run_permitted_command(["git", "status", "--porcelain"], worktree)
    diff = run_permitted_command(["git", "diff"], worktree)
    return (
        _bound(status.stdout or status.stderr, MAX_COMMAND_OUTPUT_CHARACTERS),
        _bound(diff.stdout, MAX_DIFF_CHARACTERS),
    )


def files_from_git_status(status: str) -> tuple[str, ...]:
    names = []
    for line in status.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            names.append(path)
    return tuple(names)


def is_test_command(argv: tuple[str, ...]) -> bool:
    if not argv:
        return False
    name = Path(argv[0]).name.lower()
    if name == "pytest":
        return True
    return name in _PYTHON_NAMES and len(argv) >= 3 and argv[1] == "-m" and argv[2] in {
        "pytest",
        "unittest",
    }
