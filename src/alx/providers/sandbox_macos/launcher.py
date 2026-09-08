"""The trusted launcher: start one experiment, supervise it, outlive nothing.

Run as a script by the macOS `SandboxRunner`, never imported by the runtime. It is the
one place that applies resource limits and establishes the experiment's process
group, and it exists because doing either from inside AL/X was unsafe.

Two defects made it necessary, and they share a cause: the parent that starts
an experiment is a live, multi-threaded process with its own work to do.

`preexec_fn` runs between fork and exec, in a child that has one thread and a
copy of every lock the other threads were holding. A Core turn is dispatched
through `asyncio.to_thread`, so the runtime is genuinely multi-threaded, and a
child that inherits a held allocator lock deadlocks before it can exec. `Popen`
then never returns, and the session lease, the day's reservation and the Core
turn are held by a call that cannot finish.

And when the runtime dies, whatever it started keeps running. The child was
given its own session, its pid lived only in the parent's memory, and a
sleeping process consumes no CPU allowance, so neither the wall timer nor
RLIMIT_CPU ends it. D-027 says a process group left behind by a crash is reaped
when the runtime starts; nothing implemented that.

So this process sits between them. It applies the limits in a single-threaded
process where doing so is safe, execs the interpreter, and watches two things
at once: the experiment, and the parent that asked for it. If the parent
disappears the whole group is killed and reaped here, immediately, rather than
waiting for a restart that may never come.

It is deliberately small and deterministic. It reads a fixed set of arguments,
starts exactly one program, and exits. It has no configuration file, no
network, no capability interface, and no way to be asked to run anything but
the interpreter it was given. It holds no AL/X authority: it cannot reach the
Core, a capability, a goal or a store, and it imports nothing from `alx`.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import resource
import select
import signal
import subprocess
import sys
import time


# How long a killed group is given to disappear before the launcher stops
# waiting for it. Short: SIGKILL is not negotiable, and anything still present
# after this is stuck in the kernel rather than ignoring the signal.
_REAP_GRACE_SECONDS = 2.0

# How often the parent is checked. Frequent enough that an orphaned experiment
# dies promptly, cheap enough to be irrelevant next to the run itself.
_PARENT_POLL_SECONDS = 0.25


class _DarwinBSDInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _process_started_at(pid: int) -> tuple[int, int] | None:
    """Return the Darwin kernel start identity for one live process."""
    if pid <= 0 or sys.platform != "darwin":
        return None
    info = _DarwinBSDInfo()
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        function = library.proc_pidinfo
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        function.restype = ctypes.c_int
        size = function(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    except (AttributeError, OSError):
        return None
    if size != ctypes.sizeof(info) or info.pbi_pid != pid:
        return None
    return int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)


def _publish_live_run(path: str, values: dict[str, object]) -> bool:
    """Atomically publish recovery identity before the protocol report."""
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(values, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False
    return True

def _apply_limits(cpu_seconds: int, file_bytes: int, processes: int) -> None:
    """Bound the child, in a process where it is safe to do so.

    Applied here rather than through `preexec_fn` for the reason this file
    exists: this process is single-threaded, so there is no lock another
    thread could have been holding when the fork happened.

    Deliberately no address-space limit. RLIMIT_AS is ineffective on macOS
    arm64 - verified, not assumed - so setting one would record a ceiling that
    does not hold, which is worse than recording none.
    """
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    resource.setrlimit(resource.RLIMIT_NPROC, (processes, processes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _emit(values: dict[str, object]) -> bool:
    """Write one protocol record, or report that the runtime is already gone."""
    try:
        print(json.dumps(values), flush=True)
    except (BrokenPipeError, OSError):
        return False
    return True


def _report(reason: str, status: int | None) -> int:
    """Say what happened, and exit 0 for having supervised it.

    The outcome used to be encoded in this process's exit status: 124 for a
    deadline, 125 for a lost parent, `128 - signal` for a signalled program.
    Those numbers are also ordinary exit statuses a program may choose, so a
    program that exited 124 was reported as a sandbox timeout, and one that
    exited 137 as a program killed by SIGKILL. Evidence that cannot be told
    apart from something else is not evidence.

    So the supervisor's account and the program's status travel separately.
    This line is the account; the status inside it is the program's own, or
    null when the sandbox ended the run rather than the program ending itself.
    """
    # Parent loss closes the report pipe. Cleanup has already happened;
    # reporting must not be able to undo it.
    _emit({"reason": reason, "status": status})
    return 0


def _reap(group: int, leader: subprocess.Popen | None = None) -> None:
    """End the experiment's whole process group.

    The group is the experiment's own, not this launcher's. An earlier version
    put the experiment in the launcher's group and then tried to step out of it
    before signalling, because `killpg` would otherwise have killed the
    supervisor too - which it did, turning every successful run into an exit
    status of -9.

    Stepping out cannot work. The launcher is a session leader, because the
    runtime starts it with its own session, and a session leader is forbidden
    to change its process group: `setpgid` fails with EPERM every time. The
    error was caught and the function returned, so the kill never happened at
    all and an experiment outlived the runtime exactly as before.

    Giving the experiment its own group removes the conflict rather than
    working around it: the launcher is not a member, so `killpg` reaches
    everything the experiment started and nothing else.
    """
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + _REAP_GRACE_SECONDS
    while time.monotonic() < deadline:
        if leader is not None:
            leader.poll()
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--sandbox-exec", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--program", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--live-note", required=True)
    parser.add_argument("--started-at", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--parent-process-group", type=int, required=True)
    parser.add_argument("--parent-start-seconds", type=int, required=True)
    parser.add_argument("--parent-start-microseconds", type=int, required=True)
    # The descriptors the parent opened for the program's output. Numbers
    # rather than paths, because the run directory is deliberately not
    # writable from inside the sandbox.
    parser.add_argument("--stdout-fd", type=int, required=True)
    parser.add_argument("--stderr-fd", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--processes", type=int, required=True)
    parser.add_argument("--wall-seconds", type=float, required=True)
    parser.add_argument("--parent-fd", type=int, required=True)
    arguments = parser.parse_args(argv)

    # This trusted supervisor deliberately runs outside Seatbelt. A confined
    # process may signal only itself under the approved profile, so placing the
    # supervisor inside that boundary made cross-session cleanup impossible.
    # The program it starts remains behind sandbox-exec in its own session.
    # The program's streams are the descriptors this launcher was given, which
    # the parent opened on the run directory. The run directory is not writable
    # from inside the sandbox - that is what keeps a program from editing its
    # own evidence - so the files cannot be opened here, and the descriptors
    # are inherited instead.
    try:
        child = subprocess.Popen(  # noqa: S603 - confined experiment site
            [
                arguments.sandbox_exec,
                "-f",
                arguments.profile,
                arguments.interpreter,
                "-E",
                "-S",
                arguments.program,
            ],
            stdin=subprocess.DEVNULL,
            stdout=arguments.stdout_fd,
            stderr=arguments.stderr_fd,
            cwd=arguments.directory,
            env={},
            close_fds=True,
            # The experiment's own session, so its group contains it and
            # whatever it spawns, and never this supervisor. That is what
            # makes the group signallable.
            start_new_session=True,
            # Applied to the child, from this process. `preexec_fn` is unsafe
            # in a multi-threaded program, which is what the AL/X runtime is
            # and this launcher deliberately is not: nothing here has ever
            # started a thread, so there is no lock a fork could inherit held.
            # Applying the limits to this process instead would bound the
            # supervisor and, on a host where NPROC is already exhausted,
            # refuse the very spawn it exists to perform.
            preexec_fn=lambda: _apply_limits(
                arguments.cpu_seconds,
                arguments.file_bytes,
                arguments.processes,
            ),
        )
    except OSError:
        return 71

    # The experiment's group, read now that it exists. Reported to the parent
    # so a later runtime can reap this group after a crash, and used by every
    # kill below.
    try:
        group = os.getpgid(child.pid)
    except OSError:
        group = child.pid

    process_started_at = _process_started_at(child.pid)
    if process_started_at is None or not _publish_live_run(
        arguments.live_note,
        {
            "identity": arguments.identity,
            "pid": child.pid,
            "process_group": group,
            "process_started_at": list(process_started_at or ()),
            "started_at": arguments.started_at,
            "parent_pid": arguments.parent_pid,
            "parent_process_group": arguments.parent_process_group,
            "parent_started_at": [
                arguments.parent_start_seconds,
                arguments.parent_start_microseconds,
            ],
            "launcher_pid": os.getpid(),
        },
    ):
        _reap(group, child)
        return 72

    # The identity a later runtime uses to decide whether a surviving process
    # belongs to this run. The durable note above is published first, so killing
    # this launcher before the parent reads the line cannot erase that identity.
    if not _emit(
        {
            "launcher_pid": os.getpid(),
            "experiment_pid": child.pid,
            "process_group": group,
            "identity": arguments.identity,
            "started_at": time.time(),
        }
    ):
        # The runtime can disappear in the few instructions between spawning
        # the experiment and reporting its identity. A broken first report is
        # therefore parent-loss evidence too, not a reason to abandon cleanup.
        _reap(group, child)
        return 0

    deadline = time.monotonic() + arguments.wall_seconds
    while True:
        status = child.poll()
        if status is not None:
            # The program ended on its own. Anything it spawned is still in
            # its group, and D-027 does not permit that to outlive the call.
            _reap(group, child)
            return _report("exited", status)

        if time.monotonic() >= deadline:
            _reap(group, child)
            return _report("timeout", None)

        # The runtime owns the only write end of this pipe. EOF is therefore
        # kernel evidence that the runtime disappeared or deliberately closed
        # its end; it does not depend on PID reuse or reparenting behaviour.
        readable, _, _ = select.select(
            [arguments.parent_fd],
            [],
            [],
            min(_PARENT_POLL_SECONDS, max(0.0, deadline - time.monotonic())),
        )
        if readable and os.read(arguments.parent_fd, 1) == b"":
            _reap(group, child)
            return _report("orphan", None)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
