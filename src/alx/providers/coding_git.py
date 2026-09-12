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
    MAX_INSPECTED_ENTRIES,
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
    ("show", "--name-only", "--pretty=format:", "-z", "HEAD"): "none",
    ("check-attr", "-z", "filter", "--"): "paths",
    ("switch", "-c"): "value",
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

# A directory that does not exist, pointed at by core.hooksPath so git finds
# no hook to run. Named rather than empty because an empty value is read as
# "unset" by some git versions and would silently restore the repository's own
# hooks.
_NO_HOOKS = Path("/nonexistent/alx-coding-agent-no-hooks")


def _configure(environment: dict[str, str], values: dict[str, str]) -> None:
    """Force these git settings for one command, overriding every config file.

    `GIT_CONFIG_*` takes precedence over system, global and repository config,
    so a repository cannot restore what is suppressed here by setting it
    itself. Written as a helper because the count and the indices have to stay
    consistent, and an off-by-one silently drops the last setting.
    """
    environment["GIT_CONFIG_COUNT"] = str(len(values))
    for index, (key, value) in enumerate(values.items()):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value


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
    # No repository-supplied code runs for a coding job. Two mechanisms let a
    # repository execute its own code inside an ordinary git command, and both
    # were demonstrated on 2026-09-12:
    #
    # - a `pre-commit` hook runs *after* the index has been authorised and can
    #   stage anything it likes. One added a file to the commit while
    #   `committed_files` still reported the pre-hook listing, so the evidence
    #   returned to Core was false. `core.hooksPath` pointed at a directory
    #   that does not exist suppresses every hook, which `--no-verify` does
    #   not;
    # - a `.gitattributes` clean filter runs during `git add` and rewrites the
    #   bytes that enter the index. One committed "TAMPERED change" while the
    #   worktree still read "job change", and a filter body of `sh -c ...`
    #   executed arbitrary code outside the worktree. The file set stayed
    #   authorised, which is exactly why this is worth closing: path-level
    #   authorisation says nothing about content.
    #
    # These run with AL/X's privileges, not the coding session's sandboxed
    # ones, so the kernel profile that contains the session does not contain
    # them. Disabling both is the boundary.
    #
    # Hooks are suppressed here. Clean filters cannot be: they are selected by
    # an in-tree `.gitattributes`, which no configuration overrides, and filter
    # names are arbitrary so there is no list to blank. A path carrying one is
    # therefore *refused* before it is staged, in `_refuse_attribute_filters`.
    _configure(environment, {
        "core.hooksPath": str(_NO_HOOKS),
        "core.fsmonitor": "false",
    })
    environment["GIT_AUTHOR_NAME"] = COMMIT_AUTHOR_NAME
    environment["GIT_AUTHOR_EMAIL"] = COMMIT_AUTHOR_EMAIL
    environment["GIT_COMMITTER_NAME"] = COMMIT_AUTHOR_NAME
    environment["GIT_COMMITTER_EMAIL"] = COMMIT_AUTHOR_EMAIL
    return environment


def _run(
    worktree: Path, argv: list[str], *, bounded: bool = True
) -> _GitResult:
    """Run one enumerated git command bound to this worktree.

    `bounded=False` returns stdout whole. It is for the NUL-delimited listings
    that authorisation is decided from: truncating one silently drops entries,
    and an entry the check never sees is an entry it cannot refuse. Review found
    exactly that on 2026-09-12 — a 2000-file index was compared against the 334
    paths that fitted in 16,000 characters, and the rest would have been
    committed unexamined. Diagnostic output stays bounded; structural output
    is read whole and bounded by entry count instead, where exceeding the
    bound fails closed rather than silently shortening the answer.
    """
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
    stdout = completed.stdout or ""
    return _GitResult(
        completed.returncode,
        stdout[:MAX_COMMAND_OUTPUT_CHARACTERS] if bounded else stdout,
        (completed.stderr or "")[:MAX_COMMAND_OUTPUT_CHARACTERS],
    )


def _require(
    worktree: Path, argv: list[str], reason_code: str, *, bounded: bool = True
) -> str:
    """Run an enumerated command that must succeed, and return raw stdout.

    Deliberately unstripped: the NUL-delimited readers below depend on the
    exact bytes git emitted, and stripping one merged the last two entries of
    a `-z` listing into nothing.
    """
    result = _run(worktree, argv, bounded=bounded)
    if result.exit_status != 0:
        raise CodingError(
            "git_unavailable",
            reason_code=reason_code,
            exit_status=result.exit_status,
        )
    return result.stdout


def _nul_paths(text: str) -> tuple[str, ...]:
    return tuple(item for item in text.split("\0") if item)


def _bounded_entries(listed: str, reason_code: str) -> tuple[str, ...]:
    """NUL-delimited names, refusing rather than shortening an oversized list."""
    names = _nul_paths(listed)
    if len(names) > MAX_INSPECTED_ENTRIES:
        raise CodingError(
            "git_refused", reason_code=reason_code, entry_count=len(names)
        )
    return names


def _dirty_paths(worktree: Path) -> tuple[str, ...]:
    """Every path git reports as modified, staged, or untracked.

    Read whole for the same reason the index is: this is the inherited dirt a
    job is judged against, and a shortened list would silently drop files the
    job must be told about.
    """
    status = _require(
        worktree, ["git", "status", "--porcelain=v1", "-z"], "status_failed",
        bounded=False,
    )
    if status.count("\0") > MAX_INSPECTED_ENTRIES:
        raise CodingError(
            "git_refused",
            reason_code="worktree_too_dirty",
            entry_count=status.count("\0"),
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
    else's, move to the new branch rather than being discarded. Nothing is
    deleted, reset or force-moved, so a wrong branch name costs a branch and
    never a change.

    A name that already exists is refused rather than adopted. The first
    version fell back to a plain switch so that re-running a job against its
    own branch was not a failure, and review showed on 2026-09-12 what that
    actually buys: an older or unrelated branch of the same name silently
    becomes the base, so the job's edits and its commit sit on a history
    nobody checked. Core picking a name that is already taken is ambiguous —
    it may mean "continue that work" or "this is a different repair" — and
    Law 3 sends ambiguity back to her rather than letting this resolve it.
    """
    root = assert_assigned_worktree(worktree)
    if not branch_name_permitted(branch):
        raise CodingError("git_refused", reason_code="branch_name_not_permitted")
    created = _run(root, ["git", "switch", "-c", branch])
    if created.exit_status != 0:
        raise CodingError(
            "git_refused",
            reason_code="branch_already_exists_or_unusable",
            exit_status=created.exit_status,
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
    for item in _expand_directories(root, files):
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

    _refuse_attribute_filters(root, authorised)

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
        # Fail closed and leave nothing half-prepared behind. The pre-flight
        # check above already refused any index holding an unauthorised path,
        # so what is staged here can only be this job's own: un-staging it
        # restores the index to exactly the empty state it was found in.
        _unstage(root, (*authorised, *unrelated))
        raise CodingError(
            "unrelated_changes_staged",
            reason_code="index_holds_unauthorised_paths",
            unrelated_count=len(unrelated),
        )
    if not index:
        raise CodingError("git_refused", reason_code="nothing_staged")

    # From here a commit may already exist whatever happens next. A timeout or
    # a nonzero status does not prove nothing was written: git can advance the
    # ref and then fail, and reporting "no commit" when a commit is on the
    # branch would leave Core acting on a false record. So every exit from
    # here on reconciles against HEAD before it says anything.
    try:
        committed = _run(root, ["git", "commit", "--quiet", "-m", text])
    except CodingError as error:
        raise _reconcile_after_commit(root, active.head_sha, error) from error
    if committed.exit_status != 0:
        # A commit that did not happen must not leave the job's files staged.
        # The next operation in this worktree — another job, or Friedl — would
        # inherit an index it did not create and would be refused by the
        # pre-flight check for work that was never committed.
        _unstage(root, authorised)
        raise _reconcile_after_commit(
            root,
            active.head_sha,
            CodingError(
                "git_refused",
                reason_code="commit_failed",
                exit_status=committed.exit_status,
            ),
        )

    # Read the result out of git rather than assuming it. A commit that did not
    # move HEAD is not a commit, whatever the exit status said.
    try:
        after = read_workspace_state(root, inherited_dirty)
    except CodingError as error:
        raise _reconcile_after_commit(root, active.head_sha, error) from error
    if after.head_sha == active.head_sha:
        raise CodingError("git_refused", reason_code="head_did_not_advance")

    # What the commit actually contains, not what the index held before it.
    # Hooks are disabled, so this should always equal `index`; it is checked
    # anyway because the alternative to checking is reporting a file list that
    # a hook, a git version or a configuration could have made untrue, and a
    # false `committed_files` is worse than a refusal. Reported as an
    # unresolved issue would be too quiet: Core is told the commit is not what
    # was authorised.
    committed = _committed_paths(root)
    escaped = tuple(item for item in committed if item not in set(authorised))
    if escaped:
        # The commit exists on the branch at this point. It is the only window
        # in which an unauthorised path can reach history: another writer
        # staging between the index readback and the commit. Review raised the
        # race on 2026-09-12 and it was reproduced by staging inside that
        # window — this check caught it, but only after the commit was made.
        #
        # The commit is therefore named rather than hidden. Removing it would
        # mean moving a ref, which is history rewriting and authority D-029
        # explicitly withholds, so what is reported is the truth: a commit
        # exists, it contains something this job did not authorise, and its
        # SHA is here for AL/X to act on. She has the authority to decide what
        # to do about it; this code does not.
        raise CodingError(
            "unrelated_changes_staged",
            reason_code="commit_contains_unauthorised_paths",
            commit_sha=after.head_sha,
            unrelated_count=len(escaped),
        )
    return CodingCommit(
        branch=after.branch,
        commit_sha=after.head_sha,
        committed_files=committed,
        worktree_clean=after.clean,
    )


def _expand_directories(root: Path, files: tuple[str, ...]) -> tuple[str, ...]:
    """Replace an untracked-directory entry with the files inside it.

    Porcelain status reports a wholly untracked directory as one entry ending
    in `/` rather than listing its contents, so a job that creates a directory
    arrives here authorising `newdir/` while `git add` stages `newdir/a.py`
    and `newdir/b.py`. The readback then finds two paths it never authorised
    and refuses the commit as if the job had staged somebody else's work.

    Qodo found this on 2026-09-12 and it was reproduced before being changed:
    creating a directory is ordinary coding, so the previous behaviour refused
    routine jobs while reporting a misleading cause. Expanding here keeps the
    authorised set and the index describing the same thing, and every expanded
    path is still held to the full worktree, blocked-path and `.git` checks by
    the caller — expansion widens what is named, never what is permitted.
    """
    expanded: list[str] = []
    for item in files:
        if not item.endswith("/"):
            expanded.append(item)
            continue
        lexical = lexical_worktree_path(item)
        directory = (root / lexical).resolve()
        try:
            directory.relative_to(root)
        except ValueError as error:
            raise CodingError("path_outside_worktree") from error
        if not directory.is_dir() or directory.is_symlink():
            # Not a directory after all, or a symlink that could point out of
            # the worktree. Keep the original name and let the caller's checks
            # rule on it rather than walking it here.
            expanded.append(item)
            continue
        for child in sorted(directory.rglob("*")):
            if child.is_file() and not child.is_symlink():
                expanded.append(child.relative_to(root).as_posix())
    return tuple(dict.fromkeys(expanded))


def _refuse_attribute_filters(worktree: Path, paths: tuple[str, ...]) -> None:
    """Refuse to stage a path whose content a clean filter would rewrite.

    A `.gitattributes` clean filter runs arbitrary repository-supplied code
    during `git add` and replaces the bytes that enter the index. Demonstrated
    on 2026-09-12: a filter committed "TAMPERED change" while the worktree
    still read "job change", and a filter body of `sh -c ...` executed code
    outside the worktree. The file set stayed authorised throughout, which is
    the point — path-level authorisation says nothing about content, so a
    commit could be authorised and still not contain what the job wrote.

    Hooks are suppressed by configuration; filters cannot be. They are
    selected by an in-tree `.gitattributes` that no config overrides, and
    filter names are arbitrary, so there is no set of keys to blank. What is
    available is detection: `git check-attr` reports the filter that would
    apply without running it. A path carrying one is refused, and the job
    returns to AL/X rather than committing content it did not write.

    What this does **not** do is prevent the filter from executing. `git
    status` and `git diff` run a clean filter to decide whether a path is
    modified, so any inspection of a filtered worktree runs it — including the
    read-only inspection `coding_process.py` has performed since before this
    capability existed. That is git's behaviour, not this capability's, and
    the honest statement of the boundary is: a filtered path cannot enter a
    commit, and a filtered worktree is refused at the baseline before this
    capability inspects it. A repository that arrives already filtered has
    executed its own code the moment anything reads it.

    The cost is that a repository legitimately using a clean filter — Git LFS
    is the common case — cannot be committed to by a coding job. That is the
    correct default for an authority this narrow: running arbitrary
    repository code to support it would be a change to D-029, not a default to
    loosen quietly.
    """
    if not paths:
        return
    listed = _require(
        worktree,
        ["git", "check-attr", "-z", "filter", "--", *paths],
        "attributes_unreadable",
        bounded=False,
    )
    fields = _nul_paths(listed)
    # `-z` emits repeating <path> <attribute> <value> triples.
    filtered = tuple(
        fields[index]
        for index in range(0, len(fields) - 2, 3)
        if fields[index + 2] not in ("unspecified", "unset")
    )
    if filtered:
        raise CodingError(
            "git_refused",
            reason_code="attribute_filter_would_rewrite_content",
            filtered_count=len(filtered),
        )


def _unstage(worktree: Path, paths: tuple[str, ...]) -> None:
    """Take named paths back out of the index, leaving their content alone.

    This is D-029's approved index-rollback reset: named job paths only, no
    `--hard`/`--soft`/`--mixed`, no ref, and no worktree content discarded.
    Failure is deliberately not raised — it is already unwinding a failure,
    and the caller's original error is the more useful one to report.
    """
    if not paths:
        return
    try:
        _run(worktree, ["git", "reset", "--quiet", "--", *paths])
    except CodingError:
        return


def _reconcile_after_commit(
    worktree: Path, before_sha: str, failure: CodingError
) -> CodingError:
    """Decide what a post-commit failure actually means, by asking git.

    A timeout or a failed readback does not establish that no commit was
    created: the ref may already have moved. Reporting an unqualified failure
    in that case tells Core the branch is unchanged when it is not, and Core
    would go on to redo or abandon work that is already committed.

    So HEAD is read once more. If it did not move, the original failure stands
    and is the truth. If it did, the failure is replaced by an explicitly
    indeterminate one naming the SHA that exists, because "a commit was
    created and I could not verify it" is a different fact from "no commit was
    created" and only AL/X can decide what to do about it.
    """
    try:
        current = _require(
            worktree, ["git", "rev-parse", "HEAD"], "no_head"
        ).strip()
    except CodingError:
        return CodingError(
            "git_unavailable",
            reason_code="commit_state_indeterminate",
            **failure.details,
        )
    if current == before_sha:
        return failure
    return CodingError(
        "git_unavailable",
        reason_code="commit_created_but_unverified",
        commit_sha=current,
        **{key: value for key, value in failure.details.items()
           if key not in ("reason_code", "commit_sha")},
    )


def _committed_paths(worktree: Path) -> tuple[str, ...]:
    """The paths HEAD's own commit touched, read back after it was created."""
    listed = _require(
        worktree,
        ["git", "show", "--name-only", "--pretty=format:", "-z", "HEAD"],
        "commit_unreadable",
        bounded=False,
    )
    return _bounded_entries(listed, "commit_too_large")


def _staged_paths(worktree: Path) -> tuple[str, ...]:
    """Exactly what the index holds against HEAD, by name.

    Read whole. Authorisation is decided from this list, so an entry that is
    truncated away is an entry the check cannot refuse. An index larger than
    the entry bound fails closed rather than being compared in part.
    """
    listed = _require(
        worktree,
        ["git", "diff", "--cached", "--name-only", "-z"],
        "index_unreadable",
        bounded=False,
    )
    return _bounded_entries(listed, "index_too_large")


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
