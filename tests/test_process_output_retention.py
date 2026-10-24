"""Daily process capture rotation and exact readback retention."""
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.observability.process_output import capture_output
from alx.observability.self_record import read_log, runtime_log_paths


def daily(path, at):
    return path.with_name(path.name + "." + datetime.fromtimestamp(at, UTC).date().isoformat())


def line(at, message):
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d %H:%M:%S,%f")[:23] + " ERROR process: " + message + "\n"


def run_capture(path, clock, wait=None):
    read_fd, write_fd = os.pipe()
    if wait is None:
        os.close(write_fd)
    try:
        capture_output(read_fd, path, clock=clock,
                       wait=(lambda fd, timeout: wait(write_fd, timeout)) if wait else None)
    finally:
        os.close(read_fd)


def test_frequent_line_expirations_never_copy_retained_captures_or_rescan_per_line():
    start = datetime(2026, 3, 3, 12, tzinfo=UTC).timestamp()
    moment = [start]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        boundary = daily(path, start - timedelta(days=30).total_seconds())
        content = "".join(line(start - timedelta(days=30).total_seconds() + i / 1000, str(i))
                          for i in range(1000))
        boundary.write_text(content)
        scans = []
        original_glob = Path.glob
        original_open = Path.open

        def glob(self, pattern):
            scans.append(moment[0])
            return original_glob(self, pattern)

        def guarded_open(self, mode="r", *args, **kwargs):
            assert self != boundary, "expiration must never read or rewrite a retained capture"
            assert "compacting" not in self.name, "expiration must never copy captures"
            return original_open(self, mode, *args, **kwargs)

        events = iter(range(102))

        def wait(fd, timeout):
            event = next(events)
            if event == 101:
                os.close(fd)
                return True
            moment[0] = start + event / 1000
            if event < 100:
                os.write(fd, b"frequent output\n")
                return True
            return False

        with patch.object(Path, "open", guarded_open), patch.object(Path, "glob", glob):
            run_capture(path, lambda: moment[0], wait)
        assert len(scans) == 1
        assert boundary.read_text() == content
        assert not path.exists()
        assert len(daily(path, start).read_text().splitlines()) == 100


def test_readback_exact_cutoff_order_bounds_and_legacy_discovery_without_duplicates():
    now = datetime(2026, 3, 3, 12, 0, 0, 500000, tzinfo=UTC)
    cutoff = (now - timedelta(days=30)).timestamp()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        path.write_text(line(cutoff - .001, "expired") + line(cutoff + .004, "legacy"))
        daily(path, cutoff).write_text(line(cutoff + .002, "boundary"))
        daily(path, now.timestamp()).write_text(line(now.timestamp() - 1, "new token=secret"))
        paths = runtime_log_paths(Path(directory) / "live_voice.log")
        assert len(paths) == len(set(paths)) == 3
        entries, truncated = read_log(paths, now - timedelta(days=40), now + timedelta(seconds=1),
                                      clock=lambda: now)
        assert [e["message"] for e in entries] == ["boundary", "legacy", "new token=[masked]"]
        assert not truncated
        entries, truncated = read_log(paths, now - timedelta(days=40), now + timedelta(seconds=1),
                                      limit=2, clock=lambda: now)
        assert [e["message"] for e in entries] == ["legacy", "new token=[masked]"]
        assert truncated


def test_idle_expiry_preserves_boundary_day_then_deletes_and_continues_capture():
    start = datetime(2026, 1, 1, 12, tzinfo=UTC).timestamp()
    moment = [start]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        events = iter(range(5))

        def wait(fd, timeout):
            event = next(events)
            if event == 0:
                os.write(fd, b"startup token=private-token\n")
            elif event == 1:
                moment[0] = start + timedelta(days=30, seconds=1).total_seconds()
                return False
            elif event == 2:
                assert daily(path, start).exists()
                entries, _ = read_log(runtime_log_paths(path.with_name("live_voice.log")),
                                      datetime.fromtimestamp(start - 1, UTC),
                                      datetime.fromtimestamp(moment[0] + 1, UTC),
                                      clock=lambda: datetime.fromtimestamp(moment[0], UTC))
                assert entries == []
                moment[0] = start + timedelta(days=31).total_seconds()
                return False
            elif event == 3:
                assert not daily(path, start).exists()
                os.write(fd, b"continued capture password=private-password\n")
            else:
                os.close(fd)
            return True

        run_capture(path, lambda: moment[0], wait)
        entries, _ = read_log(runtime_log_paths(path.with_name("live_voice.log")),
                              datetime.fromtimestamp(moment[0] - 1, UTC),
                              datetime.fromtimestamp(moment[0] + 1, UTC),
                              clock=lambda: datetime.fromtimestamp(moment[0], UTC))
        assert "password=[masked]" in str(entries)
        assert "private-password" not in str(entries)
        assert all(e["level"] == "ERROR" for e in entries)


def test_startup_expiry_deletes_at_most_one_file_between_pipe_reads_and_keeps_legacy():
    now = datetime.now(UTC).timestamp()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        path.write_text(line(now - 1, "legacy output"))
        legacy = path.read_bytes()
        expired = [daily(path, now - timedelta(days=age).total_seconds()) for age in (31, 32, 33)]
        archive = path.with_name(path.name + ".previous")
        archive.write_text("old unstructured output\n")
        old = now - timedelta(days=31).total_seconds()
        os.utime(archive, (old, old))
        expired.append(archive)
        for capture in expired[:-1]:
            capture.write_text("expired\n")
        counts = []
        events = iter(range(6))

        def wait(fd, timeout):
            counts.append(sum(p.exists() for p in expired))
            if next(events) < 5:
                os.write(fd, b"still draining\n")
            else:
                os.close(fd)
            return True

        run_capture(path, lambda: now, wait)
        assert counts[0] >= 3
        assert all(a - b <= 1 for a, b in zip(counts, counts[1:]))
        assert counts[-1] == 0
        assert path.read_bytes() == legacy


def test_expiration_scans_hourly_while_idle_and_expires_legacy_active_file():
    start = datetime.now(UTC).timestamp()
    moment = [start]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        path.write_text("old unstructured startup\n")
        old = start - timedelta(days=31).total_seconds()
        os.utime(path, (old, old))
        events = iter(range(5))
        scans = []
        original_glob = Path.glob

        def glob(self, pattern):
            scans.append(moment[0])
            return original_glob(self, pattern)

        def wait(fd, timeout):
            event = next(events)
            assert not path.exists()
            if event == 0:
                moment[0] += 1
            elif event == 1:
                moment[0] = start + 3599
            elif event == 2:
                moment[0] = start + 3600
            elif event == 3:
                assert scans == [start, start + 3600]
            else:
                os.close(fd)
                return True
            return False

        with patch.object(Path, "glob", glob):
            run_capture(path, lambda: moment[0], wait)
        assert scans == [start, start + 3600]


def test_partial_writes_and_utc_rollover_keep_masking_capture_and_bounds():
    start = datetime(2026, 3, 3, 23, 59, 59, tzinfo=UTC).timestamp()
    moment = [start]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        parts = iter((b"failure token=private-", b"token\n" + b"x " * 1000 + b"\n", b"next day\n", b""))

        def wait(fd, timeout):
            part = next(parts)
            if part == b"next day\n":
                moment[0] += 2
            if part:
                os.write(fd, part)
            else:
                os.close(fd)
            return True

        run_capture(path, lambda: moment[0], wait)
        assert "failure token=private-token" in daily(path, start).read_text()
        assert "x " * 1000 in daily(path, start).read_text()
        assert "next day" in daily(path, start + 2).read_text()
        entries, _ = read_log(runtime_log_paths(path.with_name("live_voice.log")),
                              datetime.fromtimestamp(start - 1, UTC),
                              datetime.fromtimestamp(start + 3, UTC),
                              clock=lambda: datetime.fromtimestamp(start + 3, UTC))
        assert len(entries) == 3
        assert "failure token=[masked]" in str(entries)
        assert "private-token" not in str(entries)
        assert all(e["level"] == "ERROR" and len(e["message"]) <= 4000 for e in entries)


def test_restarts_append_to_same_utc_capture_without_joining_unfinished_lines():
    now = datetime.now(UTC).timestamp()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"

        def collect(payload):
            sent = [False]

            def wait(fd, timeout):
                if not sent[0]:
                    os.write(fd, payload)
                    sent[0] = True
                else:
                    os.close(fd)
                return True

            run_capture(path, lambda: now, wait)

        collect(b"unfinished output")
        collect(b"next process\n")
        paths = runtime_log_paths(path.with_name("live_voice.log"))
        assert paths == (daily(path, now),)
        entries, _ = read_log(paths, datetime.fromtimestamp(now - 1, UTC),
                              datetime.fromtimestamp(now + 1, UTC))
        assert [e["message"] for e in entries] == ["unfinished output", "next process"]
