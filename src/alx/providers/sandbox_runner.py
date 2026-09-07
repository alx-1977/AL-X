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
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from alx.contracts.sandbox import (
    MAX_FILE_BYTES,
    MAX_WORKSPACE_BYTES,
    MAX_PROCESSES,
    MAX_REPORTED_ARTIFACTS,
    MAX_STDERR_CHARACTERS,
    MAX_STDOUT_CHARACTERS,
    ArtifactMetadata,
    SandboxError,
    SandboxOutcome,
    SandboxRequest,
)
from alx.providers.sandbox_workspace import SandboxWorkspace, SessionPaths, WalkResult


LOGGER = logging.getLogger(__name__)

# How much of a captured stream is held in memory. The digest and byte size
# always cover the whole file, streamed, so this bounds memory rather than
# evidence; a stream longer than this is still hashed in full and reported as
# capped.
_MAX_CAPTURED_BYTES = MAX_FILE_BYTES

# Streaming read size for hashing a captured stream without holding it all.
_READ_CHUNK = 1024 * 1024

# Grace between asking a process group to stop and insisting.
_TERM_GRACE_SECONDS = 2.0

# How much CPU time a run may use beyond its wall clock. Small: an ordinary run
# is bounded by the wall timeout, and RLIMIT_CPU is the backstop for a process
# that would otherwise burn a core for the whole window.
CPU_GRACE_SECONDS = 5


class SandboxRunner(ABC):
    """One confined execution. The platform boundary of the sandbox."""

    @abstractmethod
    def available(self) -> bool:
        """Whether this platform can actually confine a process."""

    @abstractmethod
    def run(
        self,
        request: SandboxRequest,
        paths: SessionPaths,
        launched: "Callable[[], None] | None" = None,
    ) -> SandboxOutcome:
        """Execute one program and return what it did.

        `launched` is called once, the moment a child process actually exists.
        Everything before that point - writing the profile, copying the source,
        the baseline walk - can fail without any experiment having run, and the
        caller needs to tell those apart: charging a daily run for a failure
        that never started a process spends a fuse on nothing.
        """


def _truncate(value: str, limit: int) -> tuple[str, int]:
    if len(value) <= limit:
        return value, 0
    return value[:limit], len(value) - limit


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _named_digests(entries: dict[str, tuple[str, int]]) -> dict[str, list[object]]:
    """State keyed by the digest of each name, never the name itself.

    The manifest outlives the files it describes, so an experiment-chosen name
    kept here would survive retention as authored text.
    """
    return {
        _digest(name.encode("utf-8")): [digest, size]
        for name, (digest, size) in entries.items()
    }


class SeatbeltSandboxRunner(SandboxRunner):
    """macOS confinement through `sandbox-exec`, plus rlimits and a timeout.

    The profile denies everything by default, allows the system paths CPython
    needs, then denies the whole home directory and re-allows only the session
    workspace inside it. Later rules win, so a workspace under the home
    directory stays writable while everything else beneath it is refused.

    An earlier version allowed reads everywhere and denied three explicit
    paths. That was too weak, and an independent review was right about it: an
    experiment could still read the keychain directory, `.config`, Documents
    and shell history, which D-027 prohibits. Reads must be denied by default
    over the user's own data, not merely at named paths, because a deny-list
    only covers the secrets somebody remembered.

    System reads stay broad because a restricted read profile cannot start the
    interpreter at all: CPython aborts before it can report why. That is
    acceptable where the repository, the runtime storage and the home
    directory are refused, and each denial is tested.
    """

    def __init__(
        self,
        workspace: SandboxWorkspace,
        denied_read_paths: tuple[Path, ...] = (),
        interpreter: str | None = None,
        sandbox_exec: str = "/usr/bin/sandbox-exec",
        home_directory: Path | None = None,
    ) -> None:
        self._workspace = workspace
        self._denied = tuple(Path(item).resolve() for item in denied_read_paths)
        self._interpreter = interpreter or sys.executable
        self._sandbox_exec = sandbox_exec
        self._home = (home_directory or Path.home()).resolve()

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
        # Order matters: Seatbelt applies the last matching rule, so the home
        # denial must precede the workspace re-allow, and every explicit denial
        # must precede it too.
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
            # The user's own data is refused wholesale rather than by naming
            # individual secrets: a deny-list only covers what was remembered.
            f'(deny file-read* (subpath "{self._home}"))\n'
            f"{denials}\n"
            # The session workspace is re-allowed after the denials so a
            # workspace living under the home directory still works.
            f'(allow file-read* (subpath "{state}"))\n'
            f'(allow file-write* (subpath "{state}"))\n'
            '(allow file-write-data (literal "/dev/null"))\n'
            "(deny network*)\n"
        )

    @staticmethod
    def _limits(cpu_seconds: int) -> None:
        """Applied in the child between fork and exec.

        RLIMIT_CPU bounds CPU time inside the run, which the parent's wall
        clock cannot: a process burning a core is stopped by the kernel rather
        than waiting for the wall timeout. D-027 records this limit as applied,
        and an earlier version of this file did not apply it at all.

        The CPU allowance exceeds the wall clock by a small margin so an
        ordinary run is bounded by the wall timeout, and CPU is the backstop.

        RLIMIT_AS is deliberately absent. It does not work on macOS arm64, and
        setting it would create the appearance of a memory bound that the
        kernel ignores.
        """
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FILE_BYTES, MAX_FILE_BYTES))
        resource.setrlimit(resource.RLIMIT_NPROC, (MAX_PROCESSES, MAX_PROCESSES))

    def run(
        self,
        request: SandboxRequest,
        paths: SessionPaths,
        launched: "Callable[[], None] | None" = None,
    ) -> SandboxOutcome:
        if not self.available():
            raise SandboxError("sandbox_unavailable")
        # Held across preparation, execution, evidence collection and the
        # manifest, so a concurrent run in this session cannot overwrite the
        # working copy and retention cannot purge state mid-run.
        with self._workspace.lease(request.experiment_id, request.session_id):
            return self._run_leased(request, paths, launched)

    def _run_leased(
        self,
        request: SandboxRequest,
        paths: SessionPaths,
        launched: "Callable[[], None] | None" = None,
    ) -> SandboxOutcome:
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
                preexec_fn=lambda: self._limits(
                    request.wall_seconds + CPU_GRACE_SECONDS
                ),
                # Its own process group, so a child that spawned helpers dies
                # with it rather than surviving the call.
                start_new_session=True,
            )
            # A process now exists and has begun consuming wall time on this
            # machine, so from here a failure settles rather than abandons.
            if launched is not None:
                launched()
            try:
                process.wait(timeout=request.wall_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
        elapsed = time.monotonic() - started
        finished_at = datetime.now(UTC)

        status = process.returncode if process.returncode is not None else -signal.SIGKILL
        after = self._workspace.walk(paths.session_state)

        # D-027 bounds the session workspace, and RLIMIT_FSIZE only bounds one
        # file: without this a run could fill the disk with many small files
        # and stay inside every per-file limit. The run has already finished,
        # so the ceiling is enforced by refusing to leave the overflow behind
        # rather than by pretending the run did not happen.
        # A truncated walk cannot show the workspace is within the ceiling:
        # `total_bytes` sums the entries it recorded, and the ones it stopped
        # before are exactly where the excess would be. Treated as over the
        # ceiling rather than as compliant, so the fail-closed direction is the
        # one that costs a run rather than the one that leaves the overflow.
        if after.truncated or after.total_bytes > MAX_WORKSPACE_BYTES:
            LOGGER.warning(
                "A sandbox session exceeded its workspace ceiling%s",
                " (state too large to audit)" if after.truncated else "",
            )
            self._workspace.purge_state(paths.session_state)
            raise SandboxError("workspace_exhausted")

        artifacts = self._workspace.changes(before.entries, after.entries)
        state_truncated = before.truncated or after.truncated
        reported = artifacts[:MAX_REPORTED_ARTIFACTS]

        stdout_bytes, stdout_digest, stdout_total, stdout_capped = self._read(stdout_path)
        stderr_bytes, stderr_digest, stderr_total, stderr_capped = self._read(stderr_path)
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
            stdout_digest=stdout_digest,
            stderr_digest=stderr_digest,
            stdout_byte_size=stdout_total,
            stderr_byte_size=stderr_total,
            artifacts=reported,
            artifacts_omitted=len(artifacts) - len(reported),
            wall_seconds_used=elapsed,
            started_at=started_at,
            finished_at=finished_at,
            stdout_capped=stdout_capped,
            stderr_capped=stderr_capped,
            # `after.truncated` is refused above, so in practice this carries a
            # truncated *before* snapshot: the counts are still derived from an
            # incomplete baseline and must not read as exact.
            state_truncated=state_truncated,
        )
        self._write_manifest(
            request, paths, outcome, before, after, argv, state_truncated
        )
        return outcome

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Stop the whole process group, and prove the group is actually gone.

        Waiting for the leader is not enough. A child that ignores SIGTERM can
        outlive a parent that exits promptly, which would leave a background
        process D-027 does not permit. So after the leader is reaped the group
        is signalled again with SIGKILL and polled until no member remains.
        """
        try:
            group = os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError):
            return

        for sender in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sender)
            except (ProcessLookupError, PermissionError):
                break
            try:
                process.wait(timeout=_TERM_GRACE_SECONDS)
                break
            except subprocess.TimeoutExpired:
                continue

        # The leader is done; the rest of the group may not be.
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(group, 0)
            except (ProcessLookupError, PermissionError):
                return
            try:
                os.killpg(group, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)
        LOGGER.warning("A sandbox process group did not terminate")

    @staticmethod
    def _read(path: Path) -> tuple[bytes, str, int, bool]:
        """Return a bounded prefix plus the digest and size of the whole file.

        The digest and size describe everything the child wrote, streamed so a
        large file is never held in memory. Hashing only the prefix would make
        the durable record describe something other than the output while
        appearing to describe it.
        """
        digest = hashlib.sha256()
        total = 0
        prefix = b""
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_READ_CHUNK):
                    digest.update(chunk)
                    total += len(chunk)
                    if len(prefix) < _MAX_CAPTURED_BYTES:
                        prefix += chunk[: _MAX_CAPTURED_BYTES - len(prefix)]
        except OSError as error:
            raise SandboxError("output_unreadable") from error
        return prefix, digest.hexdigest(), total, total > len(prefix)

    def _write_manifest(
        self,
        request: SandboxRequest,
        paths: SessionPaths,
        outcome: SandboxOutcome,
        before: "WalkResult",
        after: "WalkResult",
        argv: list[str],
        state_truncated: bool,
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
                "cpu_seconds": request.wall_seconds + CPU_GRACE_SECONDS,
                "max_file_bytes": MAX_FILE_BYTES,
                "max_processes": MAX_PROCESSES,
                "max_workspace_bytes": MAX_WORKSPACE_BYTES,
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
            "stdout_capped": outcome.stdout_capped,
            "stderr_capped": outcome.stderr_capped,
            # Filenames are chosen by the experiment, so they are authored
            # text. D-027 requires that retention remove every experiment
            # authored byte while the manifest survives, and a manifest that
            # kept names would let a program smuggle arbitrary text — or
            # exfiltrated output — past retention by encoding it in a filename.
            # Names are therefore recorded as digests here; the readable names
            # reach the Core in the transient result, which is never persisted.
            "state_before": _named_digests(before.entries),
            "state_after": _named_digests(after.entries),
            "state_truncated": state_truncated,
            "artifacts": [
                {
                    "name_digest": _digest(item.name.encode("utf-8")),
                    "change": item.change.value,
                    "byte_size": item.byte_size,
                    "digest": item.digest,
                }
                for item in outcome.artifacts
            ],
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
