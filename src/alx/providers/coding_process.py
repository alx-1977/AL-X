"""Allowlisted development commands inside one assigned coding worktree.

This is the one production site that starts a process for a coding job. It is
not the Sandbox, not the Claude subscription transport, and not a generic
shell. Commands are argv lists, never a shell string. Push, merge, deploy and
review invocation are refused here even if a coding model asks for them.
"""

from __future__ import annotations

import os
import hashlib
import io
import json
import platform
import signal
import tarfile
import tempfile
from importlib import metadata
from contextvars import ContextVar
from threading import Event, Lock
from time import monotonic
from typing import Any, Callable
from collections.abc import Sequence
from collections import Counter
from dataclasses import dataclass
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
    path_matches_blocked,
)
from alx.contracts.coding_verification import pytest_failure_signature


_CURRENT: ContextVar["CodingCancellation | None"] = ContextVar(
    "alx_coding_cancellation", default=None
)


class CodingCancellation:
    def __init__(self) -> None:
        self.requested = Event()
        self._lock = Lock()
        self._process: subprocess.Popen[Any] | None = None

    def cancel(self) -> None:
        self.requested.set()

    def check(self) -> None:
        if self.requested.is_set():
            raise CodingError("coding_cancelled")

    def run(self, runner: Callable[..., Any], argv: list[str], **kwargs: Any) -> Any:
        self.check()
        if runner is not subprocess.run:
            result = runner(argv, **kwargs)
            self.check()
            return result
        timeout = kwargs.pop("timeout", None)
        capture = kwargs.pop("capture_output", False)
        if capture:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        supplied_input = kwargs.pop("input", None)
        if supplied_input is not None:
            kwargs["stdin"] = subprocess.PIPE
        kwargs.pop("check", None)
        kwargs["start_new_session"] = True
        process = subprocess.Popen(argv, **kwargs)
        with self._lock:
            self._process = process
        try:
            self.check()
            deadline = None if timeout is None else monotonic() + timeout
            first = True
            while True:
                remaining = None if deadline is None else max(0, deadline - monotonic())
                if remaining == 0:
                    _stop(process)
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    stdout, stderr = process.communicate(
                        input=supplied_input if first else None,
                        timeout=min(0.1, remaining) if remaining is not None else 0.1,
                    )
                    self.check()
                    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    first = False
                    self.check()
        finally:
            with self._lock:
                self._process = None
            if process.poll() is None:
                _stop(process)
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                # A grandchild that escaped its parent's process group may
                # still hold a pipe open. It must not hold the Core worker.
                for pipe in (process.stdout, process.stderr, process.stdin):
                    if pipe is not None:
                        pipe.close()


def _stop(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def check_cancelled() -> None:
    current = _CURRENT.get()
    if current is not None:
        current.check()


def run_coding_subprocess(runner: Callable[..., Any], argv: list[str], **kwargs: Any) -> Any:
    current = _CURRENT.get()
    return runner(argv, **kwargs) if current is None else current.run(runner, argv, **kwargs)


def bind_cancellation(cancellation: CodingCancellation):
    return _CURRENT.set(cancellation)


def reset_cancellation(token: Any) -> None:
    _CURRENT.reset(token)


_GIT_INSPECT = frozenset({"status", "diff", "log"})
_GIT_FLAGS = {
    # `-uall` names every untracked file rather than collapsing a new
    # directory to one `dir/` entry. The coding job's changed-file set is
    # derived from this status and is what D-029 stages, and D-029 takes
    # concrete files only, so a collapsed directory entry would name something
    # it must refuse.
    "status": frozenset({"--porcelain", "--porcelain=v1", "-z", "-uall"}),
    # `--check` makes `git diff` report whitespace errors and conflict markers
    # instead of printing a patch. It writes nothing, so it belongs with the
    # other read-only diff forms, and it is the one check every coding job runs
    # whatever it changed.
    "diff": frozenset({"--stat", "--name-only", "--cached", "--no-color", "--check"}),
    "log": frozenset({"--oneline", "--no-color"}),
}
_PYTHON_NAMES = frozenset({"python", "python3"})
# The repository's own law gates, named exactly. `python <path>` is otherwise
# refused outright, because a script path is arbitrary code chosen by whoever
# supplied the path; these two are permitted as literal argv forms with no
# arguments at all, so the permission cannot be widened by appending a flag or
# pointed at a different file by changing the spelling. They are read-only
# checks that already run in CI.
_GATE_SCRIPTS = frozenset({
    "scripts/check_governance.py",
    "scripts/check_architecture.py",
})
_PYTEST_FLAGS = frozenset({
    "-q", "-v", "-x", "--tb=short", "--tb=line", "--tb=no",
})
_PYTEST_VALUE_FLAGS = frozenset({"-k"})
_PYTEST_PLUGIN = "no:cacheprovider"


def command_permitted(
    argv: list[str] | tuple[str, ...],
    worktree: Path | None = None,
    blocked_paths: tuple[str, ...] = (),
) -> bool:
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
        allowed = _GIT_FLAGS[rest[0]]
        # A pathspec after `--` narrows the diff to the files a job is about.
        # Everything after it is a path and is held to the same worktree and
        # blocked-path rules as a test target, so narrowing can never reach
        # outside the assigned worktree or read a blocked file.
        if rest[0] == "diff" and "--" in rest:
            flags = rest[1:rest.index("--")]
            paths = rest[rest.index("--") + 1:]
            if not paths:
                return False
            return all(item in allowed for item in flags) and all(
                _path_in_worktree(item, worktree, blocked_paths) for item in paths
            )
        return all(item in allowed for item in rest[1:])
    if executable in _PYTHON_NAMES:
        if len(rest) >= 2 and rest[0] == "-m" and rest[1] in {"pytest", "unittest"}:
            return _pytest_args_permitted(rest[2:], worktree, blocked_paths)
        # Exactly `python scripts/check_governance.py`, with nothing after it.
        # The gate must also be the worktree's own, not a path climbing out of
        # it or one the job's blocked paths cover.
        if len(rest) == 1 and rest[0] in _GATE_SCRIPTS:
            return _path_in_worktree(rest[0], worktree, blocked_paths)
        return False
    if executable == "pytest":
        return _pytest_args_permitted(rest, worktree, blocked_paths)
    return False


def _pytest_args_permitted(
    args: tuple[str, ...],
    worktree: Path | None,
    blocked_paths: tuple[str, ...] = (),
) -> bool:
    expecting_value = False
    expecting_plugin = False
    for item in args:
        if expecting_plugin:
            if item != _PYTEST_PLUGIN:
                return False
            expecting_plugin = False
            continue
        if expecting_value:
            expecting_value = False
            continue
        if item == "-p":
            # Only the cacheprovider disablement used by tests. Any other
            # plugin would load executable code chosen by the coding model.
            expecting_plugin = True
            continue
        if item in _PYTEST_VALUE_FLAGS:
            expecting_value = True
            continue
        if item in _PYTEST_FLAGS:
            continue
        if item.startswith("-"):
            return False
        if not _path_in_worktree(item, worktree, blocked_paths):
            return False
    return not expecting_value and not expecting_plugin


def _path_in_worktree(
    relative: str,
    worktree: Path | None,
    blocked_paths: tuple[str, ...] = (),
) -> bool:
    if worktree is None:
        return False
    if Path(relative).is_absolute():
        return False
    if ".." in Path(relative).parts:
        return False
    candidates = (relative,)
    # `unittest` also accepts dotted module targets, optionally followed by a
    # class or method selector. Resolve the longest existing module prefix so
    # a blocked test cannot be reached merely by changing its spelling.
    if "/" not in relative and "\\" not in relative and "." in relative:
        parts = relative.split(".")
        for end in range(len(parts), 0, -1):
            # A dotfile such as `.env` splits to an empty first segment, which
            # is not a module path at all. Skip rather than raise: the literal
            # candidate below still checks it against the blocked paths.
            if not all(parts[:end]):
                continue
            module = Path(*parts[:end]).with_suffix(".py")
            if (worktree / module).is_file():
                candidates = (module.as_posix(),)
                break
    for candidate in candidates:
        try:
            resolved = (worktree / candidate).resolve()
            resolved_relative = resolved.relative_to(worktree.resolve()).as_posix()
        except (OSError, ValueError):
            return False
        if path_matches_blocked(resolved_relative, blocked_paths):
            return False
    return True


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
    blocked_paths: tuple[str, ...] = (),
    output_characters: int = MAX_COMMAND_OUTPUT_CHARACTERS,
) -> CodingCommandRecord:
    """Run one allowlisted command with cwd bound to the worktree.

    `output_characters` of 0 returns stdout unbounded, for the one caller that
    applies its own larger bound and needs the length before it is applied.
    Bounding twice hid how much had been cut.
    """
    if not command_permitted(argv, worktree, blocked_paths):
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
        completed = run_coding_subprocess(subprocess.run,  # noqa: S603 - argv, never shell
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
            stdout if output_characters == 0 else _bound(stdout, output_characters),
            _bound(stderr, MAX_COMMAND_OUTPUT_CHARACTERS),
            True,
            True,
        )
    except OSError as error:
        raise CodingError("coding_unavailable") from error
    return CodingCommandRecord(
        tuple(argv),
        completed.returncode,
        (completed.stdout or "") if output_characters == 0
        else _bound(completed.stdout or "", output_characters),
        _bound(completed.stderr or "", MAX_COMMAND_OUTPUT_CHARACTERS),
        False,
        True,
    )


def same_main_pytest_failure(
    branch: CodingCommandRecord, checkout: Path, timeout_seconds: int,
    check_set: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[bool, str]:
    """Compare a failed pytest run with committed main without moving HEAD.

    A temporary git archive is source evidence, never a second checkout. Cache
    entries contain only a complete failure signature keyed to the exact main
    commit, command and toolchain. Any uncertainty leaves the branch blocked.
    """
    if branch.timed_out or branch.exit_status != 1 or not branch.permitted:
        return False, "branch_result_uncomparable"
    branch_signature = pytest_failure_signature(branch.stdout, checkout)
    if branch_signature is None:
        return False, "branch_failure_unparsed"
    try:
        main = run_coding_subprocess(
            subprocess.run, ["git", "rev-parse", "--verify", "refs/heads/main^{commit}"],
            cwd=checkout, capture_output=True, text=True, timeout=15, check=False,
        )
        if main.returncode != 0:
            return False, "main_unavailable"
        sha = main.stdout.strip()
        if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
            return False, "main_identity_invalid"
        origin = run_coding_subprocess(
            subprocess.run, ["git", "remote", "get-url", "origin"], cwd=checkout,
            capture_output=True, text=True, timeout=15, check=False,
        )
        origin_url = origin.stdout.strip() if origin.returncode == 0 else ""
        # The same environment filtering is used by both actual test runs.
        local_env = checkout / ".env"
        toolchain = {
            "python": sys.executable,
            "version": sys.version,
            "platform": platform.platform(),
            "environment": _clean_environment(),
            "packages": sorted((item.metadata.get("Name", ""), item.version)
                               for item in metadata.distributions()),
            "local_env_sha256": hashlib.sha256(local_env.read_bytes()).hexdigest()
                                if local_env.is_file() else None,
            "origin": origin_url,
        }
        # Executables reached through PATH can affect tests without appearing
        # among Python distributions. Fingerprint all of them, with no list of
        # favoured tools or known test failures.
        executables: list[tuple[str, str, int, int]] = []
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            try:
                candidates = tuple(Path(directory).iterdir())
            except FileNotFoundError:
                continue
            for candidate in candidates:
                try:
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        stat = candidate.stat()
                        executables.append((str(candidate), str(candidate.resolve()),
                                            stat.st_size, stat.st_mtime_ns))
                except (FileNotFoundError, PermissionError):
                    # Unusable for this process; a disappearing entry also
                    # cannot be the executable this verification invokes.
                    continue
        toolchain["executables"] = sorted(executables)
        key = hashlib.sha256(json.dumps(
            [sha, branch.argv, check_set, toolchain], sort_keys=True,
        ).encode()).hexdigest()
        cache = checkout / ".alx" / "runtime" / "verification-baselines" / f"{key}.json"
        baseline_signature: tuple[tuple[str, str], ...] | None = None
        if cache.is_file():
            stored = json.loads(cache.read_text(encoding="utf-8"))
            raw_signature = stored.get("signature")
            if stored.get("key") == key and isinstance(raw_signature, list) \
                    and all(isinstance(item, list) and len(item) == 2 and
                            all(isinstance(part, str) for part in item)
                            for item in raw_signature):
                baseline_signature = tuple(tuple(item) for item in raw_signature)
        if baseline_signature is None:
            with tempfile.TemporaryDirectory(prefix="alx-main-verification-") as directory:
                snapshot = Path(directory)
                archive = run_coding_subprocess(
                    subprocess.run, ["git", "archive", sha], cwd=checkout,
                    capture_output=True, timeout=60, check=False,
                )
                if archive.returncode != 0:
                    return False, "main_archive_failed"
                with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
                    bundle.extractall(snapshot, filter="data")
                # Some repository tests need a genuine .git directory. Give
                # the temporary archive its own clean local identity, never a
                # pointer into the canonical checkout's metadata.
                commands = (
                    ["git", "init", "-q", "-b", "main"],
                    ["git", "config", "user.email", "baseline@example.invalid"],
                    ["git", "config", "user.name", "Baseline"],
                    ["git", "add", "-A"],
                    ["git", "commit", "-qm", "committed main snapshot"],
                )
                for argv in commands:
                    result = run_coding_subprocess(
                        subprocess.run, argv, cwd=snapshot,
                        env={**_clean_environment(),
                             "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                             "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"},
                        capture_output=True, timeout=60, check=False,
                    )
                    if result.returncode != 0:
                        return False, "main_snapshot_identity_failed"
                if origin_url:
                    remote = run_coding_subprocess(
                        subprocess.run, ["git", "remote", "add", "origin", origin_url],
                        cwd=snapshot, env=_clean_environment(),
                        capture_output=True, timeout=15, check=False,
                    )
                    if remote.returncode != 0:
                        return False, "main_snapshot_origin_failed"
                if local_env.is_file():
                    destination = snapshot / ".env"
                    destination.write_bytes(local_env.read_bytes())
                    destination.chmod(0o600)
                baseline = run_permitted_command(
                    list(branch.argv), snapshot, timeout_seconds=timeout_seconds,
                    output_characters=0,
                )
                if baseline.timed_out or baseline.exit_status != 1:
                    return False, "main_result_differs_or_unavailable"
                baseline_signature = pytest_failure_signature(baseline.stdout, snapshot)
                if baseline_signature is None:
                    return False, "main_failure_unparsed"
            cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache.with_suffix(".tmp")
            temporary.write_text(json.dumps({"key": key, "signature": baseline_signature}),
                                 encoding="utf-8")
            temporary.replace(cache)
        equivalent = not (Counter(branch_signature) - Counter(baseline_signature))
        return (equivalent,
                f"same_main_failure:main={sha}:evidence={key}" if equivalent
                else f"failure_signature_changed:main={sha}:evidence={key}")
    except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError,
            subprocess.TimeoutExpired, CodingError) as error:
        return False, f"baseline_comparison_failed:{type(error).__name__}"


@dataclass(frozen=True, slots=True)
class GitEvidence:
    """Git evidence with its own completeness stated.

    Bounding used to be invisible: a clipped diff and a whole one were the
    same string, so nothing downstream could tell partial evidence from
    complete evidence. The length before bounding is kept so the difference is
    a fact rather than an inference.
    """

    status: str
    diff: str
    diff_characters: int

    @property
    def diff_truncated(self) -> bool:
        return self.diff_characters > len(self.diff)


def inspect_git(
    worktree: Path, paths: Sequence[str] = ()
) -> GitEvidence:
    """Mechanical git status and diff. One correct outcome, so not a model call.

    `paths` narrows the diff to the files this job is about. A job runs in a
    worktree it does not own, so an unrelated dirty tree otherwise spends the
    diff budget: on 2026-09-11 a 109k worktree diff clipped at 32k to files
    alphabetically before the ones under repair, and four sessions were shown
    the same truncated prefix of somebody else's work. Status still reports
    the whole tree, because what else is dirty is a fact the job needs.
    """
    status = run_permitted_command(
        ["git", "status", "--porcelain=v1", "-z", "-uall"], worktree
    )
    argv = ["git", "diff"]
    if paths:
        argv.extend(["--", *paths])
    # Read the diff at its own bound rather than the generic command bound.
    # run_permitted_command clips stdout to MAX_COMMAND_OUTPUT_CHARACTERS,
    # which is smaller: routing the diff through it clipped twice, so the
    # length reported here described an already-shortened string and could
    # call a truncated diff complete.
    diff = run_permitted_command(argv, worktree, output_characters=0)
    # The diff is bounded once, here, at its own limit. Its length before
    # bounding is what makes truncation visible rather than inferred.
    text = diff.stdout
    return GitEvidence(
        # Porcelain status is stdout only. A host warning on stderr is
        # diagnostic text, never a filename; treating it as status made a
        # clean checkout appear dirty when stdout was empty.
        _bound(status.stdout, MAX_COMMAND_OUTPUT_CHARACTERS),
        _bound(text, MAX_DIFF_CHARACTERS),
        len(text),
    )


def files_from_git_status(status: str) -> tuple[str, ...]:
    """Paths from porcelain v1, including literal spaces and quotes."""
    if "\0" in status:
        names = []
        entries = iter(status.split("\0"))
        for entry in entries:
            if not entry or len(entry) < 4:
                continue
            kind = entry[:2]
            path = entry[3:]
            # Porcelain v1 -z puts the source path of a rename/copy in the
            # next field. The first path is the destination that git diff
            # must inspect.
            if "R" in kind or "C" in kind:
                next(entries, None)
            if path:
                names.append(path)
        return tuple(names)
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
