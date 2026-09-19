"""What AL/X may do to a repository, and the one thing she may not.

Repository work was granted one capability at a time: publish a branch, open a
pull request, fast-forward main. Each was correct and each was narrow, so every
ordinary question — is this commit merged, what is on that branch, can this be
deleted — needed another bespoke capability before she could answer it. The
narrowness was not protecting anything; it was a backlog.

So the authority is now general and the restriction is singular: AL/X manages
her repositories the way an engineer does, and the only operation refused is one
that would end her ability to exist.

## Enumerated, not free

Authority is still granted by enumeration, exactly as the Coding Agent's writes
are. `Operation` lists what may run; each maps to a fixed argv shape built from
validated arguments, so `--force` where it was not asked for, a second refspec,
an alternate remote or a path escaping the checkout have no shape they can take.
The difference from before is the size of the list, not the kind of control.

This is not a shell. Nothing here accepts a command string, and no operation
takes arbitrary flags.

## The invariant

One rule, structural rather than advisory: AL/X may not irrecoverably destroy
her own canonical existence. It is deliberately narrow. Deleting a feature
branch, force-pushing one, resetting it, removing files, replacing a subsystem
or rewriting her own implementation are all ordinary work and none of them is
refused — losing work is recoverable, and a permission system that prevented
every mistake would be the thing this change exists to remove.

What is refused is the small set of operations after which there would be no
AL/X to recover anything: destroying the canonical checkout, destroying the
canonical `main` history, or removing the configuration that makes the canonical
repository findable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


# \Z rather than $: $ also matches before a terminal newline, so a value with
# one appended passes validation and reaches git as something else entirely.
_FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")
_SHORT_SHA = re.compile(r"\A[0-9a-f]{7,40}\Z")

# A ref name this service will carry. Narrower than git permits: no whitespace,
# no refspec punctuation, nothing readable as an option or a second ref. Slashes
# are allowed because real branch names use them (`fix/thing`, `alx/probe-1`),
# but `..` and a leading `-` are not.
_REF = re.compile(r"\A(?!-)(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._/-]*\Z")

# The branch that carries the canonical history. Named here because the
# invariant below is about this ref on this repository, not about branches in
# general.
CANONICAL_BRANCH = "main"


class Operation(str, Enum):
    """Every repository operation AL/X may perform.

    Adding one is a visible change to a short list, which is what keeps this an
    authority rather than a shell.
    """

    # Reading. None of these changes anything, and none can be refused by the
    # invariant: knowing the state of the repository is never destructive.
    RESOLVE = "resolve"
    SHOW_COMMIT = "show_commit"
    LIST_BRANCHES = "list_branches"
    LIST_TAGS = "list_tags"
    LOG = "log"
    DIFF = "diff"
    CHANGED_FILES = "changed_files"
    MERGE_BASE = "merge_base"
    AHEAD_BEHIND = "ahead_behind"
    IS_ANCESTOR = "is_ancestor"
    BRANCH_CONTAINS = "branch_contains"
    STATUS = "status"
    LIST_WORKTREES = "list_worktrees"

    # Synchronising with the remote.
    FETCH = "fetch"
    PULL_FAST_FORWARD = "pull_fast_forward"

    # Branch lifecycle.
    CREATE_BRANCH = "create_branch"
    SWITCH_BRANCH = "switch_branch"
    DELETE_BRANCH = "delete_branch"
    DELETE_REMOTE_BRANCH = "delete_remote_branch"

    # Building work.
    STAGE = "stage"
    COMMIT = "commit"
    AMEND = "amend"
    CHERRY_PICK = "cherry_pick"
    REVERT = "revert"
    # `local_merge`, not `merge`: this joins one local history to another and
    # knows nothing about pull requests. `merge_pull_request` is the GitHub
    # operation, bound to an exact reviewed head and subject to branch
    # protection. Naming this one `merge` invited them to be read as duplicates
    # of each other, which a reviewer duly did.
    LOCAL_MERGE = "local_merge"
    REBASE = "rebase"
    RESET = "reset"

    # Publishing.
    PUSH = "push"
    FORCE_PUSH = "force_push"

    # The pull request the work is proposed in. GitHub rather than git, but
    # the same authority: revising a proposal, finding one, reading what a
    # reviewer said and answering it are ordinary parts of the work, and
    # splitting them into a second capability would be the narrow model again.
    FIND_PULL_REQUEST = "find_pull_request"
    OPEN_PULL_REQUEST = "open_pull_request"
    UPDATE_PULL_REQUEST = "update_pull_request"
    COMMENT_ON_PULL_REQUEST = "comment_on_pull_request"
    READ_REVIEW_THREADS = "read_review_threads"
    RESOLVE_REVIEW_THREAD = "resolve_review_thread"

    # Worktrees.
    ADD_WORKTREE = "add_worktree"
    REMOVE_WORKTREE = "remove_worktree"
    PRUNE_WORKTREES = "prune_worktrees"


# Operations that only read. Kept as data rather than as a naming convention so
# the audit record can say plainly whether a call could have changed anything.
READ_ONLY: frozenset[Operation] = frozenset({
    Operation.RESOLVE,
    Operation.SHOW_COMMIT,
    Operation.LIST_BRANCHES,
    Operation.LIST_TAGS,
    Operation.LOG,
    Operation.DIFF,
    Operation.CHANGED_FILES,
    Operation.MERGE_BASE,
    Operation.AHEAD_BEHIND,
    Operation.IS_ANCESTOR,
    Operation.BRANCH_CONTAINS,
    Operation.STATUS,
    Operation.LIST_WORKTREES,
    Operation.FIND_PULL_REQUEST,
    Operation.READ_REVIEW_THREADS,
})

# Operations that reach the remote. They may only run when the checkout has
# been confirmed to be the configured repository: work must not travel to a
# repository nobody has verified is this one, and a ref must not be deleted
# there. Local work carries no such risk and is always available.
REMOTE_OPERATIONS: frozenset[Operation] = frozenset({
    Operation.FETCH,
    Operation.PULL_FAST_FORWARD,
    Operation.PUSH,
    Operation.FORCE_PUSH,
    Operation.DELETE_REMOTE_BRANCH,
})

# Operations GitHub performs rather than git. They need a configured API
# boundary; without one they are refused rather than silently absent.
GITHUB_OPERATIONS: frozenset[Operation] = frozenset({
    Operation.FIND_PULL_REQUEST,
    Operation.OPEN_PULL_REQUEST,
    Operation.UPDATE_PULL_REQUEST,
    Operation.COMMENT_ON_PULL_REQUEST,
    Operation.READ_REVIEW_THREADS,
    Operation.RESOLVE_REVIEW_THREAD,
})


REPOSITORY_FAILURES = (
    "arguments_unusable",
    "repository_unavailable",
    "operation_refused",
    "ref_unknown",
    "branch_diverged",
    "conflict",
    # The one invariant. Distinct from every other refusal so it is legible in
    # evidence rather than looking like an ordinary failure.
    "self_preservation",
)


class RepositoryAuthorityError(Exception):
    """An operation did not happen, with a declared machine-readable code."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in REPOSITORY_FAILURES:
            raise ValueError(f"undeclared repository failure: {code}")
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def valid_sha(value: object) -> bool:
    """Whether this is a full 40-character commit identifier."""
    return isinstance(value, str) and _FULL_SHA.match(value) is not None


def valid_revision(value: object) -> bool:
    """Whether this names a commit this service will accept.

    A short sha or a ref name. Deliberately not git's full revision grammar:
    `HEAD@{2}`, `main^{tree}` and `:/text` are all things git would resolve and
    none of them is something a caller here needs, while each is a way to say
    something other than what it appears to say.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    candidate = value.strip()
    return bool(_SHORT_SHA.match(candidate) or _REF.match(candidate))


def valid_ref(value: object) -> bool:
    """Whether this names a branch or tag this service will carry."""
    return isinstance(value, str) and _REF.match(value.strip()) is not None


@dataclass(frozen=True, slots=True)
class RepositoryRequest:
    """One operation, with the arguments that operation needs."""

    operation: Operation
    # Free-form per operation, validated by the provider against the operation
    # rather than here: what counts as a usable argument is a property of the
    # operation, and a second copy of that knowledge would drift.
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, Operation):
            raise TypeError("operation must be an Operation")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be a mapping")

    @property
    def reads_only(self) -> bool:
        return self.operation in READ_ONLY


@dataclass(frozen=True, slots=True)
class RepositoryOutcome:
    """What happened, in enough detail to know what changed.

    This is observability, not approval. Every mutating operation records the
    repository it acted on, what it did, where it started, where it ended and
    which remote was involved, so AL/X can tell from the evidence alone whether
    the effect was the one she intended.
    """

    repository: str
    operation: Operation
    succeeded: bool
    # Where the operation started, where relevant: the ref named and the commit
    # it resolved to when the operation began.
    source_ref: str = ""
    source_sha: str = ""
    # Where it ended. For a mutation this is what the ref points at afterwards,
    # which is the fact that matters when checking a push or a reset did what
    # was meant.
    resulting_ref: str = ""
    resulting_sha: str = ""
    remote: str = ""
    # What the operation read, for the operations that read. Structured per
    # operation; never the raw output of a command.
    values: Mapping[str, Any] = field(default_factory=dict)
    # Set only when the operation did not happen.
    failure_code: str = ""
    refusal_reason: str = ""

    def as_values(self) -> dict[str, Any]:
        """The audit record, flat enough to log and read back."""
        return {
            "repository": self.repository,
            "operation": self.operation.value,
            "succeeded": self.succeeded,
            "source_ref": self.source_ref,
            "source_sha": self.source_sha,
            "resulting_ref": self.resulting_ref,
            "resulting_sha": self.resulting_sha,
            "remote": self.remote,
            "failure_code": self.failure_code,
            "refusal_reason": self.refusal_reason,
            **dict(self.values),
        }


# ---- the one invariant ----------------------------------------------------

# Operations that destroy history or files rather than adding to them. Only
# these can trip the invariant; everything else adds a commit, a ref or a
# fetched object, and adding cannot end AL/X's existence.
_DESTRUCTIVE: frozenset[Operation] = frozenset({
    Operation.DELETE_BRANCH,
    Operation.DELETE_REMOTE_BRANCH,
    Operation.FORCE_PUSH,
    Operation.RESET,
    Operation.REBASE,
    Operation.REMOVE_WORKTREE,
})


@dataclass(frozen=True, slots=True)
class CanonicalSystem:
    """What must continue to exist for AL/X to exist.

    Three facts, all configured rather than inferred: the checkout she runs
    from, the repository that checkout belongs to, and the branch carrying her
    history. The invariant below is expressed entirely in terms of these, which
    is what keeps it narrow — a repository that is not this one, or a branch
    that is not this branch, is ordinary work.
    """

    root: Path
    repository: str
    branch: str = CANONICAL_BRANCH

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("the canonical root must be an absolute path")
        if not self.repository.strip():
            raise ValueError("the canonical repository must be named")


def refuse_if_self_destructive(
    system: CanonicalSystem,
    operation: Operation,
    *,
    ref: str = "",
    path: Path | None = None,
) -> str:
    """Why this operation would end AL/X, or "" when it would not.

    The whole invariant, in one function, evaluated before any argv is built.

    It is narrow on purpose. Three things are protected, and each is protected
    because losing it cannot be undone from inside AL/X:

    - the canonical checkout, because it is where she runs;
    - canonical `main` on the canonical repository, because it is her history,
      and a force-push or reset that discards it leaves nothing to restore from;
    - the canonical remote branch, for the same reason one level out.

    Everything else is ordinary work and is allowed, including operations that
    lose work. A feature branch may be deleted, force-pushed or reset to
    anywhere; files may be removed; subsystems may be replaced; her own
    implementation may be rewritten. Losing a day's work is a mistake and is
    recoverable through the ordinary mechanisms git provides. Losing the
    canonical system is not a mistake anybody recovers from, and that is the
    distinction this draws.
    """
    if operation not in _DESTRUCTIVE:
        return ""

    # The checkout she runs from. Removing it as a worktree would take the
    # running system with it, and no history anywhere restores a deleted root.
    if operation is Operation.REMOVE_WORKTREE and path is not None:
        try:
            target = path.resolve()
        except OSError:
            return ""
        root = system.root.resolve()
        if target == root or root.is_relative_to(target):
            return (
                "the canonical checkout is where AL/X runs; removing it would "
                "end the system rather than change it"
            )

    named = ref.strip()
    if not named:
        return ""
    # `refs/heads/main`, `origin/main` and `main` are the same branch said
    # three ways, and the invariant must not be avoidable by spelling. The
    # spellings are named rather than derived: taking the last path segment
    # also caught `fix/main`, `feat/main` and every other branch whose name
    # happens to end that way, so ordinary work on any of them was refused as
    # though it were the canonical history. Fail-closed, so nothing was lost —
    # but the invariant is supposed to be narrow, and a rule that cannot tell
    # `fix/main` from `main` is not.
    if named not in {
        system.branch,
        f"refs/heads/{system.branch}",
        f"remotes/origin/{system.branch}",
        f"refs/remotes/origin/{system.branch}",
        f"origin/{system.branch}",
    }:
        return ""

    if operation in (Operation.DELETE_BRANCH, Operation.DELETE_REMOTE_BRANCH):
        return (
            f"{system.branch} carries AL/X's canonical history; deleting it "
            "would leave nothing to recover from"
        )
    if operation is Operation.FORCE_PUSH:
        return (
            f"force-pushing {system.branch} would replace AL/X's canonical "
            "history with no recoverable source"
        )
    if operation in (Operation.RESET, Operation.REBASE):
        return (
            f"rewriting {system.branch} would discard the canonical history "
            "AL/X is recovered from; it advances by merge"
        )
    return ""


__all__ = [
    "CANONICAL_BRANCH",
    "GITHUB_OPERATIONS",
    "REMOTE_OPERATIONS",
    "CanonicalSystem",
    "Operation",
    "READ_ONLY",
    "REPOSITORY_FAILURES",
    "RepositoryAuthorityError",
    "RepositoryOutcome",
    "RepositoryRequest",
    "refuse_if_self_destructive",
    "valid_ref",
    "valid_revision",
    "valid_sha",
]
