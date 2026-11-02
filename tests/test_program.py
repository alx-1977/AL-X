"""D-063 contract: direct argv, bounded evidence, durable completion and refusals."""

import json
import os
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from alx.bootstrap.program import build_program_runtime
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.config.program import ProgramSettings
from alx.continuity.program_source import ProgramCompletionSource
from alx.providers.program import ProgramRunner
from alx.tools.program import DEFINITION
from alx.contracts import CapabilityCall, CapabilityResultState, ConversationSnapshot, ContentOrigin
from alx.conversation.gateway import ConversationGateway
from alx.safety import AuthorityContext, SafetyGate


def wait_result(root):
    deadline = time.monotonic() + 10
    while not (root / "result.json").exists():
        assert time.monotonic() < deadline
        time.sleep(.01)
    return json.loads((root / "result.json").read_text())


def test_registration_and_standing_authority(tmp_path):
    definitions, policies, executors = build_program_runtime(
        tmp_path, ("bank.example",), lambda: "call", lambda: "conversation",
    )
    assert definitions == (DEFINITION,)
    assert set(executors) == {"run_program"}
    assert not policies["run_program"].approval_required
    assert DEFINITION.input_schema.accepts({
        "argv": ["echo", "hello"], "working_directory": str(tmp_path), "time_limit_seconds": 1,
    })
    registry = CapabilityRegistry()
    registry.register(DEFINITION)
    broker = CapabilityBroker(registry, SafetyGate(policies), executors)
    arguments = {"argv": [sys.executable, "-c", "import os; print(os.getuid())"],
                 "working_directory": str(tmp_path), "time_limit_seconds": 2}
    authority = AuthorityContext("friedl", frozenset({"program.execute"}), datetime.now(UTC))
    attempt = broker.dispatch(CapabilityCall("call", "run_program", arguments), authority)
    assert attempt.result.state is CapabilityResultState.SUCCEEDED
    values = dict(attempt.result.values)
    if values["status"] == "running":
        values = wait_result(Path(values["full_output_path"]))
    assert values["stdout"].strip() == str(os.getuid())


def test_direct_argv_full_output_and_bounded_head_tail(tmp_path):
    runner = ProgramRunner(tmp_path / "runs", ("bank.example",), foreground_seconds=5)
    result = runner.run({
        "argv": [sys.executable, "-c", "import sys; print(sys.argv[1]); print('H'*10000+'TAIL'); print('E'*10000+'END',file=sys.stderr)", "$(touch forbidden); literal"],
        "working_directory": str(tmp_path), "time_limit_seconds": 4,
    })
    assert result["exit_code"] == 0
    assert result["duration_seconds"] > 0
    assert "$(touch forbidden); literal" in result["stdout"]
    assert result["stdout"].endswith("TAIL\n")
    assert result["stderr"].endswith("END\n")
    assert len(result["stdout"]) < 4200
    assert len(result["stderr"]) < 4200
    assert Path(result["stdout_path"]).stat().st_size > 10000
    assert not (tmp_path / "forbidden").exists()
    assert DEFINITION.output_schema.accepts(result)


@pytest.mark.parametrize("argv", [
    ["security", "find-generic-password"], ["/usr/bin/security", "list-keychains"],
    ["env", "security", "list-keychains"], ["sh", "-c", "/usr/bin/security list-keychains"],
    ["curl", "https://BANK.EXAMPLE/account"], ["open", "Private Banking.app"],
])
def test_prohibited_targets_never_launch(tmp_path, argv):
    runner = ProgramRunner(tmp_path / "runs", ("bank.example", "Private Banking.app"))
    with pytest.raises(PermissionError):
        runner.run({"argv": argv, "working_directory": str(tmp_path), "time_limit_seconds": 1})
    assert not tuple(runner.root.iterdir())


def test_resolved_keychain_alias_is_refused(tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to("/usr/bin/security")
    with pytest.raises(PermissionError):
        ProgramRunner(tmp_path / "runs", ()).run({
            "argv": [str(alias)], "working_directory": str(tmp_path), "time_limit_seconds": 1,
        })


def test_background_deadline_completion_survives_recomposition(tmp_path):
    runner = ProgramRunner(tmp_path / "runs", (), foreground_seconds=.01)
    started = time.monotonic()
    pending = runner.run({
        "argv": [sys.executable, "-c", "import time; print('start',flush=True); time.sleep(10)"],
        "working_directory": str(tmp_path), "time_limit_seconds": .3,
    }, "thread")
    assert pending["status"] == "running"
    assert time.monotonic() - started < .3
    root = Path(pending["full_output_path"])
    final = wait_result(root)
    assert final["timed_out"] and final["exit_code"] != 0
    assert "start" in final["stdout"]

    class Ledger:
        def __init__(self): self.rows = set()
        def exists(self, key): return key in self.rows
        def record_created(self, opportunity):
            self.rows.add(opportunity.opportunity_id)
            return True
    ledger = Ledger()
    source = ProgramCompletionSource(runner.root, ledger, enabled=True)
    opportunity, = source.due_opportunities()
    assert opportunity.conversation_id == "thread"
    event, = source.contextual_events("thread")
    assert event.transient_data["timed_out"]
    assert not source.contextual_events("other-thread")
    now = datetime.now(UTC)
    conversation = ConversationSnapshot("thread", (), 1, now + timedelta(days=1))
    gateway = ConversationGateway(None, None, conversation_events=source.contextual_events)
    attached = gateway._with_contextual_events(conversation)
    assert attached.events[0].transient_data["exit_code"] == final["exit_code"]
    assert ContentOrigin.EXTERNAL in attached.events[0].provenance.origins
    assert source.claim(opportunity)
    source.mark_honoured(opportunity)
    assert not ProgramCompletionSource(runner.root, ledger, True).due_opportunities()


def test_missing_program_reports_failure_evidence(tmp_path):
    result = ProgramRunner(tmp_path / "runs", (), 5).run({
        "argv": [str(tmp_path / "missing")], "working_directory": str(tmp_path),
        "time_limit_seconds": 1,
    })
    assert result["exit_code"] == -1 and result["error"]


def test_banking_configuration_is_external():
    assert ProgramSettings.from_environment({}).banking_targets
