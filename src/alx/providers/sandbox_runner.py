"""The one place in AL/X where a process is executed, under D-027.

`SandboxRunner` is the abstraction and macOS with Seatbelt is the current
development implementation. A future runner backed by Linux namespaces and
cgroups, a container or a virtual machine can satisfy the stronger production
containment requirement without changing the capability, the broker, the
SafetyGate, the Core or the session model. Only this file's implementation
changes. D-027 does not choose that host.

Law 0 requires that there be exactly one production execution path, and a test
asserts that no other production module contains a process-execution call.

What is enforced here, and what is not:

Filesystem and network confinement come from the kernel through Seatbelt, and
were verified by test rather than assumed. CPU time, file size and process
count come from POSIX rlimits and were verified effective. The wall clock is
enforced by the parent, escalating from SIGTERM to SIGKILL across the child's
own process group so a run cannot outlive its call.

**There is no memory ceiling.** RLIMIT_AS is ineffective on macOS arm64 — a
child allocated 600 MB against a 256 MB limit — so it is not applied at all
rather than applied and believed. D-027 records this as a limitation of the
current development host. Nothing here should be described as bounding memory,
and a runaway allocation may require this process or the server to be killed
externally.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import resource
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from alx.contracts.sandbox import (
    MAX_FILE_BYTES,
    MAX_PROCESSES,
    MAX_REPORTED_ARTIFACTS,
    MAX_STDERR_CHARACTERS,
    MAX_STDOUT_CHARACTERS,
    ArtifactMetadata,
    SandboxError,
    SandboxOutcome,
    SandboxRequest,
)
from alx.providers.sandbox_workspace import SandboxWorkspace, SessionPaths


LOGGER = logging.getLogger(__name__)

# Read caps on the captured streams. Ten times what is returned to the Core, so
# the recorded digest and byte length describe a real bound rather than an
# unbounded file, while truncation of the returned text stays visible.
_MAX_CAPTURED_BYTES = 10 * 1024 * 1024

# Grace between asking a process group to stop and insisting.
_TERM_GRACE_SECONDS = 2.0


class SandboxRunner(ABC):
    """One confined execution. The platform boundary of the sandbox."""

    @abstractmethod
    def available(self) -> bool:
        """Whether this platform can actually confine a process."""

    @abstractmethod
    def run(self, request: SandboxRequest, paths: SessionPaths) -> SandboxOutcome:
        """Execute one program and return what it did."""


def _truncate(value: str, limit: int) -> tuple[str, int]:
    if len(value) <= limit:
        return value, 0
    return value[:limit], len(value) - limit


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SeatbeltSandboxRunner(SandboxRunner):
    """macOS confinement through `sandbox-exec`, plus rlimits and a timeout.

    The profile denies everything by default, then allows reads broadly while
    denying the paths that matter and granting writes only inside the session's
    own state directory. Reads are broad because a restricted read profile
    cannot start the interpreter at all: CPython aborts before it can report
    why. Denying the repository, the runtime storage and the user's private
    keys explicitly is what makes that acceptable, and each denial is tested.
    """

    def __init__(
        self,
        workspace: SandboxWorkspace,
        denied_read_paths: tuple[Path, ...] = (),
        interpreter: str | None = None,
        sandbox_exec: str = "/usr/bin/sandbox-exec",
    ) -> None:
        self._workspace = workspace
        self._denied = tuple(Path(item).resolve() for item in denied_read_paths)
        self._interpreter = interpreter or sys.executable
        self._sandbox_exec = sandbox_exec

    def available(self) -> bool:
        return (
            sys.platform == "darwin"
            and Path(self._sandbox_exec).exists()
            and Path(self._interpreter).exists()
        )

    def profile(self, state: Path) -> str:
        """The Seatbelt profile applied to one run.

        Written as a method so a test can assert the exact rules rather than
        infer them from behaviour alone.
        """
        denials = "\n".join(
            f'(deny file-read* (subpath "{path}"))' for path in self._denied
        )
        return (
            "(version 1)\n"
            "(deny default)\n"
            "(allow process-fork)\n"
            "(allow process-exec)\n"
            "(allow sysctl-read)\n"
            "(allow mach-lookup)\n"
            "(allow signal (target self))\n"
            "(allow file-read*)\n"
            f"{denials}\n"
            f'(allow file-write* (subpath "{state}"))\n'
            '(allow file-write-data (literal "/dev/null"))\n'
            "(deny network*)\n"
        )

    @staticmethod
    def _limits() -> None:
        """Applied in the child between fork and exec.

        RLIMIT_AS is deliberately absent. It does not work on macOS arm64, and
        setting it would create the appearance of a memory bound that the
        kernel ignores.
        """
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))

    def run(self, request: SandboxRequest, paths: SessionPaths) -> SandboxOutcome:
        if not self.available():
            raise SandboxError("sandbox_unavailable")

        entry = paths.source_directory / request.entry_filename
        profile_path = paths.run_directory / "profile.sb"
        stdout_path = paths.run_directory / "stdout.log"
        stderr_path = paths.run_directory / "stderr.log"
        try:
            entry.write_text(request.source, encoding="utf-8")
            profile_path.write_text(self.profile(paths.session_state), encoding="utf-8")
            # The program runs from a copy inside the session state, so an
            # experiment can import what an earlier run left beside it.
            working_copy = paths.session_state / request.entry_filename
            shutil.copyfile(entry, working_copy)
        except OSError as error:
            raise SandboxError("workspace_unavailable") from error

        before = self._workspace.walk(paths.session_state)

        # -E ignores PYTHONPATH and friends, and -S skips site packages, so the
        # program starts from a predictable stdlib-only interpreter. -I is
        # deliberately not used: it also strips the script's own directory from
        # sys.path, which would stop a run importing a helper an earlier run in
        # the same session wrote, and that iteration is the point of a session.
        argv = [
            self._sandbox_exec,
            "-f",
            str(profile_path),
            self._interpreter,
            "-E",
            "-S",
            str(working_copy),
        ]
        started_at = datetime.now(UTC)
        started = time.monotonic()
        timed_out = False
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            process = subprocess.Popen(  # noqa: S603 - the one execution site
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                cwd=str(paths.session_state),
                # Nothing is inherited. Not os.environ, not the .env mapping.
                env={},
                preexec_fn=self._limits,
                # Its own process group, so a child that spawned helpers dies
                # with it rather than surviving the call.
                start_new_session=True,
            )
            try:
                process.wait(timeout=request.wall_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
        elapsed = time.monotonic() - started
        finished_at = datetime.now(UTC)

        status = process.returncode if process.returncode is not None else -signal.SIGKILL
        after = self._workspace.walk(paths.session_state)
        artifacts = self._workspace.changes(before, after)
        reported = artifacts[:MAX_REPORTED_ARTIFACTS]

        stdout_bytes = self._read(stdout_path)
        stderr_bytes = self._read(stderr_path)
        stdout, stdout_omitted = _truncate(
            stdout_bytes.decode("utf-8", "replace"), MAX_STDOUT_CHARACTERS
        )
        stderr, stderr_omitted = _truncate(
            stderr_bytes.decode("utf-8", "replace"), MAX_STDERR_CHARACTERS
        )

        outcome = SandboxOutcome(
            experiment_id=request.experiment_id,
            session_id=request.session_id,
            run_id=request.run_id,
            exit_status=status,
            signalled=status < 0,
            timed_out=timed_out,
            stdout=stdout,
            stderr=stderr,
            stdout_omitted_characters=stdout_omitted,
            stderr_omitted_characters=stderr_omitted,
            stdout_digest=_digest(stdout_bytes),
            stderr_digest=_digest(stderr_bytes),
            stdout_byte_size=len(stdout_bytes),
            stderr_byte_size=len(stderr_bytes),
            artifacts=reported,
            artifacts_omitted=len(artifacts) - len(reported),
            wall_seconds_used=elapsed,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._write_manifest(request, paths, outcome, before, after, argv)
        return outcome

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Stop the whole process group, insisting if asked politely fails."""
        for sender in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(os.getpgid(process.pid), sender)
            except (ProcessLookupError, PermissionError):
                return
            try:
                process.wait(timeout=_TERM_GRACE_SECONDS)
                return
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            with path.open("rb") as handle:
                return handle.read(_MAX_CAPTURED_BYTES)
        except OSError as error:
            raise SandboxError("output_unreadable") from error

    def _write_manifest(
        self,
        request: SandboxRequest,
        paths: SessionPaths,
        outcome: SandboxOutcome,
        before: dict[str, tuple[str, int]],
        after: dict[str, tuple[str, int]],
        argv: list[str],
    ) -> None:
        """The bounded record that outlives the bytes it describes.

        Contains no experiment-authored free text: hashes, sizes, argv, the
        limits in force and timings. After retention removes the source and the
        output, this still answers what ran, when, under which limits, and what
        it produced.
        """
        manifest = {
            "experiment_id": request.experiment_id,
            "session_id": request.session_id,
            "run_id": request.run_id,
            "entry_filename": request.entry_filename,
            "source_digest": _digest(request.source.encode("utf-8")),
            "source_byte_size": len(request.source.encode("utf-8")),
            "argv": argv,
            "limits": {
                "wall_seconds": request.wall_seconds,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_processes": MAX_PROCESSES,
                # Recorded as absent rather than omitted, so the manifest never
                # implies a memory ceiling that does not exist.
                "memory_ceiling": None,
            },
            "confinement": "seatbelt",
            "profile_digest": _digest(
                self.profile(paths.session_state).encode("utf-8")
            ),
            "exit_status": outcome.exit_status,
            "signalled": outcome.signalled,
            "timed_out": outcome.timed_out,
            "stdout_digest": outcome.stdout_digest,
            "stderr_digest": outcome.stderr_digest,
            "stdout_byte_size": outcome.stdout_byte_size,
            "stderr_byte_size": outcome.stderr_byte_size,
            "state_before": {name: list(value) for name, value in before.items()},
            "state_after": {name: list(value) for name, value in after.items()},
            "artifacts": [item.as_values() for item in outcome.artifacts],
            "artifacts_omitted": outcome.artifacts_omitted,
            "wall_seconds_used": round(outcome.wall_seconds_used, 3),
            "started_at": outcome.started_at.isoformat(),
            "finished_at": outcome.finished_at.isoformat(),
        }
        try:
            paths.manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
        except OSError as error:
            raise SandboxError("output_unreadable") from error
