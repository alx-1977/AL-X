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
    def build(self, running="a" * 40, main="b" * 40, coding=False):
        self.restarts = []
        return build_self_restart_executors(
            lambda: running, lambda: main, lambda: coding,
            lambda: self.restarts.append(1), lambda: "call-1",
        )[RESTART_SELF]

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
