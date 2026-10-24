"""Process capture expires during an uninterrupted, including idle, run."""
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.observability.self_record import read_log


def test_readback_does_not_return_expired_process_output_from_a_recently_written_file():
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        path.write_text(
            (now - timedelta(days=31)).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            + ",000 ERROR process: expired startup\n"
            + now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            + ",000 ERROR process: recent traceback\n")
        entries, _ = read_log((path,), now - timedelta(days=40), now + timedelta(seconds=1))
        assert [entry["message"] for entry in entries] == ["recent traceback"]


def test_idle_capture_expires_physically_and_continues_writing_with_injected_clock():
    from alx.observability.process_output import capture_output

    start = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    moment = [start]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        read_fd, write_fd = os.pipe()
        events = iter(("old", "recent", "idle", "idle_again", "continued", "end"))

        def wait(fd, timeout):
            event = next(events)
            if event == "old":
                os.write(write_fd, b"startup token=private-token\n")
            elif event == "recent":
                moment[0] = start + timedelta(days=29).total_seconds()
                os.write(write_fd, b"Traceback: recent failure\n")
            elif event == "idle":
                moment[0] = start + timedelta(days=30, seconds=1).total_seconds()
                return False
            elif event == "idle_again":
                # The preceding timeout must maintain storage without reads
                # or a new write, and must keep the writer usable afterward.
                assert "startup" not in path.read_text()
                assert "recent failure" in path.read_text()
                entries, _ = read_log((path,), datetime.fromtimestamp(start, UTC),
                                      datetime.fromtimestamp(moment[0] + 1, UTC),
                                      clock=lambda: datetime.fromtimestamp(moment[0], UTC))
                assert all(entry["level"] == "ERROR" for entry in entries)
                assert "recent failure" in str(entries)
                moment[0] = start + timedelta(days=60).total_seconds()
                return False
            elif event == "continued":
                assert "recent failure" not in path.read_text()
                os.write(write_fd, b"continued capture password=private-password\n")
            else:
                os.close(write_fd)
            return True

        try:
            capture_output(read_fd, path, clock=lambda: moment[0], wait=wait)
        finally:
            os.close(read_fd)
        assert "startup" not in path.read_text()
        assert "continued capture" in path.read_text()
        entries, _ = read_log((path,), datetime.fromtimestamp(moment[0] - 1, UTC),
                              datetime.fromtimestamp(moment[0] + 1, UTC),
                              clock=lambda: datetime.fromtimestamp(moment[0], UTC))
        assert "password=[masked]" in str(entries)
        assert "private-password" not in str(entries)


def test_capture_expires_legacy_active_and_archived_output_without_renewing_mtime():
    from alx.observability.process_output import capture_output

    now = datetime.now(UTC).timestamp()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        archive = path.with_name(path.name + ".previous")
        for capture in (path, archive):
            capture.write_text("expired startup output\n")
            old = now - timedelta(days=31).total_seconds()
            os.utime(capture, (old, old))
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            capture_output(read_fd, path, clock=lambda: now)
        finally:
            os.close(read_fd)
        assert path.read_text() == ""
        assert not archive.exists()


def test_partial_writes_keep_masking_and_capture_bounds():
    from alx.observability.process_output import capture_output

    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "live_voice.process.log"
        read_fd, write_fd = os.pipe()
        parts = iter((b"failure token=private-", b"token\n" + b"x " * 1000 + b"\n", b""))

        def wait(fd, timeout):
            part = next(parts)
            if part:
                os.write(write_fd, part)
            else:
                os.close(write_fd)
            return True

        try:
            capture_output(read_fd, path, clock=now.timestamp, wait=wait)
        finally:
            os.close(read_fd)
        entries, _ = read_log((path,), now - timedelta(seconds=1), now + timedelta(seconds=1))
        assert "failure token=[masked]" in str(entries)
        assert "private-token" not in str(entries)
        assert all(entry["level"] == "ERROR" and len(entry["message"]) <= 4000 for entry in entries)
        # Readback is bounded; capture retains all original output within TTL.
        assert "x " * 1000 in path.read_text()
