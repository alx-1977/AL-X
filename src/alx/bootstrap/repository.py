"""Compose merge authority, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot propose a
merge at all. That is the difference between the authority being withheld and
merging merely failing: an unregistered capability is honestly absent.

Friedl grants this authority by configuring it and revokes it by removing the
configuration or the permission. There is no per-merge approval: the delegation
is the decision, recorded in governance, and each merge is AL/X exercising it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import CapabilityResult, CodeAction, StructuredData
from alx.contracts.repository import MergeRequest, MergeError
from alx.contracts.review_content import ReviewContentRequest, ReviewReadError
from alx.contracts.review_provider import ReviewProvider, profile_for
from alx.contracts.repository_authority import Operation, RepositoryRequest, RepositoryAuthorityError
from alx.contracts.coding import CodingError
from alx.providers.coding_git import coding_job_lock
from alx.providers.github_merge import GitHubMergeProvider
from alx.tools.repository import build_repository_executors


LOGGER = logging.getLogger(__name__)

# Merging is its own authority. Holding it follows from no other permission,
# and no other permission follows from it.
REPOSITORY_MERGE_PERMISSION = "repository.merge"


@dataclass(frozen=True, slots=True)
class RepositoryRuntime:
    """The merge action of `code`, or nothing at all (D-074)."""

    provider: Any
    actions: Mapping[CodeAction, Callable[[StructuredData], CapabilityResult]]
    action_permissions: Mapping[CodeAction, frozenset[str]]


def require_clean_review(review_reader: Any, request: MergeRequest, equivalent=None) -> None:
    """Require a clean review of this head or its mechanically identical patch.

    D-042, Friedl 2026-10-07: AL/X may never merge without a clean external
    review unless he says so. Amended 2026-10-14: "She can merge but only
    after code passed tests and Greptile give 5/5." Clean is therefore the
    reviewer's own verdict on this exact head, read from its fixed format
    (Greptile's confidence score of 5), never her reading of the findings.
    The tests are GitHub's required `law-gates` check, which the merge
    provider waits on. Resolving a review thread does not make a review
    clean. Friedl's 2026-11-02 amendment permits reuse on this same PR when
    incorporating main leaves the own changes mechanically identical.
    """
    if review_reader is None:
        raise MergeError("review_missing", github_message="No external reviewer is configured")
    try:
        content = review_reader.read(ReviewContentRequest(request.pull_request_number,
                                                          request.head_sha))
    except ReviewReadError:
        raise MergeError("review_missing", github_message="The review could not be read") from None
    if (not content.available and equivalent is not None
            and hasattr(review_reader, "reviewed_heads")):
        try:
            for head in review_reader.reviewed_heads(request.pull_request_number):
                if head == request.head_sha:
                    continue
                candidate = review_reader.read(ReviewContentRequest(request.pull_request_number, head))
                if (candidate.available and candidate.head_sha == head
                        and candidate.pull_request_number == request.pull_request_number
                        and candidate.reviewer == ReviewProvider.GREPTILE.value
                        and profile_for(ReviewProvider.GREPTILE).states_no_findings(candidate.summary)
                        and equivalent(head, request.head_sha)):
                    return
        except (ReviewReadError, RepositoryAuthorityError):
            pass  # Missing evidence is never permission to merge.
    if (content.head_sha != request.head_sha
            or content.pull_request_number != request.pull_request_number):
        raise MergeError("review_missing", github_message="Review binding differs")
    if not content.available:
        raise MergeError("review_missing", github_message=content.unavailable_reason
                         or "No finished review of this head")
    try:
        profile = profile_for(ReviewProvider(content.reviewer))
    except ValueError:
        profile = None
    if profile is None or not profile.states_no_findings(content.summary):
        stated = re.search(r"greptile_confidence_score:(\d)", content.summary or "")
        details = {"confidence": int(stated.group(1))} if stated else {}
        raise MergeError("review_has_findings",
                         github_message="The review is not the reviewer's clean verdict",
                         findings=len(content.comments), **details)


def build_repository_runtime(
    enabled: bool,
    repository: str,
    token: str,
    call_id_source: Callable[[], str],
    provider: Any = None,
    repository_runtime: Any = None,
    # D-042: reads what the configured reviewer published about one exact
    # head. Without one, nothing can show a clean review, so nothing merges.
    review_reader: Any = None,
) -> RepositoryRuntime | None:
    """Compose merge authority, or leave it unregistered."""
    if repository_runtime is None and provider is None:
        LOGGER.info("No canonical repository authority: merge capability unavailable")
        return None
    if repository_runtime is not None and repository_runtime.repository_identity.lower() != repository.lower():
        LOGGER.warning("Merge repository differs from canonical checkout: unavailable")
        return None
    if not enabled:
        LOGGER.info("Merge authority is not enabled: no merge capability")
        return None
    if not repository.strip() or not token.strip():
        LOGGER.info("Merge authority is not configured: no merge capability")
        return None

    def perform(operation, **arguments):
        try:
            outcome = repository_runtime.authority.perform(RepositoryRequest(operation, arguments))
        except RepositoryAuthorityError as error:
            raise MergeError("merge_refused", operation=operation.value,
                             github_message=error.detail) from None
        if not outcome.succeeded:
            raise MergeError("merge_refused", operation=operation.value,
                             github_message=outcome.refusal_reason)
        return outcome

    def synchronize(merge_sha, reviewed_sha):
        try:
            with coding_job_lock(repository_runtime.root):
                status = repository_runtime.authority.read_checkout_status()
                if not status.clean or status.detached or (
                    status.branch != "main" and status.head_sha != reviewed_sha
                ):
                    raise MergeError("local_sync_failed", github_message="Checkout changed or is dirty")
                perform(Operation.SWITCH_BRANCH, branch="main")
                outcome = perform(Operation.PULL_FAST_FORWARD, branch="main")
                contains = perform(Operation.IS_ANCESTOR, ancestor=merge_sha, descendant="HEAD")
                if not contains.values.get("is_ancestor"):
                    raise MergeError("local_sync_failed", github_message="Main does not contain merged commit")
                after = repository_runtime.authority.read_checkout_status()
                if not after.clean or after.branch != "main" or after.head_sha != outcome.resulting_sha:
                    raise MergeError("local_sync_failed", github_message="Checkout changed during synchronization")
                return outcome.resulting_sha
        except (CodingError, RepositoryAuthorityError):
            raise MergeError("local_sync_failed", github_message="Canonical checkout unavailable") from None

    def bring_current(reviewed_sha, branch):
        try:
            with coding_job_lock(repository_runtime.root):
                status = repository_runtime.authority.read_checkout_status()
                if (not status.clean or status.detached or status.branch != branch
                        or branch == "main" or status.head_sha != reviewed_sha):
                    raise MergeError("branch_behind", github_message="Checkout does not match reviewed branch")
                perform(Operation.FETCH)
                remote = perform(Operation.RESOLVE, revision=f"origin/{branch}")
                if remote.values.get("sha") != reviewed_sha:
                    raise MergeError("head_changed", current_head=remote.values.get("sha"))
                base = perform(Operation.RESOLVE, revision="origin/main")
                rebase = repository_runtime.authority.perform(
                    RepositoryRequest(Operation.REBASE, {"onto": base.values["sha"]})
                )
                if not rebase.succeeded:
                    if rebase.failure_code == "conflict":
                        try:
                            repository_runtime.authority.abort_rebase()
                        except RepositoryAuthorityError as error:
                            raise MergeError(
                                "merge_conflict", github_message=error.detail
                            ) from None
                        raise MergeError(
                            "merge_conflict", github_message=rebase.refusal_reason
                        )
                    raise MergeError(
                        "merge_refused",
                        operation=Operation.REBASE.value,
                        github_message=rebase.refusal_reason,
                    )
                current = repository_runtime.authority.read_checkout_status()
                if not current.clean or current.branch != branch:
                    raise MergeError("merge_conflict")
                perform(Operation.FORCE_PUSH, branch=branch, expected_head=reviewed_sha)
                return current.head_sha
        except (CodingError, RepositoryAuthorityError):
            raise MergeError("branch_behind", github_message="Canonical checkout unavailable") from None

    try:
        def check_review(request):
            def equivalent(reviewed, current):
                with coding_job_lock(repository_runtime.root):
                    perform(Operation.FETCH)
                    return repository_runtime.authority.own_changes_equal(reviewed, current)

            require_clean_review(review_reader, request,
                                 equivalent if repository_runtime is not None else None)

        selected = provider or GitHubMergeProvider(
            repository, token,
            synchronize=synchronize if repository_runtime is not None else None,
            bring_current=bring_current if repository_runtime is not None else None,
            check_review=check_review,
        )
    except ValueError:
        LOGGER.warning("Merge authority is misconfigured: no merge capability")
        return None

    def merge(request: MergeRequest) -> Any:
        check_review(request)
        return selected.merge(request)

    LOGGER.info("Merge authority enabled: %s", CodeAction.MERGE.value)
    return RepositoryRuntime(
        provider=selected,
        actions=build_repository_executors(merge, call_id_source),
        action_permissions={
            # No per-merge approval. Friedl delegated routine merge
            # authorisation in governance rather than participating in each
            # decision, so the permission is the authority and the judgement
            # about a specific reviewed head is AL/X's. Revoking the permission
            # revokes the delegation.
            CodeAction.MERGE: frozenset(
                {REPOSITORY_MERGE_PERMISSION}
                | ({"repository.operate"} if repository_runtime else set())),
        },
    )
