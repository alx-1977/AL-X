"""Read GitHub's check runs, commit statuses, and Actions job logs.

One pull request, one exact head. The pull request is read first. If its
head is no longer that revision, nothing else is read: checks for a commit
the pull request does not point at are not the answer to the question that
was asked.

Check runs and legacy commit statuses are then read in full. A page ceiling
that still has a full page left is a failure, not a shorter list. A prefix
returned in the shape of a complete result would be a false answer.

Failed Actions jobs are the only ones whose logs are fetched. The log
endpoint answers with a redirect to a blob host. That second request goes
out without the GitHub credential: following the redirect with the bearer
token would send it to a host that is not GitHub. The read keeps a suffix
of the log and counts the characters it left off.

Nothing here reruns, cancels, or writes. Every call is GET.
"""

from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urljoin, urlparse

import httpx

from alx.contracts.pull_request_checks import (
    ACTIONS_APP_SLUG,
    LOG_TAIL_CHARACTERS,
    LOGGED_CONCLUSIONS,
    CheckReadError,
    CheckRun,
    CommitStatus,
    PullRequestChecks,
    PullRequestChecksRequest,
)


API_ROOT = "https://api.github.com"

# One path segment: no slashes, no traversal, no query characters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

# /job/{id} and /jobs/{id}. A run URL without one of those is not a job id.
_JOB_ID = re.compile(r"/jobs?/([0-9]+)(?![0-9])")

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0

# GitHub's maximum page size. A short page is the end of the list. A full
# page at the ceiling is not: GitHub still has more, and returning what fit
# would report a partial list as the whole.
PER_PAGE = 100
MAX_PAGES = 10

USER_AGENT = "alx-checks"


def _throttled(response: object) -> bool:
    """Whether GitHub is rate limiting rather than refusing.

    The same header reading `GitHubMergeProvider` uses. A retry header, or a
    remaining counter of zero beside a limit. A refusal that merely mentions
    a limit is not one, and the body is not consulted.
    """
    headers = getattr(response, "headers", None) or {}
    try:
        if headers.get("retry-after"):
            return True
        return (
            headers.get("x-ratelimit-remaining") == "0"
            and headers.get("x-ratelimit-limit") is not None
        )
    except TypeError:
        return False


def _scalar(value: object) -> object:
    """A JSON scalar as received, or a failure if GitHub sent something else."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise CheckReadError("provider_failed")


def _job_id_in(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _JOB_ID.search(value)
    return match.group(1) if match is not None else None


def _location(headers: object) -> str | None:
    if headers is None:
        return None
    for key in ("location", "Location"):
        try:
            value = headers.get(key)  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            return None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _raise_for_status(response: object, *, not_found: str | None) -> None:
    status = getattr(response, "status_code", None)
    if status == 200:
        return
    if status == 404 and not_found is not None:
        raise CheckReadError(not_found)
    if status == 429 or (status == 403 and _throttled(response)):
        raise CheckReadError("rate_limited")
    if status in (401, 403):
        raise CheckReadError("permission_denied")
    raise CheckReadError("provider_failed")


def _decoded(response: object) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, (bytes, bytearray)):
        return bytes(content).decode("utf-8", errors="replace")
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    raise CheckReadError("log_unavailable")


def _tail(decoded: str) -> tuple[str, int]:
    if len(decoded) <= LOG_TAIL_CHARACTERS:
        return decoded, 0
    omitted = len(decoded) - LOG_TAIL_CHARACTERS
    return decoded[-LOG_TAIL_CHARACTERS:], omitted


def _names_check(job: dict, url: object, identifier: object) -> bool:
    target = job.get("check_run_url")
    if not isinstance(target, str) or not target.strip():
        return False
    normalised = target.rstrip("/")
    if isinstance(url, str) and normalised == url.rstrip("/"):
        return True
    if isinstance(identifier, int) and not isinstance(identifier, bool):
        return normalised.endswith(f"/check-runs/{identifier}")
    return False


class GitHubPullRequestChecks:
    """Read one pull request's checks at one exact head, or say why not."""

    def __init__(
        self, repository: str, token: str, api_root: str = API_ROOT
    ) -> None:
        parts = repository.strip().split("/")
        if len(parts) != 2 or not all(_SEGMENT.match(part) for part in parts):
            raise ValueError("repository must be owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._repository = f"{parts[0]}/{parts[1]}"
        self._token = token.strip()
        self._api_root = api_root.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }

    def _url(self, path: str) -> str:
        return f"{self._api_root}/repos/{self._repository}{path}"

    def _get(self, url: str, headers: dict[str, str]) -> object:
        # follow_redirects stays off. The log endpoint's redirect is fetched
        # separately, and that fetch must not carry the bearer token.
        try:
            return httpx.get(
                url,
                headers=headers,
                timeout=TIMEOUT_SECONDS,
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise CheckReadError("provider_failed") from None

    def _json(self, path: str, *, not_found: str | None = None) -> object:
        response = self._get(self._url(path), self._headers())
        _raise_for_status(response, not_found=not_found)
        try:
            return response.json()
        except ValueError:
            raise CheckReadError("provider_failed") from None

    def _pages(self, path: str, key: str) -> list[dict]:
        """Every object under `key`, or a failure if the list was cut short."""
        found: list[dict] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            body = self._json(f"{path}{separator}per_page={PER_PAGE}&page={page}")
            if not isinstance(body, dict):
                raise CheckReadError("provider_failed")
            batch = body.get(key)
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise CheckReadError("provider_failed")
            found.extend(batch)
            if len(batch) < PER_PAGE:
                return found
        raise CheckReadError("provider_failed")

    def _status_pages(self, path: str) -> list[dict]:
        found: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            body = self._json(f"{path}?per_page={PER_PAGE}&page={page}")
            if not isinstance(body, list) or any(
                not isinstance(item, dict) for item in body
            ):
                raise CheckReadError("provider_failed")
            found.extend(body)
            if len(body) < PER_PAGE:
                return found
        raise CheckReadError("provider_failed")

    def _record(self, run: dict) -> CheckRun:
        app = run.get("app")
        output = run.get("output")
        if app is None:
            app_slug: object = None
            app_name: object = None
        elif isinstance(app, dict):
            app_slug = _scalar(app.get("slug"))
            app_name = _scalar(app.get("name"))
        else:
            raise CheckReadError("provider_failed")
        if output is None:
            title: object = None
            summary: object = None
        elif isinstance(output, dict):
            title = _scalar(output.get("title"))
            summary = _scalar(output.get("summary"))
        else:
            raise CheckReadError("provider_failed")
        return CheckRun(
            name=_scalar(run.get("name")),
            status=_scalar(run.get("status")),
            conclusion=_scalar(run.get("conclusion")),
            started_at=_scalar(run.get("started_at")),
            completed_at=_scalar(run.get("completed_at")),
            details_url=_scalar(run.get("details_url")),
            app_slug=app_slug,
            app_name=app_name,
            output_title=title,
            output_summary=summary,
        )

    def _resolve_job_id(self, run: dict, head_sha: str) -> str:
        for key in ("details_url", "html_url"):
            found = _job_id_in(run.get(key))
            if found is not None:
                return found
        own = run.get("url")
        identifier = run.get("id")
        # GitHub's field name for the list. Kept as a string so it is not an
        # identifier in this module.
        listed = self._pages(
            f"/actions/runs?head_sha={head_sha}", "workflow_runs"
        )
        for item in listed:
            run_id = item.get("id")
            if not isinstance(run_id, int) or isinstance(run_id, bool):
                continue
            for job in self._pages(f"/actions/runs/{run_id}/jobs", "jobs"):
                if not _names_check(job, own, identifier):
                    continue
                job_id = job.get("id")
                if isinstance(job_id, int) and not isinstance(job_id, bool):
                    return str(job_id)
        raise CheckReadError("log_unavailable")

    def _steps(self, job: dict) -> tuple[tuple[object, object], ...]:
        raw = job.get("steps", [])
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise CheckReadError("log_unavailable")
        steps: list[tuple[object, object]] = []
        for step in raw:
            if not isinstance(step, dict):
                raise CheckReadError("log_unavailable")
            steps.append((
                _scalar(step.get("name")),
                _scalar(step.get("conclusion")),
            ))
        return tuple(steps)

    def _read_log(self, job_id: str) -> tuple[str, int]:
        url = self._url(f"/actions/jobs/{job_id}/logs")
        response = self._get(url, self._headers())
        status = getattr(response, "status_code", None)
        if status == 302:
            location = _location(getattr(response, "headers", None))
            if location is None:
                raise CheckReadError("log_unavailable")
            parsed = urlparse(location)
            if parsed.scheme != "https" or not parsed.netloc:
                raise CheckReadError("log_unavailable")
            target = urljoin(url, location)
            # No Authorization. The credential stays on api.github.com.
            response = self._get(target, {"User-Agent": USER_AGENT})
            status = getattr(response, "status_code", None)
        if status != 200:
            raise CheckReadError("log_unavailable")
        return _tail(_decoded(response))

    def _with_log(self, record: CheckRun, run: dict, head_sha: str) -> CheckRun:
        try:
            job_id = self._resolve_job_id(run, head_sha)
            body = self._json(f"/actions/jobs/{job_id}")
            if not isinstance(body, dict):
                raise CheckReadError("log_unavailable")
            steps = self._steps(body)
        except CheckReadError:
            return replace(
                record,
                log_tail="",
                characters_omitted=0,
                log_failure="log_unavailable",
            )
        try:
            tail, omitted = self._read_log(job_id)
        except CheckReadError:
            return replace(
                record,
                steps=steps,
                log_tail="",
                characters_omitted=0,
                log_failure="log_unavailable",
            )
        return replace(
            record, steps=steps, log_tail=tail, characters_omitted=omitted
        )

    def read(self, request: PullRequestChecksRequest) -> PullRequestChecks:
        pull = self._json(
            f"/pulls/{request.pull_request_number}", not_found="not_found"
        )
        if not isinstance(pull, dict):
            raise CheckReadError("provider_failed")
        head = pull.get("head")
        if not isinstance(head, dict):
            raise CheckReadError("provider_failed")
        actual = head.get("sha")
        if actual != request.head_sha:
            reported = actual if isinstance(actual, str) else None
            raise CheckReadError("head_changed", actual_head=reported)
        runs = self._pages(
            f"/commits/{request.head_sha}/check-runs", "check_runs"
        )
        statuses = self._status_pages(f"/commits/{request.head_sha}/statuses")
        # Shape is checked before any job or log request, so a malformed
        # list fails the read instead of fetching logs for a prefix of it.
        records = [self._record(run) for run in runs]
        status_records = tuple(
            CommitStatus(
                context=_scalar(item.get("context")),
                state=_scalar(item.get("state")),
                description=_scalar(item.get("description")),
                target_url=_scalar(item.get("target_url")),
            )
            for item in statuses
        )
        copied: list[CheckRun] = []
        for record, run in zip(records, runs):
            if (
                record.app_slug == ACTIONS_APP_SLUG
                and record.conclusion in LOGGED_CONCLUSIONS
            ):
                record = self._with_log(record, run, request.head_sha)
            copied.append(record)
        return PullRequestChecks(
            pull_request_number=request.pull_request_number,
            head_sha=request.head_sha,
            check_runs=tuple(copied),
            commit_statuses=status_records,
        )


__all__ = ["API_ROOT", "GitHubPullRequestChecks", "MAX_PAGES", "PER_PAGE", "USER_AGENT"]
