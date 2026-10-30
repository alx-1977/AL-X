"""D-062 Git transport for isolated firmware jobs, outside AL/X's checkout."""
from __future__ import annotations

from pathlib import Path
import subprocess

from alx.contracts.coding import CodingError, branch_name_permitted
from alx.providers.coding_git import (
    GIT_TIMEOUT_SECONDS, _clean_environment, canonical_repository_root,
    read_workspace_state,
)


def prepare_firmware_clone(root: Path, canonical: Path, repository: str,
                           remotes: dict[str, str], job_id: str, resume_id: str | None = None) -> Path:
    """D-062: a job-owned clone; all remote addresses come from configuration."""
    import json
    from alx.contracts.coding import job_id_permitted
    root = root.resolve()
    if root == canonical or root.is_relative_to(canonical):
        raise CodingError('git_refused', reason_code='firmware_storage_inside_checkout')
    if repository not in remotes or not job_id_permitted(job_id) or (
        resume_id is not None and not job_id_permitted(resume_id)
    ):
        raise CodingError('arguments_unusable', reason_code='firmware_repository_denied')
    job = root / 'jobs' / (resume_id or job_id)
    source = job / 'source'
    if job.resolve() != job or source.resolve() != source:
        raise CodingError('git_refused', reason_code='firmware_clone_redirected')
    if resume_id:
        try:
            if json.loads((job / 'repository.json').read_text()) != repository:
                raise ValueError('repository mismatch')
        except (OSError, ValueError) as error:
            raise CodingError('git_refused', reason_code='firmware_clone_missing') from error
        canonical_repository_root(source)
        return source
    try:
        job.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        raise CodingError('git_refused', reason_code='firmware_job_exists') from error
    def git(argv, cwd):
        try:
            result = subprocess.run(['git', *argv], cwd=cwd, stdin=subprocess.DEVNULL,
                                    env=_clean_environment(), capture_output=True,
                                    timeout=GIT_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CodingError('git_unavailable', reason_code='firmware_sync_failed') from error
        if result.returncode:
            raise CodingError('git_refused', reason_code='firmware_sync_failed')
    git(['clone', '--single-branch', '--branch', 'main', '--', remotes[repository], str(source)], job)
    git(['fetch', '--no-tags', 'origin', '+refs/heads/main:refs/remotes/origin/main'], source)
    git(['reset', '--hard', 'origin/main'], source)
    (job / 'repository.json').write_text(json.dumps(repository))
    return source


def push_firmware_branch(root: Path, branch: str, expected_sha: str, remote: str) -> None:
    """Create a feature ref atomically; never replace an existing remote ref."""
    if not branch_name_permitted(branch) or branch == 'main':
        raise CodingError('git_refused', reason_code='firmware_main_protected')
    state = read_workspace_state(root)
    if state.branch != branch or state.head_sha != expected_sha or not state.clean:
        raise CodingError('git_refused', reason_code='firmware_push_state_changed')
    try:
        ref = 'refs/heads/' + branch
        existing = subprocess.run(['git', 'ls-remote', '--heads', '--', remote, ref], cwd=root,
                                  stdin=subprocess.DEVNULL, env=_clean_environment(), capture_output=True,
                                  timeout=GIT_TIMEOUT_SECONDS, check=False)
        if existing.returncode:
            raise CodingError('git_unavailable', reason_code='firmware_remote_unreadable')
        records = existing.stdout.decode('ascii', errors='replace').splitlines()
        if records:
            if records == [expected_sha + '\t' + ref]:
                return  # A previous push succeeded but its reply was lost.
            raise CodingError('git_refused', reason_code='firmware_remote_branch_exists', branch=branch)
        # An empty expected ref is a create-only lease, not permission to
        # overwrite history. A branch created concurrently makes this fail.
        result = subprocess.run(['git', 'push', '--force-with-lease=' + ref + ':', '--', remote,
                                 expected_sha + ':refs/heads/' + branch], cwd=root,
                                stdin=subprocess.DEVNULL, capture_output=True,
                                env=_clean_environment(), timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CodingError('git_unavailable', reason_code='firmware_push_failed') from error
    if result.returncode:
        raise CodingError('git_refused', reason_code='firmware_push_failed', branch=branch,
                          commit_sha=expected_sha)


