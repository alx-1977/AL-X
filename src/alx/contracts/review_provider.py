"""Which external reviewer is configured, and the little that differs between them.

Every reviewer AL/X can use watches this repository through GitHub. A review is
asked for by leaving a comment on a pull request, and what the reviewer says
comes back as pull request comments and reviews. That is the whole mechanism,
and it is the same one for each of them.

So the provider boundary is deliberately thin. Three values differ — what the
reviewer is called, the comment that asks it for another look, and how its
account is recognised among the other participants in a thread. Everything else
is GitHub, shared.

Core reasoning never sees any of this. AL/X asks for a review, reads what came
back, and decides whether it matters; which reviewer produced it is provenance
on the result, not a branch in her reasoning. Changing the configured provider
must therefore change no goal, no prompt, no capability and no merge behaviour.

## What is deliberately absent

There is no review mode. Greptile's TREX runs code as part of a review, and on
inspection it appears to be enabled for an organisation rather than selected per
review: its API exposes no review endpoints at all, and the documented trigger
has no mode argument. A `standard | trex` field would therefore be a knob AL/X
could turn with nothing behind it, and a test for it could only exercise a
reconstruction of a path that does not exist. If Greptile later exposes a real
per-review selection, it belongs inside the Greptile adapter and its policy,
without changing what AL/X asks for.
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from enum import Enum


class ReviewProvider(str, Enum):
    """The external reviewers this system supports."""

    CODERABBIT = "coderabbit"
    GREPTILE = "greptile"


@dataclass(frozen=True, slots=True)
class ReviewProviderProfile:
    """The provider-specific facts of one GitHub-native reviewer.

    `allowed_logins` is an exact set, and that is the whole point. It began as a
    prefix, chosen because Greptile publishes under more than one bot login and
    a prefix covered both without my having to know which. That traded
    authentication for convenience: `coderabbit-evil[bot]` and `coderabbitXYZ`
    both start with `coderabbit`, so any account somebody could register under
    a lookalike name would have passed as the reviewer.

    That matters more here than it looks. Review evidence reaches AL/X's
    reasoning and can complete a watched task, so an account that passes this
    check can put findings in front of her that she will weigh as a reviewer's.
    Matching a revision exactly does not authenticate who wrote about it.

    Each login is listed because it was verified, not because it was guessed.
    """

    provider: ReviewProvider
    # What the result calls this reviewer. Provenance for AL/X, never a branch.
    reviewer: str
    # The comment that asks for another look at the current head. Every one of
    # these reviewers reviews a new pull request without being asked; this is
    # what gets a fresh review after a corrective commit.
    trigger: str
    # The exact GitHub logins this reviewer publishes under.
    allowed_logins: frozenset[str]
    # The commit-status context this reviewer publishes on the revision it is
    # reviewing, when that is verified. It is the reviewer's own structured
    # statement of where a review round stands — pending while it works,
    # terminal once it has finished — and it is the only one: a summary
    # comment is posted as a placeholder the moment a round starts and edited
    # in place afterwards, so its existence says a review began, not that one
    # finished. Empty where no status has been verified for this reviewer.
    status_context: str = ""
    # D-042: how this reviewer states, in its own fixed format, that a review
    # raised nothing, and the statements that contradict that. A merge needs
    # one of the first and none of the second in the review of its exact head.
    # Empty where no such format has been verified, and then no review by this
    # reviewer can show itself clean: the merge is refused rather than assumed.
    no_findings_statements: tuple[str, ...] = ()
    finding_statements: tuple[str, ...] = ()

    def states_no_findings(self, summary: str) -> bool:
        """Whether this summary is the reviewer saying it raised nothing.

        A protocol check on the reviewer's own fixed wording, never a reading
        of what a finding means. Fails closed: no verified format, no clean
        statement, or any contradicting statement is not clean.
        """
        if not isinstance(summary, str) or not self.no_findings_statements:
            return False
        if any(re.search(pattern, summary) for pattern in self.finding_statements):
            return False
        return any(re.search(pattern, summary) for pattern in self.no_findings_statements)

    def authored_by_reviewer(self, login: object) -> bool:
        """Whether this GitHub account is the configured reviewer.

        Compared lowercase and stripped, which is the only normalisation
        GitHub's identity semantics actually require: logins are
        case-insensitive, so `CodeRabbitAI[bot]` is the same account. Nothing
        else is normalised, because every further liberty taken here is a
        family of accounts somebody else can register.
        """
        if not isinstance(login, str):
            return False
        return login.strip().lower() in self.allowed_logins


PROFILES: dict[ReviewProvider, ReviewProviderProfile] = {
    # Verified against this repository: CodeRabbit reviews a pull request
    # within seconds of it being opened, publishes a `CodeRabbit` commit
    # status, and re-reviews when asked by this comment.
    ReviewProvider.CODERABBIT: ReviewProviderProfile(
        provider=ReviewProvider.CODERABBIT,
        reviewer="coderabbit",
        trigger="@coderabbitai review",
        # Verified against this repository: every review CodeRabbit has
        # published here came from this account (id 136622811).
        allowed_logins=frozenset({"coderabbitai[bot]"}),
        # Verified on PR #77: `pending` "Review in progress" when a round
        # starts, `success` "Review completed" once its findings are
        # published, each on the exact commit reviewed and created by the
        # account above.
        status_context="CodeRabbit",
        # Verified on PRs #119-#123: a round with findings publishes a review
        # headed "Actionable comments posted: N"; a round without edits the
        # summary to "No actionable comments were generated in the recent
        # review"; a round refused by the review limit still marks the commit
        # status `success` ("Review rate limited") and says "rate limited" in
        # the summary, which is not a review. Findings outside the changed
        # lines are listed under "Outside diff range comments (N)".
        no_findings_statements=(
            r"No actionable comments were generated",
            r"Actionable comments posted: 0\b",
        ),
        finding_statements=(
            r"Actionable comments posted: [1-9]",
            r"Outside diff range comments \([1-9]",
            r"(?i)rate limited",
        ),
    ),
    # Greptile's documented trigger. It has published no review here yet, so
    # both accounts it is known to publish under are listed rather than one
    # being guessed at: `greptile-apps[bot]` (id 165735046) and `greptile[bot]`
    # (id 271099122) both exist on GitHub, and the documentation does not say
    # which posts reviews. Listing both is exact; a prefix covering them was
    # not. If a third appears, it is added here after being verified.
    ReviewProvider.GREPTILE: ReviewProviderProfile(
        provider=ReviewProvider.GREPTILE,
        reviewer="greptile",
        trigger="@greptileai",
        allowed_logins=frozenset({"greptile-apps[bot]", "greptile[bot]"}),
    ),
}


def profile_for(provider: ReviewProvider) -> ReviewProviderProfile:
    if not isinstance(provider, ReviewProvider):
        raise TypeError("provider must be a ReviewProvider")
    return PROFILES[provider]


__all__ = [
    "PROFILES",
    "ReviewProvider",
    "ReviewProviderProfile",
    "profile_for",
]
