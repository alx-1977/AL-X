"""Verification is what the changed files require, not whatever pytest says.

The Coding Agent's commit predicate used to be `tests_run and tests_passed`.
That made one verification class the definition of verification: a job with no
Python in it could not satisfy the predicate however correct it was, and on
2026-09-21 a one-line `TODO.md` edit was refused its commit because the full
repository suite — selected as a fallback it had no reason to run — exceeded the
verification timeout.

These tests hold the corrected model: the required checks are a deterministic
function of the job's final changed-file set, a job must pass all of them, and
passing them is what authorises a commit whether or not any test was among them.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts.coding_verification import (  # noqa: E402
    ARCHITECTURE_GATE,
    DIFF_CHECK,
    FULL_SUITE,
    GOVERNANCE_GATE,
    VerificationCheck,
    VerificationEvidence,
    required_verification,
)
from alx.providers.coding_process import command_permitted  # noqa: E402


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _names(changed, root=REPOSITORY_ROOT) -> tuple[str, ...]:
    return tuple(check.name for check in required_verification(changed, root).checks)


def _commands(changed, root=REPOSITORY_ROOT):
    return required_verification(changed, root).commands


class PolicyIsDerivedFromTheChangedFiles(unittest.TestCase):
    """The deterministic path-based policy, case by case."""

    def test_a_documentation_only_change_requires_only_the_diff_check(self) -> None:
        """1. The incident. `TODO.md` owes a diff check and nothing else.

        Specifically it does not owe the full pytest suite. That fallback is
        what turned a correct one-line documentation edit into a timeout and a
        refused commit.
        """
        self.assertEqual(_names(("TODO.md",)), ("diff_check",))
        self.assertEqual(_commands(("TODO.md",)), (DIFF_CHECK,))
        self.assertNotIn(FULL_SUITE, _commands(("TODO.md",)))
        self.assertFalse(required_verification(("TODO.md",), REPOSITORY_ROOT).requires_tests())

    def test_a_canonical_governance_document_requires_the_governance_gate(self) -> None:
        """2. `governance/DECISIONS.md` owes the diff check and that gate."""
        names = _names(("governance/DECISIONS.md",))
        self.assertEqual(names, ("diff_check", "governance_gate"))
        self.assertEqual(
            _commands(("governance/DECISIONS.md",)), (DIFF_CHECK, GOVERNANCE_GATE)
        )
        # A governance document is not Python and owes no test run.
        self.assertNotIn(FULL_SUITE, _commands(("governance/DECISIONS.md",)))

    def test_the_canonical_document_set_is_the_governance_gate_s_own(self) -> None:
        """The taxonomy is read from repository law, not restated here.

        `LAWS_OF_ALX.md` sits at the repository root, so no path prefix would
        catch it. It is required to run the governance gate because
        `scripts/check_governance.py` declares it canonical, which is the only
        place that declaration lives.
        """
        self.assertIn("governance_gate", _names(("LAWS_OF_ALX.md",)))
        self.assertIn("governance_gate", _names(("docs/LAW_ENFORCEMENT.md",)))
        # And a document the gate does not declare canonical does not select it.
        self.assertNotIn("governance_gate", _names(("README.md",)))

    def test_an_architecture_governed_file_requires_the_architecture_gate(self) -> None:
        """3. A path under the gate's declared source root selects it."""
        self.assertIn("architecture_gate", _names(("src/alx/core/loop.py",)))
        self.assertIn(ARCHITECTURE_GATE, _commands(("src/alx/core/loop.py",)))
        # The manifest itself is governed by the gate it configures — and the
        # governance gate declares it canonical too, so it owes both. Neither
        # taxonomy is restated here, so the overlap is theirs, not a rule this
        # module invented.
        self.assertEqual(
            _names(("architecture/boundaries.toml",)),
            ("diff_check", "governance_gate", "architecture_gate"),
        )

    def test_a_python_module_with_a_mapped_test_selects_that_test(self) -> None:
        """4. `src/alx/…` maps to its conventional test, which exists."""
        changed = ("src/alx/contracts/coding_verification.py",)
        names = _names(changed)
        self.assertEqual(
            names, ("diff_check", "architecture_gate", "pytest_targeted")
        )
        self.assertIn(
            ("python", "-m", "pytest", "-q", "tests/test_coding_verification.py"),
            _commands(changed),
        )
        self.assertNotIn(FULL_SUITE, _commands(changed))

    def test_a_changed_test_file_selects_itself(self) -> None:
        """5. A changed test module is its own targeted verification."""
        changed = ("tests/test_coding_agent.py",)
        self.assertEqual(_names(changed), ("diff_check", "pytest_targeted"))
        self.assertEqual(
            _commands(changed),
            (DIFF_CHECK, ("python", "-m", "pytest", "-q", "tests/test_coding_agent.py")),
        )

    def test_a_python_change_with_no_mapped_test_escalates_to_the_suite(self) -> None:
        """6. The one case that still earns the whole suite.

        The change is executable and its blast radius is unknown, so runtime
        verification is required and there is no narrower honest target. A
        *non*-Python change never reaches this escalation, which is the whole
        correction.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "alx" / "tools").mkdir(parents=True)
            (root / "src" / "alx" / "tools" / "orphan.py").write_text("x = 1\n")
            changed = ("src/alx/tools/orphan.py",)
            names = tuple(
                check.name for check in required_verification(changed, root).checks
            )
            self.assertEqual(
                names, ("diff_check", "architecture_gate", "pytest_full")
            )
            self.assertIn(FULL_SUITE, required_verification(changed, root).commands)

    def test_a_non_python_change_never_escalates_to_the_suite(self) -> None:
        """The escalation is reachable only from a Python change."""
        for changed in (
            ("TODO.md",),
            ("README.md",),
            ("governance/DECISIONS.md",),
            ("docs/TECHNICAL_PLAN.md", "TODO.md"),
            ("config/settings.json",),
        ):
            with self.subTest(changed=changed):
                self.assertNotIn(FULL_SUITE, _commands(changed))
                self.assertFalse(
                    required_verification(changed, REPOSITORY_ROOT).requires_tests()
                )

    def test_the_diff_check_is_required_of_every_change(self) -> None:
        """Always, whatever the job touched, including an empty file set."""
        for changed in ((), ("TODO.md",), ("src/alx/core/loop.py",), ("x.bin",)):
            with self.subTest(changed=changed):
                self.assertEqual(
                    required_verification(changed, REPOSITORY_ROOT).checks[0].argv,
                    DIFF_CHECK,
                )

    def test_the_policy_is_a_pure_function_of_the_paths(self) -> None:
        """Same files in, same checks out; order of the input does not matter."""
        first = _commands(("src/alx/core/loop.py", "governance/DECISIONS.md"))
        second = _commands(("governance/DECISIONS.md", "src/alx/core/loop.py"))
        self.assertEqual(set(first), set(second))
        self.assertEqual(first, _commands(("src/alx/core/loop.py", "governance/DECISIONS.md")))

    def test_every_policy_command_is_already_permitted(self) -> None:
        """11. The policy cannot widen the allowlist; it selects within it."""
        for changed in (
            ("TODO.md",),
            ("governance/DECISIONS.md",),
            ("src/alx/core/loop.py",),
            ("tests/test_coding_agent.py",),
        ):
            for argv in _commands(changed):
                with self.subTest(argv=argv):
                    self.assertTrue(
                        command_permitted(list(argv), REPOSITORY_ROOT),
                        f"policy proposed a command the allowlist refuses: {argv}",
                    )


class TheAllowlistStaysNarrow(unittest.TestCase):
    """11. The Coding Agent cannot execute commands outside the allowlist."""

    def test_the_two_gate_scripts_are_permitted_exactly(self) -> None:
        for argv in (list(GOVERNANCE_GATE), list(ARCHITECTURE_GATE)):
            with self.subTest(argv=argv):
                self.assertTrue(command_permitted(argv, REPOSITORY_ROOT))

    def test_no_other_python_script_may_run(self) -> None:
        """A script path is arbitrary code; only the two gates are named."""
        for argv in (
            ["python", "scripts/connect_xero.py"],
            ["python", "setup.py"],
            ["python", "src/alx/core/loop.py"],
            ["python", "scripts/../scripts/check_governance.py"],
            ["python", "scripts/check_governance.py", "--fix"],
            ["python", "scripts/check_governance.py", "extra"],
            ["python", "-c", "import os; os.system('sh')"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(
                    command_permitted(argv, REPOSITORY_ROOT),
                    f"the allowlist admitted {argv}",
                )

    def test_a_blocked_gate_path_is_refused(self) -> None:
        """The gate must be the worktree's own and not a blocked path."""
        self.assertFalse(
            command_permitted(
                list(GOVERNANCE_GATE), REPOSITORY_ROOT, ("scripts",)
            )
        )

    def test_git_diff_check_is_permitted_and_stays_read_only(self) -> None:
        self.assertTrue(command_permitted(list(DIFF_CHECK), REPOSITORY_ROOT))
        # Widening `git` itself is not what this change did.
        for argv in (
            ["git", "commit", "-m", "x"],
            ["git", "push"],
            ["git", "checkout", "main"],
            ["git", "diff", "--check", "--exit-code-please"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(command_permitted(argv, REPOSITORY_ROOT))

    def test_no_generic_shell_is_reachable(self) -> None:
        for argv in (
            ["sh", "-c", "ls"],
            ["bash", "-c", "ls"],
            ["make", "test"],
            ["./scripts/check_governance.py"],
            ["/usr/bin/python", "scripts/check_governance.py"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(command_permitted(argv, REPOSITORY_ROOT))


class EvidenceAnswersWhatWasRequiredAndWhatHappened(unittest.TestCase):
    """The durable record the commit predicate now reads."""

    @staticmethod
    def _evidence(*results: tuple[str, bool, bool]) -> VerificationEvidence:
        return VerificationEvidence(
            tuple(
                VerificationCheck(name, ("x",), "because", ran=ran, passed=passed)
                for name, ran, passed in results
            )
        )

    def test_all_required_passed_needs_every_check_to_have_run_and_passed(self) -> None:
        self.assertTrue(
            self._evidence(("a", True, True), ("b", True, True)).all_required_passed
        )
        self.assertFalse(
            self._evidence(("a", True, True), ("b", True, False)).all_required_passed
        )
        # 8. A required check that never ran is not a check that passed.
        self.assertFalse(
            self._evidence(("a", True, True), ("b", False, False)).all_required_passed
        )

    def test_an_empty_policy_verifies_nothing(self) -> None:
        """Unreachable in practice, but it must not read as success."""
        self.assertFalse(VerificationEvidence(()).all_required_passed)

    def test_the_record_names_what_was_required_what_ran_and_what_failed(self) -> None:
        evidence = self._evidence(
            ("diff_check", True, True),
            ("governance_gate", True, False),
            ("pytest_targeted", False, False),
        )
        values = evidence.as_values()
        self.assertEqual(
            values["required"], ["diff_check", "governance_gate", "pytest_targeted"]
        )
        self.assertEqual(values["ran"], ["diff_check", "governance_gate"])
        self.assertEqual(values["failed"], ["governance_gate", "pytest_targeted"])
        self.assertFalse(values["all_required_passed"])
        # Each check carries its own result and the reason it was required.
        self.assertEqual(len(values["checks"]), 3)
        self.assertEqual(values["checks"][0]["name"], "diff_check")
        self.assertTrue(values["checks"][0]["passed"])
        self.assertEqual(values["checks"][0]["reason"], "because")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
