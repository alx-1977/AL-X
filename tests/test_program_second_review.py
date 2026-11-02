"""Regression evidence for PR 189's second review."""

import json
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.continuity.program_source import ProgramCompletionSource
from alx.providers import program
from alx.continuity.due_source import DueCognitionSource
from alx.contracts import ConversationOrigin, ConversationTurn
from alx.conversation.gateway import ConversationGateway
from alx.conversation.store import SQLiteConversationStore
from alx.core import CoreOutcome, CoreState
from test_program_review_findings import Ledger, unfinished


def test_conversation_and_startup_do_not_attempt_cleanup(tmp_path):
    unfinished(tmp_path)
    def stuck_cleanup(root):
        pytest.fail("conversation/startup entered interrupted-program cleanup")
    source = ProgramCompletionSource(tmp_path, Ledger(), True, reconcile=stuck_cleanup)
    source.recover()
    assert not source.contextual_events("other")
    assert not source.due_opportunities()


def test_unreapable_recovery_is_bounded_pending_and_retried(tmp_path, monkeypatch):
    run = unfinished(tmp_path)
    clock = [0.0]
    def sleep(seconds):
        clock[0] += seconds
        assert clock[0] < 2, "recovery did not respect its time budget"
    monkeypatch.setattr(program.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(program.time, "sleep", sleep)
    live = {123: [1, 3]}
    monkeypatch.setattr(program, "tagged_processes", lambda token, known=None, **kw: dict(live))
    monkeypatch.setattr(program, "process_identity", lambda pid: live.get(pid))
    monkeypatch.setattr(program.os, "kill", lambda *args: None)
    program.reconcile(run)
    assert 0 < clock[0] < 2
    assert not (run / "result.json").exists()
    monkeypatch.setattr(program, "tagged_processes", lambda token, known=None, **kw: {})
    clock[0] = 0
    program.reconcile(run)
    assert not (run / "result.json").exists()
    live.clear()
    program.reconcile(run)
    assert json.loads((run / "result.json").read_text())["status"] == "interrupted"


def test_worker_scan_cadence_and_deadline_are_independent(tmp_path, monkeypatch):
    run = unfinished(tmp_path)
    (run / "request.json").write_text(json.dumps({
        "argv": ["sleep", "10"], "working_directory": str(tmp_path),
        "time_limit_seconds": .37,
    }))
    clock, scans, stopped = [0.0], [], []
    monkeypatch.setattr(program.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(program.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    process = SimpleNamespace(pid=123, poll=lambda: None, wait=lambda **kw: None, returncode=-9)
    monkeypatch.setattr(program.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(program, "process_identity", lambda pid: [1, 3])
    def scan(*args, **kwargs):
        scans.append(clock[0])
        return {}
    monkeypatch.setattr(program, "tagged_processes", scan)
    monkeypatch.setattr(program, "stop_program", lambda *args, **kwargs: stopped.append(clock[0]) or True)
    program.work(run)
    assert len(scans) <= 4, scans
    assert all(b - a >= .099 for a, b in zip(scans, scans[1:]))
    assert .37 <= stopped[0] <= .38
    assert json.loads((run / "result.json").read_text())["timed_out"]


def test_background_retries_without_holding_up_replies(tmp_path, monkeypatch):
    run = unfinished(tmp_path)
    started = Event()
    live = {123: [1, 3]}
    def census(*args, **kwargs):
        started.set()
        return dict(live)
    monkeypatch.setattr(program, "tagged_processes", census)
    monkeypatch.setattr(program, "process_identity", lambda pid: live.get(pid))
    monkeypatch.setattr(program.os, "kill", lambda *args: None)
    source = ProgramCompletionSource(tmp_path, Ledger(), True, reconcile=program.reconcile)
    class Core:
        def process(self, *args):
            return CoreOutcome(CoreState.RESPONDED, response="Reply proceeds.")
    store = SQLiteConversationStore(tmp_path / "conversation.sqlite3")
    gateway = ConversationGateway(Core(), store, conversation_events=source.contextual_events)
    async def scenario():
        lock = asyncio.Lock()
        tick = DueCognitionSource(source, object(), lock, 30,
                                  recover_programs=source.reconcile_pending)
        task = asyncio.create_task(tick.tick())
        try:
            assert await asyncio.to_thread(started.wait, 1)
            assert not task.done()
            async with lock:
                # Startup ledger recovery and an unrelated actual gateway turn
                # both proceed while the child ignores SIGKILL.
                source.recover()
                now = datetime.now(UTC)
                outcome = gateway.receive_conversation_turn(
                    ConversationTurn("other", "turn", ConversationOrigin.TYPED, "Hello", now),
                    2, now + timedelta(days=1))
                assert outcome.response == "Reply proceeds."
                # A competing recovery pass must return without entering cleanup.
                await asyncio.to_thread(source.reconcile_pending)
                assert not task.done()
            await asyncio.wait_for(task, 2)
            assert not (run / "result.json").exists()
            live.clear()
            # Retry uses the same background tick, also with autonomy disabled.
            source.enabled = False
            await tick.tick()
            assert (run / "result.json").exists()
        finally:
            await task
    try:
        asyncio.run(scenario())
    finally:
        store.close()


def test_mac_census_reuses_buffer_and_skips_arguments_for_known_ancestry(monkeypatch):
    monkeypatch.setattr(program.sys, "platform", "darwin")
    def pids(buffer, size):
        if buffer is not None:
            buffer[0], buffer[1], buffer[2], buffer[3] = 10, 11, 12, 13
        return 4
    inspected = []
    def sysctl(mib, count, buffer, size, *args):
        inspected.append(mib[2])
        raw = b"\x00\x00\x00\x00executable\x00\x00"
        program.ctypes.memmove(buffer, raw, len(raw))
        size._obj.value = len(raw)
        return 0
    library = SimpleNamespace(proc_listallpids=pids, sysctl=sysctl)
    monkeypatch.setattr(program.ctypes, "CDLL", lambda *args, **kwargs: library)
    monkeypatch.setattr(program, "process_info", lambda pid: ([1, 3], 10 if pid == 11 else 0))
    monkeypatch.setattr(program, "process_identity", lambda pid: [1, 3])
    original = program.ctypes.create_string_buffer
    allocations = []
    def allocate(size):
        allocations.append(size)
        return original(size)
    monkeypatch.setattr(program.ctypes, "create_string_buffer", allocate)
    assert program.tagged_processes("run", {10: [1, 3]}) == {10: [1, 3], 11: [1, 3]}
    assert inspected == [12, 13]
    assert allocations == [1024 * 1024]
    inspected.clear()
    allocations.clear()
    program.tagged_processes("run", {10: [1, 3]}, inspect_tags=False)
    assert not inspected and not allocations


@pytest.mark.parametrize("failure", [TimeoutError, PermissionError])
def test_uncertain_census_never_publishes_completion(tmp_path, monkeypatch, failure):
    run = unfinished(tmp_path)
    def unavailable(token, known, **kwargs):
        known[123] = [1, 3]
        raise failure("census incomplete")
    monkeypatch.setattr(program, "tagged_processes", unavailable)
    program.reconcile(run)
    assert not (run / "result.json").exists()
    assert program.load_children(run) == {123: [1, 3]}
    monkeypatch.setattr(program, "process_identity", lambda pid: None)
    monkeypatch.setattr(program, "tagged_processes", lambda *args, **kw: {})
    program.reconcile(run)
    assert (run / "result.json").exists()


def test_stop_signals_known_child_before_census(monkeypatch):
    sequence = []
    monkeypatch.setattr(program, "process_identity", lambda pid: [1, 3])
    monkeypatch.setattr(program.os, "kill", lambda *args: sequence.append("kill"))
    def slow_census(*args, **kwargs):
        sequence.append("census")
        raise TimeoutError
    monkeypatch.setattr(program, "tagged_processes", slow_census)
    assert not program.stop_program("run", {123: [1, 3]})
    assert sequence == ["kill", "census"]
