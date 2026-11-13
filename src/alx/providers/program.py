"""One program executor and its restart-independent worker (D-063).

Output goes straight to disk, never to a pipe held by the foreground Core.
The worker owns the deadline and atomic result publication even if Core exits.
No shell, user switch, network sandbox or interpretation of output is added.
"""

from __future__ import annotations

import json
import ctypes
import errno
import fcntl
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread
from uuid import uuid4

DISCOVERY_SECONDS = .1
RECOVERY_SECONDS = .5


def publish(path: Path, values: dict) -> None:
    """Publish a complete record once; recovery never replaces worker evidence."""
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def process_info(pid: int, *, require_stopped=False):
    """Kernel generation and parent PID; zombies have already stopped."""
    if sys.platform == "darwin":
        from alx.providers.darwin_process import DarwinBSDInfo
        info = DarwinBSDInfo()
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        size = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info):
            error = ctypes.get_errno()
            if error in (errno.ESRCH, errno.ENOENT):
                return None, 0
            raise OSError(error, "process identity unavailable")
        if info.pbi_pid != pid:
            raise OSError("process identity changed during inspection")
        if info.pbi_status == 5:
            return None, 0
        if require_stopped and info.pbi_status != 4:
            raise BlockingIOError("process has not stopped")
        return [int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)], int(info.pbi_ppid)
    try:
        # comm can contain spaces and parentheses. Fields after its final ')'
        # start at state (3); starttime is field 22.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None, 0
        if require_stopped and fields[0] not in ("T", "t"):
            raise BlockingIOError("process has not stopped")
        return [Path("/proc/sys/kernel/random/boot_id").read_text().strip(), fields[19]], int(fields[1])
    except (FileNotFoundError, ProcessLookupError):
        return None, 0


def process_identity(pid: int):
    return process_info(pid)[0]


def process_stopped(pid: int) -> bool:
    try:
        process_info(pid, require_stopped=True)
        return True
    except BlockingIOError:
        return False


def tagged_processes(token: str, known: dict | None = None, *, deadline=None,
                     inspect_tags=True) -> dict:
    """Find inherited run membership even after setsid, double fork or reparenting.

    Census parent links also capture children with a cleared environment, and
    observed identities remain owned after reparenting. The inherited tag covers
    forks between censuses. This is lifecycle bookkeeping, not hostile-code
    containment. No environment is logged; signals check kernel generations.
    """
    marker = ("ALX_PROGRAM_RUN=" + token).encode()
    if sys.platform == "darwin":
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        count = library.proc_listallpids(None, 0)
        if count <= 0:
            raise OSError(ctypes.get_errno(), "process census unavailable")
        pids = (ctypes.c_int * (count + 128))()
        count = library.proc_listallpids(pids, ctypes.sizeof(pids))
        if count <= 0:
            raise OSError(ctypes.get_errno(), "process census unavailable")
        candidates = list(pids[:count])
        libc = ctypes.CDLL(None, use_errno=True)
        # Reuse one bounded buffer for the census, rather than allocating a
        # megabyte for every live process. Ancestry itself uses BSD metadata.
        data = None
        def environment(pid):
            nonlocal data
            if data is None:
                data = ctypes.create_string_buffer(1024 * 1024)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
            size = ctypes.c_size_t(ctypes.sizeof(data))
            if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
                return ()
            raw = ctypes.string_at(data, size.value)
            argc = int.from_bytes(raw[:4], sys.byteorder)
            strings = raw[4:].split(b"\0")
            # Executable path, padding, argv, then environment.
            strings = strings[1:]
            while strings and not strings[0]:
                strings = strings[1:]
            return strings[argc:]
    else:
        candidates = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
        def environment(pid):
            try:
                return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            except OSError:
                return ()
    found = {}
    snapshot = {}
    def remember(values):
        found.update(values)
        if known is not None:
            known.update(values)
    for pid in candidates:
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("program census budget exhausted")
        try:
            identity, parent = process_info(pid)
        except PermissionError:
            # Other users' processes may be inaccessible, but known children
            # must never be mistaken for terminated processes.
            if known and pid in known:
                raise
            continue
        if identity is not None:
            snapshot[pid] = (identity, parent)
            if known and known.get(pid) == identity:
                found[pid] = identity
    def descendants():
        while True:
            children = {}
            for pid, (identity, parent) in snapshot.items():
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("program census budget exhausted")
                if (pid not in found and parent in found
                        and process_identity(parent) == found[parent]
                        and process_identity(pid) == identity):
                    children[pid] = identity
            if not children:
                return
            remember(children)
    descendants()
    if not inspect_tags:
        return found
    for pid, (identity, _) in snapshot.items():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("program census budget exhausted")
        if pid not in found and marker in environment(pid) and process_identity(pid) == identity:
            remember({pid: identity})
    descendants()
    return found


def stop_program(token: str, known: dict, *, checkpoint=None) -> bool:
    """Bound one attempt; false means the durable run must remain pending."""
    storage_error = None

    def persist():
        nonlocal storage_error
        if checkpoint is not None:
            try:
                checkpoint(known)
            except OSError as failure:
                # Restart evidence must never be a prerequisite for termination.
                if storage_error is None:
                    storage_error = failure

    def finish(stopped):
        if storage_error is not None:
            if not stopped:
                storage_error.add_note("program cleanup remains unconfirmed; recovery must retry")
            raise storage_error
        return stopped

    deadline = time.monotonic() + RECOVERY_SECONDS
    frozen = {}
    while True:
        if time.monotonic() >= deadline:
            return finish(False)
        try:
            # Preserve ancestry until every discovered generation is frozen.
            # SIGSTOP delivery is asynchronous: confirm it before the census
            # that proves this tree cannot create any further children.
            stable = True
            for pid, identity in known.items():
                if time.monotonic() >= deadline:
                    return finish(False)
                if process_identity(pid) == identity:
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
                    if process_stopped(pid) and process_identity(pid) == identity:
                        frozen[pid] = identity
                    elif process_identity(pid) == identity:
                        stable = False
            known.update(tagged_processes(token, known, deadline=deadline))
            persist()
            # A newly found descendant must itself be frozen and rescanned
            # before any parent is killed. Incomplete attempts retain ancestry.
            for pid, identity in known.items():
                if time.monotonic() >= deadline:
                    return finish(False)
                if process_identity(pid) == identity and frozen.get(pid) != identity:
                    stable = False
            if stable:
                for pid, identity in known.items():
                    if time.monotonic() >= deadline:
                        return finish(False)
                    if process_identity(pid) == identity:
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
            live = {}
            for pid, identity in known.items():
                if time.monotonic() >= deadline:
                    return finish(False)
                if process_identity(pid) == identity:
                    live[pid] = identity
        except (OSError, TimeoutError) as failure:
            # A census may have observed children before exhausting its budget.
            # Preserve those generations even though it cannot prove absence.
            persist()
            if storage_error is not None:
                if failure is storage_error:
                    raise
                # Keep the cleanup failure primary, with the write failure as
                # its cause. Neither is evidence that cleanup completed.
                raise failure from storage_error
            return False
        if not live:
            return finish(time.monotonic() < deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return finish(False)
        time.sleep(min(DISCOVERY_SECONDS, remaining))


def save_children(root: Path, known: dict) -> None:
    """Keep observed generations across worker death and recovery retries."""
    path = root / "children.json"
    temporary = root / "children.tmp"
    with temporary.open("w") as stream:
        json.dump(known, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def load_children(root: Path) -> dict:
    path = root / "children.json"
    return {int(pid): identity for pid, identity in json.loads(path.read_text()).items()} if path.exists() else {}


def result_values(root: Path, status: str, exit_code: int = -1,
                  duration: float = 0, timed_out: bool = False, error: str = "") -> dict:
    return {
        "run_id": root.name, "status": status, "exit_code": exit_code,
        "duration_seconds": duration, "timed_out": timed_out, "error": error,
        "stdout": summary(root / "stdout") if (root / "stdout").exists() else "",
        "stderr": summary(root / "stderr") if (root / "stderr").exists() else "",
        "full_output_path": str(root), "stdout_path": str(root / "stdout"),
        "stderr_path": str(root / "stderr"),
    }


def reconcile(root: Path) -> None:
    """The worker's inherited lease covers launch, execution and publication.

    A nonblocking acquisition proves no worker can still publish. The generation
    record also protects discovery of runs made before leases were introduced.
    """
    if (root / "result.json").exists():
        return
    with (root / "worker.lock").open("a+b") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if (root / "result.json").exists():
            return
        worker_path = root / "worker.json"
        if worker_path.exists():
            worker = json.loads(worker_path.read_text())
            if worker["started_at"] is not None and process_identity(worker["pid"]) == worker["started_at"]:
                return
        known = load_children(root)
        if not stop_program(root.name, known, checkpoint=lambda values: save_children(root, values)):
            return
        pending = root / "pending_result.json"
        values = (json.loads(pending.read_text()) if pending.exists() else
                  result_values(root, "interrupted", error="program_worker_interrupted"))
        # Descendants may have written output while cleanup was pending.
        for name in ("stdout", "stderr"):
            path = root / name
            values[name] = summary(path) if path.exists() else ""
        publish(root / "result.json", values)


def summary(path: Path, bound: int = 4096) -> str:
    with path.open("rb") as stream:
        head = stream.read(bound)
        size = stream.seek(0, 2)
        if size <= bound:
            return head.decode("utf-8", errors="replace")
        half = bound // 2
        stream.seek(-half, 2)
        return (head[:half].decode("utf-8", errors="replace")
                + "\n[output omitted]\n" + stream.read(half).decode("utf-8", errors="replace"))


# How long a call waits for its program before handing it to the background.
# A program that finishes in time answers inside the session that ran it; one
# handed over wakes a fresh session when it ends, which was 41 sessions on one
# morning of KiCad scripts when this was one second (D-067).
FOREGROUND_SECONDS = 120.0
DEFAULT_SECONDS = 60
# Where she runs most programs: her own repository.
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[3]


class ProgramRunner:
    def __init__(self, root: Path, banking_targets: tuple[str, ...],
                 foreground_seconds: float = FOREGROUND_SECONDS) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.targets = tuple(item.casefold() for item in banking_targets)
        if any(not item.strip() for item in self.targets):
            raise ValueError("banking targets must not be blank")
        self.foreground_seconds = foreground_seconds

    def run(self, arguments, conversation_id: str = "") -> dict:
        argv = arguments["argv"]
        # Defaults, so a run never fails for a field left out (2026-11-13:
        # about ten refusals in one morning, each costing her a step).
        seconds = arguments.get("time_limit_seconds", DEFAULT_SECONDS)
        if (not isinstance(argv, (list, tuple)) or not argv
                or any(not isinstance(item, str) or "\x00" in item for item in argv)
                or not argv[0] or isinstance(seconds, bool)
                or not isinstance(seconds, (int, float)) or not math.isfinite(seconds)
                or seconds <= 0):
            raise ValueError("invalid argv or time limit")
        directory = Path(arguments.get("working_directory") or DEFAULT_DIRECTORY).resolve(strict=True)
        if not directory.is_dir():
            raise ValueError("working directory must be a directory")
        # Check every argv element, including wrapper arguments and explicit
        # shell bodies. Resolve executable aliases as well as literal paths.
        for item in argv:
            folded = item.casefold()
            if any(target in folded for target in self.targets):
                raise PermissionError("banking target prohibited")
            candidate = shutil.which(item) if "/" not in item else str(directory / item)
            names = {Path(item).name.casefold()}
            if candidate:
                names.add(Path(candidate).resolve().name.casefold())
            if "security" in names or any(
                token.strip("'\";()") in ("security", "/usr/bin/security")
                for token in item.split()
            ):
                raise PermissionError("Keychain tool prohibited")
        run_id = uuid4().hex
        run_root = self.root / run_id
        run_root.mkdir(mode=0o700)
        with (run_root / "worker.lock").open("a+b") as lease, (run_root / "worker.log").open("wb") as log:
            fcntl.flock(lease, fcntl.LOCK_EX)
            publish(run_root / "request.json", {
                "argv": list(argv), "working_directory": str(directory),
                "time_limit_seconds": seconds, "conversation_id": conversation_id,
            })
            # Discovery can see the request only after the launch lease is held.
            # A stopped foreground invocation still owes Core its eventual result.
            (run_root / "background").touch()
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(run_root), str(lease.fileno())],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
                pass_fds=(lease.fileno(),),
            )
        try:
            worker.wait(timeout=self.foreground_seconds)
        except subprocess.TimeoutExpired:
            Thread(target=worker.wait, daemon=True).start()
            return {"run_id": run_id, "status": "running", "full_output_path": str(run_root)}
        result = run_root / "result.json"
        if not result.exists():
            if (run_root / "pending_result.json").exists():
                return {"run_id": run_id, "status": "running", "full_output_path": str(run_root)}
            raise OSError("program worker failed: " + summary(run_root / "worker.log"))
        (run_root / "handled").touch()
        return json.loads(result.read_text(encoding="utf-8"))


def work(root: Path) -> None:
    publish(root / "worker.json", {"pid": os.getpid(), "started_at": process_identity(os.getpid())})
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    timed_out = False
    error = ""
    exit_code = -1
    known = {}
    process = None
    with (root / "stdout").open("wb") as stdout, (root / "stderr").open("wb") as stderr:
        try:
            process = subprocess.Popen(
                request["argv"], cwd=request["working_directory"],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True, close_fds=True,
                env={**os.environ, "ALX_PROGRAM_RUN": root.name},
            )
            identity = process_identity(process.pid)
            if identity is not None:
                known[process.pid] = identity
                save_children(root, known)
            deadline = started + request["time_limit_seconds"]
            next_discovery = started
            while process.poll() is None:
                now = time.monotonic()
                if now >= deadline:
                    timed_out = True
                    break
                if now >= next_discovery:
                    try:
                        known.update(tagged_processes(root.name, known, deadline=deadline,
                                                      inspect_tags=False))
                    except TimeoutError:
                        timed_out = True
                        break
                    save_children(root, known)
                    next_discovery = time.monotonic() + DISCOVERY_SECONDS
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(.01, remaining))
        except OSError as failure:
            error = str(failure)
        finally:
            if process is not None:
                code = process.poll()
                if code is not None:
                    exit_code = code
            duration = time.monotonic() - started
            # These are facts, not completion: result.json remains absent until
            # cleanup is confirmed, including after worker death.
            pending_failed = True
            try:
                publish(root / "pending_result.json", result_values(
                    root, "completed", exit_code, duration, timed_out, error,
                ))
                pending_failed = False
            finally:
                # Even result construction or persistence failure cannot skip
                # termination. Propagate it only after attempting cleanup.
                if process is not None:
                    while True:
                        cleanup_complete = stop_program(
                            root.name, known, checkpoint=lambda values: save_children(root, values),
                        )
                        try:
                            process.wait(timeout=0)
                        except subprocess.TimeoutExpired:
                            cleanup_complete = False
                        exit_code = process.returncode
                        if cleanup_complete or pending_failed:
                            break
                        # The independent worker keeps ownership across bounded
                        # attempts; completion must not need a recovery tick.
                        time.sleep(.01)
    publish(root / "result.json", result_values(
        root, "completed", exit_code, duration, timed_out, error,
    ))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with os.fdopen(int(sys.argv[2]), "rb"):
        work(Path(sys.argv[1]))
