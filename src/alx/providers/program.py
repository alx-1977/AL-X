"""One program executor and its restart-independent worker (D-063).

Output goes straight to disk, never to a pipe held by the foreground Core.
The worker owns the deadline and atomic result publication even if Core exits.
No shell, user switch, network sandbox or interpretation of output is added.
"""

from __future__ import annotations

import json
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
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(values), encoding="utf-8")
    temporary.replace(path)


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
        publish(run_root / "request.json", {
            "argv": list(argv), "working_directory": str(directory),
            "time_limit_seconds": seconds, "conversation_id": conversation_id,
        })
        # A stopped foreground invocation still owes Core its eventual result.
        # Publish the handoff before launch, not only after the foreground wait.
        (run_root / "background").touch()
        with (run_root / "worker.log").open("wb") as log:
            worker = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(run_root)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True,
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
    request = json.loads((root / "request.json").read_text(encoding="utf-8"))
    started = time.monotonic()
    timed_out = False
    error = ""
    exit_code = -1
    with (root / "stdout").open("wb") as stdout, (root / "stderr").open("wb") as stderr:
        try:
            process = subprocess.Popen(
                request["argv"], cwd=request["working_directory"],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                start_new_session=True, close_fds=True,
            )
            try:
                exit_code = process.wait(timeout=request["time_limit_seconds"])
            except subprocess.TimeoutExpired:
                timed_out = True
            finally:
                # Stop descendants too, including children whose leader has
                # exited. They must not continue past the recorded completion.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                exit_code = process.returncode
        except OSError as failure:
            error = str(failure)
    publish(root / "result.json", {
        "run_id": root.name, "status": "completed", "exit_code": exit_code,
        "duration_seconds": time.monotonic() - started, "timed_out": timed_out,
        "error": error, "stdout": summary(root / "stdout"), "stderr": summary(root / "stderr"),
        "full_output_path": str(root), "stdout_path": str(root / "stdout"),
        "stderr_path": str(root / "stderr"),
    })


if __name__ == "__main__":
    work(Path(sys.argv[1]))
