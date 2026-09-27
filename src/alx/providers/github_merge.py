"""The one call that merges a reviewed pull request on GitHub.

GitHub is execution plumbing here, not the decision-maker. AL/X has already
read the external review and judged the change mergeable; this performs that
decision against one exact revision.

`sha` is what makes the execution safe. GitHub merges only if the pull
request's head still equals it, and answers 409 otherwise. So an authorisation
made about one revision cannot merge a different one, however much later it is
executed. That is the whole stale-head protection, and it lives on GitHub's
side precisely so nothing here has to track branch state.

Branch protection still applies. Known pending checks are observed boundedly;
unknown refusals return once. A safe rebase requires a new review decision.
After the exact authorised head merges, the existing repository authority
synchronizes the canonical checkout. No review is requested by this provider.
"""

from __future__ import annotations

import re
import time
from dataclasses import replace

import httpx

from alx.contracts.repository import (
    MERGE_METHOD,
    MergeError,
    MergeOutcome,
    MergeRequest,
    valid_sha,
)


API_ROOT = "https://api.github.com"

# One path segment: no slashes, no traversal, no query characters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0


def _throttled(response: "httpx.Response") -> bool:
    """Whether GitHub is rate limiting rather than refusing.

    Read from the headers GitHub sets rather than from the body, so a refusal
    that merely mentions a limit is not mistaken for one.
    """
    headers = response.headers
    if headers.get("retry-after"):
        return True
    return (
        headers.get("x-ratelimit-remaining") == "0"
        and headers.get("x-ratelimit-limit") is not None
    )


def _refusal_message(response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("message", ""))[:300] if isinstance(body, dict) else ""


class GitHubMergeProvider:
    """Merges one pull request at one exact head, or reports why it could not."""

    def __init__(self, repository: str, token: str, api_root: str = API_ROOT,
                 *, synchronize=None, bring_current=None, sleep=time.sleep,
                 max_polls: int = 30, interval_seconds: float = 30) -> None:
        # Exactly one owner and one name, each non-empty and free of path
        # characters. A value like "owner/" or "owner/repo/extra" passed the
        # old check, registered the capability, and then built a wrong endpoint
        # that surfaced only as a generic failure at merge time.
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(_SEGMENT.match(part) for part in parts):
            raise ValueError("repository must be owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = repository
        self._token = token
        self._api_root = api_root.rstrip("/")
        if max_polls < 1 or interval_seconds <= 0:
            raise ValueError("merge waiting must be bounded")
        self._synchronize = synchronize
        self._bring_current = bring_current
        self._sleep = sleep
        self._max_polls = max_polls
        self._interval = interval_seconds

    def _get(self, path: str, *, missing=None, accept_404_message: str | None = None):
        try:
            response = httpx.get(
                f"{self._api_root}/repos/{self._repository}{path}",
                headers={"Authorization": f"Bearer {self._token}",
                         "Accept": "application/vnd.github+json"},
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            raise MergeError("merge_unavailable") from None
        if response.status_code == 404 and missing is not None:
            # An unprotected branch says so. Any other 404, including a token
            # that cannot read protection, is an unknown state, not "no checks".
            if accept_404_message is not None:
                message = ""
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict):
                    message = str(body.get("message") or "")
                if message.casefold() != accept_404_message.casefold():
                    raise MergeError(
                        "merge_unavailable", http_status=404, github_message=message
                    )
            return missing
        if response.status_code != 200:
            code = "merge_refused" if response.status_code in (401, 403) and not _throttled(response) else "merge_unavailable"
            raise MergeError(code, http_status=response.status_code,
                             github_message=_refusal_message(response))
        try:
            return response.json()
        except ValueError:
            raise MergeError("merge_unavailable") from None

    def _required_checks(self) -> list[dict]:
        """Legacy branch protection and active rulesets, as one check list.

        "Branch not protected" means there is no legacy protection. A ruleset
        can still require checks, so that response is not an empty set.
        """
        legacy = self._get(
            "/branches/main/protection/required_status_checks",
            missing={},
            accept_404_message="Branch not protected",
        )
        if not isinstance(legacy, dict):
            raise MergeError("merge_unavailable")
        checks = list(legacy.get("checks") or [
            {"context": name, "app_id": None}
            for name in legacy.get("contexts", [])
        ])
        rules = self._get("/rules/branches/main")
        if not isinstance(rules, list):
            raise MergeError("merge_unavailable")
        seen = {(item.get("context"), item.get("app_id")) for item in checks}
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
                continue
            parameters = rule.get("parameters") or {}
            for item in parameters.get("required_status_checks") or []:
                if not isinstance(item, dict) or not item.get("context"):
                    continue
                app_id = item.get("integration_id")
                key = (item["context"], app_id)
                if key in seen:
                    continue
                seen.add(key)
                checks.append({"context": item["context"], "app_id": app_id})
        return checks

    def _readiness(self, request: MergeRequest):
        pull = self._get(f"/pulls/{request.pull_request_number}")
        if not isinstance(pull, dict) or not isinstance(pull.get("head"), dict):
            raise MergeError("merge_unavailable")
        if pull["head"].get("sha") != request.head_sha:
            raise MergeError("head_changed", current_head=pull["head"].get("sha"))
        if pull.get("merged"):
            return "merged", pull
        if pull.get("state") != "open" or pull.get("draft"):
            raise MergeError("merge_refused", github_message="Pull request is closed or draft")
        if pull.get("mergeable") is False:
            raise MergeError("merge_conflict")
        base = pull.get("base", {})
        if base.get("ref") != "main" or not valid_sha(base.get("sha")):
            raise MergeError("merge_refused", github_message="Expected canonical main as the base")
        comparison = self._get(f"/compare/{base['sha']}...{request.head_sha}")
        if not isinstance(comparison, dict) or not isinstance(comparison.get("behind_by"), int):
            raise MergeError("merge_unavailable")
        if comparison["behind_by"] > 0:
            if pull["head"].get("repo", {}).get("full_name", "").lower() != self._repository.lower():
                raise MergeError("branch_behind", github_message="Source repository does not match canonical checkout")
            return "behind", pull
        checks = self._required_checks()
        if checks:
            runs = self._get(f"/commits/{request.head_sha}/check-runs?per_page=100")
            statuses = self._get(f"/commits/{request.head_sha}/status?per_page=100")
            if not isinstance(runs, dict) or not isinstance(statuses, dict):
                raise MergeError("merge_unavailable")
            if runs.get("total_count", 0) > 100 or statuses.get("total_count", 0) > 100:
                raise MergeError("merge_unavailable", github_message="Check result exceeds bounded snapshot")
            pending = []
            failed = []
            for check in checks:
                name, app = check["context"], check.get("app_id")
                matching = [r for r in runs.get("check_runs", [])
                            if r.get("name") == name and (app in (None, -1) or r.get("app", {}).get("id") == app)]
                matching.sort(key=lambda r: r.get("id", 0), reverse=True)
                legacy = [r for r in statuses.get("statuses", []) if r.get("context") == name]
                legacy.sort(key=lambda r: r.get("id", 0), reverse=True)
                if matching:
                    run = matching[0]
                    if run.get("status") != "completed":
                        pending.append(name)
                    elif run.get("conclusion") not in ("success", "neutral", "skipped"):
                        failed.append(name)
                elif legacy and app in (None, -1):
                    if legacy[0].get("state") in ("failure", "error"):
                        failed.append(name)
                    elif legacy[0].get("state") != "success":
                        pending.append(name)
                else:
                    pending.append(name)
            if failed:
                raise MergeError("checks_failed", checks=tuple(failed))
            if pending:
                return "pending", pull
        if pull.get("mergeable") is None or pull.get("mergeable_state") == "unknown":
            return "pending", pull
        return "ready", pull

    def merge(self, request: MergeRequest) -> MergeOutcome:
        for index in range(self._max_polls):
            state, pull = self._readiness(request)
            if state != "pending":
                break
            if index + 1 == self._max_polls:
                raise MergeError("checks_timed_out")
            self._sleep(self._interval)
        if state == "behind":
            if self._bring_current is None:
                raise MergeError("branch_behind")
            # Rebase changes the reviewed revision. Never merge or buy another
            # review under the old authorisation, even if its tree is identical.
            head = self._bring_current(request.head_sha, pull["head"].get("ref", ""))
            raise MergeError("review_required", previous_head=request.head_sha,
                             current_head=head, new_review_approval_required=True)
        if state == "merged":
            outcome = MergeOutcome(request.pull_request_number, request.head_sha,
                                   True, pull.get("merge_commit_sha", ""))
        else:
            outcome = self._merge(request)
        if self._synchronize is not None:
            try:
                sha = self._synchronize(outcome.merge_commit_sha, request.head_sha)
            except MergeError as error:
                raise MergeError("local_sync_failed", merged=True,
                                 merge_commit_sha=outcome.merge_commit_sha,
                                 blocker=error.code, **error.details) from None
            outcome = replace(outcome, local_main_sha=sha, checkout_branch="main")
        return outcome

    def _merge(self, request: MergeRequest) -> MergeOutcome:
        url = (
            f"{self._api_root}/repos/{self._repository}"
            f"/pulls/{request.pull_request_number}/merge"
        )
        payload: dict[str, object] = {
            "merge_method": MERGE_METHOD,
            # The exact revision AL/X authorised. GitHub compares this against
            # the live head and refuses if they differ.
            "sha": request.head_sha,
        }
        if request.title:
            payload["commit_title"] = request.title
        if request.message:
            payload["commit_message"] = request.message

        try:
            response = httpx.put(
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "alx-merge",
                },
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            # Severed from the original so the token cannot travel on it.
            raise MergeError("merge_unavailable") from None

        if response.status_code == 409:
            # A conflict and a changed revision are different facts. Re-read
            # once; do not infer either from the HTTP status alone.
            current = self._get(f"/pulls/{request.pull_request_number}")
            if isinstance(current, dict) and current.get("head", {}).get("sha") != request.head_sha:
                code = "head_changed"
            elif isinstance(current, dict) and current.get("mergeable") is False:
                code = "merge_conflict"
            else:
                code = "merge_refused"
            raise MergeError(code, http_status=409,
                             github_message=_refusal_message(response)) from None
        if response.status_code in (403, 429) and _throttled(response):
            # GitHub uses 403 for throttling as well as for refusal. Reporting
            # a rate limit as a refusal would tell AL/X the merge was rejected
            # when it was only delayed, and the right response to those two is
            # not the same.
            raise MergeError("merge_unavailable") from None
        if response.status_code in (403, 405, 422):
            # Branch protection, an unmergeable state, or a rejected request.
            raise MergeError("merge_refused", http_status=response.status_code,
                             github_message=_refusal_message(response)) from None
        if response.status_code != 200:
            raise MergeError("merge_unavailable") from None

        try:
            body = response.json()
        except ValueError:
            raise MergeError("merge_unavailable") from None
        if not isinstance(body, dict) or body.get("merged") is not True:
            raise MergeError("merge_refused", http_status=response.status_code,
                             github_message=_refusal_message(response)) from None
        commit = body.get("sha")
        if not isinstance(commit, str):
            raise MergeError("merge_unavailable") from None

        return MergeOutcome(
            pull_request_number=request.pull_request_number,
            head_sha=request.head_sha,
            merged=True,
            merge_commit_sha=commit,
        )
