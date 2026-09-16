"""Provider-neutral failure contract for canonical repository lifecycle."""

REPOSITORY_RUNTIME_FAILURES = (
    "repository_root_unusable", "repository_identity_mismatch", "origin_missing",
    "origin_mismatch", "head_detached", "branch_not_main", "local_main_invalid",
    "worktree_dirty", "worktree_untracked", "git_unavailable", "git_timeout",
    "fetch_failed", "tracking_ref_missing", "tracking_ref_invalid", "local_ahead",
    "history_diverged", "fast_forward_refused",
)


class RepositoryRuntimeError(Exception):
    """A declared, safe canonical-repository lifecycle failure."""

    def __init__(self, code: str, phase: str = "") -> None:
        if code not in REPOSITORY_RUNTIME_FAILURES:
            raise ValueError("repository runtime failures must be declared")
        self.code = code
        self.phase = phase
        super().__init__(code)
