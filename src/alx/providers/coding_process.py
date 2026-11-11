"""Program execution and verification inside one assigned coding worktree.

Coding verification executes structured argv as Friedl's user. D-063 removes
the general-command allowlist; Core still owns scope and promotion permission.
"""

from __future__ import annotations

import os
import fcntl
import hashlib
import logging
import io
import json
import platform
import pty
import pwd
import select
import signal
import shutil
import struct
import tarfile
import tempfile
import termios
from contextlib import contextmanager
from importlib import metadata
from contextvars import ContextVar
from threading import Event, Lock, Thread
from time import monotonic, sleep
from typing import Any, Callable
from collections.abc import Iterator, Mapping, Sequence
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import subprocess  # noqa: S404 - the one coding-job process site
import sys

from alx.contracts.coding import (
    DEFAULT_COMMAND_SECONDS,
    MAX_COMMAND_OUTPUT_CHARACTERS,
    MAX_COMMAND_SECONDS,
    MAX_DIFF_CHARACTERS,
    CodingCommandRecord,
    CodingError,
    path_matches_blocked,
)
from alx.providers.coding_containment import CREDENTIAL_DENY_GLOBS
from alx.contracts.coding_verification import (
    MAX_FAILED_TEST_CHARACTERS,
    MAX_FAILED_TEST_FINDINGS,
    pytest_failed_tests,
    pytest_failure_signature,
)


LOGGER = logging.getLogger(__name__)

_CURRENT: ContextVar["CodingCancellation | None"] = ContextVar(
    "alx_coding_cancellation", default=None
)

# The pytest child has its own process memory and cannot write this cache.
# Nothing read from the checkout can assert what clean main did. A restart
# discards the cache and recomputes the baseline on the next failing check.
_MAIN_FAILURE_CACHE: dict[str, tuple[tuple[str, str], ...]] = {}


# The least time between two activity reports from one running command. The
# watchdog sees activity every scan; a console needs it far less often.
ACTIVITY_REPORT_SECONDS = 5.0


class CodingCancellation:
    def __init__(
        self, on_activity: Callable[[], None] | None = None,
    ) -> None:
        self.requested = Event()
        self._lock = Lock()
        self._process: subprocess.Popen[Any] | None = None
        # Told when the running command shows real activity (output, or a
        # change in the checkout), at most once per ACTIVITY_REPORT_SECONDS.
        # The same evidence the stall watchdog uses, so "last activity" on a
        # console means what the watchdog means by it.
        self.on_activity = on_activity
        self._reported_at = float("-inf")

    def _activity_seen(self, now: float) -> None:
        if self.on_activity is None or now - self._reported_at < ACTIVITY_REPORT_SECONDS:
            return
        self._reported_at = now
        try:
            self.on_activity()
        except Exception:  # noqa: BLE001 - reporting never alters the command
            LOGGER.info("Coding activity report failed")

    def cancel(self) -> None:
        self.requested.set()

    def check(self) -> None:
        if self.requested.is_set():
            raise CodingError("coding_cancelled")

    def run(self, runner: Callable[..., Any], argv: list[str], **kwargs: Any) -> Any:
        self.check()
        stop_descendants = kwargs.pop("stop_descendants", False)
        inactivity_timeout = kwargs.pop("inactivity_timeout", None)
        activity_root = kwargs.pop("activity_root", None)
        activity_blocked_paths = tuple(kwargs.pop("activity_blocked_paths", ()))
        # Files the child's output is relayed to, through pipes, by counting
        # threads: bytes relayed are activity, and no byte past
        # `output_limit_bytes` is ever written.
        spool_to = tuple(Path(item) for item in kwargs.pop("spool_to", ()))
        output_limit = kwargs.pop("output_limit_bytes", None)
        activity_path = Path(activity_root) if activity_root is not None else None
        if runner is not subprocess.run:
            result = runner(argv, **kwargs)
            self.check()
            return result
        timeout = kwargs.pop("timeout", None)
        capture = kwargs.pop("capture_output", False)
        if capture:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        supplied_input = kwargs.pop("input", None)
        if supplied_input is not None:
            kwargs["stdin"] = subprocess.PIPE
        kwargs.pop("check", None)
        kwargs["start_new_session"] = True
        relay = _SpoolRelay(spool_to, output_limit) if spool_to else None
        if relay is not None:
            kwargs["stdout"] = subprocess.PIPE
            kwargs["stderr"] = subprocess.PIPE
        process = subprocess.Popen(argv, **kwargs)
        if relay is not None:
            relay.start((process.stdout, process.stderr))
        stop_input = Event()
        with self._lock:
            self._process = process
        try:
            if supplied_input is not None:
                # Deliver the whole input and close it here, not through the
                # polling communicate() below. Its first 0.1 s call wrote only
                # what fit the pipe before timing out, and later calls without
                # input never resumed: a prompt larger than the pipe buffer was
                # cut short and never reached EOF, so `codex exec -` waited
                # until the deadline.
                stdin, process.stdin = process.stdin, None
                try:
                    # Encoded here, as communicate() did, so a prompt the pipe
                    # cannot carry fails this call instead of arriving short.
                    payload = (
                        supplied_input.encode(stdin.encoding, stdin.errors)
                        if isinstance(supplied_input, str) else supplied_input
                    )
                except ValueError:
                    stdin.close()
                    raise
                Thread(
                    target=_deliver_input, args=(stdin, payload, stop_input), daemon=True
                ).start()
            self.check()
            started = monotonic()
            deadline = None if timeout is None else started + timeout
            last_activity = started
            observed_output = (0, 0)
            observed_files = (
                _activity_snapshot(activity_path, activity_blocked_paths)
                if activity_path is not None else None
            )
            observed_relay = relay.last_write if relay is not None else None
            next_file_check = started
            while True:
                now = monotonic()
                if relay is not None and process.poll() is not None:
                    # The CLI has exited. Whatever is left of its group is
                    # stopped, the relays drain to EOF, and only then is the
                    # result known: it may have reached the output bound.
                    _stop_remaining(process)
                    relay.finish(STOP_WAIT_SECONDS)
                    self.check()
                    relay.require_within_limit()
                    return subprocess.CompletedProcess(argv, process.returncode, None, None)
                if process.poll() is not None:
                    try:
                        stdout, stderr = process.communicate(timeout=0)
                    except subprocess.TimeoutExpired:
                        # A detached descendant may still hold a pipe. The
                        # normal deadline/stall bounds still apply.
                        pass
                    else:
                        self.check()
                        return subprocess.CompletedProcess(
                            argv, process.returncode, stdout, stderr
                        )
                if deadline is not None and now >= deadline:
                    # A bounded individual command. A coding session passes no
                    # deadline: its only bound is the inactivity check below.
                    _stop(process)
                    raise subprocess.TimeoutExpired(argv, timeout)
                if relay is not None:
                    if relay.limit_reached.is_set():
                        _stop(process)
                        relay.require_within_limit()
                    if relay.last_write != observed_relay:
                        observed_relay = relay.last_write
                        last_activity = now
                        self._activity_seen(now)
                if observed_files is not None and now >= next_file_check:
                    current_files = _activity_snapshot(activity_path, activity_blocked_paths)
                    if current_files != observed_files:
                        last_activity = now
                        observed_files = current_files
                        self._activity_seen(now)
                    scan_interval = min(1.0, inactivity_timeout / 4) if inactivity_timeout else 1.0
                    next_file_check = now + scan_interval
                if inactivity_timeout is not None and now - last_activity >= inactivity_timeout:
                    _stop(process)
                    raise CodingError("session_interrupted", reason_code="session_stalled")
                remaining = None if deadline is None else max(0, deadline - now)
                idle_remaining = (
                    None if inactivity_timeout is None
                    else max(0, inactivity_timeout - (now - last_activity))
                )
                poll = min(
                    0.1,
                    *(value for value in (remaining, idle_remaining) if value is not None),
                )
                if relay is not None:
                    # The relays own the pipes; communicate() would compete
                    # with them for the output.
                    try:
                        process.wait(timeout=poll)
                    except subprocess.TimeoutExpired:
                        self.check()
                    continue
                try:
                    stdout, stderr = process.communicate(
                        timeout=poll,
                    )
                    self.check()
                    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired as pending:
                    self.check()
                    output = (len(pending.stdout or b""), len(pending.stderr or b""))
                    if output != observed_output:
                        observed_output = output
                        last_activity = monotonic()
                        self._activity_seen(last_activity)
        finally:
            stop_input.set()
            with self._lock:
                self._process = None
            if process.poll() is None:
                _stop(process)
            # Reap the writer and stop descendants even if the leader already
            # exited. KiCad callers may restore files as soon as this returns.
            if stop_descendants:
                _stop_remaining(process)
            if relay is not None:
                # The relays own the pipes: let them drain within a bound,
                # then close what a lingering holder keeps open.
                relay.finish(2)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            else:
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    # A grandchild that escaped its parent's process group may
                    # still hold a pipe open. It must not hold the Core worker.
                    for pipe in (process.stdout, process.stderr, process.stdin):
                        if pipe is not None:
                            pipe.close()


def _deliver_input(stdin: Any, payload: bytes, stop: Event) -> None:
    """Write a child's complete input, then close it so the child sees EOF.

    Writes as communicate() does: at most PIPE_BUF bytes once the pipe is
    writable, so no write blocks. `stop` ends it when the call returns, because
    a descendant that left the process group can hold the read end without
    reading, and this thread must not outlive the job waiting on it.
    """
    fd = stdin.fileno()
    view = memoryview(payload)
    try:
        while view and not stop.is_set():
            _, writable, _ = select.select([], [fd], [], 0.1)
            if writable:
                view = view[os.write(fd, view[:select.PIPE_BUF]):]
    except OSError:
        pass  # A broken pipe: the child exited or was stopped; its exit status says so.
    finally:
        try:
            stdin.close()
        except OSError:
            pass


def _stop(process: subprocess.Popen[Any]) -> None:
    """Stop the session's whole process group, within a bound.

    macOS answers `killpg` with EPERM once some of a group's members have
    exited unreaped. Letting that PermissionError escape replaced the reason a
    session was being stopped (stalled, at its ceiling, cancelled) with an
    OSError, which the session reports as the CLI being unavailable. When the
    group signal is refused, each live member of the group is signalled
    individually, so a tool process the CLI started cannot outlive the session.
    A process that still cannot be stopped is reported as such rather than
    waited on for ever.
    """
    if process.poll() is not None:
        return
    if _signal_group(process, signal.SIGTERM) is None:
        return
    # The CLI exiting proves only that the CLI exited. A descendant that
    # ignores SIGTERM keeps the group alive, so the group is what is awaited
    # and, if anything remains, what is killed.
    if _group_stopped(process, 2.0, group_killed=False):
        return
    killed = _signal_group(process, signal.SIGKILL)
    if not _group_stopped(process, STOP_WAIT_SECONDS, group_killed=killed is True):
        raise CodingError(
            "session_interrupted", reason_code="session_unstoppable"
        )


def _group_stopped(
    process: subprocess.Popen[Any], seconds: float, *, group_killed: bool
) -> bool:
    """Whether the CLI has exited and its process group is empty in time.

    Only a census that succeeded can show the group empty. When `ps` fails,
    the group is unknown, never empty: before SIGKILL that means escalating;
    after a group-wide SIGKILL the kernel delivered, the CLI's exit stands.
    """
    deadline = monotonic() + seconds
    while True:
        if process.poll() is not None:
            members, counted = _group_members(process.pid)
            if counted and not members:
                return True
            if not counted and group_killed:
                return True
        if monotonic() >= deadline:
            return False
        sleep(0.1)


# How long a session process may take to exit after SIGKILL. The kernel
# delivers SIGKILL unconditionally, so exceeding this means the signal was
# never delivered, not that the process is slow.
STOP_WAIT_SECONDS = 5.0


def _signal_group(process: subprocess.Popen[Any], signum: int) -> bool | None:
    """Signal the session's process group.

    True when the whole group took the signal, False when only its members
    could be signalled one by one, None when the group is already gone.
    """
    try:
        os.killpg(process.pid, signum)
        return True
    except ProcessLookupError:
        return None
    except PermissionError:
        pass
    # The group refused as a whole: signal its members one by one. A member
    # that has exited (the EPERM cause) or is not ours refuses alone.
    members, _counted = _group_members(process.pid)
    for pid in members or (process.pid,):
        try:
            os.kill(pid, signum)
        except (ProcessLookupError, PermissionError):
            continue
    return False


def _group_members(pgid: int) -> tuple[tuple[int, ...], bool]:
    """Live members of process group `pgid`, and whether `ps` could count them."""
    try:
        listing = subprocess.run(  # noqa: S603 - fixed argv, no model input
            ["ps", "-A", "-o", "pid=", "-o", "pgid=", "-o", "stat="],
            capture_output=True, text=True, timeout=5, check=False, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return (), False
    members: list[int] = []
    for line in listing.stdout.splitlines():
        fields = line.split()
        # A zombie has exited and cannot write; it only awaits its parent.
        # Counting it made a finished session look unstoppable.
        if (len(fields) == 3 and fields[0].isdigit() and fields[1] == str(pgid)
                and not fields[2].startswith("Z")):
            members.append(int(fields[0]))
    return tuple(members), listing.returncode == 0


def check_cancelled() -> None:
    current = _CURRENT.get()
    if current is not None:
        current.check()


def wait_with_cancellation(delay: float, wait: Callable[[float], None]) -> None:
    """Wait on the bound job's stop signal, or use the supplied clock wait."""
    current = _CURRENT.get()
    if current is None:
        wait(delay)
    else:
        current.check()
        current.requested.wait(delay)
        current.check()


def run_coding_subprocess(runner: Callable[..., Any], argv: list[str], **kwargs: Any) -> Any:
    current = _CURRENT.get()
    if current is None and "inactivity_timeout" not in kwargs and "stop_descendants" not in kwargs:
        return runner(argv, **kwargs)
    return (current or CodingCancellation()).run(runner, argv, **kwargs)


@contextmanager
def interactive_terminal(
    argv: list[str], *, cwd: str, env: Mapping[str, str],
    rows: int, columns: int, deadline: float,
) -> Iterator[tuple[int, subprocess.Popen[bytes]]]:
    """Own a PTY session; the caller drives its terminal protocol.

    Allow a requested clean exit up to the deadline, then kill the whole
    process group and reap its leader, even when the caller raises.
    """
    master = slave = None
    child = None
    try:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        child = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd,
            env=env, start_new_session=True, shell=False,
        )
        os.close(slave)
        slave = None
        yield master, child
        try:
            child.wait(timeout=max(0, min(0.3, deadline - monotonic())))
        except subprocess.TimeoutExpired:
            pass
    finally:
        try:
            if child is not None:
                # An exited parent can still leave descendants behind.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
        finally:
            for descriptor in (master, slave):
                if descriptor is not None:
                    os.close(descriptor)


KICAD_CLI_UNAVAILABLE_ERRORS = (OSError,)
KICAD_CLI_TIMEOUT = subprocess.TimeoutExpired


def run_kicad_cli(executable, arguments, *, runner=None, timeout_seconds=600):
    """Run KiCad's fixed argv through the approved process execution site."""
    return run_coding_subprocess(
        subprocess.run if runner is None else runner, [executable, *arguments],
        shell=False, check=False, timeout=timeout_seconds, capture_output=True, text=True,
        # Select the existing managed process-group path even outside a Core turn.
        stop_descendants=True,
    )


def run_kicad_engine(executable, arguments, directory, *, runner=None, timeout_seconds=600):
    """Analysis engines share the managed process-group execution boundary."""
    return run_coding_subprocess(
        subprocess.run if runner is None else runner, [str(executable), *map(str, arguments)],
        cwd=str(directory), shell=False, check=False, timeout=timeout_seconds,
        capture_output=True, text=True, stop_descendants=True,
    )


def kicad_containment_available():
    """KiCad scripts run only under the macOS kernel sandbox; nowhere else."""
    return platform.system() == 'Darwin' and Path('/usr/bin/sandbox-exec').is_file()


def _confined_profile(project, *readable):
    """The kernel sandbox for a contained run: one writable folder, no network.

    dyld and CPython need system reads and IPC, so reading is allowed by
    default, except anywhere in the home folder (keys, mail, documents)
    outside the work folder and the named files. Writes are confined to the
    folder. No forking: nothing can be left behind that keeps changing the
    folder after the run is reported.
    """
    quote = lambda path: json.dumps(str(path))
    # The user's own home, not $HOME, which callers may change.
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    return '\n'.join((
        '(version 1)', '(allow default)',
        '(deny file-read* (subpath ' + quote(home) + '))',
        '(allow file-read-metadata)',
        '(allow file-read* (subpath ' + quote(project) + ')'
        + ''.join(' (literal ' + quote(path) + ')' for path in readable) + ')',
        '(deny file-write*)',
        '(allow file-write* (subpath ' + quote(project)
        + ') (literal "/dev/null") (literal "/dev/tty") (regex #"^/dev/fd/"))',
        '(deny network*)',
        '(deny process-fork)',
    ))


def run_page_render(executable, arguments, directory, *, runner=None, timeout_seconds=60):
    """A datasheet page made into an image, confined like a KiCad script (D-073).

    The document is a copy inside `directory`, the only place the renderer
    may read in the home folder or write at all.
    """
    if not kicad_containment_available():
        raise OSError('page render containment unavailable')
    directory = Path(directory).resolve()
    return run_coding_subprocess(
        subprocess.run if runner is None else runner,
        ['/usr/bin/sandbox-exec', '-p', _confined_profile(directory), str(executable),
         *map(str, arguments)],
        cwd=str(directory), env={'PATH': '/usr/bin:/bin', 'HOME': str(directory),
                                 'TMPDIR': str(directory)},
        shell=False, check=False, timeout=min(600, max(1, timeout_seconds)),
        capture_output=True, text=True, stop_descendants=True,
    )


def run_kicad_python(executable, script, board, *, runner=None, timeout_seconds=120):
    """Kernel-confined KiCad script, through the managed process execution site.

    The profile is inherited by children, including detached processes. No
    fallback to unsandboxed Python is permitted on another operating system.
    Script/output files live outside the only writable directory.
    """
    if not kicad_containment_available():
        raise OSError('KiCad script containment unavailable')
    board = Path(board).resolve()
    project = board.parent
    script = Path(script).resolve()
    profile = _confined_profile(project, script)
    stdout, stderr = script.with_suffix('.stdout'), script.with_suffix('.stderr')
    result = run_coding_subprocess(
        subprocess.run if runner is None else runner,
        ['/usr/bin/sandbox-exec', '-p', profile, str(executable), '-B', str(script), str(board)],
        cwd=project, env={'PATH': '/usr/bin:/bin', 'HOME': str(project),
                          'TMPDIR': str(project), 'PYTHONDONTWRITEBYTECODE': '1'},
        shell=False, check=False, timeout=min(600, max(1, timeout_seconds)),
        capture_output=True, text=False, stop_descendants=True,
        spool_to=(stdout, stderr), output_limit_bytes=128000,
    )
    # Injected runners return captured strings; the real managed runner spools.
    return subprocess.CompletedProcess(result.args if hasattr(result, 'args') else [],
        result.returncode,
        stdout.read_text(errors='replace')[:16000] if stdout.exists() else (result.stdout or '')[:16000],
        stderr.read_text(errors='replace')[:16000] if stderr.exists() else (result.stderr or '')[:16000])


def _stop_remaining(process: subprocess.Popen[Any]) -> None:
    """Stop what is left of a finished session's process group.

    The CLI has exited, but a descendant may still hold the spool open and
    keep writing after monitoring and the output bound would otherwise end.
    It is stopped by the same bounded path as a running session, before the
    final size check.
    """
    members, counted = _group_members(process.pid)
    if counted and not members:
        return
    if _signal_group(process, signal.SIGTERM) is None:
        return
    if _group_stopped(process, 2.0, group_killed=False):
        return
    killed = _signal_group(process, signal.SIGKILL)
    if not _group_stopped(process, STOP_WAIT_SECONDS, group_killed=killed is True):
        raise CodingError("session_interrupted", reason_code="session_unstoppable")


class _SpoolRelay:
    """Copy a child's stdout and stderr to files, counting every byte.

    The bound is enforced where the bytes are written: a chunk that would
    take the combined output past the limit is cut at the limit, the limit is
    marked reached, and relaying stops. Memory is bounded by one chunk per
    stream, and the time of the last relayed byte is the output activity.
    """

    CHUNK_BYTES = 64 * 1024

    def __init__(self, paths: tuple[Path, ...], limit: int | None) -> None:
        self._paths = paths
        self._limit = limit
        self._written = 0
        self._lock = Lock()
        self._threads: list[Thread] = []
        self.limit_reached = Event()
        self.last_write = monotonic()

    def start(self, streams: Sequence[Any]) -> None:
        for stream, path in zip(streams, self._paths):
            thread = Thread(target=self._pump, args=(stream, path), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _pump(self, stream: Any, path: Path) -> None:
        with open(path, "ab") as spool:
            while not self.limit_reached.is_set():
                try:
                    chunk = stream.read1(self.CHUNK_BYTES)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                with self._lock:
                    room = (len(chunk) if self._limit is None
                            else max(0, self._limit - self._written))
                    kept = chunk[:room]
                    self._written += len(kept)
                    if len(kept) < len(chunk):
                        self.limit_reached.set()
                    self.last_write = monotonic()
                if kept:
                    spool.write(kept)
                    spool.flush()

    def finish(self, seconds: float) -> None:
        """Wait, within a bound, for both relays to reach EOF or stop."""
        deadline = monotonic() + seconds
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - monotonic()))

    def require_within_limit(self) -> None:
        if self.limit_reached.is_set():
            raise CodingError("session_interrupted", reason_code="session_output_limit")


def _activity_snapshot(
    root: Path, blocked_paths: tuple[str, ...]
) -> tuple[tuple[str, int, int], ...]:
    """Observe checkout metadata only; never read source bytes or Git internals."""
    entries: list[tuple[str, int, int]] = []
    for directory, subdirs, files in os.walk(root, followlinks=False):
        # Git's own writes and AL/X runtime logs are not coding progress.
        subdirs[:] = [
            name for name in subdirs
            if name not in {".git", ".alx"}
            and not path_matches_blocked(
                str((Path(directory) / name).relative_to(root)), blocked_paths
            )
        ]
        for name in (*subdirs, *files):
            path = Path(directory) / name
            relative = str(path.relative_to(root))
            candidate = PurePosixPath(relative)
            if path_matches_blocked(relative, blocked_paths) or any(
                candidate.match(pattern) or candidate.match(pattern.removeprefix("**/"))
                for pattern in CREDENTIAL_DENY_GLOBS
            ):
                continue
            try:
                info = path.lstat()
            except OSError:
                continue  # A concurrent rename is activity on the next snapshot.
            entries.append((relative, info.st_mtime_ns, info.st_size))
    return tuple(sorted(entries))


def bind_cancellation(cancellation: CodingCancellation):
    return _CURRENT.set(cancellation)


def reset_cancellation(token: Any) -> None:
    _CURRENT.reset(token)


_PYTHON_NAMES = frozenset({"python", "python3"})


def command_permitted(
    argv: list[str] | tuple[str, ...],
    worktree: Path | None = None,
    blocked_paths: tuple[str, ...] = (),
) -> bool:
    """Validate argv and explicit blocked targets; D-063 removes the allowlist."""
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        return False
    if not argv[0]:
        return False
    if blocked_paths:
        # Match subprocess' actual executable selection, including cwd-relative
        # PATH entries and the runtime interpreter substitution below.
        executable = sys.executable if argv[0].lower() in _PYTHON_NAMES else argv[0]
        if "/" not in executable:
            search = os.pathsep.join(
                str((worktree or Path.cwd()) / entry)
                for entry in os.environ.get("PATH", os.defpath).split(os.pathsep)
            )
            executable = shutil.which(executable, path=search) or executable
        location = (worktree or Path.cwd()) / executable
        # Both the selected path and its symlink target must respect blocks.
        for candidate in (Path(os.path.abspath(location)), location.resolve()):
            try:
                relative = candidate.relative_to(worktree.resolve()).as_posix() if worktree else None
            except ValueError:
                relative = None  # System executables outside the checkout are allowed.
            if relative is not None and path_matches_blocked(relative, blocked_paths):
                return False
        for item in argv[1:]:
            if not item.startswith("-") and not _path_in_worktree(item, worktree, blocked_paths):
                return False
    return True


def _path_in_worktree(
    relative: str,
    worktree: Path | None,
    blocked_paths: tuple[str, ...] = (),
) -> bool:
    if worktree is None:
        return False
    if Path(relative).is_absolute():
        return False
    if ".." in Path(relative).parts:
        return False
    candidates = (relative,)
    # `unittest` also accepts dotted module targets, optionally followed by a
    # class or method selector. Resolve the longest existing module prefix so
    # a blocked test cannot be reached merely by changing its spelling.
    if "/" not in relative and "\\" not in relative and "." in relative:
        parts = relative.split(".")
        for end in range(len(parts), 0, -1):
            # A dotfile such as `.env` splits to an empty first segment, which
            # is not a module path at all. Skip rather than raise: the literal
            # candidate below still checks it against the blocked paths.
            if not all(parts[:end]):
                continue
            module = Path(*parts[:end]).with_suffix(".py")
            if (worktree / module).is_file():
                candidates = (module.as_posix(),)
                break
    for candidate in candidates:
        try:
            resolved = (worktree / candidate).resolve()
            resolved_relative = resolved.relative_to(worktree.resolve()).as_posix()
        except (OSError, ValueError):
            return False
        if path_matches_blocked(resolved_relative, blocked_paths):
            return False
    return True


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


def _clean_environment() -> dict[str, str]:
    """PATH and locale only. No inherited credentials, tokens or AL/X config."""
    allowed = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "HOME")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def run_permitted_command(
    argv: list[str],
    worktree: Path,
    timeout_seconds: int = DEFAULT_COMMAND_SECONDS,
    blocked_paths: tuple[str, ...] = (),
    output_characters: int = MAX_COMMAND_OUTPUT_CHARACTERS,
) -> CodingCommandRecord:
    """Run one argv with cwd bound to the worktree (D-063).

    `output_characters` of 0 returns stdout unbounded, for the one caller that
    applies its own larger bound and needs the length before it is applied.
    Bounding twice hid how much had been cut.
    """
    if not command_permitted(argv, worktree, blocked_paths):
        raise CodingError("command_not_permitted")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
        raise CodingError("arguments_unusable")
    if not 1 <= timeout_seconds <= MAX_COMMAND_SECONDS:
        raise CodingError("arguments_unusable")
    bound = list(argv)
    if bound[0].lower() in _PYTHON_NAMES:
        # Use this runtime's interpreter when Python is named without a path.
        bound[0] = sys.executable
    try:
        completed = run_coding_subprocess(subprocess.run,  # noqa: S603 - argv, never shell
            bound,
            cwd=worktree,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout if isinstance(error.stdout, str) else ""
        stderr = error.stderr if isinstance(error.stderr, str) else ""
        return CodingCommandRecord(
            tuple(argv),
            -1,
            stdout if output_characters == 0 else _bound(stdout, output_characters),
            _bound(stderr, MAX_COMMAND_OUTPUT_CHARACTERS),
            True,
            True,
        )
    except OSError as error:
        raise CodingError("coding_unavailable") from error
    return CodingCommandRecord(
        tuple(argv),
        completed.returncode,
        (completed.stdout or "") if output_characters == 0
        else _bound(completed.stdout or "", output_characters),
        _bound(completed.stderr or "", MAX_COMMAND_OUTPUT_CHARACTERS),
        False,
        True,
    )


def same_main_pytest_failure(
    branch: CodingCommandRecord, checkout: Path, timeout_seconds: int,
    check_set: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[bool, str]:
    """Compare a failed pytest run with committed main without moving HEAD.

    A temporary git archive is source evidence, never a second checkout. Cache
    entries contain only a complete failure signature keyed to the exact main
    commit, command and toolchain. Any uncertainty leaves the branch blocked.
    """
    if branch.timed_out or branch.exit_status != 1 or not branch.permitted:
        return False, "branch_result_uncomparable"
    branch_signature = pytest_failure_signature(branch.stdout, checkout)
    if branch_signature is None:
        return False, "branch_failure_unparsed"
    try:
        main = run_coding_subprocess(
            subprocess.run, ["git", "rev-parse", "--verify", "refs/heads/main^{commit}"],
            cwd=checkout, capture_output=True, text=True, timeout=15, check=False,
        )
        if main.returncode != 0:
            return False, "main_unavailable"
        sha = main.stdout.strip()
        if len(sha) not in (40, 64) or any(c not in "0123456789abcdef" for c in sha):
            return False, "main_identity_invalid"
        origin = run_coding_subprocess(
            subprocess.run, ["git", "remote", "get-url", "origin"], cwd=checkout,
            capture_output=True, text=True, timeout=15, check=False,
        )
        origin_url = origin.stdout.strip() if origin.returncode == 0 else ""
        # The same environment filtering is used by both actual test runs.
        local_env = checkout / ".env"
        toolchain = {
            "python": sys.executable,
            "version": sys.version,
            "platform": platform.platform(),
            "environment": _clean_environment(),
            "packages": sorted((item.metadata.get("Name", ""), item.version)
                               for item in metadata.distributions()),
            "local_env_sha256": hashlib.sha256(local_env.read_bytes()).hexdigest()
                                if local_env.is_file() else None,
            "origin": origin_url,
        }
        # Executables reached through PATH can affect tests without appearing
        # among Python distributions. Fingerprint all of them, with no list of
        # favoured tools or known test failures.
        executables: list[tuple[str, str, int, int]] = []
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            try:
                candidates = tuple(Path(directory).iterdir())
            except FileNotFoundError:
                continue
            # Other OSError values deliberately reach the outer fail-closed
            # handler: an unreadable PATH directory may contain an executable
            # the tests use, so its toolchain identity cannot be established.
            for candidate in candidates:
                try:
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        stat = candidate.stat()
                        executables.append((str(candidate), str(candidate.resolve()),
                                            stat.st_size, stat.st_mtime_ns))
                except (FileNotFoundError, PermissionError):
                    # Unusable for this process; a disappearing entry also
                    # cannot be the executable this verification invokes.
                    continue
        toolchain["executables"] = sorted(executables)
        key = hashlib.sha256(json.dumps(
            [sha, branch.argv, check_set, toolchain], sort_keys=True,
        ).encode()).hexdigest()
        baseline_signature = _MAIN_FAILURE_CACHE.get(key)
        if baseline_signature is None:
            with tempfile.TemporaryDirectory(prefix="alx-main-verification-") as directory:
                snapshot = Path(directory)
                archive = run_coding_subprocess(
                    subprocess.run, ["git", "archive", sha], cwd=checkout,
                    capture_output=True, timeout=60, check=False,
                )
                if archive.returncode != 0:
                    return False, "main_archive_failed"
                with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
                    bundle.extractall(snapshot, filter="data")
                # Some repository tests need a genuine .git directory. Give
                # the temporary archive its own clean local identity, never a
                # pointer into the canonical checkout's metadata.
                commands = (
                    ["git", "init", "-q", "-b", "main"],
                    ["git", "config", "user.email", "baseline@example.invalid"],
                    ["git", "config", "user.name", "Baseline"],
                    ["git", "add", "-A"],
                    ["git", "commit", "-qm", "committed main snapshot"],
                )
                for argv in commands:
                    result = run_coding_subprocess(
                        subprocess.run, argv, cwd=snapshot,
                        env={**_clean_environment(),
                             "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
                             "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000"},
                        capture_output=True, timeout=60, check=False,
                    )
                    if result.returncode != 0:
                        return False, "main_snapshot_identity_failed"
                if origin_url:
                    remote = run_coding_subprocess(
                        subprocess.run, ["git", "remote", "add", "origin", origin_url],
                        cwd=snapshot, env=_clean_environment(),
                        capture_output=True, timeout=15, check=False,
                    )
                    if remote.returncode != 0:
                        return False, "main_snapshot_origin_failed"
                if local_env.is_file():
                    destination = snapshot / ".env"
                    destination.write_bytes(local_env.read_bytes())
                    destination.chmod(0o600)
                baseline = run_permitted_command(
                    list(branch.argv), snapshot, timeout_seconds=timeout_seconds,
                    output_characters=0,
                )
                if baseline.timed_out or baseline.exit_status != 1:
                    return False, "main_result_differs_or_unavailable"
                baseline_signature = pytest_failure_signature(baseline.stdout, snapshot)
                if baseline_signature is None:
                    return False, "main_failure_unparsed"
            _MAIN_FAILURE_CACHE[key] = baseline_signature
        equivalent = not (Counter(branch_signature) - Counter(baseline_signature))
        return (equivalent,
                f"same_main_failure:main={sha}:evidence={key}" if equivalent
                else f"failure_signature_changed:main={sha}:evidence={key}")
    except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError,
            subprocess.TimeoutExpired, CodingError) as error:
        return False, f"baseline_comparison_failed:{type(error).__name__}"



def failed_tests_pass_alone(
    branch: CodingCommandRecord, worktree: Path, timeout_seconds: int,
    blocked_paths: tuple[str, ...] = (),
) -> tuple[bool, str, tuple[str, ...]]:
    """Run only the tests a pytest check failed, once, on their own.

    A timing-sensitive test can fail under a loaded parallel run and pass
    alone. A different set fails each time, so main's failures never matched
    and jobs were blocked on tests they did not break (Friedl, 2026-10-23).
    Passing alone is recorded, never hidden: the tests are returned by name
    so AL/X can have them fixed. Any doubt leaves the check failed.
    """
    if branch.timed_out or branch.exit_status != 1 or not branch.permitted:
        return False, "rerun_uncomparable", ()
    failed = pytest_failed_tests(branch.stdout)
    # A full or clipped list may be missing a failure the rerun would skip.
    if (not failed or len(failed) >= MAX_FAILED_TEST_FINDINGS
            or any(len(node) >= MAX_FAILED_TEST_CHARACTERS for node in failed)):
        return False, "rerun_failures_unlisted", ()
    # The allowlist checks a node identifier as a whole path, so the file it
    # names is held to the worktree and blocked-path rules here as well.
    for node in failed:
        test_file = node.split("::", 1)[0]
        if (not _path_in_worktree(test_file, worktree, blocked_paths)
                or not (worktree / test_file).is_file()):
            return False, "rerun_not_permitted", ()
    argv = ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *failed]
    if not command_permitted(argv, worktree, blocked_paths):
        return False, "rerun_not_permitted", ()
    try:
        rerun = run_permitted_command(
            argv, worktree, timeout_seconds=timeout_seconds,
            blocked_paths=blocked_paths,
        )
    except CodingError as error:
        if error.code == "coding_cancelled":
            raise
        return False, f"rerun_failed_to_run:{error.code}", ()
    if rerun.timed_out or rerun.exit_status != 0:
        return False, "failed_alone_too", ()
    return True, f"passed_alone:{len(failed)}", failed

@dataclass(frozen=True, slots=True)
class GitEvidence:
    """Git evidence with its own completeness stated.

    Bounding used to be invisible: a clipped diff and a whole one were the
    same string, so nothing downstream could tell partial evidence from
    complete evidence. The length before bounding is kept so the difference is
    a fact rather than an inference.
    """

    status: str
    diff: str
    diff_characters: int

    @property
    def diff_truncated(self) -> bool:
        return self.diff_characters > len(self.diff)


def inspect_git(
    worktree: Path, paths: Sequence[str] = ()
) -> GitEvidence:
    """Mechanical git status and diff. One correct outcome, so not a model call.

    `paths` narrows the diff to the files this job is about. A job runs in a
    worktree it does not own, so an unrelated dirty tree otherwise spends the
    diff budget: on 2026-09-11 a 109k worktree diff clipped at 32k to files
    alphabetically before the ones under repair, and four sessions were shown
    the same truncated prefix of somebody else's work. Status still reports
    the whole tree, because what else is dirty is a fact the job needs.
    """
    status = run_permitted_command(
        ["git", "status", "--porcelain=v1", "-z", "-uall"], worktree
    )
    argv = ["git", "diff"]
    if paths:
        argv.extend(["--", *paths])
    # Read the diff at its own bound rather than the generic command bound.
    # run_permitted_command clips stdout to MAX_COMMAND_OUTPUT_CHARACTERS,
    # which is smaller: routing the diff through it clipped twice, so the
    # length reported here described an already-shortened string and could
    # call a truncated diff complete.
    diff = run_permitted_command(argv, worktree, output_characters=0)
    # The diff is bounded once, here, at its own limit. Its length before
    # bounding is what makes truncation visible rather than inferred.
    text = diff.stdout
    return GitEvidence(
        # Porcelain status is stdout only. A host warning on stderr is
        # diagnostic text, never a filename; treating it as status made a
        # clean checkout appear dirty when stdout was empty.
        _bound(status.stdout, MAX_COMMAND_OUTPUT_CHARACTERS),
        _bound(text, MAX_DIFF_CHARACTERS),
        len(text),
    )


def files_from_git_status(status: str) -> tuple[str, ...]:
    """Paths from porcelain v1, including literal spaces and quotes."""
    if "\0" in status:
        names = []
        entries = iter(status.split("\0"))
        for entry in entries:
            if not entry or len(entry) < 4:
                continue
            kind = entry[:2]
            path = entry[3:]
            # Porcelain v1 -z puts the source path of a rename/copy in the
            # next field. The first path is the destination that git diff
            # must inspect.
            if "R" in kind or "C" in kind:
                next(entries, None)
            if path:
                names.append(path)
        return tuple(names)
    names = []
    for line in status.splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            names.append(path)
    return tuple(names)


def is_test_command(argv: tuple[str, ...]) -> bool:
    if is_pytest_command(argv):
        return True
    return (len(argv) >= 3 and Path(argv[0]).name.lower() in _PYTHON_NAMES
            and argv[1] == "-m" and argv[2] == "unittest")


def is_pytest_command(argv: tuple[str, ...]) -> bool:
    """Whether this command runs pytest, whose summary names failed tests."""
    if not argv:
        return False
    name = Path(argv[0]).name.lower()
    if name == "pytest":
        return True
    return (name in _PYTHON_NAMES and len(argv) >= 3 and argv[1] == "-m"
            and argv[2] == "pytest")
