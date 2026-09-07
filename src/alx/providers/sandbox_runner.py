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
import signal
import stat
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
from alx.providers.sandbox_retention import LIVE_RUN_NAME as _LIVE_RUN_NAME
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


# Where data lives on a macOS or Unix host, denied as a class rather than by
# naming individual stores. D-027 withholds production reads entirely; a
# deny-list only ever covers what somebody remembered, so these are roots
# rather than files.
#
# The temporary roots are here because they were forgotten once and are not
# obviously "data": /tmp and the per-user directories under
# /private/var/folders hold every process's temp and cache for this uid -
# editors, browsers, package tools, other agents - which is user data that
# does not live under the home directory. An earlier version omitted
# /private/var/folders on the reasoning that a temporary sandbox root lives
# there and denying it would refuse the workspace. That reasoning was wrong:
# the sandbox root is already denied and the session state re-allowed after
# it, and later rules win, so the same pattern covers this.
_DATA_ROOTS = (
    "/Users",
    "/Volumes",
    "/private/var/root",
    "/private/var/db",
    "/private/etc",
    "/opt",
    "/srv",
    "/data",
    "/usr/local/var",
    "/Library/Application Support",
    # Credential stores, which are the reason a read boundary exists at all.
    "/Library/Keychains",
    "/private/var/db/KeychainSync",
    # Temporary and cache roots. /tmp is a symlink to /private/tmp; both are
    # named so neither spelling is a way round.
    "/tmp",
    "/private/tmp",
    "/private/var/tmp",
    "/private/var/folders",
)


def _sbpl(path: "Path | str") -> str:
    """One filesystem path as a Seatbelt string literal.

    Paths reach the profile from the checkout location, the home directory,
    runtime storage and an operator-set sandbox root, and every one of those
    may legally contain a quote or a backslash. Interpolated raw, such a
    character ends the string early and the profile becomes syntactically
    invalid, so `sandbox-exec` refuses every experiment before Python starts.

    Only the two characters SBPL strings treat specially are escaped, and a
    control character is refused outright rather than encoded: a newline in a
    path is not something to accommodate quietly inside a security profile.
    """
    text = str(path)
    if any(character in text for character in ("\n", "\r", "\x00")):
        raise SandboxError("workspace_unavailable")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# The trusted launcher, run as a script rather than imported. Keeping it a
# separate program is what lets it apply limits in a single-threaded process
# and outlive nothing: it is not part of the runtime, and the runtime never
# calls into it.
_LAUNCHER = Path(__file__).with_name("sandbox_launcher.py")

# The launcher's own exit codes, distinct from anything a program can return.
_LAUNCHER_TIMED_OUT = 124
_LAUNCHER_ORPHANED = 125

# How long the parent waits beyond the run's own wall clock before deciding the
# launcher itself is stuck. The launcher enforces the deadline; this only
# catches a supervisor that has stopped supervising.
_LAUNCH_GRACE = 5.0


_DATA_ROOT_DENIALS = "".join(
    f"(deny file-read* (subpath \"{item}\"))\n" for item in _DATA_ROOTS
)


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
            f"(deny file-read* (subpath {_sbpl(path)}))" for path in self._denied
        )
        return (
            "(version 1)\n"
            "(deny default)\n"
            "(allow process-fork)\n"
            # Execution is narrowed to the interpreter this runner launches.
            # D-027 authorises Python programs using the standard library, and
            # an unrestricted process-exec authorised every binary on the host:
            # `os.execv('/bin/echo', ...)` ran, needing no fork, so the
            # RLIMIT_NPROC exhaustion that refuses subprocess did not mask it.
            f"{self._interpreter_exec()}"
            "(allow sysctl-read)\n"
            "(allow mach-lookup)\n"
            "(allow signal (target self))\n"
            "(allow file-read*)\n"
            # The user's own data is refused wholesale rather than by naming
            # individual secrets: a deny-list only covers what was remembered.
            f"(deny file-read* (subpath {_sbpl(self._home)}))\n"
            # The conventional places data lives, refused wholesale. An
            # allow-list of what CPython needs would be stronger still, and was
            # tried: a framework interpreter aborts with SIGABRT and no
            # diagnostic before it can report what it was denied, so it cannot
            # be built by observation. Denying the data roots is what is
            # actually achievable here, and it closes the case the review
            # named - a production store outside the home directory, the
            # repository and the runtime root was readable.
            f"{_DATA_ROOT_DENIALS}"
            # And the sandbox root itself, so one session cannot read another.
            # Session isolation was previously only incidental: it held when
            # the root happened to sit under the home directory, and not
            # otherwise. A root at /var/lib/alx - the layout D-027 pushes
            # toward by keeping the sandbox apart from runtime storage - left
            # every session readable by every other, reachable as
            # ../../<other>/state from the working directory. The re-allow
            # below restores exactly this run's own state.
            f"(deny file-read* (subpath {_sbpl(self._workspace.root)}))\n"
            f"{denials}\n"
            # The launcher itself, which lives in the repository the profile
            # otherwise denies. One file, by literal path, read-only: the
            # supervisor runs inside the same confinement as the thing it
            # supervises, so it has to be readable from in here. Naming the
            # file rather than the directory keeps the rest of the repository
            # exactly as denied as it was.
            f"(allow file-read* (literal {_sbpl(_LAUNCHER)}))\n"
            # The session workspace is re-allowed after the denials so a
            # workspace living under the home directory still works.
            f"(allow file-read* (subpath {_sbpl(state)}))\n"
            f"(allow file-write* (subpath {_sbpl(state)}))\n"
            '(allow file-write-data (literal "/dev/null"))\n'
            "(deny network*)\n"
        )

    def _interpreter_exec(self) -> str:
        """The exec rules for the interpreter this runner launches.

        Narrower than "any binary on the host" and wider than one literal
        path, because starting CPython is not one exec. The configured name is
        usually a symlink, and a framework build then re-execs a further binary
        inside its own bundle - naming only the first refused the launch, and
        naming only the resolved one refused it a step later.

        So execution is confined to the interpreter's own installation prefix.
        An experiment still cannot reach a shell, a compiler or another
        interpreter, which is what D-027 is protecting; what it can reach is
        the Python it was already running.
        """
        configured = Path(self._interpreter)
        resolved = configured.resolve()
        rules = {
            f"(allow process-exec (literal {_sbpl(configured)}))",
            f"(allow process-exec (literal {_sbpl(resolved)}))",
        }
        prefix = self._interpreter_prefix(resolved)
        if prefix is not None:
            rules.add(f"(allow process-exec (subpath {_sbpl(prefix)}))")
        return "".join(f"{rule}\n" for rule in sorted(rules))

    @staticmethod
    def _interpreter_prefix(resolved: Path) -> Path | None:
        """The framework version directory, when the interpreter is in one.

        A prefix grant exists for exactly one reason: a macOS framework build
        does not run the binary it was given, it re-execs a further binary
        inside its own bundle, and that target lives under the version
        directory. No other layout needs one - a MacPorts, Homebrew, conda or
        system interpreter starts from the literals alone.

        So this recognises the one layout that needs the grant rather than
        listing the ones that must not have it. Two previous versions were
        such lists: the first returned the parent of any ancestor named `bin`,
        which gave `/usr` for `/usr/bin/python3`; the second refused a fixed
        set of shared prefixes, which still handed out all of `/opt/local` for
        MacPorts and a whole conda prefix, both of which carry compilers,
        `curl`, `openssl` and sometimes a shell. A deny-list only ever covers
        what somebody remembered, and this one had already been wrong twice.

        Returning None is always safe. At worst a framework re-exec is refused
        and the misconfiguration is immediately visible, which is the failure
        anyone would rather have than an invisible grant over a toolchain.
        """
        for index, part in enumerate(resolved.parts):
            if part != "Versions":
                continue
            parent = Path(*resolved.parts[:index])
            if parent.suffix != ".framework":
                continue
            # `.../Python.framework/Versions/3.13`, and nothing above it.
            version = Path(*resolved.parts[: index + 2])
            return version if version in resolved.parents else None
        return None

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
            #
            # Session state is writable by the experiment and persists between
            # runs, so every destination under it is attacker-controlled. A
            # previous run can leave its entry file as a symlink to any host
            # path, and shutil.copyfile follows a link at the destination: the
            # privileged parent would then write the new source through it and
            # overwrite a file outside the sandbox. Opening with O_NOFOLLOW
            # refuses the link instead of following it, and O_TRUNC keeps the
            # ordinary case - a real file left by an earlier run - working.
            working_copy = paths.session_state / request.entry_filename
            self._write_working_copy(working_copy, request.source)
        except OSError as error:
            raise SandboxError("workspace_unavailable") from error

        before = self._workspace.walk(paths.session_state)

        # -E ignores PYTHONPATH and friends, and -S skips site packages, so the
        # program starts from a predictable stdlib-only interpreter. -I is
        # deliberately not used: it also strips the script's own directory from
        # sys.path, which would stop a run importing a helper an earlier run in
        # the same session wrote, and that iteration is the point of a session.
        # The launcher is what actually starts the program. It applies the
        # resource limits in a single-threaded process, establishes the group,
        # and supervises both the experiment and this parent. `preexec_fn` used
        # to do the first of those from inside a runtime that dispatches Core
        # turns through asyncio.to_thread: a fork from a multi-threaded process
        # inherits held locks, and a child that deadlocks before exec makes
        # Popen never return, holding the lease, the reservation and the turn.
        #
        # It runs inside the same profile as the experiment, so the supervisor
        # is confined exactly as the thing it supervises is.
        identity = f"{request.experiment_id}/{request.session_id}/{request.run_id}"
        argv = [
            self._sandbox_exec,
            "-f",
            str(profile_path),
            self._interpreter,
            "-E",
            "-S",
            str(_LAUNCHER),
            "--interpreter", self._interpreter,
            "--program", str(working_copy),
            "--directory", str(paths.session_state),
            "--identity", identity,
            "--cpu-seconds", str(request.wall_seconds + CPU_GRACE_SECONDS),
            "--file-bytes", str(MAX_FILE_BYTES),
            "--processes", str(MAX_PROCESSES),
            "--wall-seconds", str(request.wall_seconds),
            "--parent-pid", str(os.getpid()),
        ]
        started_at = datetime.now(UTC)
        started = time.monotonic()
        timed_out = False
        # The parent opens the evidence files and hands the descriptors down.
        # The run directory is not writable from inside the sandbox - that is
        # what stops a program editing its own evidence - so the launcher
        # cannot open them, and inheriting the descriptors keeps that boundary
        # intact while still capturing the output.
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            argv += [
                "--stdout-fd", str(out.fileno()),
                "--stderr-fd", str(err.fileno()),
            ]
            process = subprocess.Popen(  # noqa: S603 - the one execution site
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(paths.session_state),
                # Nothing is inherited. Not os.environ, not the .env mapping.
                env={},
                # Its own session, so the launcher and everything under it can
                # be signalled as one group.
                start_new_session=True,
                pass_fds=(out.fileno(), err.fileno()),
            )
            # A process now exists and has begun consuming wall time on this
            # machine, so from here a failure settles rather than abandons.
            if launched is not None:
                launched()
            # The group id is read while the leader still exists. After it is
            # reaped the pid can be recycled, and signalling a recycled group
            # would be signalling someone else's processes.
            group = self._group_of(process)
            # Recorded before waiting, so a crash between here and the end
            # leaves something a later runtime can verify and reap. Written by
            # the parent rather than the launcher: a file the confined process
            # could write is a file it could forge.
            self._record_live_run(paths, identity, process.pid, group, started_at)
            try:
                # The launcher enforces the wall clock itself; this bound is
                # the backstop for a launcher that has itself become stuck.
                self._read_identity(process)
                process.wait(timeout=request.wall_seconds + _LAUNCH_GRACE)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate(process)
            if process.returncode == _LAUNCHER_TIMED_OUT:
                timed_out = True
        # Sweep the group after every exit, not only after a timeout. A
        # program that spawns a background helper and then returns cleanly
        # left that helper running: outside the wall-time accounting, still
        # able to write to session state while the evidence walk read it,
        # and still alive after the lease was released. The leader's own
        # status is already recorded, so this changes what is left running
        # rather than what is reported.
        self._reap_group(group)
        self._clear_live_run(paths)
        elapsed = time.monotonic() - started
        finished_at = datetime.now(UTC)

        # A leader that could not be reaped has no observed status. Reporting
        # -SIGKILL for it would be inventing evidence: the run is recorded as
        # signalled and timed out, which is what is actually known, and the
        # exit status says the same thing rather than naming a signal nobody
        # saw delivered. In practice the reap above succeeds and the real
        # status is used.
        status = self._program_status(process.returncode, timed_out)
        if process.returncode is None:
            LOGGER.warning(
                "A sandbox run reports an unobserved exit status: the leader "
                "could not be reaped"
            )
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
            # The run directory goes with it. This path returns no outcome, so
            # no manifest can be written for it, and leaving stdout, stderr and
            # the source behind would keep experiment-authored bytes that
            # nothing describes and retention would only reach at the TTL.
            # Refusing the run means keeping none of it.
            self._workspace.purge_run(paths.run_directory)
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
    def _program_status(launcher_status: int | None, timed_out: bool) -> int:
        """The program's own exit status, from what the launcher reported.

        The launcher is a supervisor, so its exit code is about the run rather
        than about the program: 124 for a deadline it enforced, 125 for a
        parent that disappeared, and `128 - signal` for a program a signal
        ended. Reporting the supervisor's number as the program's would say a
        timed-out run exited 124, when what actually happened is that it was
        killed.
        """
        if launcher_status is None:
            return -signal.SIGKILL
        if launcher_status in (_LAUNCHER_TIMED_OUT, _LAUNCHER_ORPHANED):
            # Ended by this sandbox rather than by itself.
            return -signal.SIGKILL
        if launcher_status > 128:
            return 128 - launcher_status
        return launcher_status

    @staticmethod
    def _read_identity(process: subprocess.Popen) -> None:
        """Drain the launcher's identity line so its stdout pipe cannot fill.

        The launcher prints one JSON line and then never writes again. Left
        unread, a full pipe would block it; read here, the pipe stays empty for
        the life of the run. The content is not used - the parent already knows
        the pid and group it started, and a value the confined side supplied
        would be a value it could choose.
        """
        stream = process.stdout
        if stream is None:
            return
        try:
            stream.readline()
        except (OSError, ValueError):
            return

    def _record_live_run(
        self,
        paths: SessionPaths,
        identity: str,
        pid: int,
        group: int | None,
        started_at: datetime,
    ) -> None:
        """Note a running experiment where a later runtime can find it.

        D-027 says a process group left behind by a crash is reaped when the
        runtime starts. Nothing recorded what to reap: the pid lived only in
        the memory of the process that died. This is the record that makes the
        promise true.

        Written by the parent, into the run directory the confined process
        cannot write to. A file the experiment could author would be a file it
        could forge, and forging this one would aim a kill at another process.
        """
        if group is None:
            return
        record = {
            "identity": identity,
            "pid": pid,
            "process_group": group,
            "started_at": started_at.isoformat(),
            "parent_pid": os.getpid(),
        }
        try:
            (paths.run_directory / _LIVE_RUN_NAME).write_text(
                json.dumps(record), encoding="utf-8"
            )
        except OSError:
            # Losing the note costs recovery, not the run.
            LOGGER.warning("A sandbox run could not record its process identity")

    @staticmethod
    def _clear_live_run(paths: SessionPaths) -> None:
        """Forget a run that has ended, so recovery has nothing to consider."""
        try:
            (paths.run_directory / _LIVE_RUN_NAME).unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("A finished sandbox run could not clear its note")

    @staticmethod
    def _group_of(process: subprocess.Popen) -> int | None:
        """The process group, read while the leader is certainly alive."""
        try:
            return os.getpgid(process.pid)
        except (ProcessLookupError, PermissionError):
            return None

    @staticmethod
    def _reap_group(group: int | None) -> None:
        """Ensure no member of a finished run's group is still running.

        D-027 does not permit a background process to outlive the call that
        started it. The leader exiting says nothing about its children: they
        were given their own group precisely so they could be signalled
        together, and that only happened on the timeout path.
        """
        if group is None:
            return
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return
        try:
            os.killpg(group, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(group, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.05)
        LOGGER.warning("A sandbox process group outlived its run")

    @staticmethod
    def _write_working_copy(destination: Path, source: str) -> None:
        """Write the program into session state without following a link.

        O_NOFOLLOW fails on a symlink at the final path component rather than
        resolving it, so an entry file an experiment turned into a link is
        refused instead of being written through. The descriptor is then used
        directly, so nothing re-resolves the name between the check and the
        write.
        """
        try:
            # O_NOFOLLOW refuses a symlink, but a FIFO is not a symlink: an
            # experiment can leave its entry file as one, and opening a FIFO
            # for writing blocks until a reader arrives - forever, holding the
            # session lease and the day's reservation, with the capability call
            # never returning. O_NONBLOCK opens it instead, so the kind can be
            # checked, and O_TRUNC is applied only once it is known to be a
            # regular file: truncating is meaningless on a FIFO and harmful on
            # anything else.
            handle = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
            )
        except OSError as error:
            # ELOOP is a symlink; ENXIO is a FIFO with no reader. Both are
            # refused rather than followed or waited on.
            raise SandboxError("workspace_unavailable") from error
        try:
            status = os.fstat(handle)
            if not stat.S_ISREG(status.st_mode):
                raise SandboxError("workspace_unavailable")
            os.set_blocking(handle, True)
            os.ftruncate(handle, 0)
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                handle = -1
                stream.write(source)
        except OSError as error:
            raise SandboxError("workspace_unavailable") from error
        finally:
            if handle >= 0:
                os.close(handle)

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
        # One last attempt to reap the leader. Both grace windows above can
        # expire without `wait` returning - an uninterruptible sleep survives
        # SIGKILL until its syscall completes - and the leader would then still
        # be unreaped, leaving a zombie and a returncode of None. The caller
        # substitutes -SIGKILL for None, which would be a status nobody
        # observed, so it is worth one non-blocking poll to record the real one
        # where it exists.
        try:
            process.wait(timeout=_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            LOGGER.warning("A sandbox process leader could not be reaped")

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
