"""Fixed-command lifecycle operations for one configured canonical checkout.

This provider deliberately has no generic git or shell interface.  Its public
methods select one of two governed outcomes; every process invocation is an
enumerated literal argv tuple rooted at the checkout supplied at construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from threading import Lock, RLock
from typing import Callable

from alx.contracts.repository_runtime import REPOSITORY_RUNTIME_FAILURES, RepositoryRuntimeError

_SHOW_TOPLEVEL = ("git", "rev-parse", "--show-toplevel")
_ORIGIN_URL = ("git", "config", "--get", "remote.origin.url")
_BRANCH = ("git", "symbolic-ref", "--quiet", "--short", "HEAD")
_HEAD_COMMIT = ("git", "rev-parse", "--verify", "HEAD^{commit}")
_LOCAL_MAIN = ("git", "rev-parse", "--verify", "refs/heads/main^{commit}")
_STATUS = ("git", "status", "--porcelain=v1", "--untracked-files=all")
_FETCH = ("git", "fetch", "origin", "refs/heads/main:refs/remotes/origin/main")
_TRACKING_EXISTS = ("git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main")
_TRACKING_COMMIT = ("git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
_LOCAL_ANCESTOR = ("git", "merge-base", "--is-ancestor", "refs/heads/main", "refs/remotes/origin/main")
_REMOTE_ANCESTOR = ("git", "merge-base", "--is-ancestor", "refs/remotes/origin/main", "refs/heads/main")
_MERGE = ("git", "merge", "--ff-only", "refs/remotes/origin/main")


@dataclass(frozen=True, slots=True)
class RepositoryState:
    repository_identity: str
    branch: str
    local_before: str
    origin_main: str = ""
    local_after: str = ""
    transition: str = ""

    def as_values(self) -> dict[str, str]:
        return {
            "repository_identity": self.repository_identity,
            "branch": self.branch,
            "local_before": self.local_before,
            "origin_main": self.origin_main,
            "local_after": self.local_after,
            "transition": self.transition,
        }


Runner = Callable[..., subprocess.CompletedProcess[str]]


# Every configured runtime for the same checkout shares this lock.  It covers
# inspection and the complete synchronization transition so AL/X never
# interleaves its own lifecycle operations on a canonical checkout.
_LIFECYCLE_LOCKS: dict[Path, RLock] = {}
_LIFECYCLE_LOCKS_GUARD = Lock()


def _lifecycle_lock(root: Path) -> RLock:
    with _LIFECYCLE_LOCKS_GUARD:
        return _LIFECYCLE_LOCKS.setdefault(root, RLock())


class CanonicalRepositoryRuntime:
    """Inspect and fast-forward only the checkout fixed at construction."""

    def __init__(self, root: Path, repository_identity: str, origin_url: str,
                 timeout_seconds: int, runner: Runner = subprocess.run) -> None:
        if not root.is_absolute() or not repository_identity.strip() or not origin_url.strip():
            raise ValueError("repository runtime requires absolute canonical configuration")
        if timeout_seconds <= 0:
            raise ValueError("repository runtime timeout must be positive")
        self._root = root.resolve()
        self._identity = repository_identity.strip().lower()
        self._origin = _normalised_origin(origin_url)
        self._timeout = timeout_seconds
        self._runner = runner
        self._lifecycle_lock = _lifecycle_lock(self._root)

    def inspect(self) -> RepositoryState:
        with self._lifecycle_lock:
            return self._preflight()

    def synchronize(self) -> RepositoryState:
        with self._lifecycle_lock:
            before = self._preflight()
            self._must_succeed(_FETCH, "fetch", "fetch_failed")
            if not self._ok(_TRACKING_EXISTS, "tracking_ref"):
                raise RepositoryRuntimeError("tracking_ref_missing", "tracking_ref")
            origin_main = self._commit(_TRACKING_COMMIT, "tracking_ref", "tracking_ref_invalid")
            if origin_main == before.local_before:
                return RepositoryState(before.repository_identity, before.branch,
                                       before.local_before, origin_main,
                                       before.local_before, "already_current")
            if self._ancestry(_LOCAL_ANCESTOR):
                # Fetch and ancestry are facts only while this checkout remains
                # exactly the verified canonical main. Recheck immediately
                # before the effect and refuse if either relevant ref changed.
                current = self._preflight()
                if current.local_before != before.local_before:
                    raise RepositoryRuntimeError("repository_state_changed", "pre_merge")
                if self._commit(_TRACKING_COMMIT, "pre_merge", "tracking_ref_invalid") != origin_main:
                    raise RepositoryRuntimeError("tracking_ref_changed", "pre_merge")
                self._must_succeed(_MERGE, "fast_forward", "fast_forward_refused")
                after = self._commit(_HEAD_COMMIT, "post_merge", "fast_forward_refused")
                return RepositoryState(before.repository_identity, before.branch,
                                       before.local_before, origin_main, after,
                                       "fast_forwarded")
            if self._ancestry(_REMOTE_ANCESTOR):
                raise RepositoryRuntimeError("local_ahead", "ancestry")
            raise RepositoryRuntimeError("history_diverged", "ancestry")

    def _preflight(self) -> RepositoryState:
        if not self._root.is_dir():
            raise RepositoryRuntimeError("repository_root_unusable", "root")
        top = self._text(_SHOW_TOPLEVEL, "root", "repository_root_unusable")
        if Path(top).resolve() != self._root:
            raise RepositoryRuntimeError("repository_root_unusable", "root")
        origin = self._text(_ORIGIN_URL, "origin", "origin_missing")
        if _normalised_origin(origin) != self._origin:
            raise RepositoryRuntimeError("origin_mismatch", "origin")
        if _origin_identity(origin) != self._identity:
            raise RepositoryRuntimeError("repository_identity_mismatch", "identity")
        branch = self._text(_BRANCH, "head", "head_detached")
        if branch != "main":
            raise RepositoryRuntimeError("branch_not_main", "head")
        head = self._commit(_HEAD_COMMIT, "head", "local_main_invalid")
        local = self._commit(_LOCAL_MAIN, "local_main", "local_main_invalid")
        if head != local:
            raise RepositoryRuntimeError("local_main_invalid", "local_main")
        status = self._text(_STATUS, "status", "repository_root_unusable")
        entries = tuple(line for line in status.splitlines() if line)
        if any(line.startswith("??") for line in entries):
            raise RepositoryRuntimeError("worktree_untracked", "status")
        if entries:
            raise RepositoryRuntimeError("worktree_dirty", "status")
        return RepositoryState(self._identity, branch, local)

    def _commit(self, argv: tuple[str, ...], phase: str, code: str) -> str:
        value = self._text(argv, phase, code)
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            raise RepositoryRuntimeError(code, phase)
        return value

    def _text(self, argv: tuple[str, ...], phase: str, code: str) -> str:
        completed = self._run(argv, phase)
        if completed.returncode != 0:
            raise RepositoryRuntimeError(code, phase)
        return completed.stdout.strip()

    def _must_succeed(self, argv: tuple[str, ...], phase: str, code: str) -> None:
        if not self._ok(argv, phase):
            raise RepositoryRuntimeError(code, phase)

    def _ok(self, argv: tuple[str, ...], phase: str) -> bool:
        return self._run(argv, phase).returncode == 0

    def _ancestry(self, argv: tuple[str, ...]) -> bool:
        completed = self._run(argv, "ancestry")
        if completed.returncode == 0:
            return True
        if completed.returncode == 1:
            return False
        raise RepositoryRuntimeError("ancestry_failed", "ancestry")

    def _run(self, argv: tuple[str, ...], phase: str) -> subprocess.CompletedProcess[str]:
        try:
            return self._runner(argv, cwd=self._root, shell=False, check=False,
                                capture_output=True, text=True, timeout=self._timeout)
        except OSError as error:
            raise RepositoryRuntimeError("git_unavailable", phase) from error
        except subprocess.TimeoutExpired as error:
            raise RepositoryRuntimeError("git_timeout", phase) from error


def _normalised_origin(value: str) -> str:
    return value.strip().rstrip("/").removesuffix(".git").lower()


def _origin_identity(value: str) -> str:
    origin = _normalised_origin(value)
    if origin.startswith("git@github.com:"):
        return origin.split(":", 1)[1]
    if origin.startswith("https://github.com/") or origin.startswith("ssh://git@github.com/"):
        return origin.rsplit("github.com/", 1)[1]
    return ""
