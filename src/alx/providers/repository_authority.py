"""Run one enumerated repository operation and report what it did.

One provider for the whole of AL/X's repository work, replacing a capability
per verb. The authority is still granted by enumeration: `_SHAPES` maps each
operation to the exact argv it may produce, built from validated arguments, so
a force where none was asked for, a second refspec, an alternate remote or a
path leaving the checkout are shapes that cannot be constructed rather than
cases somebody has to remember to reject.

Nothing here accepts a command string. There is no shell, no flag passthrough
and no interpolation of caller text into an option position.

Every mutation returns where the ref started and where it ended. That is what
makes the evidence worth having: "pushed" is a claim, while `fix/x 3a1f9c2 ->
9d4b1e7 on origin` is a fact AL/X can check against what she meant to do.

The one refusal is `refuse_if_self_destructive`, consulted before any argv is
built. It protects the canonical checkout and canonical `main`, and nothing
else — a feature branch may be deleted, force-pushed, reset or rebased freely.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from alx.providers.repository_runtime import origin_identity
from alx.contracts.github_pull_request import (
    PullRequestError,
    PullRequestRequest,
)
from alx.contracts.repository_authority import (
    GITHUB_OPERATIONS,
    REMOTE_OPERATIONS,
    CanonicalSystem,
    Operation,
    normalised_arguments,
    READ_ONLY,
    RepositoryAuthorityError,
    RepositoryCheckoutStatus,
    RepositoryOutcome,
    RepositoryRequest,
    refuse_if_self_destructive,
    valid_ref,
    valid_revision,
    valid_sha,
)

LOGGER = logging.getLogger(__name__)

Runner = Callable[..., Any]

# The one remote this service reaches. Fixed here so no caller can direct work
# at a remote nobody is watching.
ORIGIN = "origin"

_ORIGIN_URL = ("git", "config", "--get", "remote.origin.url")
# Which branch HEAD is on. `reset` and `rebase` rewrite it without naming it.
_SYMBOLIC_REF = ("git", "symbolic-ref", "--quiet", "--short", "HEAD")

_SAFE_GIT_CONFIG = {
    "core.hooksPath": os.devnull,
    # Reset first: an empty value discards every helper the system and global
    # configuration would otherwise contribute, so nothing AL/X did not choose
    # can answer a credential prompt.
    "credential.helper": "",
    # Then one helper, for one host. Without this she could read and commit but
    # never push: the environment above deliberately removes the machine's
    # `osxkeychain` helper, and with no helper at all git has nothing to ask.
    #
    # `gh auth git-credential` resolves the credential from the GitHub CLI's
    # own keyring at the moment it is needed. No token is written here, stored
    # in configuration, or passed through the environment — this names a
    # program, and the program holds the secret. Scoped to github.com so it
    # answers for that host and no other.
    "credential.https://github.com.helper": "!gh auth git-credential",
}


def _git_environment() -> dict[str, str]:
    """The small fixed environment every command runs under.

    The locale is set rather than inherited because several decisions here are
    read from git's own wording — a divergence, a conflict, an up-to-date push
    — and git translates all of them when the environment asks it to.
    """
    environment = {
        name: os.environ[name]
        for name in ("PATH", "TZ", "HOME")
        if name in os.environ
    }
    environment.update({
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": os.devnull,
        "SSH_ASKPASS": os.devnull,
        "GIT_CONFIG_COUNT": str(len(_SAFE_GIT_CONFIG)),
    })
    for index, (key, value) in enumerate(_SAFE_GIT_CONFIG.items()):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


def _revision(arguments: Mapping[str, Any], name: str, *, required: bool = True) -> str:
    value = str(arguments.get(name, "") or "").strip()
    if not value:
        if required:
            raise RepositoryAuthorityError("arguments_unusable", f"{name} is required")
        return ""
    if not valid_revision(value):
        raise RepositoryAuthorityError("arguments_unusable", f"{name} is not a revision")
    return value


def _ref(arguments: Mapping[str, Any], name: str, *, required: bool = True) -> str:
    value = str(arguments.get(name, "") or "").strip()
    if not value:
        if required:
            raise RepositoryAuthorityError("arguments_unusable", f"{name} is required")
        return ""
    if not valid_ref(value):
        raise RepositoryAuthorityError("arguments_unusable", f"{name} is not a ref")
    return value


def _text(arguments: Mapping[str, Any], name: str) -> str:
    value = str(arguments.get(name, "") or "").strip()
    if not value:
        raise RepositoryAuthorityError("arguments_unusable", f"{name} is required")
    return value


def _count(arguments: Mapping[str, Any], name: str, default: int, limit: int) -> int:
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RepositoryAuthorityError("arguments_unusable", f"{name} must be positive")
    return min(value, limit)


class RepositoryAuthority:
    """AL/X's repository authority over one configured checkout."""

    def __init__(
        self,
        system: CanonicalSystem,
        timeout_seconds: int = 120,
        runner: Runner = subprocess.run,
        pull_requests: Any = None,
        remote_verified: bool = True,
        verified_remote: str = "",
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("repository timeout must be positive")
        self._system = system
        self._root = system.root.resolve()
        self._timeout = timeout_seconds
        self._runner = runner
        # The GitHub side of the same authority. Absent when GitHub is not
        # configured, in which case those operations say so rather than
        # appearing in the catalogue and failing as unusable arguments.
        self._pull_requests = pull_requests
        # Whether this checkout was confirmed to be the configured repository.
        # False leaves local work available and refuses everything that reaches
        # the remote: an unverified origin is not somewhere AL/X's work may go.
        self._remote_verified = remote_verified
        # The URL confirmed to be the configured repository. A push names this
        # rather than `origin`, because a name is a lookup in `.git/config`
        # that git performs when the command runs — after the check — and
        # anything able to write that file could point it elsewhere in between.
        self._verified_remote = verified_remote

    # ---- process ---------------------------------------------------------

    def _run(self, command: tuple[str, ...]) -> subprocess.CompletedProcess:
        try:
            return self._runner(
                list(command),
                cwd=str(self._root),
                env=_git_environment(),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                # Explicit, never inherited: a shell would let a ref name be
                # read as syntax rather than as an argument.
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning("Repository command failed: %s", type(error).__name__)
            raise RepositoryAuthorityError("repository_unavailable") from error

    def _text_of(self, command: tuple[str, ...], failure: str = "ref_unknown") -> str:
        completed = self._run(command)
        if completed.returncode != 0:
            raise RepositoryAuthorityError(failure)
        return (completed.stdout or "").strip()

    def _sha_of(self, revision: str) -> str:
        """What this revision points at now, or "" when it names nothing."""
        completed = self._run(("git", "rev-parse", "--verify", f"{revision}^{{commit}}"))
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    def _remote_for(self, operation: Operation) -> str:
        """The remote this operation involves, for the audit record."""
        return self._remote() if operation in REMOTE_OPERATIONS else ""

    def _remote(self) -> str:
        """Where a remote operation may go.

        The verified URL when composition captured one, and otherwise the
        `origin` name. The name is only reached in tests and in a checkout
        whose origin was never confirmed — and in that case every remote
        operation is refused before this is consulted.
        """
        return self._verified_remote or ORIGIN

    def origin(self) -> tuple[str, str]:
        """The origin URL and the `owner/name` it resolves to, from one read.

        Both together, because they must describe the same value. Reading the
        configuration twice — once to check the identity and once to keep the
        URL — leaves a window in which `.git/config` can change between them,
        and the URL that gets stored is then not the one whose identity was
        verified. The check and the thing checked have to come from the same
        answer.

        Empty when there is no origin, when it cannot be read, or when it is
        not a GitHub remote in a form this system recognises.
        """
        completed = self._run(_ORIGIN_URL)
        if completed.returncode != 0:
            return "", ""
        url = (completed.stdout or "").strip()
        return url, origin_identity(url)

    def origin_identity(self) -> str:
        """The `owner/name` this checkout's origin points at, or ""."""
        return self.origin()[1]

    # ---- the invariant ---------------------------------------------------

    def _affected_ref(
        self, operation: Operation, arguments: Mapping[str, Any]
    ) -> str:
        """The ref this operation would actually touch.

        One answer, used both to decide the refusal and to record it. They were
        computed separately, and diverged: `reset` and `rebase` act on whatever
        is checked out, so the invariant already ignored the named arguments —
        but the audit record still read them, and a refusal could name a decoy
        branch the command would never have touched. The record has to describe
        the same operation the decision was about.
        """
        if operation in (Operation.RESET, Operation.REBASE):
            return self._checked_out_branch()
        return str(
            arguments.get("branch")
            or arguments.get("ref")
            or arguments.get("target")
            or ""
        ).strip()

    def _refuse_if_self_destructive(
        self, operation: Operation, arguments: Mapping[str, Any]
    ) -> str:
        """The one rule, consulted before any argv exists."""
        path: Path | None = None
        raw_path = arguments.get("path")
        if isinstance(raw_path, (str, Path)) and str(raw_path).strip():
            candidate = Path(str(raw_path)).expanduser()
            path = candidate if candidate.is_absolute() else self._root / candidate
        # What the command acts on, never what the request names it. `reset`
        # and `rebase` take a revision and rewrite whatever is checked out, so
        # a request carrying a valid revision and an unrelated `branch` would
        # otherwise have the invariant examine the decoy while the command
        # rewrote canonical `main`.
        ref = self._affected_ref(operation, arguments)
        return refuse_if_self_destructive(
            self._system, operation, ref=ref, path=path
        )

    def _checked_out_branch(self) -> str:
        """The branch HEAD is on, or "" when detached.

        A detached HEAD rewrites no branch, so there is nothing for the
        invariant to protect and "" is the honest answer rather than a guess.
        """
        completed = self._run(_SYMBOLIC_REF)
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    # ---- argv ------------------------------------------------------------

    def _argv(self, operation: Operation, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        """The exact command this operation may run, and no other.

        Every element is either a literal or a validated value. A caller cannot
        add a flag, because no branch here reads an unvalidated string into a
        position where git would parse it as one.
        """
        match operation:
            case Operation.RESOLVE:
                return ("git", "rev-parse", "--verify",
                        f"{_revision(arguments, 'revision')}^{{commit}}")
            case Operation.SHOW_COMMIT:
                return ("git", "show", "--no-patch",
                        "--format=%H%x00%an%x00%aI%x00%s%x00%P",
                        _revision(arguments, "revision"))
            case Operation.LIST_BRANCHES:
                return ("git", "for-each-ref", "--format=%(refname:short)%00%(objectname)",
                        "refs/heads", "refs/remotes")
            case Operation.LIST_TAGS:
                return ("git", "for-each-ref", "--format=%(refname:short)%00%(objectname)",
                        "refs/tags")
            case Operation.LOG:
                return ("git", "log", "--format=%H%x00%an%x00%aI%x00%s",
                        f"--max-count={_count(arguments, 'limit', 20, 200)}",
                        _revision(arguments, "revision"), "--")
            case Operation.DIFF:
                return ("git", "diff", "--no-color", "--unified=3",
                        f"{_revision(arguments, 'base')}...{_revision(arguments, 'head')}",
                        "--")
            case Operation.CHANGED_FILES:
                return ("git", "diff", "--name-status", "-z",
                        f"{_revision(arguments, 'base')}...{_revision(arguments, 'head')}",
                        "--")
            case Operation.MERGE_BASE:
                return ("git", "merge-base",
                        _revision(arguments, "base"), _revision(arguments, "head"))
            case Operation.AHEAD_BEHIND:
                return ("git", "rev-list", "--left-right", "--count",
                        f"{_revision(arguments, 'base')}...{_revision(arguments, 'head')}")
            case Operation.IS_ANCESTOR:
                return ("git", "merge-base", "--is-ancestor",
                        _revision(arguments, "ancestor"), _revision(arguments, "descendant"))
            case Operation.BRANCH_CONTAINS:
                return ("git", "for-each-ref", "--format=%(refname:short)",
                        f"--contains={_revision(arguments, 'revision')}",
                        "refs/heads", "refs/remotes")
            case Operation.STATUS:
                return ("git", "status", "--porcelain=v1", "-z", "-uall")
            case Operation.LIST_WORKTREES:
                return ("git", "worktree", "list", "--porcelain")
            case Operation.FETCH:
                # The refspec is named because the remote is a URL. Naming
                # `origin` would carry its configured fetch refspec with it; a
                # URL carries none, so git wrote FETCH_HEAD and left
                # `refs/remotes/origin/*` untouched — the fetch reported
                # success and `pull_fast_forward` then merged stale state.
                return ("git", "fetch", "--prune", self._remote(),
                        f"+refs/heads/*:refs/remotes/{ORIGIN}/*")
            case Operation.PULL_FAST_FORWARD:
                # `git merge` advances whatever is checked out, not the branch
                # named. Naming `main` while `fix/x` was checked out advanced
                # `fix/x` and reported `main` as the affected ref — the record
                # describing an operation that did not happen. The branch must
                # be the one HEAD is on, and a mismatch is refused rather than
                # silently applied somewhere else.
                branch = _ref(arguments, "branch")
                current = self._checked_out_branch()
                if current != branch:
                    raise RepositoryAuthorityError(
                        "arguments_unusable",
                        f"{branch} is not checked out ({current or 'detached'} is); "
                        "switch to it first",
                    )
                return ("git", "fetch", "--no-tags", self._remote(),
                        f"+refs/heads/{branch}:refs/remotes/{ORIGIN}/{branch}")
            case Operation.CREATE_BRANCH:
                return ("git", "branch", _ref(arguments, "branch"),
                        _revision(arguments, "start_point"))
            case Operation.SWITCH_BRANCH:
                return ("git", "switch", _ref(arguments, "branch"))
            case Operation.DELETE_BRANCH:
                # `-D` rather than `-d`: AL/X decides whether the work is
                # wanted. The invariant, not git's merged check, is what keeps
                # her from deleting the branch she cannot lose.
                return ("git", "branch", "-D", _ref(arguments, "branch"))
            case Operation.DELETE_REMOTE_BRANCH:
                branch = _ref(arguments, "branch")
                return ("git", "push", self._remote(), f":refs/heads/{branch}")
            case Operation.STAGE:
                paths = self._paths(arguments)
                return ("git", "add", "--", *paths)
            case Operation.COMMIT:
                return ("git", "commit", "-m", _text(arguments, "message"))
            case Operation.AMEND:
                return ("git", "commit", "--amend", "-m", _text(arguments, "message"))
            case Operation.CHERRY_PICK:
                return ("git", "cherry-pick", _revision(arguments, "revision"))
            case Operation.REVERT:
                return ("git", "revert", "--no-edit", _revision(arguments, "revision"))
            case Operation.LOCAL_MERGE:
                return ("git", "merge", "--no-edit", _revision(arguments, "revision"))
            case Operation.REBASE:
                return ("git", "rebase", _revision(arguments, "onto"))
            case Operation.RESET:
                mode = str(arguments.get("mode", "mixed")).strip()
                if mode not in ("soft", "mixed", "hard"):
                    raise RepositoryAuthorityError(
                        "arguments_unusable", "mode must be soft, mixed or hard"
                    )
                return ("git", "reset", f"--{mode}", _revision(arguments, "revision"))
            case Operation.PUSH:
                branch = _ref(arguments, "branch")
                return ("git", "push", self._remote(),
                        f"refs/heads/{branch}:refs/heads/{branch}")
            case Operation.FORCE_PUSH:
                branch = _ref(arguments, "branch")
                # `--force-with-lease` rather than `--force`: it still
                # replaces the branch, and it refuses when the remote holds
                # something this checkout has never seen, which is somebody
                # else's work rather than AL/X's own.
                #
                # The expected revision is named because the remote is a URL.
                # The bare form derives its lease from the tracking ref, and a
                # URL does not select one — so the lease would be empty and the
                # protection silently absent. Without a tracking ref there is
                # nothing to lease against, and the operation is refused rather
                # than performed as an unguarded force.
                tracking = self._sha_of(f"refs/remotes/{ORIGIN}/{branch}")
                if not tracking:
                    raise RepositoryAuthorityError(
                        "arguments_unusable",
                        f"no tracking ref for {branch}; fetch before forcing",
                    )
                return ("git", "push",
                        f"--force-with-lease={branch}:{tracking}",
                        self._remote(),
                        f"refs/heads/{branch}:refs/heads/{branch}")
            case Operation.ADD_WORKTREE:
                return ("git", "worktree", "add", "-b", _ref(arguments, "branch"),
                        str(self._worktree_path(arguments)),
                        _revision(arguments, "start_point"))
            case Operation.REMOVE_WORKTREE:
                return ("git", "worktree", "remove", "--force",
                        str(self._worktree_path(arguments)))
            case Operation.PRUNE_WORKTREES:
                return ("git", "worktree", "prune")
        raise RepositoryAuthorityError("arguments_unusable", "unknown operation")

    def _paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        """Named paths inside the checkout, never a sweep and never an escape."""
        raw = arguments.get("paths")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)) or not raw:
            raise RepositoryAuthorityError("arguments_unusable", "paths are required")
        resolved: list[str] = []
        for item in raw:
            candidate = str(item).strip()
            if not candidate or candidate.startswith("-"):
                raise RepositoryAuthorityError("arguments_unusable", "unusable path")
            full = (self._root / candidate).resolve()
            if not full.is_relative_to(self._root):
                raise RepositoryAuthorityError(
                    "arguments_unusable", "path is outside the checkout"
                )
            resolved.append(str(full.relative_to(self._root)))
        return tuple(resolved)

    def _worktree_path(self, arguments: Mapping[str, Any]) -> Path:
        raw = str(arguments.get("path", "") or "").strip()
        if not raw or raw.startswith("-"):
            raise RepositoryAuthorityError("arguments_unusable", "path is required")
        candidate = Path(raw).expanduser()
        return candidate if candidate.is_absolute() else (self._root / candidate)

    # ---- reading results -------------------------------------------------

    def _values(
        self, operation: Operation, completed: subprocess.CompletedProcess
    ) -> Mapping[str, Any]:
        """What a reading operation found, structured rather than raw text."""
        output = (completed.stdout or "").strip()
        match operation:
            case Operation.RESOLVE:
                return {"sha": output}
            case Operation.SHOW_COMMIT:
                parts = output.split("\x00")
                if len(parts) < 5:
                    return {}
                return {
                    "sha": parts[0], "author": parts[1], "authored_at": parts[2],
                    "subject": parts[3],
                    "parents": tuple(p for p in parts[4].split() if p),
                }
            case Operation.LIST_BRANCHES | Operation.LIST_TAGS:
                refs = []
                for line in output.splitlines():
                    name, _, sha = line.partition("\x00")
                    if name:
                        refs.append({"ref": name, "sha": sha})
                return {"refs": tuple(refs)}
            case Operation.LOG:
                commits = []
                for line in output.splitlines():
                    parts = line.split("\x00")
                    if len(parts) >= 4:
                        commits.append({
                            "sha": parts[0], "author": parts[1],
                            "authored_at": parts[2], "subject": parts[3],
                        })
                return {"commits": tuple(commits)}
            case Operation.DIFF:
                return {"diff": completed.stdout or ""}
            case Operation.CHANGED_FILES:
                entries = [item for item in (completed.stdout or "").split("\x00") if item]
                files = []
                index = 0
                while index + 1 < len(entries):
                    status = entries[index]
                    # A rename or copy emits three fields — status, old path,
                    # new path — where every other status emits two. Consuming
                    # two for all of them reported the old path as the changed
                    # one and shifted every later entry by a field, pairing the
                    # remaining statuses with the wrong paths.
                    if status[:1] in ("R", "C") and index + 2 < len(entries):
                        files.append({
                            "status": status,
                            "path": entries[index + 2],
                            "previous_path": entries[index + 1],
                        })
                        index += 3
                        continue
                    files.append({"status": status, "path": entries[index + 1]})
                    index += 2
                return {"files": tuple(files)}
            case Operation.MERGE_BASE:
                return {"sha": output}
            case Operation.AHEAD_BEHIND:
                left, _, right = output.partition("\t")
                return {
                    "behind": int(left or 0) if left.strip().isdigit() else 0,
                    "ahead": int(right or 0) if right.strip().isdigit() else 0,
                }
            case Operation.IS_ANCESTOR:
                return {"is_ancestor": completed.returncode == 0}
            case Operation.BRANCH_CONTAINS:
                return {"refs": tuple(item for item in output.splitlines() if item)}
            case Operation.STATUS:
                entries = [item for item in (completed.stdout or "").split("\x00") if item]
                return {"entries": tuple(entries), "clean": not entries}
            case Operation.LIST_WORKTREES:
                trees, current = [], {}
                for line in output.splitlines():
                    if not line:
                        if current:
                            trees.append(current); current = {}
                        continue
                    key, _, value = line.partition(" ")
                    current[key] = value
                if current:
                    trees.append(current)
                return {"worktrees": tuple(trees)}
        return {}

    # ---- the pull request ------------------------------------------------

    def _perform_on_github(
        self, operation: Operation, arguments: Mapping[str, Any]
    ) -> RepositoryOutcome:
        """The GitHub half of the same authority.

        Separate from the git argv path because these are API calls rather than
        commands, and part of the same capability because proposing, revising
        and answering a review are parts of one job. Splitting them out would
        be the narrow model again, one capability per verb.

        The self-preservation invariant does not reach here: nothing this
        boundary offers can destroy the canonical repository.
        """

        def outcome(succeeded: bool, **values: Any) -> RepositoryOutcome:
            # A pull-request mutation is as auditable as a git one: the branch
            # it concerns and the revision that branch pointed at, so the
            # record says which proposal moved rather than only that one did.
            branch = str(values.get("branch") or "")
            head = str(values.get("head_sha") or "")
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=succeeded,
                source_ref=branch,
                source_sha=head,
                resulting_ref=branch,
                resulting_sha=head,
                remote=ORIGIN if operation not in READ_ONLY else "",
                values=values,
            )

        if self._pull_requests is None:
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=False,
                failure_code="repository_unavailable",
                refusal_reason="GitHub is not configured",
            )

        def number() -> int:
            value = arguments.get("pull_request_number")
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RepositoryAuthorityError(
                    "arguments_unusable", "pull_request_number must be positive"
                )
            return value

        try:
            match operation:
                case Operation.FIND_PULL_REQUEST:
                    found = self._pull_requests.find(_ref(arguments, "branch"))
                    if found is None:
                        return outcome(True, found=False)
                    return outcome(True, found=True, **found.as_values())
                case Operation.OPEN_PULL_REQUEST:
                    # `PullRequestRequest` validates more strictly than `_ref`
                    # does — a protected branch is a usable ref and not a
                    # publishable one — and it says so with `ValueError`, which
                    # is outside the handler below. Unconverted it reached the
                    # broker as an executor fault rather than as the declared
                    # "these arguments cannot be used".
                    try:
                        proposal = PullRequestRequest(
                            _ref(arguments, "branch"),
                            _text(arguments, "title"),
                            str(arguments.get("body", "") or ""),
                        )
                    except (TypeError, ValueError) as error:
                        raise RepositoryAuthorityError(
                            "arguments_unusable", str(error)
                        ) from error
                    opened = self._pull_requests.open(proposal)
                    return outcome(True, **opened.as_values())
                case Operation.UPDATE_PULL_REQUEST:
                    updated = self._pull_requests.update(
                        number(),
                        str(arguments.get("title", "") or ""),
                        str(arguments.get("body", "") or ""),
                    )
                    return outcome(True, **updated.as_values())
                case Operation.COMMENT_ON_PULL_REQUEST:
                    posted = self._pull_requests.comment(
                        number(), _text(arguments, "body")
                    )
                    return outcome(bool(posted), posted=bool(posted))
                case Operation.READ_REVIEW_THREADS:
                    threads = self._pull_requests.review_threads(number())
                    return outcome(True, threads=threads, count=len(threads))
                case Operation.RESOLVE_REVIEW_THREAD:
                    resolved = self._pull_requests.resolve_review_thread(
                        _text(arguments, "thread_id")
                    )
                    return outcome(bool(resolved), resolved=bool(resolved))
        except PullRequestError as error:
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=False,
                failure_code="operation_refused",
                refusal_reason=error.code,
            )
        raise RepositoryAuthorityError("arguments_unusable", "unknown operation")

    # ---- checkout status -------------------------------------------------

    def read_checkout_status(self) -> RepositoryCheckoutStatus:
        """Branch, full HEAD and cleanliness of the configured checkout.

        The checkout is the canonical root fixed at construction. This only
        reads which branch HEAD is on, the commit that name resolves to, and
        whether the tree is clean. It does not stage, commit, fetch or switch.
        """
        branch = self._checked_out_branch()
        head_sha = self._sha_of("HEAD")
        if not valid_sha(head_sha):
            raise RepositoryAuthorityError("ref_unknown")
        completed = self._run(self._argv(Operation.STATUS, {}))
        if completed.returncode != 0:
            raise RepositoryAuthorityError("repository_unavailable")
        values = self._values(Operation.STATUS, completed)
        return RepositoryCheckoutStatus(
            branch=branch,
            detached=branch == "",
            head_sha=head_sha,
            clean=values["clean"],
        )

    # ---- the one entry point --------------------------------------------

    def perform(self, request: RepositoryRequest) -> RepositoryOutcome:
        """Run one operation and report what it did."""
        operation = request.operation
        # Under the names this operation declares, so a caller who said `head`
        # for a pull request's source branch is not refused for the word.
        arguments = normalised_arguments(operation, request.arguments)

        refusal = self._refuse_if_self_destructive(operation, arguments)
        if refusal:
            LOGGER.warning(
                "Repository operation refused to protect AL/X: %s (%s)",
                operation.value, refusal,
            )
            ref = self._affected_ref(operation, arguments)
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=False,
                source_ref=ref,
                source_sha=self._sha_of(ref) if ref else "",
                remote=self._remote_for(operation),
                failure_code="self_preservation",
                refusal_reason=refusal,
            )

        if operation in REMOTE_OPERATIONS and not self._remote_verified:
            LOGGER.warning(
                "Repository operation refused: %s reaches an unverified remote",
                operation.value,
            )
            ref = str(arguments.get("branch") or "").strip()
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=False,
                source_ref=ref,
                source_sha=self._sha_of(ref) if ref else "",
                remote=ORIGIN,
                failure_code="operation_refused",
                refusal_reason=(
                    "the checkout's origin is not the configured repository, "
                    "so nothing may be sent to it"
                ),
            )

        if operation in GITHUB_OPERATIONS:
            return self._perform_on_github(operation, arguments)

        command = self._argv(operation, arguments)

        # Where the affected ref stood before, so the evidence can say what
        # changed rather than only that something ran. The same answer the
        # invariant and the refusal record use: a third copy of this resolution
        # is how a successful reset came to report the branch the request named
        # rather than the one it moved.
        named_ref = self._affected_ref(operation, arguments)
        source_ref = named_ref or ("HEAD" if operation not in READ_ONLY else "")
        source_sha = self._sha_of(source_ref) if source_ref else ""

        expected_sha = ""
        if operation is Operation.PULL_FAST_FORWARD:
            # A successful merge of a cached tracking ref says nothing about
            # synchronisation. Fetch only the named branch from the verified
            # remote, and bind this operation to the commit actually fetched.
            tracking = f"refs/remotes/{ORIGIN}/{named_ref}"
            self._text_of(command, "operation_refused")
            expected_sha = self._sha_of("FETCH_HEAD")
            if not expected_sha or self._sha_of(tracking) != expected_sha:
                raise RepositoryAuthorityError(
                    "operation_refused", "fetched and tracking revisions differ"
                )
            # Merge the verified snapshot rather than a ref another fetch can
            # move between verification and merge.
            command = ("git", "merge", "--ff-only", expected_sha)

        completed = self._run(command)

        # `merge-base --is-ancestor` answers with its exit code: 0 is yes and 1
        # is no, and both are answers. Anything else is git failing — an
        # unknown ref, an unreadable repository — and treating every non-zero
        # code as "no" turned "I could not tell" into a confident negative on
        # the question AL/X uses to decide whether work is already merged.
        if operation is Operation.IS_ANCESTOR and completed.returncode in (0, 1):
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=True,
                values=self._values(operation, completed),
            )

        if completed.returncode != 0:
            combined = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
            if "conflict" in combined or "could not apply" in combined:
                code = "conflict"
            elif (
                "non-fast-forward" in combined
                or "[rejected]" in combined
                or "fetch first" in combined
                or "stale info" in combined
            ):
                code = "branch_diverged"
            elif (
                "unknown revision" in combined
                or "not a valid" in combined
                # `rev-parse --verify` says this when the name resolves to
                # nothing, which is the ordinary "no such ref" answer.
                or "needed a single revision" in combined
                or "bad revision" in combined
                or "did not match any" in combined
            ):
                code = "ref_unknown"
            else:
                code = "operation_refused"
            LOGGER.info("Repository operation did not complete: %s (%s)",
                        operation.value, code)
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=False,
                source_ref=source_ref,
                source_sha=source_sha,
                remote=self._remote_for(operation),
                failure_code=code,
                refusal_reason=(completed.stderr or "").strip()[:400],
            )

        resulting_ref = named_ref or ("HEAD" if operation not in READ_ONLY else "")
        resulting_sha = self._sha_of(resulting_ref) if resulting_ref else ""
        remote = self._remote_for(operation)

        if expected_sha and (
            resulting_sha != expected_sha
            or self._checked_out_branch() != named_ref
            or self._sha_of("HEAD") != expected_sha
        ):
            return RepositoryOutcome(
                repository=self._system.repository,
                operation=operation,
                succeeded=False,
                source_ref=source_ref,
                source_sha=source_sha,
                resulting_ref=resulting_ref,
                resulting_sha=resulting_sha,
                remote=remote,
                failure_code="operation_refused",
                refusal_reason="local branch did not reach the fetched revision",
            )

        outcome = RepositoryOutcome(
            repository=self._system.repository,
            operation=operation,
            succeeded=True,
            source_ref=source_ref,
            source_sha=source_sha,
            resulting_ref=resulting_ref,
            resulting_sha=resulting_sha,
            remote=remote,
            values=self._values(operation, completed),
        )
        if operation not in READ_ONLY:
            # One line per mutation, carrying what changed.
            LOGGER.info(
                "Repository %s: %s %s -> %s%s",
                operation.value,
                source_ref or "-",
                source_sha[:8] or "-",
                resulting_sha[:8] or "-",
                f" on {remote}" if remote else "",
            )
        return outcome


__all__ = ["ORIGIN", "RepositoryAuthority"]
