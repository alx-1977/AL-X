"""Law 0: the declared production-outcome manifest, and the mutations it catches.

`docs/LAW_ENFORCEMENT.md` requires that a Law 0 check be proved by restoring a
superseded entry point and showing the suite fails. A test that only reads
today's code proves nothing about tomorrow's, so every rule the gate enforces
is exercised here by mutating a copy of the real source tree the way a
regression actually would: a function copied into a second module, a superseded
identifier typed back in, an entry deleted.

This is the named-entry-point floor, not a semantic proof. The behavioural Law 0
tests — `test_single_production_path.py`, `test_web_single_path.py`,
`test_cognition_opportunity.py` and the rest — remain the evidence that the one
surviving path behaves correctly. Deleting them because this file exists would
itself be the mistake Law 0 describes.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from scripts.check_architecture import (
    Outcome,
    Rules,
    _definition_qualnames,
    check_source,
    load_rules,
)

import ast


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "architecture/boundaries.toml"


@contextmanager
def mutated_tree():
    """A writable copy of the real source tree, for one mutation."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "repository"
        (root).mkdir()
        shutil.copytree(REPOSITORY_ROOT / "src", root / "src")
        yield root


class ManifestIsEnforceableTests(unittest.TestCase):
    """The manifest must describe the code that actually exists."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(REPOSITORY_ROOT)

    def test_the_manifest_declares_outcomes(self) -> None:
        self.assertGreaterEqual(len(self.rules.outcomes), 6)

    def test_every_declared_entry_resolves_to_exactly_one_definition(self) -> None:
        """A manifest naming a function that does not exist enforces nothing."""
        source_root = REPOSITORY_ROOT / self.rules.source_root
        sites: dict[str, list[str]] = {}
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            relative = path.relative_to(source_root).as_posix()
            for qualified in _definition_qualnames(tree):
                sites.setdefault(qualified, []).append(relative)

        for outcome in self.rules.outcomes:
            with self.subTest(outcome=outcome.identifier):
                self.assertEqual(
                    sites.get(outcome.qualified_name, []),
                    [outcome.module],
                    f"{outcome.entry} must be defined once, in its declared module",
                )

    def test_outcome_identifiers_and_entries_are_unique(self) -> None:
        identifiers = [outcome.identifier for outcome in self.rules.outcomes]
        entries = [outcome.entry for outcome in self.rules.outcomes]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(entries), len(set(entries)))

    def test_the_live_repository_satisfies_the_manifest(self) -> None:
        """The repository itself must pass, not only synthesised samples."""
        messages = [
            violation.render()
            for violation in check_source(REPOSITORY_ROOT, self.rules)
            if "outcome" in violation.message or "superseded path" in violation.message
        ]
        self.assertEqual([], messages)

    def test_the_manifest_states_what_it_does_not_prove(self) -> None:
        """A gate read as more than it is becomes false confidence."""
        text = (REPOSITORY_ROOT / MANIFEST).read_text(encoding="utf-8")
        self.assertIn("WHAT THE GATE DOES NOT PROVE", text)
        self.assertIn("only declared outcomes are covered", text)


class MutationTests(unittest.TestCase):
    """Each failure mode, reintroduced the way a regression would."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rules = load_rules(REPOSITORY_ROOT)

    def violations(self, root: Path, rules: Rules | None = None) -> list[str]:
        return [
            violation.message for violation in check_source(root, rules or self.rules)
        ]

    def assert_caught(self, messages: list[str], expected: str) -> None:
        self.assertTrue(
            any(expected in message for message in messages),
            f"expected {expected!r} in {messages!r}",
        )

    def test_a_second_definition_of_an_entry_is_caught(self) -> None:
        """The Law 0 violation itself: a second live route to one outcome.

        `HttpWebFetchProvider.fetch` is copied into a second module, which is
        how a duplicate production path is actually born — by copy-paste, not
        by anyone declaring a competing route.
        """
        with mutated_tree() as root:
            second = root / "src/alx/providers/web_fetch_legacy.py"
            second.write_text(
                "class HttpWebFetchProvider:\n"
                "    def fetch(self, url, max_characters=0):\n"
                "        return None\n",
                encoding="utf-8",
            )
            messages = self.violations(root)
        self.assert_caught(messages, "must have one production path")
        self.assert_caught(messages, "web.public_page_is_retrieved")

    def test_restoring_a_superseded_identifier_is_caught(self) -> None:
        """"Not currently used" is not deleted."""
        with mutated_tree() as root:
            path = root / "src/alx/tools/xero.py"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\nEXECUTE_XERO_BILL = 'execute_xero_bill'\n",
                encoding="utf-8",
            )
            messages = self.violations(root)
        self.assert_caught(messages, "superseded path")
        self.assert_caught(messages, "execute_xero_bill")

    def test_restoring_a_superseded_ingress_method_is_caught(self) -> None:
        """The gateway method that stayed callable after being superseded."""
        with mutated_tree() as root:
            path = root / "src/alx/conversation/gateway.py"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\n\nclass Restored:\n"
                "    def receive_background_event(self, event):\n"
                "        return None\n",
                encoding="utf-8",
            )
            messages = self.violations(root)
        self.assert_caught(messages, "def receive_background_event")
        self.assert_caught(messages, "mail.observed_fact_becomes_core_turn")

    def test_deleting_a_declared_entry_is_caught(self) -> None:
        """A manifest that rots against the code must fail, not pass quietly."""
        with mutated_tree() as root:
            path = root / "src/alx/providers/repository_runtime.py"
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace("    def synchronize(self)", "    def sync_main(self)"),
                encoding="utf-8",
            )
            messages = self.violations(root)
        self.assert_caught(messages, "no such definition exists")
        self.assert_caught(messages, "repository.canonical_main_synchronizes")

    def test_moving_an_entry_out_of_its_declared_module_is_caught(self) -> None:
        """Renamed and left behind is not deleted; nor is moved and undeclared."""
        with mutated_tree() as root:
            source = root / "src/alx/providers/web_fetch.py"
            text = source.read_text(encoding="utf-8")
            source.write_text(
                text.replace("    def fetch(self, url", "    def retrieve(self, url"),
                encoding="utf-8",
            )
            (root / "src/alx/providers/web_fetch_moved.py").write_text(
                "class HttpWebFetchProvider:\n"
                "    def fetch(self, url, max_characters=0):\n"
                "        return None\n",
                encoding="utf-8",
            )
            messages = self.violations(root)
        self.assert_caught(messages, "is defined in")
        self.assert_caught(messages, "web.public_page_is_retrieved")

    def test_a_clean_tree_reports_no_outcome_violation(self) -> None:
        """The mutations must be what fails, not the copying."""
        with mutated_tree() as root:
            messages = [
                message
                for message in self.violations(root)
                if "outcome" in message or "superseded path" in message
            ]
        self.assertEqual([], messages)


class ManifestConfigurationTests(unittest.TestCase):
    """A manifest that cannot be enforced is a configuration error.

    A gate that silently skips an outcome is worse than no gate, because it
    reports success over a route nobody checked.
    """

    def load(self, body: str) -> Rules:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "architecture").mkdir()
            original = (REPOSITORY_ROOT / MANIFEST).read_text(encoding="utf-8")
            head = original.split("[[outcomes]]")[0]
            (root / MANIFEST).write_text(head + body, encoding="utf-8")
            return load_rules(root)

    def test_duplicate_outcome_identifiers_are_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.load(
                '[[outcomes]]\nid = "a"\ndescription = "d"\n'
                'entry = "core/loop.py::One"\n'
                '[[outcomes]]\nid = "a"\ndescription = "d"\n'
                'entry = "core/loop.py::Two"\n'
            )
        self.assertIn("duplicate outcome id", str(caught.exception))

    def test_two_outcomes_sharing_one_entry_are_refused(self) -> None:
        """One function cannot be the single path for two outcomes."""
        with self.assertRaises(ValueError) as caught:
            self.load(
                '[[outcomes]]\nid = "a"\ndescription = "d"\n'
                'entry = "core/loop.py::Shared"\n'
                '[[outcomes]]\nid = "b"\ndescription = "d"\n'
                'entry = "core/loop.py::Shared"\n'
            )
        self.assertIn("share the entry", str(caught.exception))

    def test_a_malformed_entry_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.load(
                '[[outcomes]]\nid = "a"\ndescription = "d"\nentry = "core/loop.py"\n'
            )
        self.assertIn("<path>::<qualified name>", str(caught.exception))

    def test_a_blank_field_is_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.load(
                '[[outcomes]]\nid = ""\ndescription = "d"\n'
                'entry = "core/loop.py::One"\n'
            )
        self.assertIn("non-blank", str(caught.exception))

    def test_an_unsupported_schema_version_is_refused(self) -> None:
        """A stale checkout must fail loudly rather than skip the new gate."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "architecture").mkdir()
            text = (REPOSITORY_ROOT / MANIFEST).read_text(encoding="utf-8")
            (root / MANIFEST).write_text(
                text.replace("schema_version = 2", "schema_version = 1", 1),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as caught:
                load_rules(root)
        self.assertIn("schema_version", str(caught.exception))


class QualifiedNameTests(unittest.TestCase):
    """Nested executors must be nameable exactly as Python names them."""

    def qualnames(self, source: str) -> dict:
        return _definition_qualnames(ast.parse(source))

    def test_a_method_is_named_by_its_class(self) -> None:
        found = self.qualnames("class A:\n    def b(self):\n        pass\n")
        self.assertIn("A.b", found)

    def test_a_closure_carries_the_locals_segment(self) -> None:
        """This is how a capability executor closed over its adapter is named."""
        found = self.qualnames("def outer():\n    def inner():\n        pass\n")
        self.assertIn("outer.<locals>.inner", found)

    def test_a_guarded_definition_still_exists(self) -> None:
        """A route inside `if` or `try` is still a live route."""
        found = self.qualnames("if True:\n    def guarded():\n        pass\n")
        self.assertIn("guarded", found)

    def test_repeated_definitions_are_each_recorded(self) -> None:
        found = self.qualnames("def a():\n    pass\n\ndef a():\n    pass\n")
        self.assertEqual(len(found["a"]), 2)


if __name__ == "__main__":
    unittest.main()
