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
    """The external reviewer this system supports.

    Greptile alone since 2026-10-14: Friedl, "please add Greptile and
    completely remove CR. I do not think we need both."
    """

    GREPTILE = "greptile"


@dataclass(frozen=True, slots=True)
class ReviewProviderProfile:
    """The provider-specific facts of one GitHub-native reviewer.

    `allowed_logins` is an exact set, and that is the whole point. A prefix
    would admit any account somebody could register under a lookalike name,
    and review evidence reaches AL/X's reasoning and decides merges, so an
    account that passes this check can put findings in front of her that she
    will weigh as a reviewer's. Matching a revision exactly does not
    authenticate who wrote about it.

    Each login is listed because it was verified, not because it was guessed.
    """

    provider: ReviewProvider
    # What the result calls this reviewer. Provenance for AL/X, never a branch.
    reviewer: str
    # The comment that asks for a review of the current head.
    trigger: str
    # The exact GitHub logins this reviewer publishes under.
    allowed_logins: frozenset[str]
    # The check run this reviewer publishes on the exact commit it reviews, and
    # the GitHub App that publishes it. It is the reviewer's own structured
    # statement of where a review round stands - running while it works,
    # completed once it has finished - and the only one: a summary comment is
    # edited in place across rounds, so its existence says a review began, not
    # that one finished. Empty where no check run has been verified.
    check_run_name: str = ""
    check_run_app: str = ""
    # D-042: how this reviewer states, in its own fixed format, that a review
    # is clean, and the statements that contradict that. A merge needs one of
    # the first and none of the second in the review of its exact head. Empty
    # where no such format has been verified, and then no review by this
    # reviewer can show itself clean: the merge is refused rather than assumed.
    no_findings_statements: tuple[str, ...] = ()
    finding_statements: tuple[str, ...] = ()

    def states_no_findings(self, summary: str) -> bool:
        """Whether this summary is the reviewer saying the review is clean.

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
        case-insensitive. Nothing else is normalised, because every further
        liberty taken here is a family of accounts somebody else can register.
        """
        if not isinstance(login, str):
            return False
        return login.strip().lower() in self.allowed_logins

    def published_by_reviewer(self, app: object) -> bool:
        """Whether this check run's GitHub App is the configured reviewer's."""
        if not isinstance(app, dict) or not self.check_run_app:
            return False
        slug = app.get("slug")
        return isinstance(slug, str) and slug.strip().lower() == self.check_run_app


PROFILES: dict[ReviewProvider, ReviewProviderProfile] = {
    # Verified against this repository on PRs #136-#138 and the firmware
    # repository's PR #1:
    # - every summary, review and inline finding came from `greptile-apps[bot]`;
    # - each round publishes a `Greptile Review` check run by the
    #   `greptile-apps` app on the exact commit reviewed, `in_progress` while
    #   it works and `completed` when done, concluding `success` at the
    #   required 5/5 and `failure` below it ("Confidence 3/5 - below your
    #   required 5/5"), which is still a finished review;
    # - the summary comment is edited in place each round and carries
    #   `<!-- greptile_confidence_score:N -->` and "Last reviewed commit"
    #   linking the full SHA of the commit it reviewed;
    # - it reviews only when asked here: no round appeared on PR #139 unasked.
    # The trigger names no review tier on purpose: naming one, even to
    # decline it, ran that tier.
    ReviewProvider.GREPTILE: ReviewProviderProfile(
        provider=ReviewProvider.GREPTILE,
        reviewer="greptile",
        trigger="@greptileai review",
        allowed_logins=frozenset({"greptile-apps[bot]"}),
        check_run_name="Greptile Review",
        check_run_app="greptile-apps",
        no_findings_statements=(r"<!-- greptile_confidence_score:5 -->",),
        finding_statements=(r"<!-- greptile_confidence_score:[0-4] -->",),
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
