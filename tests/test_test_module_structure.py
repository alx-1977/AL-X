"""A directly executable test module must not hide tests behind its guard.

`unittest.main()` under `if __name__ == "__main__":` collects the classes that
exist when it runs, which is the ones defined above it. Anything defined below
is never collected, and the run reports OK — so the file says it passed while
testing less than it contains.

This was found three times in two review rounds, and each time by a reviewer
rather than by the suite. Fixing the instances did not stop it: tests get
appended to the end of a file, and once the guard is at the end, appending puts
them after it. The affected files included the reviewer-identity and locale
suites, and at its widest 13 files were skipping tests — `test_mail_reply` ran
35 of 59 directly.

So the rule is checked rather than remembered. It is deliberately the one rule:
where the guard sits relative to the test definitions. Nothing here judges
naming, ordering, imports or style, because a general linter is a different
thing with a different cost, and this is a specific recurring defect with a
specific cost — a green run that proves less than it appears to.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent


def _guard(module: ast.Module) -> ast.If | None:
    """The module-level `if __name__ == "__main__":`, if it has one.

    Read from the tree rather than matched as text, so a mention inside a
    string or a comment is not mistaken for the guard itself.
    """
    found = None
    for node in module.body:
        if isinstance(node, ast.If) and "__name__" in ast.unparse(node.test):
            found = node
    return found


def _definitions_after(module: ast.Module, guard: ast.If) -> list[str]:
    """Test definitions the guard would skip, named for the failure message."""
    return [
        node.name
        for node in module.body
        if node.lineno > guard.lineno
        and isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]


class TestModuleStructureTests(unittest.TestCase):
    """Every test in a file must run when the file is run."""

    def test_no_test_definitions_follow_the_main_guard(self) -> None:
        missed: list[str] = []
        for path in sorted(TESTS_ROOT.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            try:
                module = ast.parse(source)
            except SyntaxError:  # pragma: no cover - a parse failure is its own test
                continue
            guard = _guard(module)
            if guard is None:
                # No guard means nothing claims to be directly executable, and
                # discovery collects the file either way.
                continue
            skipped = _definitions_after(module, guard)
            if skipped:
                missed.append(
                    f"{path.relative_to(TESTS_ROOT.parent)}: "
                    f"defined after the __main__ guard and never collected by "
                    f"direct execution: {', '.join(skipped)}"
                )
        self.assertEqual(
            missed,
            [],
            "\n".join(
                ["a test module hides tests behind its __main__ guard:", *missed]
            ),
        )


if __name__ == "__main__":
    unittest.main()
