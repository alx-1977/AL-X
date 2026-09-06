"""Bounds, identity and evidence records for isolated experimentation, under D-027.

The sandbox is where AL/X can find out what a program actually does rather
than predict it. Everything here describes the boundary around that: what may
be run, for how long, what comes back, and what survives.

Three identifiers carry three different meanings. An experiment is a line of
enquiry. A session is one iterative working context within it, and is the
reason a later run can build on what an earlier run produced. A run is one
execution and its immutable evidence. Collapsing any two of them would either
lose iteration or lose the audit.

Nothing in this module executes anything, touches the filesystem, or decides
whether an experiment was any good. It defines the facts the runner produces
and the limits it must hold to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# One run at a time, bounded so a mistake cannot occupy the machine. D-027
# halved the first proposal: an experiment needing a minute is doing something
# V1 is not for.
DEFAULT_WALL_SECONDS = 30
MAX_WALL_SECONDS = 60

# Daily fuses, deliberately low. Twenty runs is enough for a genuine iterative
# session and small enough that a repeating mistake burns the fuse quickly and
# visibly. They are independent invariants: many quick runs exhaust the count,
# one slow run exhausts the seconds.
DAILY_RUNS = 20
DAILY_WALL_SECONDS = 300

# What returns to the Core. The Core's input is finite and expensive, and an
# autonomous turn refuses rather than truncates when its ceiling is exceeded.
# These bound context, and say nothing about which output mattered.
MAX_STDOUT_CHARACTERS = 8_000
MAX_STDERR_CHARACTERS = 4_000
MAX_REPORTED_ARTIFACTS = 50

# Bounds on the hash walk itself, so a run that creates a hundred thousand
# files cannot make the audit step the expensive part of the call.
MAX_WALKED_FILES = 5_000
MAX_WORKSPACE_BYTES = 64 * 1024 * 1024

# Per-process limits applied through setrlimit. RLIMIT_AS is deliberately
# absent: it is ineffective on macOS arm64, and applying it would assert a
# memory ceiling the kernel does not enforce. D-027 records that as a
# development-host limitation rather than pretending it is bounded.
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PROCESSES = 32

# The source AL/X writes. Bounded because it is transported and hashed, not
# because length says anything about quality.
MAX_SOURCE_CHARACTERS = 64_000

# Identifiers name directories, so they are constrained to what cannot escape
# one. This is the first of three independent traversal defences; the kernel
# profile and the resolved-path check are the others.
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# Entry filenames are single path segments with a Python suffix. No directory
# component can appear, so an entry name cannot reach out of the workspace.
_ENTRY_FILENAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\.py$")


SANDBOX_FAILURES = (
    "arguments_unusable",
    "sandbox_unavailable",
    "workspace_unavailable",
    "budget_exhausted",
    "execution_timeout",
    "output_unreadable",
    "ledger_corrupt",
    # The session filled beyond its permitted size. Distinct from a per-file
    # limit: many small files can exhaust a workspace while every write stays
    # legal.
    "workspace_exhausted",
    # Another run holds this session. Sessions are iterative state, so two
    # concurrent runs in one would race over the same files.
    "session_busy",
)


class SandboxError(Exception):
    """A run could not be performed, with a declared machine-readable code."""

    def __init__(self, code: str) -> None:
        if code not in SANDBOX_FAILURES:
            raise ValueError("sandbox failures must be declared")
        self.code = code
        super().__init__(code)


def valid_identifier(value: str) -> bool:
    return isinstance(value, str) and _IDENTIFIER.match(value) is not None


def valid_entry_filename(value: str) -> bool:
    return isinstance(value, str) and _ENTRY_FILENAME.match(value) is not None


def _identifier(value: str, name: str) -> None:
    if not valid_identifier(value):
        raise ValueError(f"{name} must be lowercase alphanumeric with hyphens")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


class FileChange(str, Enum):
    """What happened to one file across a run.

    Derived by comparing two hash walks taken by the parent process, never
    reported by the experiment itself.
    """

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """One file a run created or changed. Never its content."""

    name: str
    change: FileChange
    byte_size: int
    digest: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("artifact name must not be blank")
        if not isinstance(self.change, FileChange):
            raise TypeError("change must be a FileChange")
        if not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool):
            raise TypeError("byte_size must be an integer")
        if self.byte_size < 0:
            raise ValueError("byte_size must not be negative")
        # A digest is required wherever there is content to hash, because it is
        # what makes the durable record verifiable after the bytes are gone.
        #
        # Two cases legitimately have none: a deleted file, and a symbolic
        # link, whose target is never opened. An earlier version required a
        # digest for every non-deleted entry, which made any run that created a
        # symlink raise after execution and lose both its result and its
        # manifest. A link is ordinary evidence, not a failure.
        if self.digest and len(self.digest) != 64:
            raise ValueError("artifact digest must be a sha256 hex digest")
        if self.change is FileChange.DELETED and self.digest:
            raise ValueError("a deleted artifact has no digest")

    def as_values(self) -> dict[str, object]:
        return {
            "name": self.name,
            "change": self.change.value,
            "byte_size": self.byte_size,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    """One experiment AL/X has decided to run.

    `source` is a program she wrote. It is not conversational language and is
    never interpreted here: this module bounds and identifies it, and the
    runner writes it to a file.
    """

    experiment_id: str
    session_id: str
    run_id: str
    source: str
    entry_filename: str = "experiment.py"
    wall_seconds: int = DEFAULT_WALL_SECONDS

    def __post_init__(self) -> None:
        _identifier(self.experiment_id, "experiment_id")
        _identifier(self.session_id, "session_id")
        _identifier(self.run_id, "run_id")
        if not self.source.strip():
            raise ValueError("source must not be blank")
        if len(self.source) > MAX_SOURCE_CHARACTERS:
            raise ValueError("source exceeds the permitted size")
        if not valid_entry_filename(self.entry_filename):
            raise ValueError("entry filename must be a plain .py segment")
        if not isinstance(self.wall_seconds, int) or isinstance(self.wall_seconds, bool):
            raise TypeError("wall_seconds must be an integer")
        if not 1 <= self.wall_seconds <= MAX_WALL_SECONDS:
            raise ValueError("wall_seconds must be within the permitted bound")


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    """What one run did. Evidence about a program, not a claim about the world."""

    experiment_id: str
    session_id: str
    run_id: str
    exit_status: int
    signalled: bool
    timed_out: bool
    stdout: str
    stderr: str
    stdout_omitted_characters: int
    stderr_omitted_characters: int
    stdout_digest: str
    stderr_digest: str
    stdout_byte_size: int
    stderr_byte_size: int
    artifacts: tuple[ArtifactMetadata, ...]
    artifacts_omitted: int
    wall_seconds_used: float
    started_at: datetime
    finished_at: datetime
    # Whether the captured stream was larger than the runner could read, in
    # which case the digest and byte size describe a prefix. Recorded rather
    # than assumed false, so evidence is never silently partial.
    stdout_capped: bool = False
    stderr_capped: bool = False

    def __post_init__(self) -> None:
        _identifier(self.experiment_id, "experiment_id")
        _identifier(self.session_id, "session_id")
        _identifier(self.run_id, "run_id")
        _aware(self.started_at, "started_at")
        _aware(self.finished_at, "finished_at")
        if len(self.stdout) > MAX_STDOUT_CHARACTERS:
            raise ValueError("stdout exceeds the reported bound")
        if len(self.stderr) > MAX_STDERR_CHARACTERS:
            raise ValueError("stderr exceeds the reported bound")
        if len(self.artifacts) > MAX_REPORTED_ARTIFACTS:
            raise ValueError("too many artifacts reported")
        if any(not isinstance(item, ArtifactMetadata) for item in self.artifacts):
            raise TypeError("artifacts must be ArtifactMetadata records")
        for count, name in (
            (self.stdout_omitted_characters, "stdout_omitted_characters"),
            (self.stderr_omitted_characters, "stderr_omitted_characters"),
            (self.artifacts_omitted, "artifacts_omitted"),
        ):
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for digest, name in (
            (self.stdout_digest, "stdout_digest"),
            (self.stderr_digest, "stderr_digest"),
        ):
            if len(digest) != 64:
                raise ValueError(f"{name} must be a sha256 hex digest")
        if self.wall_seconds_used < 0:
            raise ValueError("wall_seconds_used must not be negative")

    def as_values(self) -> dict[str, object]:
        """Everything the Core sees for one turn, including the output itself."""
        return {
            **self.durable_values(),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "artifacts": [item.as_values() for item in self.artifacts],
        }

    def durable_values(self) -> dict[str, object]:
        """What survives in goal state: no experiment-authored bytes at all.

        Every field here is an identifier, an integer, a boolean, a timestamp
        or a hash. `durable_values` on a CapabilityResult defaults to the whole
        result, so a capability that did not override it would persist every
        byte an experiment printed into durable goal state forever. There is
        deliberately no field here that output could occupy.

        The digests are what keep this honest rather than merely small: the
        record proves what the output was without retaining it.
        """
        return {
            "experiment_id": self.experiment_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "exit_status": self.exit_status,
            "signalled": self.signalled,
            "timed_out": self.timed_out,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
            "stdout_byte_size": self.stdout_byte_size,
            "stderr_byte_size": self.stderr_byte_size,
            "stdout_omitted_characters": self.stdout_omitted_characters,
            "stderr_omitted_characters": self.stderr_omitted_characters,
            "stdout_capped": self.stdout_capped,
            "stderr_capped": self.stderr_capped,
            "artifact_count": len(self.artifacts),
            "artifacts_omitted": self.artifacts_omitted,
            "wall_seconds_used": round(self.wall_seconds_used, 3),
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
        }


__all__ = [
    "ArtifactMetadata",
    "DAILY_RUNS",
    "DAILY_WALL_SECONDS",
    "DEFAULT_WALL_SECONDS",
    "FileChange",
    "MAX_FILE_BYTES",
    "MAX_PROCESSES",
    "MAX_REPORTED_ARTIFACTS",
    "MAX_SOURCE_CHARACTERS",
    "MAX_STDERR_CHARACTERS",
    "MAX_STDOUT_CHARACTERS",
    "MAX_WALKED_FILES",
    "MAX_WALL_SECONDS",
    "MAX_WORKSPACE_BYTES",
    "SANDBOX_FAILURES",
    "SandboxError",
    "SandboxOutcome",
    "SandboxRequest",
    "valid_entry_filename",
    "valid_identifier",
]
