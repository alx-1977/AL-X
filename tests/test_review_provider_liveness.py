"""A reviewer provider that cannot work is seen promptly and never costs the diff.

On 2026-09-26 a preserved coding job was resumed at local review while the
Codex subscription was exhausted. The reviewer child sat idle in a provider
wait for the full 1,200 second call timeout with REVIEW shown as active, then
spent two more attempts learning the quota was gone. These tests drive the
real Codex adapter against a scripted `codex` child process, so what they prove
is the observed process behaviour, not a stubbed return value.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from tests.test_coding_agent import (
    _FIXED, NOW, _worktree, PlanningModel, RecordingSession,
)
from alx.contracts import ModelMessage, ModelRequest, ModelRole
from alx.contracts.coding import CODING_STALL_SECONDS, CodingRequest, CodingTelemetry
from alx.interfaces.live_voice import VoiceActivityStatus
from alx.providers.codex_subscription import CodexSubscriptionReasoningModel
from alx.providers.coding_agent import CodingAgent
from alx.providers.coding_process import bind_provider_observer, reset_provider_observer
from alx.providers.errors import ProviderError


_USAGE_LIMIT = (
    "You've hit your usage limit. Upgrade to Pro "
    "(https://chatgpt.com/explore/pro), visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at 7:32 PM."
)
_REVIEW = json.dumps({"findings": []})


def _event(**values) -> str:
    return json.dumps(values)


# What each scripted child does after reading its prompt from stdin. `emit`
# writes one event line; `sleep` holds the child open without output. Each
# script records its pid and counts its own launches.
_QUOTA = [
    ("emit", _event(type="thread.started", thread_id="t")),
    ("emit", _event(type="turn.started")),
    ("emit", _event(type="error", message=_USAGE_LIMIT)),
    ("emit", _event(type="turn.failed", error={"message": _USAGE_LIMIT})),
    # A child slow to exit after refusing must not hold the review.
    ("sleep", 30),
]
_SILENT = [
    ("emit", _event(type="thread.started", thread_id="t")),
    ("emit", _event(type="turn.started")),
    ("sleep", 60),
]
_STALLS_AFTER_WORK = [
    ("emit", _event(type="turn.started")),
    ("emit", _event(type="item.completed", item={"type": "reasoning", "text": "r"})),
    ("sleep", 60),
]
_ACTIVE = [
    ("emit", _event(type="thread.started", thread_id="t")),
    ("emit", _event(type="turn.started")),
    # Longer in total than the idle bound, but never silent for that long.
    *[
        step
        for _ in range(8)
        for step in (
            ("sleep", 0.3),
            ("emit", _event(type="item.completed", item={"type": "reasoning", "text": "r"})),
        )
    ],
    ("emit", _event(type="item.completed", item={"type": "agent_message", "text": _REVIEW})),
    ("emit", _event(type="turn.completed", usage={"input_tokens": 3, "output_tokens": 2})),
]
# Keeps reporting progress forever, so only the call timeout can end it.
_PROGRESS_FOREVER = [
    ("emit", _event(type="turn.started")),
    *[
        step
        for _ in range(200)
        for step in (
            ("sleep", 0.2),
            ("emit", _event(type="item.completed", item={"type": "reasoning", "text": "r"})),
        )
    ],
]


def _fake_codex(directory: Path, name: str, *plans: list) -> Path:
    """A `codex` stand-in that plays plans[n] on its nth launch."""
    script = directory / name
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"state = {str(directory / (name + '.launches'))!r}\n"
        "count = int(open(state).read()) if os.path.exists(state) else 0\n"
        "open(state, 'w').write(str(count + 1))\n"
        f"open({str(directory / (name + '.pid'))!r}, 'w').write(str(os.getpid()))\n"
        f"plans = json.loads({json.dumps(json.dumps(plans))})\n"
        "sys.stdin.read()\n"
        "for kind, value in plans[min(count, len(plans) - 1)]:\n"
        "    if kind == 'emit':\n"
        "        print(value, flush=True)\n"
        "    else:\n"
        "        time.sleep(value)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _launches(script: Path) -> int:
    counter = script.with_name(script.name + ".launches")
    return int(counter.read_text()) if counter.exists() else 0


def _child_is_gone(script: Path) -> bool:
    pid = int(script.with_name(script.name + ".pid").read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    # Reaped or not, a zombie is not running.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
    ).stdout.strip()
    return not state or state.startswith("Z")


def _reviewer(script: Path, *, timeout: int = 30, idle: float = 1.0) -> CodexSubscriptionReasoningModel:
    return CodexSubscriptionReasoningModel(
        "gpt-5.6-luna", timeout, executable=str(script), idle_seconds=idle,
        environment={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
    )


def _request() -> ModelRequest:
    return ModelRequest(
        (
            ModelMessage(ModelRole.SYSTEM, "Return only the requested JSON."),
            ModelMessage(ModelRole.USER, '{"candidate": "review"}'),
        ),
        "alx_coding_local_review",
        {"type": "object", "properties": {"findings": {"type": "array"}},
         "required": ["findings"], "additionalProperties": False},
        kind="coding",
    )


class CodexAdapterObservesItsChild(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.states: list[str] = []
        token = bind_provider_observer(self.states.append)
        self.addCleanup(reset_provider_observer, token)

    def test_exhausted_quota_fails_as_soon_as_the_provider_refuses(self) -> None:
        script = _fake_codex(self.directory, "codex", _QUOTA)
        started = time.monotonic()
        with self.assertRaises(ProviderError) as raised:
            _reviewer(script).complete(_request())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(raised.exception.reason, "subscription_usage_exhausted")
        self.assertTrue(_child_is_gone(script))

    def test_a_silent_child_that_never_began_is_unresponsive_within_the_idle_bound(self) -> None:
        script = _fake_codex(self.directory, "codex", _SILENT)
        started = time.monotonic()
        with self.assertRaises(ProviderError) as raised:
            _reviewer(script, timeout=60, idle=1).complete(_request())
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(raised.exception.reason, "provider_unresponsive")
        self.assertTrue(_child_is_gone(script))
        # It was never reported as working.
        self.assertIn("connecting", self.states)
        self.assertNotIn("active", self.states)

    def test_a_child_that_goes_silent_after_working_is_stalled(self) -> None:
        script = _fake_codex(self.directory, "codex", _STALLS_AFTER_WORK)
        with self.assertRaises(ProviderError) as raised:
            _reviewer(script, timeout=60, idle=1).complete(_request())
        self.assertEqual(raised.exception.reason, "provider_stalled")
        self.assertIn("active", self.states)

    def test_a_working_review_outlasts_the_idle_bound_while_it_reports_progress(self) -> None:
        script = _fake_codex(self.directory, "codex", _ACTIVE)
        started = time.monotonic()
        completion = _reviewer(script, idle=1).complete(_request())
        self.assertGreater(time.monotonic() - started, 1.5)
        self.assertEqual(dict(completion.output), {"findings": ()})
        self.assertGreaterEqual(self.states.count("active"), 8)

    def test_the_call_timeout_still_bounds_a_child_that_never_finishes(self) -> None:
        script = _fake_codex(self.directory, "codex", _PROGRESS_FOREVER)
        started = time.monotonic()
        with self.assertRaises(ProviderError) as raised:
            _reviewer(script, timeout=2, idle=1).complete(_request())
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(raised.exception.reason, "reasoning_timeout")
        self.assertTrue(_child_is_gone(script))


class ReviewProviderFailureKeepsTheJob(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root = _worktree(self.directory)
        self.request = CodingRequest(
            task="fix add", job_id="job-1", repair_branch="fix/job-1",
            commit_message="fix add",
        )
        self.telemetry: list[CodingTelemetry] = []

    def _agent(self, reviewer, session, model=None) -> CodingAgent:
        return CodingAgent(
            model or PlanningModel(), session, reviewer,
            repository=self.root, telemetry_sink=self.telemetry.append,
        )

    def _review_reports(self) -> list[CodingTelemetry]:
        return [item for item in self.telemetry if item.phase == "review"]

    def test_exhausted_quota_is_one_fast_attempt_that_preserves_the_diff(self) -> None:
        script = _fake_codex(self.directory, "codex", _QUOTA)
        session = RecordingSession(edits={"app.py": _FIXED})
        started = time.monotonic()
        outcome = self._agent(_reviewer(script), session).run(self.request)

        self.assertLess(time.monotonic() - started, 15)
        # The provider refused; asking it twice more could not help.
        self.assertEqual(_launches(script), 1)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.review_classification, "infrastructure")
        self.assertTrue(outcome.diff_preserved)
        self.assertEqual(outcome.diagnostics["reason_code"], "subscription_usage_exhausted")
        self.assertIs(outcome.diagnostics["provider_unavailable"], True)
        self.assertEqual((self.root / "app.py").read_text(), _FIXED)
        self.assertEqual(json.loads(outcome.checkpoint)["stage"], "review")

        # The terminal says what happened and does not stay on REVIEW.
        transitions = [item.transition for item in self.telemetry]
        self.assertIn(
            "REVIEW provider unavailable (subscription_usage_exhausted)", transitions,
        )
        unavailable = next(
            item for item in self.telemetry if item.provider_state == "unavailable"
        )
        self.assertFalse(unavailable.in_flight)
        self.assertEqual(unavailable.provider, "codex_subscription")
        self.assertTrue(self.telemetry[-1].terminal)
        self.assertEqual(self.telemetry[-1].outcome, "failed")

    def test_a_silent_provider_is_bounded_and_the_job_stays_resumable_at_review(self) -> None:
        script = _fake_codex(self.directory, "codex", _SILENT)
        session = RecordingSession(edits={"app.py": _FIXED})
        model = PlanningModel()
        started = time.monotonic()
        first = self._agent(_reviewer(script, timeout=600, idle=1), session, model).run(
            self.request
        )
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(_launches(script), 1)
        self.assertEqual(first.diagnostics["reason_code"], "provider_unresponsive")
        self.assertTrue(first.diff_preserved)
        self.assertTrue(self.telemetry[-1].terminal)

        # A working reviewer later: the review runs, and nothing before it.
        checkpoint = json.loads(first.checkpoint)
        self.assertEqual(checkpoint["stage"], "review")
        planning_calls = len(model.requests)
        second = self._agent(PlanningModel(), session, model).run(replace(
            self.request, job_id="job-2", resume_checkpoint=checkpoint,
        ))
        self.assertEqual(second.status, "succeeded")
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(len(model.requests), planning_calls)
        self.assertIsNotNone(second.commit)

    def test_a_reviewer_timeout_retries_the_review_only(self) -> None:
        script = _fake_codex(self.directory, "codex", _PROGRESS_FOREVER, _ACTIVE)
        session = RecordingSession(edits={"app.py": _FIXED})
        model = PlanningModel()
        outcome = self._agent(_reviewer(script, timeout=4, idle=1), session, model).run(
            self.request
        )
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(_launches(script), 2)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(
            [request.output_schema_name for request in model.requests], ["alx_coding_plan"],
        )
        self.assertEqual(
            [attempt.reason for attempt in outcome.review_attempts], ["reasoning_timeout"],
        )

    def test_review_telemetry_is_a_heartbeat_from_the_provider(self) -> None:
        script = _fake_codex(self.directory, "codex", _ACTIVE)
        session = RecordingSession(edits={"app.py": _FIXED})
        outcome = self._agent(_reviewer(script, idle=1), session).run(self.request)
        self.assertEqual(outcome.status, "succeeded")
        beats = [item for item in self._review_reports() if item.provider_state]
        self.assertEqual(beats[0].provider_state, "connecting")
        self.assertIn("active", {item.provider_state for item in beats})
        self.assertTrue(all(item.in_flight for item in beats))
        self.assertTrue(all(item.provider == "codex_subscription" for item in beats))
        # Each heartbeat carries its own observation time.
        self.assertGreater(beats[-1].last_activity_at, beats[0].last_activity_at)
        self.assertIn("REVIEW completed", [item.transition for item in self.telemetry])


class TheTerminalShowsOnlyWhatIsObserved(unittest.TestCase):
    def _snapshot(self, telemetry: CodingTelemetry, age: int) -> dict:
        return VoiceActivityStatus._snapshot(
            telemetry, lambda: True, telemetry.last_activity_at + timedelta(seconds=age),
        )

    def _review(self, **changes) -> CodingTelemetry:
        values = dict(
            job_id="job-1", phase="review", started_at=NOW, phase_started_at=NOW,
            last_activity_at=NOW, provider="codex_subscription", model="gpt-5.6-luna",
            in_flight=True,
        )
        values.update(changes)
        return CodingTelemetry(**values)

    def test_a_live_heartbeat_is_active_and_a_lost_one_is_stalled(self) -> None:
        for provider_state in ("connecting", "active"):
            telemetry = self._review(provider_state=provider_state)
            live = self._snapshot(telemetry, 5)
            self.assertFalse(live["stalled"])
            self.assertEqual(live["provider_state"], provider_state)
            lost = self._snapshot(telemetry, CODING_STALL_SECONDS)
            self.assertTrue(lost["stalled"], provider_state)

    def test_an_unavailable_provider_is_reported_as_such(self) -> None:
        snapshot = self._snapshot(
            self._review(in_flight=False, provider_state="unavailable",
                         transition="REVIEW provider unavailable (subscription_usage_exhausted)"),
            1,
        )
        self.assertEqual(snapshot["provider_state"], "unavailable")
        self.assertFalse(snapshot["in_flight"])

    def test_an_unobserved_in_flight_call_is_not_called_stalled_from_elapsed_time(self) -> None:
        # Without a provider heartbeat, age is only elapsed time since a
        # lifecycle boundary, which says nothing about the child.
        snapshot = self._snapshot(self._review(phase="execution"), CODING_STALL_SECONDS * 5)
        self.assertFalse(snapshot["stalled"])

    def test_the_contract_refuses_an_unknown_provider_state(self) -> None:
        with self.assertRaises(ValueError):
            self._review(provider_state="probably fine")


if __name__ == "__main__":
    unittest.main()
