"""Bounded git state management for one assigned coding worktree.

The Coding Agent must be able to hand back a repair as a branch and a commit
SHA rather than as a dirty worktree AL/X has to interpret. That requires git
commands that write, and `coding_process.py` deliberately permits only reads.
This module is the one production site that runs a writing git command, and it
is reachable only from `CodingAgent`.

Authority is granted by enumeration, not by denial. `_WRITE_SHAPES` lists the
exact argv forms that may run; anything not written there cannot be produced,
so push, fetch, pull, merge, rebase, reset, checkout, stash, remote, branch
deletion and `commit --amend` are refused by construction rather than by a
denylist somebody has to keep complete. Adding a capability here means adding a
shape, which is a visible change to a short list.

Three further properties matter:

- **The worktree binding cannot be redirected.** Every command runs with `cwd`
  set to the resolved worktree and no global option is permitted, so `-C`,
  `--git-dir` and `--work-tree` cannot move the operation elsewhere. The
  worktree is also verified to be the git top level it claims to be.

- **Staging is by named path only.** `git add -- <path>...` with an explicit
  list; `-A`, `-u` and `.` are not shapes, so a job cannot sweep in the tree.
  Each path is held to the same worktree and blocked-path rules as any other
  coding path.

- **Committing fails closed.** After staging, the index is read back and
  compared against the authorised set. A single unauthorised staged path
  aborts before `git commit` runs, and the index is restored to what it was.

Nothing here interprets Friedl or decides what the job should do. It turns an
assigned worktree and a job-owned file list into a branch and a commit SHA.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - the one coding-job git-write site
from dataclasses import dataclass
from pathlib import Path

from alx.contracts.coding import (
    MAX_BRANCH_NAME_CHARACTERS,
    MAX_COMMIT_MESSAGE_CHARACTERS,
    MAX_COMMAND_OUTPUT_CHARACTERS,
    MAX_STAGED_FILES,
    CodingCommit,
    CodingError,
    GitWorkspaceState,
    lexical_worktree_path,
    path_matches_blocked,
)


GIT_TIMEOUT_SECONDS = 60

# Every argv the Coding Agent may run against its worktree's git, as a fixed
# prefix plus how the remainder is checked. A shape absent from this mapping
# cannot be built, which is what makes push, merge, rebase, reset, stash and
# remote manipulation impossible rather than merely discouraged.
#
# "none"  - no further arguments at all
# "value" - exactly one further argument, checked by the caller that built it
# "paths" - a `--` separator followed by one or more worktree-relative paths,
#           none of which may be spelled as a ref
_WRITE_SHAPES: dict[tuple[str, ...], str] = {
    ("rev-parse", "HEAD"): "none",
    ("rev-parse", "--abbrev-ref", "HEAD"): "none",
    ("rev-parse", "--show-toplevel"): "none",
    ("symbolic-ref", "--quiet", "--short", "HEAD"): "none",
    ("status", "--porcelain=v1", "-z"): "none",
    ("diff", "--cached", "--name-only", "-z"): "none",
    ("switch", "-c"): "value",
    ("switch",): "value",
    ("add", "--"): "paths",
    ("reset", "--quiet", "--"): "paths",
    ("commit", "--quiet", "-m"): "value",
}

# A branch name this capability may create or switch to. Deliberately narrower
# than git's own rules: no `..`, no leading dash, no refspec or option
# punctuation, so a name can never be read as a flag or reach another ref
# namespace. `refs/` and `HEAD` are excluded for the same reason.
_BRANCH_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-_."
)


@dataclass(frozen=True, slots=True)
class _GitResult:
    exit_status: int
    stdout: str
    stderr: str


def git_write_permitted(argv: list[str] | tuple[str, ...]) -> bool:
    """Whether this exact argv is one of the enumerated git shapes.

    The check is the authority. It is exported so tests can assert directly
    that a forbidden operation cannot be expressed, without needing a
    repository to run it against.
    """
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        return False
    if any("\x00" in item for item in argv):
        return False
    if argv[0] != "git":
        return False
    rest = tuple(argv[1:])
    if not rest:
        return False
    # A global option precedes the subcommand and could rebind the repository.
    # None is permitted, so the `cwd` binding is the only binding.
    if rest[0].startswith("-"):
        return False
    for prefix, remainder in _WRITE_SHAPES.items():
        if rest[: len(prefix)] != prefix:
            continue
        tail = rest[len(prefix):]
        if remainder == "none":
            return not tail
        if remainder == "value":
            return len(tail) == 1 and not tail[0].startswith("-")
        if remainder == "paths":
            return bool(tail) and all(_pathspec_permitted(item) for item in tail)
    return False


# Git already reads everything after `--` as a pathspec, so a ref spelled there
# is a filename. D-029 nonetheless states that the approved index rollback
# cannot target a commit or a ref, and that guarantee should hold by reading the
# argv rather than by knowing git's separator semantics. A ref-shaped argument
# is refused, so the reset shape is a path operation on its face.
_REF_SHAPED = frozenset({"HEAD", "ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD"})


def _pathspec_permitted(item: str) -> bool:
    """A path after `--`, never something a reader could mistake for a ref."""
    if item.startswith("-"):
        return False
    if item in _REF_SHAPED or item.startswith("refs/"):
        return False
    # Revision syntax: HEAD~1, main@{1}, a..b, branch^, :/message. `~` and `^`
    # are legal in a filename but vanishingly rare in a source path, and a job
    # that cannot stage one is a better outcome than an argv a reader has to
    # reason about git's separator rules to clear.
    if any(token in item for token in ("@{", "..", "~", "^")):
        return False
    if item.startswith(":"):
        return False
    return True


def branch_name_permitted(name: str) -> bool:
    """Whether a branch name is one this capability may create or switch to."""
    if not isinstance(name, str):
        return False
    candidate = name.strip()
    if not candidate or len(candidate) > MAX_BRANCH_NAME_CHARACTERS:
        return False
    if candidate != name:
        return False
    if any(character not in _BRANCH_ALLOWED for character in candidate):
        return False
    if candidate.startswith(("-", "/", ".")) or candidate.endswith(("/", ".", ".lock")):
        return False
    if ".." in candidate or "//" in candidate or "@{" in candidate:
        return False
    if candidate == "HEAD" or candidate.startswith("refs/"):
        return False
    return True


# A commit this capability creates is attributed to the coding job, never to
# whoever happens to be logged in. Stated here rather than read from the host's
# git configuration, so the author of a repair is legible in `git log` and does
# not depend on the machine the runtime is running on.
COMMIT_AUTHOR_NAME = "AL/X Coding Agent"
COMMIT_AUTHOR_EMAIL = "coding-agent@alx.invalid"


def _clean_environment() -> dict[str, str]:
    """PATH and locale only, plus an explicit committer identity.

    No token, credential helper or AL/X configuration reaches the process, and
    `GIT_TERMINAL_PROMPT=0` means a command that wanted a credential fails
    rather than waiting for one. Nothing enumerated here talks to a remote, so
    this is defence in depth rather than the boundary itself.
    """
    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HOME")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_AUTHOR_NAME"] = COMMIT_AUTHOR_NAME
    environment["GIT_AUTHOR_EMAIL"] = COMMIT_AUTHOR_EMAIL
    environment["GIT_COMMITTER_NAME"] = COMMIT_AUTHOR_NAME
    environment["GIT_COMMITTER_EMAIL"] = COMMIT_AUTHOR_EMAIL
    return environment


def _run(worktree: Path, argv: list[str]) -> _GitResult:
    """Run one enumerated git command bound to this worktree."""
    if not git_write_permitted(argv):
        raise CodingError("git_refused", reason_code="operation_not_permitted")
    try:
        completed = subprocess.run(  # noqa: S603 - enumerated argv, never shell
            argv,
            cwd=worktree,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CodingError("git_unavailable", reason_code="timeout") from error
    except OSError as error:
        raise CodingError("git_unavailable", reason_code="git_not_runnable") from error
    return _GitResult(
        completed.returncode,
        (completed.stdout or "")[:MAX_COMMAND_OUTPUT_CHARACTERS],
        (completed.stderr or "")[:MAX_COMMAND_OUTPUT_CHARACTERS],
    )


def _require(worktree: Path, argv: list[str], reason_code: str) -> str:
    """Run an enumerated command that must succeed, and return raw stdout.

    Deliberately unstripped: the NUL-delimited readers below depend on the
    exact bytes git emitted, and stripping one merged the last two entries of
    a `-z` listing into nothing.
    """
    result = _run(worktree, argv)
    if result.exit_status != 0:
        raise CodingError(
            "git_unavailable",
            reason_code=reason_code,
            exit_status=result.exit_status,
        )
    return result.stdout


def _nul_paths(text: str) -> tuple[str, ...]:
    return tuple(item for item in text.split("\0") if item)


def _dirty_paths(worktree: Path) -> tuple[str, ...]:
    """Every path git reports as modified, staged, or untracked."""
    status = _require(
        worktree, ["git", "status", "--porcelain=v1", "-z"], "status_failed"
    )
    names: list[str] = []
    entries = iter(status.split("\0"))
    for entry in entries:
        if not entry or len(entry) < 4:
            continue
        kind = entry[:2]
        path = entry[3:]
        # Porcelain v1 -z puts a rename or copy source in the following field.
        if "R" in kind or "C" in kind:
            next(entries, None)
        if path:
            names.append(path)
    return tuple(names)


def assert_assigned_worktree(worktree: Path) -> Path:
    """Resolve the worktree and prove git agrees it is that repository's root.

    A job is assigned one worktree. Running git from a subdirectory would
    silently operate on the enclosing repository, so the resolved path must be
    the top level git itself reports.
    """
    root = Path(worktree).expanduser().resolve()
    if not root.is_dir():
        raise CodingError("worktree_unusable", reason_code="missing")
    toplevel = _require(
        root, ["git", "rev-parse", "--show-toplevel"], "not_a_repository"
    ).strip()
    try:
        reported = Path(toplevel).resolve()
    except OSError as error:
        raise CodingError(
            "git_unavailable", reason_code="toplevel_unresolvable"
        ) from error
    if reported != root:
        raise CodingError(
            "git_refused", reason_code="worktree_is_not_repository_root"
        )
    return root


def read_workspace_state(
    worktree: Path, inherited_dirty: tuple[str, ...] | None = None
) -> GitWorkspaceState:
    """Branch, HEAD SHA and dirt for the assigned worktree.

    Called before the session to establish the baseline a job can prove, and
    again afterwards to report cleanliness. `inherited_dirty` carries the
    baseline's dirt forward into a later reading so the report distinguishes
    what this job left from what it found; omitted, the current dirt is the
    inherited dirt, which is what "before the job" means.
    """
    root = assert_assigned_worktree(worktree)
    head = _require(root, ["git", "rev-parse", "HEAD"], "no_head").strip()
    reference = _run(root, ["git", "symbolic-ref", "--quiet", "--short", "HEAD"])
    detached = reference.exit_status != 0
    branch = "" if detached else reference.stdout.strip()
    dirty = _dirty_paths(root)
    return GitWorkspaceState(
        branch=branch,
        head_sha=head,
        inherited_dirty=tuple(dirty if inherited_dirty is None else inherited_dirty),
        clean=not dirty,
        detached=detached,
    )


def create_repair_branch(worktree: Path, branch: str) -> GitWorkspaceState:
    """Create and switch to a repair branch inside the assigned worktree.

    Switching preserves uncommitted work: a job's own edits, and anybody
    else's, move to the new branch rather than being discarded. If the branch
    already exists the switch is plain rather than a creation, so re-running a
    job against its own branch is not a failure. Nothing is deleted, reset or
    force-moved, so a wrong branch name costs a branch and never a change.
    """
    root = assert_assigned_worktree(worktree)
    if not branch_name_permitted(branch):
        raise CodingError("git_refused", reason_code="branch_name_not_permitted")
    created = _run(root, ["git", "switch", "-c", branch])
    if created.exit_status != 0:
        existing = _run(root, ["git", "switch", branch])
        if existing.exit_status != 0:
            raise CodingError(
                "git_refused",
                reason_code="branch_not_switchable",
                exit_status=existing.exit_status,
            )
    state = read_workspace_state(root)
    if state.branch != branch:
        raise CodingError("git_refused", reason_code="branch_not_active")
    return state


def _authorised_paths(
    root: Path,
    files: tuple[str, ...],
    inherited_dirty: tuple[str, ...],
    blocked_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """The job-owned paths that may be staged, or a refusal naming why.

    `files` is the job's own changed-file set, which the agent computes by
    fingerprinting inherited dirt before the session and comparing afterwards:
    an inherited file only appears here when the job itself rewrote it. That
    is what lets this check stay mechanical — it does not re-derive ownership,
    it enforces that nothing outside the derived set reaches the index.
    """
    if not files:
        raise CodingError("git_refused", reason_code="no_job_owned_changes")
    if len(files) > MAX_STAGED_FILES:
        raise CodingError(
            "git_refused", reason_code="too_many_files", received_count=len(files)
        )
    inherited = {item for item in inherited_dirty}
    authorised: list[str] = []
    for item in files:
        lexical = lexical_worktree_path(item)
        if not lexical:
            raise CodingError("git_refused", reason_code="path_is_worktree_root")
        if path_matches_blocked(lexical, blocked_paths):
            raise CodingError("path_not_permitted", path=lexical)
        if Path(lexical).parts[0] == ".git":
            raise CodingError("path_not_permitted", path=lexical)
        resolved = (root / lexical).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise CodingError("path_outside_worktree") from error
        # An inherited dirty path reaching here has been rewritten by the job,
        # so it is the job's to stage. One that was never touched is excluded
        # upstream and would not appear in `files` at all.
        inherited.discard(lexical)
        if lexical not in authorised:
            authorised.append(lexical)
    return tuple(authorised)


def commit_job_changes(
    worktree: Path,
    branch: str,
    message: str,
    files: tuple[str, ...],
    inherited_dirty: tuple[str, ...] = (),
    blocked_paths: tuple[str, ...] = (),
) -> CodingCommit:
    """Stage exactly this job's files and commit them, or refuse entirely.

    The verification is the point. Staging by name is not enough on its own:
    the index may already hold something staged before the job began, and a
    path added by name can expand to more than the caller expected. So the
    index is read back and compared against the authorised set, and one
    unauthorised entry aborts before any commit exists. `unrelated_changes_staged`
    is a refusal, never a partial commit.
    """
    root = assert_assigned_worktree(worktree)
    if not branch_name_permitted(branch):
        raise CodingError("git_refused", reason_code="branch_name_not_permitted")
    text = message.strip() if isinstance(message, str) else ""
    if not text:
        raise CodingError("git_refused", reason_code="commit_message_blank")
    if len(text) > MAX_COMMIT_MESSAGE_CHARACTERS:
        raise CodingError("git_refused", reason_code="commit_message_too_long")

    active = read_workspace_state(root)
    if active.branch != branch:
        raise CodingError(
            "git_refused", reason_code="branch_not_active", detail=active.branch
        )
    authorised = _authorised_paths(root, files, inherited_dirty, blocked_paths)

    # An index that already holds something is not this job's to rearrange.
    # Quietly un-staging it would be a change to somebody else's working state
    # made without being asked, and the reason they staged it is exactly the
    # kind of thing this code cannot know. Refuse before touching anything.
    already_staged = _staged_paths(root)
    unauthorised_before = tuple(
        item for item in already_staged if item not in set(authorised)
    )
    if unauthorised_before:
        raise CodingError(
            "unrelated_changes_staged",
            reason_code="index_dirty_before_job",
            unrelated_count=len(unauthorised_before),
        )

    staged = _run(root, ["git", "add", "--", *authorised])
    if staged.exit_status != 0:
        raise CodingError(
            "git_refused",
            reason_code="staging_failed",
            exit_status=staged.exit_status,
        )

    index = _staged_paths(root)
    unrelated = tuple(item for item in index if item not in set(authorised))
    if unrelated:
        # Fail closed and leave nothing half-prepared behind: the job's own
        # paths come back out of the index too, so the tree is as it was.
        _run(root, ["git", "reset", "--quiet", "--", *authorised, *unrelated])
        raise CodingError(
            "unrelated_changes_staged",
            reason_code="index_holds_unauthorised_paths",
            unrelated_count=len(unrelated),
        )
    if not index:
        raise CodingError("git_refused", reason_code="nothing_staged")

    committed = _run(root, ["git", "commit", "--quiet", "-m", text])
    if committed.exit_status != 0:
        raise CodingError(
            "git_refused",
            reason_code="commit_failed",
            exit_status=committed.exit_status,
        )

    # Read the result out of git rather than assuming it. A commit that did not
    # move HEAD is not a commit, whatever the exit status said.
    after = read_workspace_state(root, inherited_dirty)
    if after.head_sha == active.head_sha:
        raise CodingError("git_refused", reason_code="head_did_not_advance")
    return CodingCommit(
        branch=after.branch,
        commit_sha=after.head_sha,
        committed_files=tuple(index),
        worktree_clean=after.clean,
    )


def _staged_paths(worktree: Path) -> tuple[str, ...]:
    """Exactly what the index holds against HEAD, by name."""
    listed = _require(
        worktree,
        ["git", "diff", "--cached", "--name-only", "-z"],
        "index_unreadable",
    )
    return _nul_paths(listed)


__all__ = [
    "COMMIT_AUTHOR_EMAIL",
    "COMMIT_AUTHOR_NAME",
    "GIT_TIMEOUT_SECONDS",
    "assert_assigned_worktree",
    "branch_name_permitted",
    "commit_job_changes",
    "create_repair_branch",
    "git_write_permitted",
    "read_workspace_state",
]
