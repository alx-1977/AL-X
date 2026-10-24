"""The launcher's sole process-output writer, with idle-time expiration.

The child writes to a pipe, never to an inode that maintenance might replace.
Only this collector writes the capture. Each output line gets its capture time
and ERROR label, including output emitted before Python logging is available.
"""
from __future__ import annotations

import codecs
from datetime import datetime
import os
from pathlib import Path
import select
import sys
import time
from typing import Callable

from alx.observability.self_record import LOG_RETENTION, _LINE


def _expire(path: Path, now: float) -> float:
    """Compact one capture and return its next expiration time.

    The collector has no open writer during replacement. Readback can finish
    on its old descriptor; its independent time filter excludes expired data.
    Legacy untimestamped captures retain their explicit file-mtime provenance.
    """
    cutoff = now - LOG_RETENTION.total_seconds()
    fallback = path.stat().st_mtime
    temporary = path.with_name("." + path.name + ".compacting")
    next_expiry = float("inf")
    expired = False
    at = fallback
    try:
        with path.open(encoding="utf-8", errors="replace") as source, temporary.open(
                "w", encoding="utf-8") as destination:
            for line in source:
                match = _LINE.match(line.rstrip("\n"))
                if match:
                    at = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
                if at <= cutoff:
                    expired = True
                    continue
                next_expiry = min(next_expiry, at + LOG_RETENTION.total_seconds())
                destination.write(line)
        if expired:
            os.replace(temporary, path)
            # Compaction must not renew an untimestamped archive's provenance.
            os.utime(path, (fallback, fallback))
    finally:
        temporary.unlink(missing_ok=True)
    return next_expiry


def capture_output(fd: int, path: Path, *, clock: Callable[[], float] = time.time,
                   wait: Callable[[int, float], bool] | None = None) -> None:
    """Drain stdout/stderr through EOF; maintain retention even with no output."""
    wait = wait or (lambda descriptor, timeout: bool(select.select([descriptor], [], [], timeout)[0]))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Opening an existing capture must not renew legacy mtime provenance.
    with path.open("ab"):
        pass
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    with path.open("rb") as existing:
        if path.stat().st_size:
            existing.seek(-1, os.SEEK_END)
            line_start = existing.read(1) == b"\n"
        else:
            line_start = True
    next_expiry = 0.0

    def write(text: str) -> None:
        nonlocal line_start, next_expiry
        with path.open("a", encoding="utf-8") as output:
            for part in text.splitlines(keepends=True):
                if line_start:
                    at = clock()
                    stamp = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S,%f")[:23]
                    output.write(f"{stamp} ERROR process: ")
                    next_expiry = min(next_expiry, at + LOG_RETENTION.total_seconds())
                output.write(part)
                line_start = part.endswith("\n")

    while True:
        now = clock()
        if now >= next_expiry:
            next_expiry = float("inf")
            for capture in path.parent.glob("live_voice.process.log*"):
                if capture.is_file():
                    next_expiry = min(next_expiry, _expire(capture, now))
                    if capture != path and not capture.stat().st_size:
                        capture.unlink()
            if not path.stat().st_size:
                line_start = True
        # Wake independently of writes; also notice wall-clock adjustments.
        if not wait(fd, min(60.0, max(0.0, next_expiry - clock()))):
            continue
        payload = os.read(fd, 65536)
        if not payload:
            write(decoder.decode(b"", final=True))
            return
        write(decoder.decode(payload))


if __name__ == "__main__":
    capture_output(sys.stdin.fileno(), Path(sys.argv[1]))
