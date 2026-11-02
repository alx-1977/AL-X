"""PR 189: result delivery, lifecycle, retained path blocks and provenance."""
import json
import fcntl
import os
import signal
import sys
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.continuity.program_source import ProgramCompletionSource
from alx.conversation.gateway import ConversationGateway
from alx.conversation.store import SQLiteConversationStore
from alx.contracts import ContentOrigin
from alx.core import CoreAgent, CoreOutcome, CoreState
from alx.providers.program import ProgramRunner, reconcile
from alx.providers import program
from alx.providers.coding_process import run_permitted_command
from alx.contracts.coding import CodingError
from alx.tools.program import build_program_executors
from test_program import wait_result
import test_sandbox_integrated_review_findings as sandbox_tests


class Ledger:
    def __init__(self): self.rows = {}
    def exists(self, key): return key in self.rows
    def record_created(self, opportunity):
        if self.exists(opportunity.opportunity_id): return False
        self.rows[opportunity.opportunity_id] = opportunity
        return True
    def unfinished(self): return ()


def unfinished(root, name="run"):
    run = root / name
    run.mkdir(parents=True)
    (run / "background").touch()
    (run / "request.json").write_text(json.dumps({"conversation_id": "thread"}))
    (run / "stdout").write_text("partial output")
    (run / "stderr").write_text("")
    return run


def test_completion_reaches_core_intact_and_is_acknowledged(tmp_path):
    run = unfinished(tmp_path / "runs")
    values = {"run_id": run.name, "exit_code": 7, "stdout": "output", "stderr": "error",
              "stdout_path": str(run / "stdout"), "stderr_path": str(run / "stderr")}
    (run / "result.json").write_text(json.dumps(values))
    source = ProgramCompletionSource(run.parent, Ledger(), True, reconcile=reconcile)
    opportunity, = source.due_opportunities()
    seen = []
    class Core:
        def process(self, conversation, *args, **kwargs):
            seen.append(conversation)
            return CoreOutcome(CoreState.RESPONDED, response="Observed.")
    store = SQLiteConversationStore(tmp_path / "conversation.sqlite3")
    try:
        gateway = ConversationGateway(Core(), store, conversation_events=source.contextual_events)
        assert source.claim(opportunity)
        gateway.receive_cognition_opportunity("thread", opportunity, 2, datetime.now(UTC) + timedelta(days=1))
        completion, = [e for e in seen[0].events if e.kind == "program.completed"]
        assert dict(completion.transient_data) == values
        assert completion.event_id != opportunity.opportunity_id
        assert any(e.kind == "cognition.opportunity" for e in seen[0].events)
        derived = CoreAgent._derived_provenance(datetime.now(UTC), seen[0], None, (), ())
        assert ContentOrigin.EXTERNAL in derived.origins
        assert source.contextual_events("thread")
        source.mark_honoured(opportunity)
        assert not source.contextual_events("thread")
        assert not source.due_opportunities()
    finally:
        store.close()


@pytest.mark.parametrize("timeout,clean_environment", [(False, False), (True, False), (False, True)])
def test_detached_descendant_stopped_before_result(tmp_path, timeout, clean_environment):
    pid_file = tmp_path / "helper.pid"
    heartbeat = tmp_path / "heartbeat"
    helper = f"import time; from pathlib import Path; p=Path({str(heartbeat)!r});\nwhile True: p.write_text(str(time.time_ns())); time.sleep(.01)"
    script = ("import subprocess,sys,time; from pathlib import Path; "
              f"p=subprocess.Popen([sys.executable,'-c',{helper!r}],start_new_session=True,env={ '{}' if clean_environment else 'None' }); "
              f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
              + ("time.sleep(10)" if timeout else "time.sleep(.1)"))
    try:
        pending = ProgramRunner(tmp_path / "runs", (), .001).run({
            "argv": [sys.executable, "-c", script], "working_directory": str(tmp_path),
            "time_limit_seconds": .5 if timeout else 3,
        })
        final = wait_result(Path(pending["full_output_path"]))
        assert final["timed_out"] == timeout
        before = heartbeat.read_text()
        time.sleep(.1)
        assert heartbeat.read_text() == before, "helper still executes after terminal result"
    finally:
        if pid_file.exists():
            try: os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError: pass


@pytest.mark.parametrize("discovery", ["normal", "restart"])
def test_interrupted_run_reports_once_after_restart(tmp_path, discovery):
    run = unfinished(tmp_path)
    (run / "worker.json").write_text(json.dumps({"pid": 99999999, "started_at": [0, 0]}))
    source = ProgramCompletionSource(tmp_path, Ledger(), True, reconcile=reconcile)
    if discovery == "restart": source.recover()
    source.reconcile_pending()
    opportunity, = source.due_opportunities()
    event, = source.contextual_events("thread")
    assert event.transient_data["status"] in ("failed", "interrupted")
    assert event.transient_data["stdout"] == "partial output"
    assert event.transient_data["error"]
    assert source.claim(opportunity)
    assert not source.due_opportunities()
    source.mark_honoured(opportunity)
    assert not ProgramCompletionSource(tmp_path, source.ledger, True, reconcile=reconcile).contextual_events()


@pytest.mark.parametrize("spelling", ["relative", "absolute", "path", "alias"])
def test_blocked_executable_never_starts(tmp_path, monkeypatch, spelling):
    private = tmp_path / "private"
    private.mkdir()
    executable = private / "check"
    executable.write_text("#!/bin/sh\ntouch ran\n")
    executable.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(executable)
    monkeypatch.setenv("PATH", str(private) + os.pathsep + os.environ.get("PATH", ""))
    name = {"relative": "./private/check", "absolute": str(executable),
            "path": "check", "alias": "./alias"}[spelling]
    with pytest.raises(CodingError, match="command_not_permitted"):
        run_permitted_command([name], tmp_path, blocked_paths=("private",))
    assert not (tmp_path / "ran").exists()


def test_executable_symlink_in_blocked_directory_is_refused(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "check").symlink_to(sys.executable)
    with pytest.raises(CodingError, match="command_not_permitted"):
        run_permitted_command(["./private/check", "-V"], tmp_path, blocked_paths=("private",))


@pytest.mark.parametrize("available", [False, True])
def test_git_probe_respects_runner_availability(available):
    case = sandbox_tests.ProfileCompletenessTest("test_git_can_be_executed")
    case.workspace = Mock()
    case.runner = Mock()
    case.runner.available.return_value = available
    case.runner.run.return_value.stdout = "git version 2"
    if available:
        case.test_git_can_be_executed()
        case.runner.run.assert_called_once()
    else:
        with pytest.raises(unittest.SkipTest): case.test_git_can_be_executed()
        case.workspace.prepare.assert_not_called()
        case.runner.run.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "running"])
def test_program_results_keep_external_provenance_when_core_derives_records(status):
    now = datetime.now(UTC)
    result = build_program_executors(lambda _: {"status": status}, lambda: "call")["run_program"]({})
    assert result.provenance is not None
    assert ContentOrigin.EXTERNAL in result.provenance.origins
    derived = CoreAgent._derived_provenance(now, SimpleNamespace(turns=(), events=()), None, (), (SimpleNamespace(result=result),))
    assert ContentOrigin.EXTERNAL in derived.origins


def test_live_worker_is_not_interrupted_and_dead_worker_is_recovered(tmp_path):
    pending = ProgramRunner(tmp_path / "runs", (), .001).run({
        "argv": [sys.executable, "-c", "import time; print('partial',flush=True); time.sleep(30)"],
        "working_directory": str(tmp_path), "time_limit_seconds": 35,
    }, "thread")
    run = Path(pending["full_output_path"])
    source = ProgramCompletionSource(run.parent, Ledger(), True, reconcile=reconcile)
    deadline = time.monotonic() + 5
    while not (run / "worker.json").exists() or not (run / "stdout").exists() or not (run / "stdout").read_text():
        assert time.monotonic() < deadline
        time.sleep(.01)
    worker = json.loads((run / "worker.json").read_text())
    try:
        source.recover()
        assert not source.due_opportunities()
        assert not (run / "result.json").exists()
    finally:
        os.kill(worker["pid"], signal.SIGKILL)
    while program.process_identity(worker["pid"]) is not None:
        assert time.monotonic() < deadline
        time.sleep(.01)
    source.recover()
    source.reconcile_pending()
    events = source.contextual_events("thread")
    while not events:
        assert time.monotonic() < deadline
        time.sleep(.01)
        source.reconcile_pending()
        events = source.contextual_events("thread")
    event, = events
    assert event.transient_data["status"] == "interrupted"
    assert event.transient_data["stdout"] == "partial\n"
    assert not program.tagged_processes(run.name)


def test_launch_lease_prevents_recovery_before_worker_identity_is_written(tmp_path):
    run = unfinished(tmp_path)
    source = ProgramCompletionSource(tmp_path, Ledger(), True, reconcile=reconcile)
    with (run / "worker.lock").open("a+b") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX)
        source.recover()
        assert not source.due_opportunities()
        assert not (run / "result.json").exists()
    source.reconcile_pending()
    assert len(source.due_opportunities()) == 1


def test_recovery_preserves_result_published_during_identity_check(tmp_path, monkeypatch):
    run = unfinished(tmp_path)
    (run / "worker.json").write_text(json.dumps({"pid": 99999999, "started_at": [0, 0]}))
    expected = {"status": "completed", "exit_code": 4, "stdout": "worker evidence"}
    def exited(pid):
        program.publish(run / "result.json", expected)
        return None
    monkeypatch.setattr(program, "process_identity", exited)
    monkeypatch.setattr(program, "tagged_processes", lambda token, known=None, **kw: {})
    reconcile(run)
    reconcile(run)
    assert json.loads((run / "result.json").read_text()) == expected


def test_simultaneous_recovery_publishes_one_result(tmp_path):
    run = unfinished(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(reconcile, [run] * 8))
    before = (run / "result.json").read_bytes()
    reconcile(run)
    assert (run / "result.json").read_bytes() == before
    assert not tuple(run.glob("*.tmp"))


def test_reused_pid_generation_is_never_signalled(monkeypatch):
    monkeypatch.setattr(program, "tagged_processes", lambda token, known=None, **kw: {})
    monkeypatch.setattr(program, "process_identity", lambda pid: [2, 3])
    kill = Mock()
    monkeypatch.setattr(program.os, "kill", kill)
    program.stop_program("token", {123: [1, 3]})
    kill.assert_not_called()


@pytest.mark.parametrize("same_generation", [True, False])
def test_recovery_distinguishes_live_worker_from_reused_pid(tmp_path, same_generation, monkeypatch):
    monkeypatch.setattr(program, "tagged_processes", lambda *args, **kw: {})
    run = unfinished(tmp_path)
    generation = program.process_identity(os.getpid()) if same_generation else [-1, -1]
    (run / "worker.json").write_text(json.dumps({"pid": os.getpid(), "started_at": generation}))
    reconcile(run)
    assert (run / "result.json").exists() is not same_generation


def test_permitted_verification_commands_still_execute(tmp_path):
    record = run_permitted_command([sys.executable, "-V"], tmp_path, blocked_paths=("private",))
    assert record.exit_status == 0
    assert "Python" in record.stdout
