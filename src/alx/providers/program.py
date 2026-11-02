"""One program executor and its restart-independent worker (D-063).

Output goes straight to disk, never to a pipe held by the foreground Core.
The worker owns the deadline and atomic result publication even if Core exits.
No shell, user switch, network sandbox or interpretation of output is added.
"""

from __future__ import annotations

import json
import ctypes
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
    finally:
        temporary.unlink(missing_ok=True)


def process_info(pid: int):
    """Kernel generation and parent PID; zombies have already stopped."""
    if sys.platform == "darwin":
        from alx.providers.darwin_process import DarwinBSDInfo
        info = DarwinBSDInfo()
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        size = library.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size != ctypes.sizeof(info) or info.pbi_pid != pid or info.pbi_status == 5:
            return None, 0
        return [int(info.pbi_start_tvsec), int(info.pbi_start_tvusec)], int(info.pbi_ppid)
    try:
        # comm can contain spaces and parentheses. Fields after its final ')'
        # start at state (3); starttime is field 22.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None, 0
        return [Path("/proc/sys/kernel/random/boot_id").read_text().strip(), fields[19]], int(fields[1])
    except (OSError, IndexError, ValueError):
        return None, 0


def process_identity(pid: int):
    return process_info(pid)[0]


def tagged_processes(token: str, known: dict | None = None) -> dict:
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
        def environment(pid):
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
            size = ctypes.c_size_t(1024 * 1024)
            data = ctypes.create_string_buffer(size.value)
            if libc.sysctl(mib, 3, data, ctypes.byref(size), None, 0):
                return ()
            raw = data.raw[:size.value]
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
    for pid in candidates:
        identity, parent = process_info(pid)
        if identity is not None:
            snapshot[pid] = (identity, parent)
            if known and known.get(pid) == identity:
                found[pid] = identity
        if identity is not None and marker in environment(pid) and process_identity(pid) == identity:
            found[pid] = identity
    while True:
        children = {pid: identity for pid, (identity, parent) in snapshot.items()
                    if pid not in found and parent in found
                    and process_identity(parent) == found[parent]
                    and process_identity(pid) == identity}
        if not children:
            break
        found.update(children)
    return found


def stop_program(token: str, known: dict) -> None:
    """Stop and await all run members before any terminal record is visible."""
    while True:
        known.update(tagged_processes(token, known))
        live = {pid: identity for pid, identity in known.items()
                if process_identity(pid) == identity}
        if not live:
            return
        for pid, identity in live.items():
            if process_identity(pid) == identity:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        time.sleep(.01)


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
        stop_program(root.name, {})
        publish(root / "result.json", result_values(root, "interrupted", error="program_worker_interrupted"))


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


class ProgramRunner:
    def __init__(self, root: Path, banking_targets: tuple[str, ...],
                 foreground_seconds: float = 1.0) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.targets = tuple(item.casefold() for item in banking_targets)
        if any(not item.strip() for item in self.targets):
            raise ValueError("banking targets must not be blank")
        self.foreground_seconds = foreground_seconds

    def run(self, arguments, conversation_id: str = "") -> dict:
        argv = arguments["argv"]
        seconds = arguments["time_limit_seconds"]
        if (not isinstance(argv, (list, tuple)) or not argv
                or any(not isinstance(item, str) or "\x00" in item for item in argv)
                or not argv[0] or isinstance(seconds, bool)
                or not isinstance(seconds, (int, float)) or not math.isfinite(seconds)
                or seconds <= 0):
            raise ValueError("invalid argv or time limit")
        directory = Path(arguments["working_directory"]).resolve(strict=True)
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
            while process.poll() is None:
                known.update(tagged_processes(root.name, known))
                if time.monotonic() - started >= request["time_limit_seconds"]:
                    timed_out = True
                    break
                time.sleep(.01)
        except OSError as failure:
            error = str(failure)
        finally:
            if process is not None:
                # Cleanup failures must not be turned into a finished run.
                # The lease is released on worker exit; discovery retries here.
                stop_program(root.name, known)
                process.wait()
                exit_code = process.returncode
    publish(root / "result.json", result_values(
        root, "completed", exit_code, time.monotonic() - started, timed_out, error,
    ))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    with os.fdopen(int(sys.argv[2]), "rb"):
        work(Path(sys.argv[1]))
