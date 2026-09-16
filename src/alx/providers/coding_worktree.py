"""Allocate and release one isolated worktree per coding job, under D-030.

D-028 and D-029 both grant authority *inside* "an assigned worktree" and
neither says who assigns it. Nothing did: the worktree was a path Core handed
in as a string, and the capability description named `"."` — the live AL/X
checkout — as a valid value. The kernel sandbox then faithfully made whatever
that path resolved to writable, so the confinement was only ever as isolated
as a value nothing validated.

This module is where that value comes from instead. Core supplies no path at
all; the allocator derives one from the job's own identity, creates it as a
linked worktree of the canonical repository, and hands back a directory the
live checkout can never be.

Three properties carry the decision:

- **The root resolves outside the repository.** Configuration chooses where
  coding worktrees live, but a root that resolves inside the canonical
  repository — directly, or through a symlink — refuses at startup and again
  at every allocation. A misconfiguration cannot quietly put job state back
  inside the tree this exists to isolate jobs from.

- **Branch and worktree are one command.** `git worktree add -b` creates both
  atomically, so there is no window where a branch exists without its worktree
  or the reverse, and no second branch-creation mechanism for these jobs.
  D-029's naming, collision classification, suffix order and retry bound are
  reused exactly; only the argv that applies them changed.

- **Release proves ownership before removing anything.** Removal is not a
  consequence of success. It happens when Core explicitly asks, and only after
  this module has re-derived the path from the job identity and confirmed the
  directory it is about to remove is the one it created for that job.

Nothing here interprets Friedl or decides whether a job is finished. It turns
a job identity into an isolated directory, and an explicit release into an
empty one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from alx.contracts.coding import (
    MAX_JOB_ID_CHARACTERS,
    CodingError,
    job_id_permitted,
)

LOGGER = logging.getLogger(__name__)
from alx.providers.coding_git import (
    MAX_REPAIR_BRANCH_ATTEMPTS,
    allocate_job_worktree,
    branch_name_permitted,
    canonical_repository_root,
    commit_exists,
    read_head_sha,
    release_job_worktree,
    worktree_belongs_to_repository,
    worktree_branch,
)


# A job identity reaching the filesystem becomes one path segment, so it is
# held to a narrower grammar than the broker's call IDs happen to use. No
# separator, no dot segment, no leading dash: a `..` or an absolute-looking
# identity cannot climb out of the root, and a dash-led one cannot be read as
# an option by the git command it is interpolated into.
# The branch a job's work lands on. D-029 owns the collision scheme applied to
# this base name; D-030 only fixes how the base name is derived when Core did
# not name one, so that a job always has a branch to be isolated on.
JOB_BRANCH_PREFIX = "alx/coding"

# What this allocator created, recorded beside the worktree rather than inside
# it. Beside, because a record inside a job's own worktree is a file the coding
# session could edit: ownership would then be the agent's claim rather than
# AL/X's. The `.json` sits in the root, which only AL/X writes.
OWNERSHIP_SUFFIX = ".allocation.json"

# A worktree git created that no allocation record accounts for. Separate from
# the record above and never read as one: a marker explaining why a release
# cannot be authorised must not become the thing that authorises it.
ORPHAN_SUFFIX = ".orphan.json"

# AL/X's provenance for a slot, written before git creates anything there. A
# directory under the root is AL/X's only if this file says it was about to be
# created; a worktree somebody added by hand has no claim and is not ours to
# report, let alone to remove. Never read by release: it records an intention
# to create, not a job that succeeded.
CLAIM_SUFFIX = ".claim.json"


@dataclass(frozen=True, slots=True)
class CodingWorktree:
    """One isolated worktree allocated to one coding job.

    `job_id` is the job's identity — the broker call ID — and never changes.
    `slot` is the directory name actually used, which may carry a collision
    suffix the identity does not: a taken branch name or a retained worktree
    from an earlier job moves the *directory*, not the job it belongs to.
    Keeping these separate matters because everything downstream looks the job
    up by identity, and a job whose identity shifted under it would not be
    findable by the capability that allocated it.
    """

    job_id: str
    path: Path
    branch: str
    base: str
    slot: str = ""
    # Which pass of the D-029 collision search produced this allocation: 1 for
    # the requested names, 2 for the first `-2` retry, and so on. Recorded
    # because it cannot be recovered from the names afterwards — `fix-2` is a
    # legitimate base name Core may choose on attempt 1, and it is
    # indistinguishable by inspection from `fix` suffixed on attempt 2.
    attempt: int = 1

    def __post_init__(self) -> None:
        if not self.slot:
            object.__setattr__(self, "slot", self.path.name)

    def as_values(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "worktree": str(self.path),
            "branch": self.branch,
            "base": self.base,
        }


def resolve_worktree_root(root: Path, repository: Path) -> Path:
    """Resolve the configured coding-worktree root, or refuse it.

    D-030 requires the resolved path to lie outside the canonical repository
    and not be a descendant of it, including through symlinks. Both sides are
    fully resolved before the comparison, so a symlinked root pointing back
    into the repository is caught by the same check as a literal one.

    The root is not created here. Refusing first and creating second means a
    misconfigured root never has a directory made for it.
    """
    if not isinstance(root, Path):
        raise CodingError("worktree_unusable", reason_code="root_not_configured")
    try:
        resolved_repository = Path(repository).expanduser().resolve()
    except OSError as error:
        raise CodingError(
            "worktree_unusable", reason_code="repository_unresolvable"
        ) from error
    try:
        # `strict=False`: the root legitimately may not exist yet. Symlinks in
        # the parts that do exist are still followed, which is what the
        # containment check depends on.
        resolved_root = Path(root).expanduser().resolve()
    except OSError as error:
        raise CodingError(
            "worktree_unusable", reason_code="root_unresolvable"
        ) from error
    if not resolved_root.is_absolute():
        raise CodingError("worktree_unusable", reason_code="root_not_absolute")
    if resolved_root == resolved_repository or _is_descendant(
        resolved_root, resolved_repository
    ):
        raise CodingError(
            "worktree_unusable", reason_code="root_inside_repository"
        )
    return resolved_root


def _slot_owners(slot: str) -> tuple[str, ...]:
    """The job identities whose collision sequence could contain this slot.

    `job-1-3` could be `job-1`'s third attempt, or a job literally called
    `job-1-3` on its first. Both are returned and the caller checks the record,
    so nothing depends on guessing which.
    """
    owners: list[str] = []
    for attempt in range(2, MAX_REPAIR_BRANCH_ATTEMPTS + 1):
        marker = f"-{attempt}"
        if slot.endswith(marker):
            stem = slot[: -len(marker)]
            if stem and job_id_permitted(stem):
                owners.append(stem)
    return tuple(owners)


def _unsuffixed(branch: str, attempt: int) -> str:
    """The base name `allocate` suffixed to reach this branch on this attempt.

    Applied only to a name this allocator just built, where the attempt is a
    known fact rather than something inferred from the text — so removing the
    suffix here is exact, and the stored result rebuilds the branch exactly.
    """
    if attempt <= 1:
        return branch
    marker = f"-{attempt}"
    return branch[: -len(marker)] if branch.endswith(marker) else branch


def _is_descendant(candidate: Path, ancestor: Path) -> bool:
    try:
        candidate.relative_to(ancestor)
    except ValueError:
        return False
    return True


class CodingWorktreeAllocator:
    """Assign one isolated worktree per job, and release it when told to.

    Holds no lifecycle state of its own. Ownership is re-derived from the job
    identity and confirmed against git's own account of the repository, so a
    restart loses nothing and a stale worktree is still recognisably the job's
    when somebody comes back to it.
    """

    def __init__(
        self,
        root: Path,
        repository: Path,
        outcome_source: Callable[[str], str] | None = None,
    ) -> None:
        self._repository = canonical_repository_root(repository)
        self._root = resolve_worktree_root(root, self._repository)
        # What the broker durably recorded for a job, by job id: "succeeded",
        # another status, or "" when it has no record of one. Injected rather
        # than reached for, because the allocator has no business knowing how
        # the goal store is shaped — and because the point of it is that the
        # answer comes from outside anything this module writes.
        self._outcome_source = outcome_source

    @property
    def root(self) -> Path:
        return self._root

    @property
    def repository(self) -> Path:
        return self._repository

    def path_for(self, job_id: str) -> Path:
        """Where this job's worktree lives. Deterministic, never guessed."""
        if not job_id_permitted(job_id):
            raise CodingError("worktree_unusable", reason_code="job_id_not_permitted")
        return self._root / job_id

    def _record_path(self, job_id: str) -> Path:
        return self._root / f"{job_id}{OWNERSHIP_SUFFIX}"

    def record_outcome(self, job_id: str, status: str) -> None:
        """Note how a job ended, beside its worktree.

        Release needs to know whether the job succeeded, and the job that knows
        has already finished by the time Core decides to release. Recorded here
        rather than reconstructed later: a worktree's contents cannot say
        whether the job that filled it passed its verification.

        Failure to record never fails the job. The consequence of an absent
        outcome is a workspace that refuses release, which is the safe
        direction — retained state rather than a removal on an unproven fact.
        """
        record = self.read_record(job_id)
        if record is None:
            return
        record["status"] = str(status)
        record["finished_at"] = datetime.now(UTC).isoformat()
        try:
            self._record_path(job_id).write_text(
                json.dumps(record, indent=2, sort_keys=True) + "\n"
            )
        except OSError:
            return

    def _claim_path(self, slot: str) -> Path:
        return self._root / f"{slot}{CLAIM_SUFFIX}"

    def _claim_slot(self, slot: str, job_id: str, branch: str) -> None:
        """Record that this allocator is about to create this exact directory.

        The claim is AL/X's positive provenance for a slot, and it is the only
        thing that makes a directory under this root *ours*. Being a linked
        worktree of this repository in the right place is not enough: somebody
        can run `git worktree add` there by hand, and D-030 grants no authority
        over a directory AL/X did not create.

        Deliberately not release authority. It says a directory was going to
        exist, not that a job succeeded in it, and `release_authorised` never
        reads it.
        """
        claim = {
            "slot": slot,
            "job_id": job_id,
            "branch": branch,
            "claimed_at": datetime.now(UTC).isoformat(),
        }
        try:
            self._claim_path(slot).write_text(
                json.dumps(claim, indent=2, sort_keys=True) + "\n"
            )
        except OSError as error:
            # Without a claim the directory about to be created could not
            # later be recognised as AL/X's, which is the state this exists to
            # prevent. Refuse before git creates anything.
            raise CodingError(
                "worktree_unusable",
                reason_code="allocation_claim_not_written",
                slot=slot,
            ) from error

    def _withdraw_claim(self, slot: str) -> None:
        """Drop a claim for a directory that was never created."""
        try:
            self._claim_path(slot).unlink(missing_ok=True)
        except OSError:
            LOGGER.warning(
                "Coding worktree claim for %s could not be withdrawn; it names "
                "a directory that does not exist", slot,
            )

    def read_claim(self, slot: str) -> dict[str, object] | None:
        """AL/X's provenance for a slot, or None if it never claimed one."""
        if not job_id_permitted(slot):
            return None
        return self._read_json(self._claim_path(slot))

    def _note_orphan(self, allocated: CodingWorktree, error: OSError) -> None:
        """Leave a marker naming a worktree whose record could not be written.

        Deliberately a *different* file from the allocation record, and one
        `read_record` never reads: an orphan marker must not become a way to
        authorise the release it exists because we could not authorise. It
        says only what the worktree is and why it has no record, so a later
        reader — or Friedl — can identify it rather than finding a directory
        nothing accounts for.

        Best effort. The thing that just failed was writing to this directory,
        so this write may fail too; `orphan_worktrees` finds the directory
        either way by asking git, and the marker only adds the explanation.
        """
        note = {
            "job_id": allocated.job_id,
            "slot": allocated.slot,
            "branch": allocated.branch,
            "base": allocated.base,
            "worktree": str(allocated.path),
            "noted_at": datetime.now(UTC).isoformat(),
            "reason": "allocation record could not be written",
            "error": type(error).__name__,
        }
        try:
            self._orphan_path(allocated.slot).write_text(
                json.dumps(note, indent=2, sort_keys=True) + "\n"
            )
        except OSError:
            LOGGER.warning(
                "Coding worktree %s has neither an allocation record nor an "
                "orphan marker; it is still discoverable through git",
                allocated.path,
            )

    def _orphan_path(self, slot: str) -> Path:
        return self._root / f"{slot}{ORPHAN_SUFFIX}"

    def orphan_worktrees(self) -> tuple[dict[str, object], ...]:
        """Worktrees AL/X created that no allocation record accounts for.

        Three things must all hold, and the first is the one that matters:

        - **AL/X claimed the slot.** The claim is written before git creates
          anything, so it is positive provenance that this allocator made the
          directory. Without it a worktree is somebody else's — a manually
          created one under this root is not an AL/X orphan, and reporting it
          as one would invite acting on a directory D-030 gives no authority
          over;
        - it is a linked worktree of the canonical repository;
        - no readable allocation record accounts for it.

        Discovery therefore survives the record-write failure it exists for:
        the claim is written first and the record last, so the gap between them
        is exactly the state reported here. The orphan marker, when one was
        written, supplies the explanation but is never what makes a directory
        discoverable.

        Reported only. D-030 grants no pruning authority; this removes,
        repairs and reuses nothing, and a claim confers no release authority.
        """
        if not self._root.is_dir():
            return ()
        found: list[dict[str, object]] = []
        for child in sorted(self._root.iterdir()):
            if not child.is_dir() or not job_id_permitted(child.name):
                continue
            claim = self.read_claim(child.name)
            if claim is None or str(claim.get("slot") or "") != child.name:
                continue
            if not worktree_belongs_to_repository(child, self._repository):
                continue
            if self._record_for_slot(child.name) is not None:
                continue
            entry: dict[str, object] = {
                "slot": child.name,
                "job_id": str(claim.get("job_id") or ""),
                "worktree": str(child),
                "branch": worktree_branch(child),
                "claimed_at": str(claim.get("claimed_at") or ""),
            }
            marker = self._read_json(self._orphan_path(child.name))
            if marker is not None:
                entry["noted"] = marker
            found.append(entry)
        return tuple(found)

    def _record_for_slot(self, slot: str) -> dict[str, object] | None:
        """The allocation record whose slot is this directory, if there is one.

        A slot is `job_id` plus an optional collision suffix, so the record
        that owns `job-1-3` is `job-1`'s. Both are tried rather than parsed.
        """
        for candidate in (slot, *_slot_owners(slot)):
            record = self.read_record(candidate)
            if record is not None and str(record.get("slot") or candidate) == slot:
                return record
        return None

    def _write_record(self, allocated: CodingWorktree) -> None:
        """Record what was allocated, for a reader that comes back later.

        Deliberately not authority on its own: `owns` asks git whether the
        directory is a linked worktree of this repository, and this file only
        adds what git cannot say — which job it was allocated to, and when.
        A missing or unreadable record therefore refuses a release rather than
        being reconstructed from the filesystem.
        """
        record = {
            "job_id": allocated.job_id,
            "slot": allocated.slot,
            "branch": allocated.branch,
            # The two facts that make the branch checkable later without
            # parsing it: which attempt produced it, and the base name that
            # attempt suffixed. Together they reconstruct `branch` exactly.
            "attempt": allocated.attempt,
            "base_branch": _unsuffixed(allocated.branch, allocated.attempt),
            "base": allocated.base,
            "worktree": str(allocated.path),
            "allocated_at": datetime.now(UTC).isoformat(),
        }
        self._record_path(allocated.job_id).write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n"
        )

    def read_record(self, job_id: str) -> dict[str, object] | None:
        """What this allocator recorded for a job, or None if nothing did."""
        if not job_id_permitted(job_id):
            return None
        return self._read_json(self._record_path(job_id))

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        try:
            raw = path.read_text()
        except (OSError, ValueError):
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def candidate_slots(self, job_id: str) -> tuple[str, ...]:
        """Every directory name this allocator could have given this job.

        The same deterministic sequence `allocate` walks — `job-1`, `job-1-2`,
        and so on — so a slot is something re-derived from the job identity
        rather than something read back out of a file. A record naming a slot
        outside this sequence is naming a directory this job could never have
        been allocated, which is what makes tampering detectable.
        """
        if not job_id_permitted(job_id):
            return ()
        slots = [job_id]
        for attempt in range(2, MAX_REPAIR_BRANCH_ATTEMPTS + 1):
            candidate = f"{job_id}-{attempt}"
            if job_id_permitted(candidate):
                slots.append(candidate)
        return tuple(slots)

    def worktree_of(self, job_id: str) -> Path | None:
        """The directory allocated to this job, proved rather than believed.

        The record says which slot the job got, because a collision moves the
        directory without moving the job and nothing else remembers that. But
        the record is an ordinary file in an ordinary directory, so it is
        treated as a *claim* to be checked, never as authority:

        - the slot must be one `allocate` could have produced for this job_id;
        - the record's own `job_id` and `worktree` must agree with it;
        - git must agree the directory is a linked worktree of the canonical
          repository, checked out on the branch the record names.

        A record edited to name another job's slot fails the first check, and
        one edited to name another job's slot *and* branch fails the last: the
        branch git reports is that other job's, and it is not the branch this
        job's record can name without also failing the slot check. Returns
        None whenever any of it does not line up, which refuses the release.
        """
        record = self.read_record(job_id)
        if record is None:
            return None
        slot = str(record.get("slot") or job_id)
        if slot not in self.candidate_slots(job_id):
            return None
        path = self._root / slot
        if str(record.get("job_id") or "") != job_id:
            return None
        if str(record.get("worktree") or "") != str(path):
            return None
        return path

    def stale_job_ids(self) -> tuple[str, ...]:
        """Every job whose worktree is still on disk, newest name order aside.

        Reported so retained state is visible rather than merely present.
        Nothing here removes or repairs anything: D-030 grants no pruning
        authority, and a stale worktree is evidence until somebody decides
        otherwise.
        """
        if not self._root.is_dir():
            return ()
        found: list[str] = []
        for child in sorted(self._root.iterdir()):
            if child.is_dir() and job_id_permitted(child.name):
                found.append(child.name)
        return tuple(found)

    def branch_for(self, job_id: str, requested: str = "") -> str:
        """The base branch name for this job, before D-029's collision scheme.

        Core may name the repair branch, as D-029 already allows. It is a
        judgement about what the repair is, so it is kept. When Core named
        nothing, the job still needs a branch to be isolated on, and that name
        is mechanical rather than meaningful.
        """
        chosen = requested.strip()
        if chosen:
            if not branch_name_permitted(chosen):
                raise CodingError(
                    "git_refused", reason_code="branch_name_not_permitted"
                )
            return chosen
        if not job_id_permitted(job_id):
            raise CodingError("worktree_unusable", reason_code="job_id_not_permitted")
        derived = f"{JOB_BRANCH_PREFIX}/{job_id}"
        if not branch_name_permitted(derived):
            raise CodingError("git_refused", reason_code="branch_name_not_permitted")
        return derived

    def allocate(self, job_id: str, requested_branch: str = "") -> CodingWorktree:
        """Create this job's branch and worktree in one command.

        The suffix search is D-029's, applied to both names together: a job's
        worktree path and its branch are allocated by the same attempt, so the
        two can never disagree about which suffix won.
        """
        if not job_id_permitted(job_id):
            raise CodingError("worktree_unusable", reason_code="job_id_not_permitted")
        # Re-resolve rather than trusting the value proved at construction. A
        # root that became a symlink into the repository after startup is
        # refused here, so the invariant holds per allocation and not merely
        # per process.
        root = resolve_worktree_root(self._root, self._repository)
        # The claim below is written into this directory before git creates
        # anything, so it has to exist first.
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CodingError(
                "worktree_unusable", reason_code="worktree_root_not_writable"
            ) from error
        base_branch = self.branch_for(job_id, requested_branch)
        base_commit = read_head_sha(self._repository)
        for attempt in range(1, MAX_REPAIR_BRANCH_ATTEMPTS + 1):
            suffix = "" if attempt == 1 else f"-{attempt}"
            candidate_id = f"{job_id}{suffix}"
            candidate_branch = f"{base_branch}{suffix}"
            if not branch_name_permitted(candidate_branch):
                raise CodingError(
                    "git_refused", reason_code="branch_name_not_permitted"
                )
            path = root / candidate_id
            if path.exists() or self._claim_path(candidate_id).exists():
                # A retained worktree from an earlier job of this identity, or
                # a slot an earlier attempt already claimed. D-030 keeps both,
                # so this attempt yields rather than reusing or removing them.
                continue
            # Claim the slot *before* git creates anything. This is the
            # provenance orphan discovery reads: a directory is AL/X's only if
            # this allocator said it was about to create it. Written first so
            # it survives the record-write failure it exists to explain, and
            # so a crash between the two leaves a claim rather than a
            # directory nothing can account for.
            self._claim_slot(candidate_id, job_id, candidate_branch)
            try:
                created = allocate_job_worktree(
                    self._repository, path, candidate_branch, base_commit
                )
            except CodingError:
                # git refused for a reason that is not a name collision. The
                # claim describes a directory that was never created, so it is
                # withdrawn: leaving it would make the next allocation skip a
                # free slot and would name an orphan that does not exist.
                self._withdraw_claim(candidate_id)
                raise
            if not created:
                # The branch or path was taken. Same reasoning: nothing was
                # created under this claim, so it is withdrawn before the
                # collision scheme moves to the next attempt.
                self._withdraw_claim(candidate_id)
            if created:
                allocated = CodingWorktree(
                    job_id=job_id,
                    path=path,
                    branch=candidate_branch,
                    base=base_commit,
                    slot=candidate_id,
                    attempt=attempt,
                )
                try:
                    self._write_record(allocated)
                except OSError as error:
                    # The worktree exists and its record does not. D-030 does
                    # not authorise deleting it — removal needs an explicit
                    # Core release, and there is now no record to prove this
                    # one is releasable — so it is kept and made findable
                    # instead. Without this the directory would be an orphan
                    # nothing could explain: a linked worktree on a real
                    # branch, belonging to a job no file names.
                    self._note_orphan(allocated, error)
                    raise CodingError(
                        "worktree_unusable",
                        reason_code="allocation_record_not_written",
                        worktree=str(path),
                        branch=candidate_branch,
                    ) from error
                return allocated
        raise CodingError(
            "git_refused",
            reason_code="worktree_attempts_exhausted",
            attempts=MAX_REPAIR_BRANCH_ATTEMPTS,
        )

    def owns(self, path: Path) -> bool:
        """Whether this path is a worktree this allocator would have created.

        Structural only: it says the path sits directly under the configured
        root and is a linked worktree of the canonical repository. Whether the
        *job* may release it is a separate question the release capability
        answers from the job's own record.
        """
        try:
            resolved = Path(path).expanduser().resolve()
        except OSError:
            return False
        if resolved.parent != self._root:
            return False
        if not resolved.is_dir():
            return False
        return worktree_belongs_to_repository(resolved, self._repository)

    def release(self, job_id: str, path: Path) -> None:
        """Remove one job's worktree, having proved it is that job's.

        Every check fails closed and removes nothing. The path is re-derived
        from the job identity rather than taken on trust, so a release can only
        ever remove the directory this allocator would itself have allocated
        for that job.
        """
        if not job_id_permitted(job_id):
            raise CodingError("worktree_unusable", reason_code="job_id_not_permitted")
        try:
            resolved = Path(path).expanduser().resolve()
        except OSError as error:
            raise CodingError(
                "worktree_unusable", reason_code="worktree_unresolvable"
            ) from error
        # The directory this job actually got, which a collision may have
        # suffixed. Re-derived from the record rather than taken from the
        # caller, so the path is still this allocator's answer and not theirs.
        expected = self.worktree_of(job_id)
        if expected is None or resolved != expected:
            raise CodingError(
                "worktree_unusable", reason_code="worktree_not_owned_by_job"
            )
        if resolved == self._repository or _is_descendant(resolved, self._repository):
            raise CodingError(
                "worktree_unusable", reason_code="worktree_inside_repository"
            )
        if not self.owns(resolved):
            raise CodingError(
                "worktree_unusable", reason_code="worktree_not_allocated_here"
            )
        record = self.read_record(job_id)
        if record is None:
            raise CodingError(
                "worktree_unusable", reason_code="allocation_record_missing"
            )
        # Everything the record claims is checked against something that is not
        # the record. The slot came back from `worktree_of` only because it is
        # in this job's own deterministic sequence; the branch is git's answer,
        # not the file's; and the base must be a real commit in the canonical
        # repository. A record edited to point at another job fails here rather
        # than redirecting the removal onto that job's worktree.
        claimed_branch = str(record.get("branch") or "")
        actual_branch = worktree_branch(resolved)
        if not claimed_branch or claimed_branch != actual_branch:
            raise CodingError(
                "worktree_unusable",
                reason_code="allocation_record_conflicts",
                detail="branch",
            )
        # The branch must also be the one this job could have been given: Core
        # may name it, so it is not derivable, but it must at least belong to
        # the same slot the record survived the slot check with.
        if not self._branch_matches_slot(record, claimed_branch):
            raise CodingError(
                "worktree_unusable",
                reason_code="allocation_record_conflicts",
                detail="branch_slot",
            )
        if not self._base_is_known(record):
            raise CodingError(
                "worktree_unusable",
                reason_code="allocation_record_conflicts",
                detail="base",
            )
        release_job_worktree(self._repository, resolved)
        # Only after git removed the worktree. A record deleted first would
        # leave an unreleasable directory behind if removal then refused, and a
        # claim deleted first would leave a directory AL/X no longer recognises
        # as its own.
        self._record_path(job_id).unlink(missing_ok=True)
        self._withdraw_claim(resolved.name)

    def _require_durable_success(
        self, job_id: str, record: dict[str, object]
    ) -> None:
        """Refuse unless the durable outcome says this job succeeded.

        The authority is `outcome_source`, supplied by the runtime and reading
        the broker's own record of what the capability returned. That record
        lives in the durable goal store, is written when the job finished, and
        is not a file in the coding-worktree root — so it is not editable by
        whatever can edit the allocation sidecar.

        Fails closed in both directions: no source configured, no durable
        outcome found, or an outcome that is not `succeeded` all refuse. A
        release that cannot prove success does not happen.
        """
        if self._outcome_source is None:
            raise CodingError(
                "job_not_successful",
                reason_code="durable_outcome_unavailable",
            )
        try:
            durable = self._outcome_source(job_id)
        except Exception as error:  # noqa: BLE001 - an unreadable answer is a refusal
            raise CodingError(
                "job_not_successful",
                reason_code="durable_outcome_unreadable",
            ) from error
        if not durable:
            raise CodingError(
                "job_not_successful",
                reason_code="durable_outcome_missing",
            )
        if str(durable) != "succeeded":
            raise CodingError(
                "job_not_successful",
                reason_code="job_did_not_succeed",
                status=str(durable),
            )
        # The sidecar stays descriptive, but it may not *contradict* the
        # durable outcome: disagreement means one of the two is wrong about
        # this job, and D-030 refuses rather than choosing which to believe.
        recorded = str(record.get("status") or "")
        if recorded and recorded != "succeeded":
            raise CodingError(
                "job_not_successful",
                reason_code="outcome_record_conflicts",
                status=recorded,
            )

    def _branch_matches_slot(
        self, record: dict[str, object], branch: str
    ) -> bool:
        """Whether the branch is the one this recorded allocation produced.

        Reconstructed, never parsed. `allocate` suffixes the slot and the
        branch together on the same pass, so the recorded attempt number plus
        the recorded base branch name rebuild both names exactly — and they
        must match the slot the record survived the slot check with and the
        branch git reports for the directory.

        Parsing a trailing `-N` off the final branch name is what this
        replaces, and it was wrong in both directions: `fix-2` is a legitimate
        base name on attempt 1, indistinguishable by inspection from `fix`
        suffixed on attempt 2. D-029's naming and collision semantics are
        untouched; only the way the result is checked afterwards changed.
        """
        slot = str(record.get("slot") or "")
        job_id = str(record.get("job_id") or "")
        base_branch = str(record.get("base_branch") or "")
        attempt = record.get("attempt")
        if not slot or not job_id or not base_branch:
            return False
        if not isinstance(attempt, int) or isinstance(attempt, bool):
            return False
        if not 1 <= attempt <= MAX_REPAIR_BRANCH_ATTEMPTS:
            return False
        suffix = "" if attempt == 1 else f"-{attempt}"
        return slot == f"{job_id}{suffix}" and branch == f"{base_branch}{suffix}"

    def _base_is_known(self, record: dict[str, object]) -> bool:
        """Whether the recorded start point is a real commit in the repository.

        A base the canonical repository has never heard of means the record is
        describing an allocation this repository did not make.
        """
        base = str(record.get("base") or "")
        if not base:
            return False
        return commit_exists(self._repository, base)

    def release_authorised(self, job_id: str) -> dict[str, object]:
        """Release one job's workspace on Core's explicit instruction.

        Every D-030 release check runs here, and each refuses before anything
        is removed. The job must have finished successfully: a failed,
        cancelled or still-running job keeps its worktree, because that
        worktree is the evidence of what went wrong.

        Whether it succeeded is asked of the durable capability outcome, not of
        the allocation record. The record's `status` is audit metadata written
        beside the workspace, and editing `failed` to `succeeded` in it used to
        be enough to release a failed job's evidence. What a job did is
        recorded by the broker when the capability returned, outside anything
        this module writes, and that is what is consulted.
        """
        if not job_id_permitted(job_id):
            raise CodingError("worktree_unusable", reason_code="job_id_not_permitted")
        record = self.read_record(job_id)
        if record is None:
            raise CodingError(
                "worktree_unusable", reason_code="allocation_record_missing"
            )
        self._require_durable_success(job_id, record)
        path = self.worktree_of(job_id)
        if path is None:
            raise CodingError(
                "worktree_unusable", reason_code="allocation_record_missing"
            )
        self.release(job_id, path)
        return {
            "job_id": job_id,
            "worktree": str(path),
            "branch": str(record.get("branch", "")),
            "released": True,
        }


__all__ = [
    "CodingWorktree",
    "CodingWorktreeAllocator",
    "JOB_BRANCH_PREFIX",
    "MAX_JOB_ID_CHARACTERS",
    "ORPHAN_SUFFIX",
    "OWNERSHIP_SUFFIX",
    "job_id_permitted",
    "resolve_worktree_root",
]
