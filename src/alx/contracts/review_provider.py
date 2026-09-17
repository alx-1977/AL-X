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

from dataclasses import dataclass
from enum import Enum


class ReviewProvider(str, Enum):
    """The external reviewers this system supports."""

    CODERABBIT = "coderabbit"
    GREPTILE = "greptile"


@dataclass(frozen=True, slots=True)
class ReviewProviderProfile:
    """The provider-specific facts of one GitHub-native reviewer.

    `account_prefix` matches the reviewer's bot login rather than a numeric
    account id. An id is exact, but it is also unverifiable for a reviewer that
    has never commented here, and a wrong constant would silently read every
    review as empty. Greptile publishes under more than one bot login, so the
    prefix is the property that actually identifies the account across them.
    """

    provider: ReviewProvider
    # What the result calls this reviewer. Provenance for AL/X, never a branch.
    reviewer: str
    # The comment that asks for another look at the current head. Every one of
    # these reviewers reviews a new pull request without being asked; this is
    # what gets a fresh review after a corrective commit.
    trigger: str
    # The lowercase start of the reviewer's GitHub login.
    account_prefix: str

    def authored_by_reviewer(self, login: object) -> bool:
        """Whether this GitHub account is the configured reviewer."""
        return (
            isinstance(login, str)
            and login.strip().lower().startswith(self.account_prefix)
        )


PROFILES: dict[ReviewProvider, ReviewProviderProfile] = {
    # Verified against this repository: CodeRabbit reviews a pull request
    # within seconds of it being opened, publishes a `CodeRabbit` commit
    # status, and re-reviews when asked by this comment.
    ReviewProvider.CODERABBIT: ReviewProviderProfile(
        provider=ReviewProvider.CODERABBIT,
        reviewer="coderabbit",
        trigger="@coderabbitai review",
        account_prefix="coderabbit",
    ),
    # Greptile's documented trigger. It has published no review here yet, so
    # the account is matched by prefix: both `greptile-apps[bot]` and
    # `greptile[bot]` exist on GitHub and the documentation does not say which
    # posts reviews.
    ReviewProvider.GREPTILE: ReviewProviderProfile(
        provider=ReviewProvider.GREPTILE,
        reviewer="greptile",
        trigger="@greptileai",
        account_prefix="greptile",
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
