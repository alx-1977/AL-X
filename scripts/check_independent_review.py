"""Verify that an accepted independent reviewer examined this exact diff.

D-026 requires production changes to be independently reviewed. What that
means is a property, not a product: someone other than the agent that wrote
the change looked at the whole proposed diff and reported what they found.
This checks that property against GitHub's own record of the review, which
the implementing agent cannot write.

Five things are verifiable and are verified here:

  * the review covered the head actually being merged, not an earlier one;
  * the reviewer is on the governed accepted list, matched by immutable id;
  * the reviewer did not author any commit in the change under review;
  * the review carries substantive content rather than a bare verdict;
  * an approved exception, if relied on, names this exact head.

One thing is not verifiable and is not claimed: that the reviewer genuinely
read the diff and exercised judgement over correctness, regressions,
architecture, governance, safety and economic boundaries, and test
enforcement. No mechanism can establish that. What this gate does is make a
false claim a recorded false statement rather than an absence, which is the
standard `docs/LAW_ENFORCEMENT.md` already sets: "the reviewer will notice"
is not evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

try:  # The runner has system roots; a developer machine may not.
    import certifi

    _SSL_CONTEXT: ssl.SSLContext | None = ssl.create_default_context(
        cafile=certifi.where()
    )
except ImportError:  # pragma: no cover - certifi is a declared dependency
    _SSL_CONTEXT = None


API_ROOT = "https://api.github.com"

# A review body shorter than this is not a report. The threshold is
# deliberately low: it rejects "LGTM" and "Approved", not a terse but real
# account of what was examined.
MINIMUM_SUBSTANTIVE_CHARACTERS = 200

# A review whose state is DISMISSED has been explicitly retracted: someone
# with write access said it no longer stands. It keeps its commit_id and its
# body, so nothing else here would notice. CHANGES_REQUESTED is deliberately
# not rejected -- a reviewer who found problems still reviewed, and the
# corrective commits that follow move the head, which the staleness check
# already catches.
RETRACTED_REVIEW_STATE = "DISMISSED"

# Bare verdicts. A body consisting only of one of these, with or without
# punctuation, is a token rather than evidence -- including a clean one.
BARE_VERDICTS = {
    "lgtm",
    "approved",
    "approve",
    "ok",
    "okay",
    "looks good",
    "looks good to me",
    "no issues",
    "no issues found",
    "no findings",
    "clean",
    "pass",
    "passed",
    "ship it",
    "+1",
}


class ReviewEvidenceError(RuntimeError):
    """A verification failure carrying its reason and nothing else."""


def _request(url: str, token: str) -> object:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "alx-law-gates",
        },
    )
    with urllib.request.urlopen(
        request, timeout=30, context=_SSL_CONTEXT
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def load_accepted_reviewers(root: Path) -> dict[int, str]:
    """The governed allowlist, keyed by immutable GitHub account id."""
    path = root / "review/accepted_reviewers.json"
    if not path.is_file():
        raise ReviewEvidenceError("review/accepted_reviewers.json is missing")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ReviewEvidenceError(
            "review/accepted_reviewers.json is not valid JSON"
        ) from None
    entries = document.get("reviewers")
    if not isinstance(entries, list) or not entries:
        raise ReviewEvidenceError(
            "review/accepted_reviewers.json names no reviewers"
        )
    accepted: dict[int, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReviewEvidenceError("each accepted reviewer must be an object")
        identity = entry.get("id")
        login = entry.get("login")
        # A bool is an int in Python, and `true` here would silently become 1.
        if not isinstance(identity, int) or isinstance(identity, bool):
            raise ReviewEvidenceError(
                "each accepted reviewer needs a numeric GitHub id"
            )
        if not isinstance(login, str) or not login.strip():
            raise ReviewEvidenceError(
                "each accepted reviewer needs a login for diagnostics"
            )
        accepted[identity] = login
    return accepted


def is_substantive(body: str, inline_findings: int) -> bool:
    """Whether a review reported something, rather than merely concluding.

    A clean review is valid: finding nothing is a legitimate outcome, and
    requiring a defect would reward inventing one. What is not valid is a
    verdict with no account of what was examined. So either the reviewer
    anchored findings to files, or the body is long enough to describe the
    scope actually reviewed.
    """
    if inline_findings > 0:
        return True
    text = (body or "").strip()
    if not text:
        return False
    collapsed = re.sub(r"[\s\W_]+", " ", text.lower()).strip()
    if collapsed in BARE_VERDICTS:
        return False
    return len(text) >= MINIMUM_SUBSTANTIVE_CHARACTERS


# The only file a record-only commit may touch. An exception documents itself
# in the register and nowhere else; anything more is implementation arriving
# above an approved SHA.
EXCEPTION_RECORD_PATHS = frozenset({"governance/EXCEPTIONS.md"})


def exception_names_pull_request(exceptions_text: str, sha: str, number: int) -> bool:
    """Whether the exception naming `sha` also names this exact pull request.

    An approved SHA is not a licence for whatever pull request happens to
    contain it. The register must name both, so an exception approved for one
    migration cannot silently authorise a different branch that rebased onto
    the same commit.
    """
    section = _exception_section(exceptions_text, sha)
    if section is None:
        return False
    return re.search(rf"(?:pull request|PR)\s*#?{number}\b", section, re.IGNORECASE) is not None


def _exception_section(exceptions_text: str, sha: str) -> str | None:
    """The whole register entry containing `sha`, from its heading.

    Taken from the heading rather than from the SHA, because the pull-request
    number usually precedes the commit in the same sentence — "pull request #20
    at `abc…`" — and a section starting at the SHA would miss it.
    """
    if len(sha) != 40 or sha not in exceptions_text:
        return None
    position = exceptions_text.index(sha)
    start = exceptions_text.rfind("\n## ", 0, position)
    section = exceptions_text[start if start != -1 else 0 :]
    boundary = section.find("\n## ", 1)
    return section if boundary == -1 else section[:boundary]


def approved_ancestor(
    exceptions_text: str, commits: list[dict], head_sha: str
) -> str | None:
    """The approved exception SHA below this head, if there is exactly one.

    Only a commit in this pull request qualifies, and it must not be the head
    itself: naming the head is the ordinary path handled above. An unrelated
    approved SHA that is not an ancestor of this head returns nothing.
    """
    shas = [commit.get("sha") for commit in commits if isinstance(commit.get("sha"), str)]
    if head_sha not in shas:
        return None
    ancestors = shas[: shas.index(head_sha)]
    covered = [sha for sha in ancestors if exception_covers(exceptions_text, sha)]
    # More than one approved ancestor is ambiguous, and ambiguity must not
    # resolve itself into permission.
    return covered[0] if len(covered) == 1 else None


def record_only_above(
    commits: list[dict], approved_sha: str, files_for: "Callable[[str], list[str]]"
) -> bool:
    """Whether every commit above `approved_sha` only records the exception.

    This exists for one situation and must not grow past it: an exception that
    documents itself inside the pull request it covers cannot name its own
    head, because a commit cannot contain its own SHA. So the approved SHA sits
    below the tip, and the commits above it are required to be nothing but the
    register entry.

    "Nothing but" is checked against the files each commit actually changed,
    not against its message. A commit that touches the verifier, the workflow,
    configuration or any source file above the approved SHA makes the exception
    inapplicable, however it describes itself.
    """
    shas = [commit.get("sha") for commit in commits]
    if approved_sha not in shas:
        return False
    above = shas[shas.index(approved_sha) + 1 :]
    if not above:
        # The approved SHA is the head itself; the ancestor path is unnecessary.
        return True
    for sha in above:
        if not isinstance(sha, str):
            return False
        changed = files_for(sha)
        if not changed:
            return False
        if not set(changed) <= EXCEPTION_RECORD_PATHS:
            return False
    return True


def exception_covers(exceptions_text: str, head_sha: str) -> bool:
    """Whether an approved exception names this exact head.

    The full SHA must appear, and an abbreviation is not enough: an exception
    is approved for one commit, and a prefix could match a commit nobody
    approved. Governance already requires the register to carry an approval
    date, so an entry naming a head without one does not count.
    """
    if len(head_sha) != 40:
        return False
    if head_sha not in exceptions_text:
        return False
    section = exceptions_text[exceptions_text.index(head_sha) :]
    boundary = section.find("\n## ")
    section = section if boundary == -1 else section[:boundary]
    # A whole word, not a substring. "pending" marks an unapproved entry, but
    # matching it anywhere silently voided any exception whose prose contained
    # "depending", "appending", "impending" or "spending" — EX-004 and EX-005
    # both do. An exception that reads as approved and is quietly ignored is
    # the worst failure this function can have, because nothing reports it.
    return "**Approval date:**" in section and not re.search(
        r"\bpending\b", section, re.IGNORECASE
    )


def verify(
    reviews: list[dict],
    inline_comments: list[dict],
    commit_authors: set[int],
    head_sha: str,
    accepted: dict[int, str],
) -> str:
    """The whole decision, over data already fetched. Returns the reason it passed."""
    if not reviews:
        raise ReviewEvidenceError(
            "no review of any kind was submitted for this pull request"
        )

    findings_by_reviewer: dict[int, int] = {}
    for comment in inline_comments:
        user = comment.get("user") or {}
        identity = user.get("id")
        # A finding counts only if it is anchored to a file in this head.
        if comment.get("commit_id") == head_sha and comment.get("path"):
            if isinstance(identity, int):
                findings_by_reviewer[identity] = (
                    findings_by_reviewer.get(identity, 0) + 1
                )

    stale: list[str] = []
    dismissed: list[str] = []
    unaccepted: list[str] = []
    self_review: list[str] = []
    insubstantial: list[str] = []

    for review in reviews:
        user = review.get("user") or {}
        identity = user.get("id")
        login = user.get("login") or "unknown"
        if not isinstance(identity, int):
            continue
        if review.get("commit_id") != head_sha:
            stale.append(f"{login} reviewed {str(review.get('commit_id'))[:12]}")
            continue
        if str(review.get("state") or "").upper() == RETRACTED_REVIEW_STATE:
            dismissed.append(f"{login}'s review was dismissed")
            continue
        if identity not in accepted:
            unaccepted.append(f"{login} (id {identity})")
            continue
        if identity in commit_authors:
            # The agent that wrote the change cannot be the independent check
            # on it, whatever the review says.
            self_review.append(f"{login} authored commits in this change")
            continue
        if not is_substantive(
            review.get("body") or "", findings_by_reviewer.get(identity, 0)
        ):
            insubstantial.append(f"{login} left a verdict without a report")
            continue
        return (
            f"{login} (id {identity}) reviewed {head_sha[:12]} with "
            f"{findings_by_reviewer.get(identity, 0)} anchored finding(s)"
        )

    for reason in (stale, dismissed, unaccepted, self_review, insubstantial):
        if reason:
            raise ReviewEvidenceError("; ".join(reason))
    raise ReviewEvidenceError("no accepted independent review covers this head")


def _paged(url: str, token: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while page <= 10:
        batch = _request(f"{url}?per_page=100&page={page}", token)
        if not isinstance(batch, list) or not batch:
            break
        items.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
        page += 1
    return items


def check_pull_request(
    root: Path, repository: str, number: int, head_sha: str, token: str
) -> str:
    accepted = load_accepted_reviewers(root)
    exceptions_path = root / "governance/EXCEPTIONS.md"
    exceptions_text = (
        exceptions_path.read_text(encoding="utf-8")
        if exceptions_path.is_file()
        else ""
    )
    if exception_covers(exceptions_text, head_sha):
        return f"an approved exception names {head_sha[:12]}"

    base = f"{API_ROOT}/repos/{repository}/pulls/{number}"
    try:
        reviews = _paged(f"{base}/reviews", token)
        inline_comments = _paged(f"{base}/comments", token)
        commits = _paged(f"{base}/commits", token)
    except (urllib.error.URLError, urllib.error.HTTPError) as error:
        raise ReviewEvidenceError(
            f"could not read review evidence from GitHub: {type(error).__name__}"
        ) from None

    # The self-referential case, and only that case. An exception recorded
    # inside the pull request it covers cannot name its own head, so it names
    # the implementation below the tip and the commits above must be the
    # register entry and nothing else. Every condition is required: the
    # exception names this pull request, the approved SHA is an ancestor of
    # this head, and no substantive file changed above it.
    approved = approved_ancestor(exceptions_text, commits, head_sha)
    if approved is not None:
        def files_for(sha: str) -> list[str]:
            try:
                commit = _request(f"{API_ROOT}/repos/{repository}/commits/{sha}", token)
            except (urllib.error.URLError, urllib.error.HTTPError):
                # Unreadable means unverifiable, and unverifiable must not pass.
                return []
            if not isinstance(commit, dict):
                return []
            return [
                entry.get("filename")
                for entry in commit.get("files") or ()
                if isinstance(entry, dict) and isinstance(entry.get("filename"), str)
            ]

        if exception_names_pull_request(
            exceptions_text, approved, number
        ) and record_only_above(commits, approved, files_for):
            return (
                f"an approved exception names {approved[:12]} for pull request "
                f"{number}, and only its own record sits above it"
            )

    commit_authors = {
        (commit.get("author") or {}).get("id")
        for commit in commits
        if isinstance((commit.get("author") or {}).get("id"), int)
    }
    return verify(reviews, inline_comments, commit_authors, head_sha, accepted)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--pull-request", type=int, default=0)
    parser.add_argument("--head-sha", default="")
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="fail on missing evidence; otherwise report and pass",
    )
    parser.add_argument(
        "--event-name",
        default="",
        help=(
            "the GitHub event being handled. When it is a pull_request event "
            "the pull request number is mandatory under --enforce."
        ),
    )
    args = parser.parse_args(argv)

    if not args.pull_request:
        # Two different situations reach here, and as a required check they
        # must not share an answer.
        #
        # A push to an already-merged branch genuinely has no pull request to
        # read, and the merge it came from was gated on its own evidence. That
        # passes.
        #
        # A pull_request event that arrived without its number is a broken
        # invocation, not an absent obligation. Passing there would satisfy a
        # required check while verifying nothing, which is the one failure a
        # blocking gate must not have. The event name is supplied by the
        # workflow rather than inferred from the missing number, because
        # inferring it from the absent value is what made the two cases
        # indistinguishable in the first place.
        if args.enforce and args.event_name.startswith("pull_request"):
            print(
                "AL/X independent review NOT VERIFIED: a pull_request event "
                "supplied no pull request number"
            )
            return 1
        print("AL/X independent review: no pull request in context; nothing to verify")
        return 0

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or not args.repository or len(args.head_sha) != 40:
        reason = "missing repository, head SHA or token"
        print(f"AL/X independent review: cannot verify ({reason})")
        return 1 if args.enforce else 0

    try:
        evidence = check_pull_request(
            args.root, args.repository, args.pull_request, args.head_sha, token
        )
    except ReviewEvidenceError as error:
        print(f"AL/X independent review NOT VERIFIED: {error}")
        if args.enforce:
            return 1
        print(
            "This gate is reporting only. D-026 makes it blocking once it has "
            "accumulated evidence on real merges and Friedl approves promotion."
        )
        return 0
    print(f"AL/X independent review verified: {evidence}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
