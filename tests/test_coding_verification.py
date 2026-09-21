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
    MAX_CONTENT_CHARACTERS,
    MAX_CONTENT_FINDINGS,
    DIFF_CHECK,
    FULL_SUITE,
    GOVERNANCE_GATE,
    VerificationCheck,
    VerificationEvidence,
    content_violations,
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
        self.assertEqual(_names(("TODO.md",)), ("diff_check", "content_check"))
        # One command; the content check is performed in process and has none.
        self.assertEqual(_commands(("TODO.md",)), (DIFF_CHECK,))
        self.assertNotIn(FULL_SUITE, _commands(("TODO.md",)))
        self.assertFalse(required_verification(("TODO.md",), REPOSITORY_ROOT).requires_tests())

    def test_a_canonical_governance_document_requires_the_governance_gate(self) -> None:
        """2. `governance/DECISIONS.md` owes the diff check and that gate."""
        names = _names(("governance/DECISIONS.md",))
        self.assertEqual(names, ("diff_check", "content_check", "governance_gate"))
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

    def test_a_dot_directory_document_still_selects_the_governance_gate(self) -> None:
        """A leading-dot directory is not a relative-path prefix.

        Normalisation used to be `lstrip("./")`, which strips a character set
        rather than a prefix, so `.github/workflows/law-gates.yml` became
        `github/workflows/…` and matched no canonical document. A job changing
        the CI workflow or the pull-request template would therefore have
        skipped the governance gate that protects them. Found in review on
        PR #54.
        """
        for name in (
            ".github/workflows/law-gates.yml",
            ".github/pull_request_template.md",
            ".github/CODEOWNERS",
            ".github/copilot-instructions.md",
        ):
            with self.subTest(name=name):
                self.assertIn("governance_gate", _names((name,)), name)

    def test_relative_prefixes_are_stripped_without_eating_the_path(self) -> None:
        """`./x` and `x` are the same file; `.github` is not `github`."""
        self.assertEqual(
            _commands(("./governance/DECISIONS.md",)),
            _commands(("governance/DECISIONS.md",)),
        )
        self.assertEqual(
            _commands(("././TODO.md",)), _commands(("TODO.md",))
        )
        # And a repeated spelling of one file is still one file.
        self.assertEqual(
            _commands(("./TODO.md", "TODO.md")), (DIFF_CHECK,)
        )

    def test_a_path_s_own_whitespace_is_preserved(self) -> None:
        """Git permits leading and trailing spaces in a path.

        `strip()` changed the path itself, so a file genuinely named
        `broken.py ` became `broken.py`: the content check inspected a path
        that did not exist and reported nothing, while `git diff --check` could
        not see the untracked file either. Found in review on PR #54.
        """
        from alx.contracts.coding_verification import _normalise

        self.assertEqual(_normalise(("broken.py ",)), ("broken.py ",))
        self.assertEqual(_normalise((" lead.py",)), (" lead.py",))
        # The relative prefixes are still removed.
        self.assertEqual(_normalise(("./TODO.md",)), ("TODO.md",))
        self.assertEqual(_normalise((".github/x.yml",)), (".github/x.yml",))

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
            ("diff_check", "content_check", "governance_gate", "architecture_gate"),
        )

    def test_changing_a_gate_script_runs_that_gate(self) -> None:
        """A gate must be exercised by the change that edits it.

        Neither script is a canonical document nor sits under the architecture
        source root, so nothing else selected them: editing one escalated to
        the full suite, which runs neither gate and so never exercised the
        edit. Found in review on PR #54.
        """
        governance = _names(("scripts/check_governance.py",))
        self.assertIn("governance_gate", governance)
        architecture = _names(("scripts/check_architecture.py",))
        self.assertIn("architecture_gate", architecture)
        # Still Python, so it still owes a runtime check as well.
        self.assertIn("pytest_full", governance)
        self.assertIn("pytest_full", architecture)

    def test_a_python_module_with_a_mapped_test_selects_that_test(self) -> None:
        """4. `src/alx/…` maps to its conventional test, which exists."""
        changed = ("src/alx/contracts/coding_verification.py",)
        names = _names(changed)
        self.assertEqual(
            names, ("diff_check", "content_check", "architecture_gate", "pytest_targeted")
        )
        self.assertIn(
            ("python", "-m", "pytest", "-q", "tests/test_coding_verification.py"),
            _commands(changed),
        )
        self.assertNotIn(FULL_SUITE, _commands(changed))

    def test_a_changed_test_file_selects_itself(self) -> None:
        """5. A changed test module is its own targeted verification."""
        changed = ("tests/test_coding_agent.py",)
        self.assertEqual(_names(changed), ("diff_check", "content_check", "pytest_targeted"))
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
                names, ("diff_check", "content_check", "architecture_gate", "pytest_full")
            )
            self.assertIn(FULL_SUITE, required_verification(changed, root).commands)

    def test_one_mapped_file_does_not_speak_for_an_unmapped_one(self) -> None:
        """Every changed Python path must be covered, not merely one of them.

        `_targeted_tests` combined mappings across the whole set and the policy
        asked only whether any existed, so a job changing a covered module and
        an uncovered one ran the covered module's test and called the pair
        verified. Found in review on PR #54.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "alx" / "core").mkdir(parents=True)
            (root / "tests").mkdir()
            for name in ("mapped.py", "unmapped.py"):
                (root / "src" / "alx" / "core" / name).write_text("x = 1\n")
            (root / "tests" / "test_core_mapped.py").write_text(
                "def test_x():\n    pass\n"
            )
            mapped_only = required_verification(
                ("src/alx/core/mapped.py",), root
            )
            self.assertIn(
                ("python", "-m", "pytest", "-q", "tests/test_core_mapped.py"),
                mapped_only.commands,
            )
            self.assertNotIn(FULL_SUITE, mapped_only.commands)

            mixed = required_verification(
                ("src/alx/core/mapped.py", "src/alx/core/unmapped.py"), root
            )
            self.assertIn(FULL_SUITE, mixed.commands)
            names = tuple(check.name for check in mixed.checks)
            self.assertIn("pytest_full", names)
            self.assertNotIn("pytest_targeted", names)
            # The reason names the path that forced the escalation.
            reason = next(
                check.reason for check in mixed.checks
                if check.name == "pytest_full"
            )
            self.assertIn("unmapped.py", reason)
            self.assertNotIn("mapped.py,", reason)

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


class TheContentCheckSeesWhatGitCannot(unittest.TestCase):
    """`git diff --check` inspects tracked changes; a new file is untracked.

    Reproduced on PR #54: `git diff --check` exits 0 on an untracked file whose
    content carries leftover conflict markers, so such a file passed
    verification and was committed.
    """

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def write(self, name: str, text: str) -> str:
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return name

    def test_a_leftover_conflict_marker_is_a_finding(self) -> None:
        name = self.write(
            "new.py",
            "def f():\n<<<<<<< HEAD\n    return 1\n=======\n"
            "    return 2\n>>>>>>> other\n",
        )
        findings = content_violations((name,), self.root)
        self.assertTrue(findings)
        self.assertTrue(all("new.py:" in item for item in findings))
        self.assertTrue(any("conflict marker" in item for item in findings))

    def test_trailing_whitespace_is_a_finding(self) -> None:
        name = self.write("notes.md", "# Notes\n\nA line with a space \n")
        findings = content_violations((name,), self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("trailing whitespace", findings[0])
        self.assertIn("notes.md:3", findings[0])

    def test_space_before_tab_in_indent_is_a_finding(self) -> None:
        """Git's own `--check` reports this by default, so this must too.

        The two checks answer one question over different halves of the
        change, so a rule git enforces on a tracked file must not go
        unenforced on a new one. Found in review on PR #54.
        """
        name = self.write("indent.py", "def f():\n \treturn 1\n")
        findings = content_violations((name,), self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("space before tab in indent", findings[0])
        self.assertIn("indent.py:2", findings[0])

    def test_a_space_then_tab_outside_the_indent_is_not_a_finding(self) -> None:
        """Git reports it in the initial indent, not mid-line."""
        name = self.write("prose.md", "A sentence with a space \tand a tab.\n")
        self.assertEqual(content_violations((name,), self.root), ())

    def test_clean_content_is_no_finding(self) -> None:
        for name, text in (
            ("clean.md", "# Notes\n\nOne line.\n"),
            ("clean.py", "def f():\n    return 1\n"),
            ("empty.txt", ""),
            ("crlf.txt", "a line\r\nanother\r\n"),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    content_violations((self.write(name, text),), self.root), ()
                )

    def test_a_file_whose_name_ends_in_a_space_is_still_checked(self) -> None:
        """The path the job changed, spelled exactly as the job changed it."""
        name = self.write("broken.py ", "x = 1 \n")
        findings = content_violations((name,), self.root)
        self.assertEqual(len(findings), 1)
        self.assertIn("trailing whitespace", findings[0])

    def test_only_the_job_s_own_files_are_read(self) -> None:
        """A check on the change, not an audit of the worktree."""
        self.write("mine.md", "# Fine\n")
        self.write("theirs.md", "trailing space \n")
        self.assertEqual(content_violations(("mine.md",), self.root), ())

    def test_unreadable_and_binary_content_is_not_invented_into_a_finding(self) -> None:
        """Binary and missing files are ordinary, not violations."""
        (self.root / "image.bin").write_bytes(b"\x00\x01\xfe\xff" * 64)
        self.assertEqual(content_violations(("image.bin",), self.root), ())
        self.assertEqual(content_violations(("absent.md",), self.root), ())
        # A directory named as a changed path is skipped, not read.
        (self.root / "adir").mkdir()
        self.assertEqual(content_violations(("adir",), self.root), ())

    def test_findings_are_bounded(self) -> None:
        name = self.write("many.txt", "bad \n" * (MAX_CONTENT_FINDINGS * 3))
        self.assertEqual(
            len(content_violations((name,), self.root)), MAX_CONTENT_FINDINGS
        )

    def test_a_symlink_is_not_followed_out_of_the_worktree(self) -> None:
        """Containment: this reads the job's files, not what they point at.

        `Path.is_file()` follows symlinks, so a changed symlink inside the
        worktree would have had this read a regular file outside it — trailing
        whitespace in somebody else's file failing the job's required check,
        and a path this module has no business reading. Every other read in the
        coding package is contained; this one was not. Found in review on
        PR #54.
        """
        outside = self.root.parent / "outside.txt"
        outside.write_text("trailing space \n", encoding="utf-8")
        (self.root / "link.txt").symlink_to(outside)
        self.assertEqual(content_violations(("link.txt",), self.root), ())

    def test_a_symlink_inside_the_worktree_is_also_skipped(self) -> None:
        """A symlink is not malformed text, wherever it points."""
        self.write("real.md", "# Fine\n")
        (self.root / "alias.md").symlink_to(self.root / "real.md")
        self.assertEqual(content_violations(("alias.md",), self.root), ())

    def test_a_file_past_the_character_bound_fails_rather_than_passing(self) -> None:
        """Fails closed: an unchecked remainder must not read as clean.

        The bound used to slice the text after reading it whole, so a conflict
        marker past the limit was silently skipped *and* the read had no memory
        bound. Clean content spans the limit here, with the violation placed
        after it, so an implementation that checked only the prefix would report
        the file clean and fail this test. Found in review on PR #54.
        """
        filler = "clean line\n" * ((MAX_CONTENT_CHARACTERS // 11) + 10)
        self.assertGreater(len(filler), MAX_CONTENT_CHARACTERS)
        name = self.write("huge.txt", filler + "<<<<<<< HEAD\n")
        findings = content_violations((name,), self.root)
        self.assertTrue(findings)
        self.assertIn("was not checked", findings[0])
        self.assertIn("huge.txt", findings[0])

    def test_a_file_at_the_bound_is_still_checked_normally(self) -> None:
        """The bound is a ceiling, not an off-by-one that refuses valid files."""
        name = self.write("atlimit.txt", "a" * MAX_CONTENT_CHARACTERS)
        self.assertEqual(content_violations((name,), self.root), ())

    def test_no_root_means_no_finding_rather_than_a_crash(self) -> None:
        self.assertEqual(content_violations(("x.md",), None), ())


class TheBoundNeverSilentlyDropsARequirement(unittest.TestCase):
    """A ceiling on work is not permission to skip a required check."""

    def test_the_policy_fits_well_inside_the_command_bound(self) -> None:
        from alx.contracts.coding import MAX_VERIFICATION_COMMANDS

        widest = required_verification(
            (
                "governance/DECISIONS.md",
                "architecture/boundaries.toml",
                "src/alx/core/loop.py",
                "tests/test_coding_agent.py",
                "TODO.md",
            ),
            REPOSITORY_ROOT,
        )
        self.assertLessEqual(len(widest.checks), MAX_VERIFICATION_COMMANDS)

    def test_an_oversized_policy_would_verify_nothing_rather_than_part(self) -> None:
        """Fails closed: a truncated policy must not read as fully passed."""
        from alx.contracts.coding import MAX_VERIFICATION_COMMANDS

        oversized = VerificationEvidence(
            tuple(
                VerificationCheck(f"check-{index}", ("x",), "because")
                for index in range(MAX_VERIFICATION_COMMANDS + 1)
            )
        )
        self.assertFalse(oversized.all_required_passed)
        self.assertEqual(len(oversized.failed), MAX_VERIFICATION_COMMANDS + 1)


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
