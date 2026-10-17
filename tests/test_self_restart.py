"""AL/X restarts herself onto code she has merged (D-055).

Friedl, 2026-10-17: "ALX should be able to improve things around the core",
and an improvement she merged should go live without him restarting her.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS  # noqa: E402
from alx.providers.self_restart import restart_environment  # noqa: E402
from alx.tools.self_restart import RESTART_SELF, build_self_restart_executors  # noqa: E402


class RestartSelfTests(unittest.TestCase):
    def build(self, running="a" * 40, main="b" * 40, coding=False, launch=None):
        self.restarts, self.released = [], []
        return build_self_restart_executors(
            lambda: running, lambda: main, lambda: not coding,
            lambda: self.released.append(1),
            launch or (lambda: self.restarts.append(1)), lambda: "call-1",
        )[RESTART_SELF]

    def test_a_restart_that_cannot_start_lets_jobs_run_again(self) -> None:
        def broken():
            raise OSError("no launcher")
        result = self.build(launch=broken)({})
        self.assertEqual(result.failure["code"], "unavailable")
        self.assertEqual(self.released, [1])

    def test_merged_code_not_yet_running_is_restarted_onto(self) -> None:
        result = self.build()({})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["main_commit"], "b" * 40)
        self.assertEqual(self.restarts, [1])

    def test_nothing_new_is_not_a_restart(self) -> None:
        result = self.build(main="a" * 40)({})
        self.assertEqual(result.failure["code"], "nothing_new_to_run")
        self.assertEqual(self.restarts, [])

    def test_a_running_coding_job_is_not_cut_off(self) -> None:
        result = self.build(coding=True)({})
        self.assertEqual(result.failure["code"], "coding_job_running")
        self.assertEqual(self.restarts, [])

    def test_unknown_commits_refuse(self) -> None:
        result = self.build(running="")({})
        self.assertEqual(result.failure["code"], "unavailable")
        self.assertEqual(self.restarts, [])

    def test_the_launcher_runs_committed_mains_copy(self) -> None:
        """The runtime inherits the launcher's mark; passed on, the launcher
        would run the checkout's copy, which may be a feature branch's."""
        with patch.dict(os.environ, {"ALX_LAUNCHER_COMMITTED": "1", "PATH": "/bin"}):
            environment = restart_environment()
        self.assertNotIn("ALX_LAUNCHER_COMMITTED", environment)
        self.assertEqual(environment["PATH"], "/bin")


class RestartAndCodingExcludeEachOtherTests(unittest.TestCase):
    """PR #150 review: a planned coding job could start in the launcher's
    five seconds and be cut off."""

    def agent(self):
        import subprocess, tempfile
        from tests.test_coding_agent import PlanningModel, RecordingSession, _worktree
        from alx.providers.coding_agent import CodingAgent
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = _worktree(Path(directory.name))
        self.session = RecordingSession()
        return CodingAgent(PlanningModel(), self.session, repository=self.root,
                           protected_paths=(self.root / "runtime-data",))

    def test_no_job_starts_once_a_restart_is_reserved(self) -> None:
        from alx.contracts.coding import CodingError, CodingRequest
        agent = self.agent()
        self.assertTrue(agent.reserve_for_restart())
        with self.assertRaises(CodingError) as refused:
            agent.run(CodingRequest(task="t", job_id="j", repair_branch="fix/j",
                                    commit_message="m"))
        self.assertEqual(refused.exception.details["reason_code"], "restart_pending")
        self.assertEqual(self.session.calls, [])
        agent.release_restart()
        agent.run(CodingRequest(task="t", job_id="j2", repair_branch="fix/j2",
                                commit_message="m"))
        self.assertEqual(len(self.session.calls), 1)

    def test_storage_configured_anywhere_in_the_checkout_is_blocked(self) -> None:
        """PR #150 review: a storage root other than .alx stayed reachable."""
        from alx.contracts.coding import CodingRequest
        agent = self.agent()
        agent.run(CodingRequest(task="t", job_id="j", repair_branch="fix/j",
                                commit_message="m"))
        request, briefing = self.session.calls[0]
        self.assertIn("runtime-data", request.blocked_paths)
        self.assertIn("runtime-data", briefing)


class SheKnowsTests(unittest.TestCase):
    def test_she_restarts_herself_and_improves_around_her_core(self) -> None:
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        self.assertIn("Restart yourself onto it with restart_self", text)
        self.assertNotIn("tell Friedl a restart is needed", text)
        self.assertIn("Improving yourself is yours too", text)
        self.assertIn("first in the work you do together, but not only there", text)
        self.assertIn("you bring to Friedl as a proposal first", text)


if __name__ == "__main__":
    unittest.main()
