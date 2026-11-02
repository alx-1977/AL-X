"""PR 189: freeze late descendants and retain delayed results."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers import program
from alx.continuity.program_source import ProgramCompletionSource
from test_program_review_findings import Ledger, unfinished


def test_untagged_late_helper_is_found_before_parent_is_killed(monkeypatch):
    live = {123: [1, 3], 124: [1, 4]}
    frozen, signals = set(), []
    monkeypatch.setattr(program, "process_identity", lambda pid: live.get(pid))
    monkeypatch.setattr(program, "process_stopped", lambda pid: pid in frozen, raising=False)
    def kill(pid, sig):
        signals.append((pid, sig))
        if sig == signal.SIGSTOP:
            frozen.add(pid)
        else:
            live.pop(pid, None)
    monkeypatch.setattr(program.os, "kill", kill)
    def census(token, known, **kwargs):
        # A helper with env={} born after the last execution scan is only
        # discoverable through its still-live parent.
        found = {pid: identity for pid, identity in known.items() if pid in live}
        if 123 in live and 124 in live:
            found[124] = live[124]
        return found
    monkeypatch.setattr(program, "tagged_processes", census)
    assert program.stop_program("run", {123: [1, 3]})
    assert not live, "untagged helper survived terminal cleanup"
    first_kill = next(i for i, (_, sig) in enumerate(signals) if sig == signal.SIGKILL)
    assert {(123, signal.SIGSTOP), (124, signal.SIGSTOP)} <= set(signals[:first_kill])


def test_real_untagged_descendant_is_terminated(tmp_path):
    ready = tmp_path / "helper.pid"
    parent = subprocess.Popen([
        sys.executable, "-c",
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],env={},start_new_session=True); "
        f"Path({str(ready)!r}).write_text(str(p.pid)); time.sleep(30)",
    ])
    helper = None
    try:
        deadline = time.monotonic() + 3
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(.01)
        helper = int(ready.read_text())
        known = {parent.pid: program.process_identity(parent.pid)}
        assert program.stop_program("absent-tag", known)
        assert helper in known
        assert program.process_identity(helper) is None
    finally:
        for pid in (parent.pid, helper):
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        parent.wait(timeout=2)


def test_freeze_not_delivered_remains_bounded_without_killing(monkeypatch):
    clock, signals = [0.0], []
    monkeypatch.setattr(program.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(program.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(program, "process_identity", lambda pid: [1, 3])
    monkeypatch.setattr(program, "process_stopped", lambda pid: False)
    monkeypatch.setattr(program.os, "kill", lambda pid, sig: signals.append(sig))
    monkeypatch.setattr(program, "tagged_processes", lambda token, known, **kw: dict(known))
    assert not program.stop_program("run", {123: [1, 3]})
    assert clock[0] == pytest.approx(program.RECOVERY_SECONDS)
    assert signals and set(signals) == {signal.SIGSTOP}


@pytest.mark.parametrize("output_exists", [False, True])
def test_recovery_without_saved_facts_uses_interrupted_defaults(tmp_path, monkeypatch, output_exists):
    run = unfinished(tmp_path)
    if not output_exists:
        (run / "stdout").unlink()
        (run / "stderr").unlink()
    monkeypatch.setattr(program, "stop_program", lambda *a, **kw: True)
    program.reconcile(run)
    result = json.loads((run / "result.json").read_text())
    assert result["status"] == "interrupted"
    assert result["exit_code"] == -1
    assert result["timed_out"] is False
    assert result["duration_seconds"] == 0
    assert result["error"] == "program_worker_interrupted"


@pytest.mark.parametrize("timed_out", [False, True])
def test_delayed_cleanup_recovers_known_program_facts(tmp_path, monkeypatch, timed_out):
    run = unfinished(tmp_path)
    (run / "request.json").write_text(json.dumps({
        "argv": ["program"], "working_directory": str(tmp_path),
        "time_limit_seconds": .03, "conversation_id": "thread",
    }))
    clock = [0.0]
    monkeypatch.setattr(program.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(program.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    def poll():
        if timed_out or clock[0] < .02:
            return None
        process.returncode = 7
        return 7
    process = SimpleNamespace(pid=123, poll=poll, returncode=None)
    monkeypatch.setattr(program.subprocess, "Popen", lambda *a, **kw: process)
    monkeypatch.setattr(program, "process_identity", lambda pid: [1, 3])
    monkeypatch.setattr(program, "tagged_processes", lambda *a, **kw: {})
    def pending(*args, **kwargs):
        assert not (run / "result.json").exists()
        saved = json.loads((run / "pending_result.json").read_text())
        assert saved["exit_code"] == (-1 if timed_out else 7)
        assert saved["timed_out"] is timed_out
        return False
    monkeypatch.setattr(program, "stop_program", pending)
    program.work(run)
    assert not (run / "result.json").exists()
    monkeypatch.setattr(program, "process_identity", lambda pid: None)
    source = ProgramCompletionSource(tmp_path, Ledger(), True, reconcile=program.reconcile)
    source.reconcile_pending()
    assert not source.contextual_events("thread")
    assert not (run / "result.json").exists()
    monkeypatch.setattr(program, "stop_program", lambda *a, **kw: True)
    source.reconcile_pending()
    event, = source.contextual_events("thread")
    result = event.transient_data
    assert result["status"] == "completed"
    assert result["exit_code"] == (-1 if timed_out else 7)
    assert result["timed_out"] is timed_out
    assert result["duration_seconds"] == pytest.approx(.03 if timed_out else .02)
    assert result["stdout_path"] == str(run / "stdout")
    assert result["stderr_path"] == str(run / "stderr")
    assert result["full_output_path"] == str(run)
    assert result["error"] == ""
