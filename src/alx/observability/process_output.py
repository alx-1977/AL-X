"""The launcher's sole process-output writer, with UTC daily rotation.

Only this collector writes captures. Expiration inspects names and metadata,
never capture contents, and unlinks whole files without replacing writers.
"""
from __future__ import annotations

import codecs
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import select
import sys
import time
from typing import Callable, Iterator

from alx.observability.self_record import LOG_RETENTION

EXPIRY_INTERVAL = 3600.0


def _expiration_candidates(path: Path, now: float) -> Iterator[Path | None]:
    """Yield one directory entry's deletion decision per pipe-handling turn.

    Daily captures are safe to delete only after the end of their UTC day
    expires. Legacy files are no longer written; their last-write time bounds
    their capture times (and is the provenance for untimestamped output).
    Neither kind is opened, copied or rewritten by maintenance.
    """
    cutoff = now - LOG_RETENTION.total_seconds()
    for capture in path.parent.glob(path.name + "*"):
        try:
            if not capture.is_file():
                yield None
                continue
            suffix = capture.name[len(path.name) + 1:]
            try:
                day = datetime.strptime(suffix, "%Y-%m-%d").replace(tzinfo=UTC)
                latest = (day + timedelta(days=1)).timestamp()
            except ValueError:
                latest = capture.stat().st_mtime
            yield capture if latest <= cutoff else None
        except FileNotFoundError:
            yield None


def capture_output(fd: int, path: Path, *, clock: Callable[[], float] = time.time,
                   wait: Callable[[int, float], bool] | None = None) -> None:
    """Drain stdout/stderr through EOF; maintain retention even with no output."""
    wait = wait or (lambda descriptor, timeout: bool(select.select([descriptor], [], [], timeout)[0]))
    path.parent.mkdir(parents=True, exist_ok=True)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    line_start = True
    active: Path | None = None
    opened: set[Path] = set()
    next_expiry = 0.0
    pending: Iterator[Path | None] = iter(())

    def write(text: str) -> None:
        nonlocal line_start, active
        # Buffer a pipe read's writes, rather than opening a file for each line.
        with ExitStack() as stack:
            outputs = {}
            for part in text.splitlines(keepends=True):
                at = clock()
                if line_start:
                    day = datetime.fromtimestamp(at, UTC).date().isoformat()
                    active = path.with_name(path.name + "." + day)
                assert active is not None
                # Restarting must not join a timestamp to an unfinished line.
                separator = False
                if active not in opened:
                    try:
                        with active.open("rb") as existing:
                            if existing.seek(0, os.SEEK_END):
                                existing.seek(-1, os.SEEK_END)
                                separator = existing.read(1) != b"\n"
                    except FileNotFoundError:
                        pass
                    opened.add(active)
                if active not in outputs:
                    outputs[active] = stack.enter_context(active.open("a", encoding="utf-8"))
                output = outputs[active]
                if separator:
                    output.write("\n")
                if line_start:
                    stamp = datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S,%f")[:23]
                    output.write(f"{stamp} ERROR process: ")
                output.write(part)
                line_start = part.endswith("\n")

    while True:
        now = clock()
        if now >= next_expiry:
            pending = _expiration_candidates(path, now)
            next_expiry = now + EXPIRY_INTERVAL
        try:
            candidate = next(pending)
            if candidate is not None:
                candidate.unlink(missing_ok=True)
                if candidate == active:
                    line_start = True
                    opened.discard(candidate)
            timeout = 0.0  # Inspect the next candidate after checking the pipe.
        except StopIteration:
            timeout = min(60.0, max(0.0, next_expiry - clock()))
        if not wait(fd, timeout):
            continue
        payload = os.read(fd, 65536)
        if not payload:
            write(decoder.decode(b"", final=True))
            # EOF leaves no pipe to drain; finish the current sweep only.
            for candidate in pending:
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
            return
        write(decoder.decode(payload))


if __name__ == "__main__":
    capture_output(sys.stdin.fileno(), Path(sys.argv[1]))
