"""The trusted launcher: start one experiment, supervise it, outlive nothing.

Run as a script by `SandboxRunner`, never imported by the runtime. It is the
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
import json
import os
import resource
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


def _reap(group: int) -> None:
    """End everything in the group except this process.

    Signalling the group would include the launcher, which leads it: doing so
    turned every successful run into an exit status of -9, and SIGKILL cannot
    be ignored to work around it. So the launcher leaves the group first and
    then signals it. From outside, `killpg` reaches every member and not this
    process, which is exactly the set that must not outlive the call.

    Enumerating the group would need `ps`, and the profile allows executing
    only the interpreter - correctly, and this is not a reason to widen it.
    """
    try:
        os.setpgid(0, 0)
    except OSError:
        # Already elsewhere, or not permitted. Killing the group would then
        # include this process; exiting is what ends the run either way.
        return
    try:
        os.killpg(group, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + _REAP_GRACE_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            return
        time.sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--interpreter", required=True)
    parser.add_argument("--program", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--identity", required=True)
    # The descriptors the parent opened for the program's output. Numbers
    # rather than paths, because the run directory is deliberately not
    # writable from inside the sandbox.
    parser.add_argument("--stdout-fd", type=int, required=True)
    parser.add_argument("--stderr-fd", type=int, required=True)
    parser.add_argument("--cpu-seconds", type=int, required=True)
    parser.add_argument("--file-bytes", type=int, required=True)
    parser.add_argument("--processes", type=int, required=True)
    parser.add_argument("--wall-seconds", type=float, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args(argv)

    # Its own session, so the experiment and anything it spawns can be
    # signalled as one group, and so a terminal signal to AL/X does not reach
    # the experiment by accident. The parent already starts this process in a
    # new session, in which case it is the leader already and setsid refuses;
    # either way the group below is this process's own.
    try:
        os.setsid()
    except OSError:
        pass
    group = os.getpgid(0)

    # The identity a later runtime uses to decide whether a surviving process
    # is this run's or an unrelated one that reused the number. Written before
    # the program starts and to stdout, so the caller records it without
    # racing the child.
    print(
        json.dumps(
            {
                "launcher_pid": os.getpid(),
                "process_group": group,
                "identity": arguments.identity,
                "started_at": time.time(),
            }
        ),
        flush=True,
    )

    # The program's streams are the descriptors this launcher was given, which
    # the parent opened on the run directory. The run directory is not writable
    # from inside the sandbox - that is what keeps a program from editing its
    # own evidence - so the files cannot be opened here, and the descriptors
    # are inherited instead.
    try:
        child = subprocess.Popen(  # noqa: S603 - the one execution site
            [arguments.interpreter, "-E", "-S", arguments.program],
            stdin=subprocess.DEVNULL,
            stdout=arguments.stdout_fd,
            stderr=arguments.stderr_fd,
            cwd=arguments.directory,
            env={},
            close_fds=False,
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

    deadline = time.monotonic() + arguments.wall_seconds
    while True:
        status = child.poll()
        if status is not None:
            # The leader finished. Anything it spawned is still in this
            # group, and D-027 does not permit that to outlive the call.
            _reap(group)
            return status if status >= 0 else 128 - status

        if time.monotonic() >= deadline:
            _reap(group)
            return 124

        # The parent that asked for this experiment. If it is gone, the
        # run is unsupervised: nothing is measuring its wall clock, nothing
        # will collect its evidence, and nothing will delete what it wrote.
        # A sleeping process would otherwise sit there indefinitely,
        # because CPU limits do not end a process that uses no CPU.
        if os.getppid() != arguments.parent_pid:
            _reap(group)
            return 125

        time.sleep(_PARENT_POLL_SECONDS)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    sys.exit(main())
