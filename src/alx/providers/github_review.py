"""Ask the configured reviewer for a look, and read what it published.

One provider serves every reviewer AL/X can use, because every one of them
works the same way: it watches this repository through GitHub, reviews a pull
request when one is opened, reviews again when asked in a comment, and
publishes what it found as pull request comments and reviews. The differences
between them are a name, a trigger phrase and a bot login, and those live in
`contracts/review_provider.py` rather than here.

## Requesting

A new pull request is reviewed without being asked. The trigger exists for the
other case: a corrective commit moved the head, the previous review is evidence
about a revision that no longer exists, and a fresh look is needed. So a request
is a comment, and the outcome records the revision the pull request pointed at
when it was left. Where the reviewer's status shows it already reviewing or
done reviewing that revision, no comment is left and the wait joins that round.

A review round ends when the reviewer's commit status on the revision says so.
Until then nothing it has published about the revision is reported as a review.

## Reading

`ReviewContent` reports a review of one exact revision or reports that there is
none. The exactness is the point: a review is evidence about the commit it
examined, and a clean review of an earlier head must never read as approval of
the code that replaced it. Where a reviewer states the range it covered, that
statement is what binds the review to a head; where it does not, the review's
own commit is used, and where neither is available the content is reported
unavailable rather than guessed at.

Inline findings are bound through the review object they were submitted under —
head, then review, then that review's comments. The comment's own `commit_id`
looks like the obvious key and is not one: GitHub re-anchors it onto a newer
head whenever the marked line survives there, so findings from an earlier round
arrive wearing the current revision's SHA. The review object is submitted once
against one commit and never moves, so it is what the binding rests on.

The question this answers is "what did the reviewer say about this revision",
not "what is still worth fixing". A finding from an earlier round that nobody
has addressed is real, but it is not evidence about this head, and collapsing
the two would let an old round's findings read as a fresh review's.

Nothing here judges. There is no severity, no finding count, no clean flag and
no merge opinion: the reviewer's words are returned as the reviewer wrote them,
marked as external content, and what they mean is AL/X's to decide.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any, NamedTuple

import httpx

from alx.contracts.review import ReviewError, ReviewOutcome, ReviewRequest
from alx.contracts.review_content import (
    REVIEW_FAILED,
    REVIEW_IN_PROGRESS,
    ReviewComment,
    ReviewContent,
    ReviewContentRequest,
    ReviewReadError,
)
from alx.contracts.review_provider import ReviewProviderProfile
from alx.providers.github_http import unavailable

LOGGER = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"

# One path segment: no slashes, traversal or query characters.
_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

_SHA = re.compile(r"\b[0-9a-f]{40}\b")

# The whole request, not one socket operation.
TIMEOUT_SECONDS = 30.0

# How many pages of comments are read before giving up. A pull request with
# more than this is not one a reviewer's latest word is being missed on; it is
# one where something else has gone wrong.
MAX_PAGES = 10

# Why a read found nothing. A fact about the search — this reviewer has not
# published about this revision — never an opinion about the code.
NO_REVIEW_FOR_REVISION = "no_review_for_revision"

# Commit-status states that end a review round. `pending` is the round still
# running; anything GitHub adds later is not known to be an end, so it is read
# as still running rather than as finished.
_COMPLETED_STATE = "success"
_FAILED_STATES = frozenset({"failure", "error"})

# How long a request waits for the reviewer's own automatic round to show on
# the current head before asking for one. CodeRabbit marked a freshly opened
# pull request `pending` 7-9 seconds after it opened on seven of PR #70-#77;
# PR #77's trigger went out 10 seconds after it did. Sixty seconds covers that
# several times over and is a small part of the wait that follows. Rechecked
# mechanically inside this one call: nothing is scheduled and no Core turn is
# spent.
AUTOMATIC_REVIEW_GRACE_SECONDS = 60.0
AUTOMATIC_REVIEW_RECHECK_SECONDS = 10.0
# How many times one request follows the head to a newer commit before it
# stops trying to join a round. Each follow needs a push inside one request,
# so only a branch being pushed continuously reaches it.
MAX_HEAD_CHANGES = 3


def _moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class GitHubReviewProvider:
    """Request and read reviews by one configured reviewer, through GitHub."""

    def __init__(
        self,
        repository: str,
        token: str,
        profile: ReviewProviderProfile,
        api_root: str = API_ROOT,
        clock: Any = None,
    ) -> None:
        owner, _, name = repository.strip().partition("/")
        if not _SEGMENT.match(owner) or not _SEGMENT.match(name):
            raise ValueError("repository must be exactly owner/name")
        if not token.strip():
            raise ValueError("a GitHub token is required")
        self._owner = owner
        self._name = name
        self._repository = f"{owner}/{name}"
        self._token = token.strip()
        self._profile = profile
        self._api_root = api_root.rstrip("/")
        self._now = clock or (lambda: datetime.now(UTC))

    @property
    def reviewer(self) -> str:
        return self._profile.reviewer

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _call(self, method: str, path: str, payload: object = None) -> object:
        try:
            response = httpx.request(
                method,
                f"{self._api_root}{path}",
                headers=self._headers(),
                json=payload,
                timeout=TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            # The type, never the message: the wording can carry a URL with a
            # token in it.
            LOGGER.warning("GitHub request failed: %s", type(error).__name__)
            raise _Unavailable() from error
        if unavailable(response):
            raise _Unavailable()
        if response.status_code >= 400:
            raise _Refused()
        try:
            return response.json()
        except ValueError as error:
            raise _Unavailable() from error

    def _pages(self, path: str) -> list[Any]:
        items: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            joiner = "&" if "?" in path else "?"
            found = self._call("GET", f"{path}{joiner}per_page=100&page={page}")
            if not isinstance(found, list) or not found:
                return items
            items.extend(found)
            if len(found) < 100:
                return items
        # The cap is exhausted and the last page was full, so GitHub has more
        # to give. What has been read is a prefix of the review, not the review,
        # and returning it would put a partial answer in front of AL/X wearing
        # the shape of a complete one — a review she weighs before a merge,
        # missing however many findings did not fit. Unreadable is the honest
        # answer: the task keeps waiting rather than acting on part of it.
        raise _Unavailable()

    def _head(self, number: int) -> str:
        pull = self._call("GET", f"/repos/{self._repository}/pulls/{number}")
        if not isinstance(pull, dict):
            raise _Unavailable()
        head = pull.get("head")
        sha = head.get("sha") if isinstance(head, dict) else None
        return sha if isinstance(sha, str) and _SHA.fullmatch(sha) else ""

    def _round(self, head_sha: str) -> "_Round | None":
        """Where this reviewer's latest review round of this revision stands.

        Read from the commit status the reviewer publishes on the exact commit
        it is reviewing, by its own account. That binds the round to the head
        by GitHub's data rather than by prose, and says whether the round has
        ended, which nothing else it publishes does: its summary comment is
        posted as a placeholder when the round starts. Taking that placeholder
        for a review completed the wait on PR #77 three minutes before the
        finding it was waiting for existed.

        Returns the latest state and when it was published, or None when
        this reviewer has published no status on this revision. The time is
        what tells a round that ended before a request from one that answers
        it.
        """
        statuses = [
            item
            for item in self._pages(
                f"/repos/{self._repository}/commits/{head_sha}/statuses"
            )
            if isinstance(item, dict)
            and item.get("context") == self._profile.status_context
            and isinstance(item.get("creator"), dict)
            and self._profile.authored_by_reviewer(item["creator"].get("login"))
            and isinstance(item.get("state"), str)
        ]
        if not statuses:
            return None
        # GitHub lists a commit's statuses newest first, and `max` keeps the
        # first of equals, so a same-second transition resolves to the newer.
        latest = max(
            statuses,
            key=lambda item: _moment(item.get("created_at"))
            or datetime.min.replace(tzinfo=UTC),
        )
        return _Round(latest["state"], _moment(latest.get("created_at")))

    # ---- requesting -----------------------------------------------------

    def request(self, review: ReviewRequest) -> ReviewOutcome:
        """Ask the configured reviewer to look at this pull request again.

        Unless it is already looking. A reviewer that reviews a pull request
        unasked has usually started on the current head before anyone asks,
        and a trigger then is a second request for work already underway: on
        PR #77 it was posted ten seconds after the automatic round began, and
        answered "Already reviewed the last commit." Where the reviewer's own
        status shows a round running or completed on this exact head, nothing
        is posted and the outcome says so; the caller's wait attaches to that
        round. Where there is no status yet, it is looked for again for a
        short bounded grace, because a head only just opened or pushed is
        usually marked seconds after AL/X asks. A failed round, or none by the
        end of the grace, still gets the trigger.
        """
        number = review.pull_request_number
        # Stamped before the trigger goes out, never after. The observer
        # refuses a review published at or before this moment, because such a
        # review answers the previous request rather than this one. Taking the
        # time afterwards put the POST and a second head lookup inside the
        # window: a reviewer that answers quickly — CodeRabbit does, within
        # seconds — would have its review dismissed as older than the request
        # that prompted it, and the task would wait forever for a review that
        # had already arrived.
        requested_at = self._now()
        try:
            head = self._head(number)
            if self._profile.status_context:
                awaited = self._await_automatic_round(number, head)
                head = awaited.sha
                if awaited.state:
                    # The round is bound to this head by the reviewer's own
                    # status, so the head is established without a trigger
                    # having gone out for it to move under.
                    return ReviewOutcome(
                        pull_request_number=number,
                        head_sha=head,
                        requested=False,
                        reviewer=self._profile.reviewer,
                        requested_at=requested_at,
                        attached_round=awaited.state,
                    )
            self._call(
                "POST",
                f"/repos/{self._repository}/issues/{number}/comments",
                {"body": self._profile.trigger},
            )
            # Read again after the comment lands. If the head moved while the
            # request was going out, the revision the reviewer will examine is
            # not established, and saying so is better than naming the commit
            # that happened to be current a moment earlier.
            confirmed = self._head(number)
        except _Refused as error:
            raise ReviewError("review_refused") from error
        except _Unavailable as error:
            raise ReviewError("review_unavailable") from error
        return ReviewOutcome(
            pull_request_number=number,
            head_sha=head if head and head == confirmed else "",
            requested=True,
            reviewer=self._profile.reviewer,
            requested_at=requested_at,
        )

    def _await_automatic_round(self, number: int, head: str) -> "_Head":
        """The current head, and whether its automatic round has begun.

        A pull request just opened, or a head just pushed, has usually not yet
        been marked by the reviewer when AL/X asks: the status follows seconds
        later. So an absent status is looked at again, on a fixed cadence, for
        a bounded grace period before a trigger is judged necessary. The head
        is re-read each time, so a push during the grace is followed to the
        revision the reviewer will actually take up. A failed round ends the
        grace at once: nothing automatic is coming, and asking again is right.
        """
        deadline = time.monotonic() + AUTOMATIC_REVIEW_GRACE_SECONDS
        changes = 0
        while head:
            found = self._round(head)
            if found is not None:
                # Confirmed before it decides anything, as a trigger is
                # confirmed after it is posted. A push between reading the head
                # and reading its status would otherwise let the old head's
                # round decide for the commit now under review: joining a
                # round that says nothing about it, or retrying a failure that
                # was never its own while its own round is already running.
                # If the head moved, the old head's state decides nothing and
                # the question is asked again of the new head, at once.
                current = self._head(number)
                if current == head:
                    if found.state in _FAILED_STATES:
                        # This head's own round failed: ask again.
                        return _Head(head, "")
                    return _Head(head, found.state)
                changes += 1
                if changes > MAX_HEAD_CHANGES:
                    # Still moving. No round can be tied to a head, so none is
                    # joined; the trigger path confirms the head it reports.
                    return _Head(current, "")
                head = current
                continue
            if time.monotonic() >= deadline:
                break
            time.sleep(AUTOMATIC_REVIEW_RECHECK_SECONDS)
            head = self._head(number)
        return _Head(head, "")

    # ---- reading --------------------------------------------------------

    def pull_request_closed(self, number: int) -> bool:
        """Whether GitHub has closed (including merged) this review's PR."""
        try:
            pull = self._call("GET", f"/repos/{self._repository}/pulls/{number}")
        except (_Refused, _Unavailable) as error:
            raise ReviewReadError("review_unavailable") from error
        if not isinstance(pull, dict) or pull.get("state") not in ("open", "closed"):
            raise ReviewReadError("review_unavailable")
        return pull["state"] == "closed"

    @staticmethod
    def _when(item: dict) -> datetime:
        return (
            _moment(item.get("submitted_at"))
            # Issue summaries are edited in place for later review rounds.
            # Their creation time describes the first round, not the exact-
            # head content being returned now. Submitted reviews retain their
            # original submission time even if edited afterwards.
            or _moment(item.get("updated_at"))
            or _moment(item.get("created_at"))
            or datetime.min.replace(tzinfo=UTC)
        )

    def _authored(self, item: object) -> bool:
        return (
            isinstance(item, dict)
            and isinstance(item.get("user"), dict)
            and self._profile.authored_by_reviewer(item["user"].get("login"))
        )

    @staticmethod
    def _comment(item: dict) -> ReviewComment | None:
        body = item.get("body")
        if not isinstance(body, str) or not body.strip():
            return None
        line = item.get("line")
        return ReviewComment(
            body=body,
            path=item.get("path") if isinstance(item.get("path"), str) else "",
            line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        )

    def _review_for(self, reviews: list[dict], head_sha: str) -> dict | None:
        """The reviewer's own submitted review of this exact revision.

        A review object is the unit the reviewer actually publishes: it is
        submitted once, against one commit, and never re-pointed afterwards.
        That is what makes it the thing to bind to.

        The comment fields cannot carry this. `commit_id` is where a comment
        applies *now*, and GitHub rewrites it onto a newer head whenever the
        line it marks still exists there — so a finding written about an
        earlier revision reappears wearing the current one's SHA. Observed
        directly on this repository: three comments submitted in a review of
        one head were re-anchored onto the next, while the review object they
        belong to kept the head it was actually submitted against.

        `original_commit_id` fails the other way: it stays on the authoring
        revision, so selecting by it alone drops the reviewer's findings about
        the current head whenever it re-states an earlier one. Neither field,
        and no combination of them, tells you which review round a comment came
        from. The review id does.
        """
        submitted = [
            item
            for item in reviews
            if self._authored(item)
            and item.get("commit_id") == head_sha
            and isinstance(item.get("id"), int)
            # A review still being drafted is not something the reviewer has
            # said; only a submitted one is evidence.
            and item.get("state") != "PENDING"
        ]
        if not submitted:
            return None
        return max(submitted, key=self._when)

    @staticmethod
    def _belongs_to(item: dict, review_id: int) -> bool:
        """Whether this comment was published as part of that review."""
        return item.get("pull_request_review_id") == review_id

    def _covers(self, body: str, head_sha: str) -> bool:
        """Whether this text is the reviewer saying it looked at this revision.

        Reviewers state the range they covered — CodeRabbit writes "between
        <base> and <head>". Where such a statement is present it is what binds
        the review to a commit, because it is the reviewer's own account of
        what it read rather than an inference from when something was posted.
        """
        return head_sha in body

    def read(self, request: ReviewContentRequest) -> ReviewContent:
        """Return what the reviewer published about this exact revision."""
        number = request.pull_request_number
        head_sha = request.head_sha
        try:
            # The round's state first, then its content. The reviewer
            # publishes its findings before it marks the round ended, so
            # content read after an ended round includes them; read the other
            # way round, a round ending between the two reads would pair a
            # completed status with content fetched before the findings
            # existed.
            found = (
                self._round(head_sha) if self._profile.status_context
                else _Round(_COMPLETED_STATE, None)
            )
        except (_Refused, _Unavailable) as error:
            raise ReviewReadError("review_unavailable") from error

        # Nothing is a review of this revision until the reviewer says its
        # round on this revision has ended. Before that, whatever it has
        # published here is a placeholder or a partial account, and reporting
        # it as available settles a wait on evidence the real review will
        # contradict. Returned before any content is fetched: a known state
        # must not become an unknown one because a read it never needed
        # failed.
        if found is None or found.state != _COMPLETED_STATE:
            return ReviewContent(
                pull_request_number=number,
                head_sha=head_sha,
                reviewer=self._profile.reviewer,
                available=False,
                retrieved_at=self._now(),
                unavailable_reason=(
                    NO_REVIEW_FOR_REVISION
                    if found is None
                    else REVIEW_FAILED
                    if found.state in _FAILED_STATES
                    else REVIEW_IN_PROGRESS
                ),
                round_at=None if found is None else found.at,
            )

        try:
            issue_comments = self._pages(
                f"/repos/{self._repository}/issues/{number}/comments"
            )
            reviews = self._pages(f"/repos/{self._repository}/pulls/{number}/reviews")
            inline = self._pages(f"/repos/{self._repository}/pulls/{number}/comments")
        except (_Refused, _Unavailable) as error:
            # Both mean the same thing to a reader: nothing is known about the
            # review right now, and it may be readable later. The contract
            # declares one code for that, and inventing a second here would be
            # a distinction nothing downstream can act on.
            raise ReviewReadError("review_unavailable") from error

        # The review object submitted against this exact revision, where there
        # is one. It is what binds the inline findings below: comments belong
        # to a review round, and the round is the only thing GitHub records
        # that a later revision cannot move.
        review = self._review_for(reviews, head_sha)

        # The reviewer's own summaries, newest first, restricted to ones that
        # name this revision. A summary about an earlier head is evidence about
        # that head and must not answer a question about this one.
        #
        # Prose is not the only way a revision gets named, and it is the weaker
        # way. Where a review object carries GitHub's own `commit_id` for this
        # head, that is the reviewer's revision stated as data, and it stands
        # whatever the body says: a reviewer whose summary words the range
        # differently, or omits it, was still reviewing this commit. Requiring
        # the text to restate the SHA discarded a review that was already
        # bound, together with the findings linked to it, and reported no
        # review of a revision that had one.
        summaries = [
            item
            for item in issue_comments + reviews
            if self._authored(item)
            and isinstance(item.get("body"), str)
            and (
                self._covers(item["body"], head_sha)
                or (review is not None and item.get("id") == review.get("id"))
            )
        ]
        if not summaries:
            return ReviewContent(
                pull_request_number=number,
                head_sha=head_sha,
                reviewer=self._profile.reviewer,
                available=False,
                retrieved_at=self._now(),
                unavailable_reason=NO_REVIEW_FOR_REVISION,
            )

        # The newest summary that actually says something, and only if none
        # does, the newest of them.
        #
        # A review object can be bound to this head and carry an empty body —
        # a reviewer that publishes its prose as an issue comment and submits
        # the review itself as the container for inline findings. Admitting
        # bound reviews as summaries, which is what lets an authoritative
        # binding outrank prose, made such a review eligible; being the newest,
        # it then displaced the readable summary beside it, and `ReviewContent`
        # refuses to be available with nothing readable in it. A completed
        # review would have surfaced as `review_unavailable`, which is the
        # reading this whole path exists to prevent.
        #
        # Every candidate here is already bound to this revision, so preferring
        # a readable one chooses between evidence about this head rather than
        # reaching back to an earlier one for text.
        readable = [item for item in summaries if item["body"].strip()]
        latest = max(readable or summaries, key=self._when)
        # The findings this reviewer published in its review of this revision,
        # identified by the review they were submitted under rather than by
        # where GitHub currently anchors them.
        #
        # With no review object for this head there is nothing to bind to, and
        # no inline findings are reported. That is an honest empty rather than
        # a gap: a summary can name a revision without a review object being
        # readable, and importing whatever comments happen to sit on the pull
        # request would be inventing their applicability.
        #
        # A finding from an earlier round that is still unfixed is deliberately
        # not carried forward here. It remains true of the code, but it is not
        # something the reviewer said about this revision, and `ReviewContent`
        # answers the second question. Where the reviewer itself re-states a
        # finding in the new round, it arrives as a comment of that round and
        # is returned like any other.
        comments: tuple[ReviewComment, ...] = ()
        if review is not None:
            review_id = review["id"]
            comments = tuple(
                comment
                for comment in (
                    self._comment(item)
                    for item in inline
                    if self._authored(item) and self._belongs_to(item, review_id)
                )
                if comment is not None
            )
        return ReviewContent(
            pull_request_number=number,
            head_sha=head_sha,
            reviewer=self._profile.reviewer,
            available=True,
            summary=latest["body"],
            comments=comments,
            submitted_at=self._when(latest),
            retrieved_at=self._now(),
        )


class _Round(NamedTuple):
    """The reviewer's latest status on one revision, and when it said so."""

    state: str
    at: datetime | None


class _Head(NamedTuple):
    """A head SHA, and the state of the round already on it, if one is."""

    sha: str
    # Empty when no round was joined and a trigger is needed.
    state: str


class _Unavailable(Exception):
    """GitHub could not answer; nothing is known about the review."""


class _Refused(Exception):
    """GitHub refused the call."""


__all__ = ["API_ROOT", "NO_REVIEW_FOR_REVISION", "GitHubReviewProvider"]
