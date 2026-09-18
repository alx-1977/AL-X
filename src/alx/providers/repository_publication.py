"""Put one repair branch on the remote, and nothing else.

This is the one production site that pushes. Authority is granted by
enumeration, as it is for the Coding Agent's writes: the exact argv is built
here from a validated branch name, so there is no shape in which a force, a
lease, a deletion refspec, a second ref, an alternate remote or a bare option
can be produced. Adding a capability means writing another command, which is a
visible change rather than a new argument somebody passes.

Four properties carry the safety:

- **The branch cannot be the default one.** `main`, `master` and `HEAD` are
  refused by the contract before anything runs. A repair reaches main by being
  reviewed and merged; nothing here may write it.

- **The revision is asserted, not read.** The caller names the commit it
  decided to publish and the local branch is verified to point at exactly that.
  Reading whatever the branch points at now would publish work written after
  the decision, under a decision that never saw it.

- **Divergence is refused, never resolved.** The push is an ordinary
  fast-forward. If the remote holds commits this would discard, git refuses and
  that refusal is reported as the fact it is. There is no force and no lease,
  so "the remote had something we did not" can never become "the remote no
  longer has it".

- **Nothing here interprets.** It publishes a named commit or explains why it
  could not. Whether to publish, what the branch is for, and what to do about a
  refusal are AL/X's.

The Coding Agent gains nothing from this module. Its git authority is unchanged
and still excludes push by construction: a job commits inside the worktree it
was given, and AL/X decides whether that work is published.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess  # noqa: S404 - the one branch-publication site
from pathlib import Path
from typing import Any, Callable

from alx.providers.repository_runtime import origin_identity
from alx.contracts.publication import (
    PublicationError,
    PublicationOutcome,
    PublicationRequest,
    publishable_branch,
    valid_sha,
)

LOGGER = logging.getLogger(__name__)

Runner = Callable[..., Any]

# The remote a repair may reach. One name, fixed here, so no caller can direct
# a push at somewhere nobody is watching.
ORIGIN = "origin"

_SHOW_TOPLEVEL = ("git", "rev-parse", "--show-toplevel")
# Which GitHub repository this checkout actually pushes to. Read rather than
# assumed: the push goes to whatever `origin` names, and the pull request is
# opened against a separately configured repository, so nothing but this
# comparison keeps the two the same place.
_ORIGIN_URL = ("git", "config", "--get", "remote.origin.url")

# Configuration forced on every call, so an inherited environment cannot
# reintroduce a credential helper, an editor, or a config injection.
_SAFE_GIT_CONFIG = {
    "core.hooksPath": os.devnull,
    "credential.helper": "",
}


def _git_environment() -> dict[str, str]:
    """The small fixed environment every publication command runs under."""
    environment = {
        name: os.environ[name]
        for name in ("PATH", "TZ", "HOME")
        if name in os.environ
    }
    environment.update({
        # Fixed, never inherited. Two decisions this module reports are read
        # from git's own wording: whether a refusal was a divergence, and
        # whether the remote already had the revision. Git translates both when
        # the locale says to, so inheriting a translated locale would leave the
        # push correctly refused but described wrongly — a diverged remote
        # reported as `publication_refused`, an up-to-date one as newly
        # published. The environment decides what the text says, so it is set
        # here rather than hoped for.
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": os.devnull,
        "SSH_ASKPASS": os.devnull,
        "GIT_CONFIG_COUNT": str(len(_SAFE_GIT_CONFIG)),
    })
    for index, (key, value) in enumerate(_SAFE_GIT_CONFIG.items()):
        environment[f"GIT_CONFIG_KEY_{index}"] = key
        environment[f"GIT_CONFIG_VALUE_{index}"] = value
    return environment


# A remote rejection git reports when the push is not a fast-forward. Matched
# on git's own words rather than on an exit code, because the exit code is the
# same for every refusal and this one has to be told apart: it means the remote
# moved, which is a fact about the work, not a fault.
_REJECTED = re.compile(
    r"\[rejected\]|non-fast-forward|fetch first|stale info", re.IGNORECASE
)


class RepositoryPublication:
    """Publish a repair branch from one fixed checkout to its origin."""

    def __init__(
        self,
        root: Path,
        timeout_seconds: int = 120,
        runner: Runner = subprocess.run,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("publication requires an absolute checkout path")
        if timeout_seconds <= 0:
            raise ValueError("publication timeout must be positive")
        self._root = root.resolve()
        self._timeout = timeout_seconds
        self._runner = runner
        # The remote URL this object was allowed to publish to, remembered
        # when it was checked. See `origin_identity`.
        self._validated_remote = ""

    def origin_identity(self) -> str:
        """The `owner/name` this checkout's origin points at, or "".

        Empty when there is no origin, when it cannot be read, or when it is
        not a GitHub remote in a form this system recognises — all of which
        are answers the caller must refuse on rather than guess past.

        The URL behind that identity is kept, because the name `origin` is
        only a lookup in `.git/config` and the push would perform it again
        later. Validating a name and then using the name leaves the interval
        between them open: anything able to write that file could point
        `origin` elsewhere and the approved commit would go there. What was
        checked is what gets pushed to.
        """
        url = self._origin_url()
        identity = origin_identity(url)
        self._validated_remote = url if identity else ""
        return identity

    def _origin_url(self) -> str:
        """The URL `origin` currently names, or "" when it cannot be read."""
        completed = self._run(_ORIGIN_URL)
        if completed.returncode != 0:
            return ""
        return (completed.stdout or "").strip()

    def _run(self, command: tuple[str, ...]) -> subprocess.CompletedProcess:
        try:
            return self._runner(
                list(command),
                cwd=str(self._root),
                env=_git_environment(),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                # Explicit, never inherited: a shell would let the branch name
                # be read as syntax rather than as an argument.
                shell=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            LOGGER.warning("Publication command failed: %s", type(error).__name__)
            raise PublicationError("publication_unavailable") from error

    def _local_sha(self, branch: str) -> str:
        """The commit the local branch points at, or "" if it has none."""
        # `refs/heads/<branch>` rather than the bare name: a bare name can also
        # resolve a tag or a remote-tracking ref, and publishing whatever
        # happened to share the name is not what was asked for.
        completed = self._run(
            ("git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        )
        value = (completed.stdout or "").strip()
        return value if valid_sha(value) else ""

    def publish(self, request: PublicationRequest) -> PublicationOutcome:
        """Push one branch at one exact commit, or explain why it did not."""
        if not publishable_branch(request.branch):
            raise PublicationError("branch_not_permitted")

        toplevel = self._run(_SHOW_TOPLEVEL)
        if toplevel.returncode != 0:
            raise PublicationError("publication_unavailable")
        if Path((toplevel.stdout or "").strip()).resolve() != self._root:
            # The checkout is not the one this object was built for.
            raise PublicationError("publication_unavailable")

        local = self._local_sha(request.branch)
        if not local:
            raise PublicationError("branch_unknown")
        if local != request.head_sha:
            # The branch moved between the decision and the push. Publishing it
            # anyway would send a revision nobody authorised.
            raise PublicationError("branch_unknown")

        # `<approved sha>:refs/heads/<branch>`, explicit on both sides, so the
        # destination cannot be inferred from configuration and cannot be a
        # deletion (which is an empty source) or a rename.
        #
        # The source is the commit that was just verified, not the branch name
        # that pointed at it. A name is read by git when the push runs, which
        # is after the check: a branch that moved in between — another job
        # committing, a checkout, anything sharing the worktree — would send a
        # revision nobody approved, under a request that named the old one.
        # The verified revision is what travels, and if the branch has moved
        # the push simply carries the approved commit regardless.
        # Push to a URL, never to the name `origin`. A name is a lookup in
        # `.git/config` that git performs when the push runs, so validating
        # the name and then pushing to the name leaves the interval between
        # them open: anything able to write that file could point `origin`
        # elsewhere, and the approved commit would go there.
        #
        # The URL is read here and handed to git directly, so what was checked
        # is what is pushed to. Where composition validated an identity, that
        # exact URL is required to still be the one configured — a remote
        # repointed since then is refused rather than followed.
        remote = self._origin_url()
        if not remote:
            raise PublicationError("publication_unavailable")
        if self._validated_remote and remote != self._validated_remote:
            LOGGER.warning("Publication origin changed since it was checked")
            raise PublicationError("publication_unavailable")
        completed = self._run((
            "git", "push", remote,
            f"{local}:refs/heads/{request.branch}",
        ))
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode != 0:
            if _REJECTED.search(output):
                raise PublicationError("branch_diverged")
            LOGGER.warning("Publication refused by the remote")
            raise PublicationError("publication_refused")

        # git says "Everything up-to-date" when the remote already held this
        # commit. That is a successful publication of the same revision, and
        # saying so is more useful than reporting it as new work.
        already = "up-to-date" in output.lower() or "up to date" in output.lower()
        return PublicationOutcome(
            branch=request.branch,
            head_sha=request.head_sha,
            published=True,
            already_current=already,
        )


__all__ = ["ORIGIN", "RepositoryPublication"]
