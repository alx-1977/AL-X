"""A verification command AL/X requires for a coding job actually runs.

On 2026-10-05 a coding acceptance probe asked, in its acceptance criteria and
test guidance, for `python -m pytest tests/test_governance_consistency.py -q`.
The job committed and was recorded `succeeded` with every required check
passed, yet that command never ran: verification is derived from the changed
paths only, by design, and prose is never parsed for commands. A request
written as prose had nowhere structured to go.

`run_coding_task` now takes `verification_commands`: exact argv arrays AL/X
chooses. They run beside the derived checks, through the same allowlist, and
a requested command that is refused, fails or never runs fails verification.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding import CodingRequest  # noqa: E402
from unittest import mock  # noqa: E402

from alx.providers import coding_agent  # noqa: E402
from alx.providers.coding_agent import (  # noqa: E402
    CodingAgent, build_briefing, recorded_verification_proves, requested_verification,
)
from alx.providers.coding_workspace import CodingWorkspace  # noqa: E402
from alx.contracts.coding_verification import required_verification  # noqa: E402
from alx.tools.coding import DEFINITION, parse_coding_arguments  # noqa: E402

PYTHON = Path(sys.executable).name


class ArgumentTests(unittest.TestCase):
    BASE = {"task": "t", "repair_branch": "chore/x", "commit_message": "m"}

    def test_commands_become_structured_requested_checks(self) -> None:
        request, failure = parse_coding_arguments({
            **self.BASE,
            "verification_commands": [["python", "-m", "pytest", "tests/a.py", "-q"]],
        }, "job-1")
        self.assertIsNone(failure)
        self.assertEqual(
            request.requested_checks, (("python", "-m", "pytest", "tests/a.py", "-q"),)
        )

    def test_prose_guidance_still_requests_nothing(self) -> None:
        request, failure = parse_coding_arguments({
            **self.BASE, "test_guidance": "Run python -m pytest tests/a.py -q.",
        }, "job-1")
        self.assertIsNone(failure)
        self.assertEqual(request.requested_checks, ())

    def test_malformed_or_excessive_commands_are_refused(self) -> None:
        for value in (
            "python -m pytest", [[]], [["python", ""]], [[1, 2]],
            [["python"], ["python"], ["python"]],
        ):
            with self.subTest(value=value):
                _request, failure = parse_coding_arguments(
                    {**self.BASE, "verification_commands": value}, "job-1"
                )
                self.assertIsNotNone(failure)

    def test_the_capability_declares_and_keeps_the_field(self) -> None:
        self.assertIn("verification_commands", DEFINITION.input_schema.properties)
        self.assertIn("verification_commands", DEFINITION.durable_input_fields)
        self.assertIn("verification_commands", DEFINITION.purpose)

    def test_the_session_is_told_what_will_run(self) -> None:
        request = CodingRequest(task="t", job_id="j", requested_checks=(("python", "-m", "pytest"),))
        self.assertIn("python -m pytest", build_briefing(request, {}))

    def test_a_command_the_policy_already_requires_is_not_run_twice(self) -> None:
        derived = required_verification(("README.md",))
        request = CodingRequest(task="t", job_id="j", requested_checks=(
            ("git", "diff", "--check"), ("python", "-m", "pytest", "-q"),
        ))
        checks = requested_verification(request, derived.checks)
        self.assertEqual([c.argv for c in checks], [("python", "-m", "pytest", "-q")])
        self.assertEqual(checks[0].name, "requested_2")


class VerificationRunsTheRequestTests(unittest.TestCase):
    """The real `_verify` against a real repository and real commands."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self._git("init", "-q", "-b", "main")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n", encoding="utf-8")
        (self.root / "tests" / "test_bad.py").write_text(
            "def test_bad():\n    assert False\n", encoding="utf-8")
        (self.root / "notes.md").write_text("# notes\n", encoding="utf-8")
        self.agent = CodingAgent.__new__(CodingAgent)

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def _verify(self, *requested: tuple[str, ...]):
        request = CodingRequest(
            task="t", job_id="j", worktree=str(self.root), requested_checks=requested,
        )
        commands: list = []
        evidence, tests_run, tests_passed = self.agent._verify(
            request, CodingWorkspace(str(self.root), ()), ("notes.md",), commands,
        )
        return evidence, tests_run, commands

    def _pytest(self, target: str) -> tuple[str, ...]:
        return (PYTHON, "-m", "pytest", target, "-q", "-p", "no:cacheprovider")

    def test_a_requested_command_runs_beside_the_derived_checks(self) -> None:
        evidence, tests_run, commands = self._verify(self._pytest("tests/test_ok.py"))
        by_name = {check.name: check for check in evidence.checks}
        self.assertTrue(by_name["requested_1"].ran)
        self.assertTrue(by_name["requested_1"].passed)
        self.assertTrue(by_name["diff_check"].ran)
        self.assertTrue(by_name["content_check"].ran)
        self.assertTrue(evidence.all_required_passed)
        self.assertTrue(tests_run)
        self.assertIn(self._pytest("tests/test_ok.py"), [c.argv for c in commands])

    def test_a_failing_requested_command_fails_verification(self) -> None:
        evidence, _tests_run, _commands = self._verify(self._pytest("tests/test_bad.py"))
        check = next(c for c in evidence.checks if c.name == "requested_1")
        self.assertTrue(check.ran)
        self.assertFalse(check.passed)
        self.assertFalse(evidence.all_required_passed)

    def test_a_refused_requested_command_is_required_and_not_passed(self) -> None:
        evidence, _tests_run, commands = self._verify(("rm", "-rf", "tests"))
        check = next(c for c in evidence.checks if c.name == "requested_1")
        self.assertFalse(check.ran)
        self.assertFalse(evidence.all_required_passed)
        self.assertTrue((self.root / "tests").exists())
        self.assertEqual(commands[-1].stderr, "command_not_permitted")

    def test_a_requested_command_is_never_excused_as_failing_on_main(self) -> None:
        """Deduplicated under a derived pytest name, it is still AL/X's requirement."""
        changed = ("tests/test_bad.py",)
        derived = required_verification(changed, self.root).checks
        pytest_check = next(c for c in derived if c.name.startswith("pytest"))

        def verify(requested):
            request = CodingRequest(task="t", job_id="j", worktree=str(self.root),
                                    requested_checks=requested)
            with mock.patch.object(coding_agent, "same_main_pytest_failure",
                                   return_value=(True, "same failure on main")):
                evidence, _run, _passed = self.agent._verify(
                    request, CodingWorkspace(str(self.root), ()), changed, [])
            return next(c for c in evidence.checks if c.argv == pytest_check.argv), evidence

        excused, _evidence = verify(())
        self.assertTrue(excused.passed)  # a derived check may be excused
        required, evidence = verify((pytest_check.argv,))
        self.assertFalse(required.passed)
        self.assertFalse(evidence.all_required_passed)

    def test_resume_accepts_evidence_that_passed_a_requested_command(self) -> None:
        request = CodingRequest(task="t", job_id="j", worktree=str(self.root),
                                requested_checks=(self._pytest("tests/test_ok.py"),))
        evidence, _run, _commands = self._verify(self._pytest("tests/test_ok.py"))
        self.assertTrue(recorded_verification_proves(evidence, request, ("notes.md",), self.root))
        # Evidence that never ran the requested command does not prove it.
        derived_only, _r, _c = self._verify()
        self.assertFalse(recorded_verification_proves(derived_only, request, ("notes.md",), self.root))


if __name__ == "__main__":
    unittest.main()
