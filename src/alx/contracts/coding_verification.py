"""Which checks one coding job must pass, derived from the files it changed.

The Coding Agent used to define verification as "pytest ran and pytest passed".
That is one verification class mistaken for the definition of verification, and
on 2026-09-21 it cost a documentation job its commit: a one-line `TODO.md` edit
found no targeted Python test, fell back to the whole repository suite, and the
suite exceeded the verification timeout. The job had nothing to prove by running
pytest at all, and it was refused a commit for failing a check that was never
relevant to it.

So the question this module answers is not "did the tests pass" but "which
checks does *this* change require, and did each of them pass". Tests remain one
class of check among several.

Everything here is a pure function of the job's final changed-path set. It reads
no task wording, no plan prose and no model output: a policy derived from what a
model said it would change is a policy a model can talk its way around, and the
only honest input is what is actually on disk after the local reviewer's
corrections have landed.

The path taxonomies are not restated here. The governance gate already declares
which documents are canonical and the architecture gate already declares which
tree it governs; both are read from their own declarations at policy time, so
this module cannot drift from them.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


# The always-required structural check. A diff carrying whitespace errors or a
# conflict marker is malformed whatever it touches, and the check costs
# milliseconds, so every job runs it regardless of what it changed.
DIFF_CHECK: tuple[str, ...] = ("git", "diff", "--check")

GOVERNANCE_GATE: tuple[str, ...] = ("python", "scripts/check_governance.py")
ARCHITECTURE_GATE: tuple[str, ...] = ("python", "scripts/check_architecture.py")

# The escalation target for a Python change with no safe targeted mapping. It
# is reached only from that case: a non-Python change never arrives here, which
# is the whole point of the correction this module exists for.
FULL_SUITE: tuple[str, ...] = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")

# Where the governance gate declares its canonical documents, and where the
# architecture gate declares the tree it governs. The source root below is only
# a fallback for a worktree whose manifest cannot be read.
_GOVERNANCE_SCRIPT = "scripts/check_governance.py"
_ARCHITECTURE_MANIFEST = "architecture/boundaries.toml"
_ARCHITECTURE_SOURCE_ROOT = "src/alx"


@dataclass(frozen=True, slots=True)
class VerificationCheck:
    """One required check: what it is, and how the job's run of it ended.

    `ran` and `passed` are filled in after execution. A check that was required
    and never ran is not a check that passed, which is the distinction the old
    boolean pair could not express.
    """

    name: str
    argv: tuple[str, ...]
    reason: str
    ran: bool = False
    passed: bool = False

    def as_values(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "reason": self.reason,
            "ran": self.ran,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """The checks a job is required to pass, in the order they should run."""

    checks: tuple[VerificationCheck, ...]

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        return tuple(check.argv for check in self.checks)

    def requires_tests(self) -> bool:
        return any(check.name.startswith("pytest") for check in self.checks)


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """What was required, what ran, how each ended, and the overall verdict.

    This is the durable record that replaces `tests_run and tests_passed` as the
    commit predicate. `all_required_passed` is false when any required check
    failed *or* did not run, so an absent check can never read as a satisfied
    one.
    """

    checks: tuple[VerificationCheck, ...]

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks)

    @property
    def ran(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if check.ran)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(
            check.name for check in self.checks if not (check.ran and check.passed)
        )

    @property
    def all_required_passed(self) -> bool:
        # An empty policy cannot occur — the diff check is unconditional — but
        # if it somehow did, nothing has been verified and nothing may be
        # committed on the strength of it.
        if not self.checks:
            return False
        return all(check.ran and check.passed for check in self.checks)

    def as_values(self) -> dict[str, object]:
        return {
            "required": list(self.required),
            "ran": list(self.ran),
            "failed": list(self.failed),
            "all_required_passed": self.all_required_passed,
            "checks": [check.as_values() for check in self.checks],
        }


def _governed_documents(root: Path | None) -> frozenset[str]:
    """The canonical documents, read from the governance gate's own list.

    Repository law already declares these in `scripts/check_governance.py`, and
    a second copy here would be a duplicated taxonomy that drifts the first time
    someone adds a document to one and not the other. The list is parsed out of
    the module's source rather than imported, because `src/alx` may not import
    from `scripts/`.
    """
    literal = _literal_tuple(root, _GOVERNANCE_SCRIPT, "REQUIRED_FILES")
    if literal is None:
        # The gate is unreadable. Everything under `governance/` is canonical by
        # name, so the governance gate is still required for those; refusing to
        # require it would be the unsafe direction.
        return frozenset()
    return frozenset(literal)


def _architecture_root(root: Path | None) -> str:
    """The tree the architecture gate governs, read from its own manifest."""
    if root is None:
        return _ARCHITECTURE_SOURCE_ROOT
    try:
        with (_as_path(root) / _ARCHITECTURE_MANIFEST).open("rb") as handle:
            declared = tomllib.load(handle).get("source_root")
    except (OSError, ValueError):
        return _ARCHITECTURE_SOURCE_ROOT
    if isinstance(declared, str) and declared.strip():
        return declared.strip().strip("/")
    return _ARCHITECTURE_SOURCE_ROOT


def _literal_tuple(
    root: Path | None, relative: str, name: str
) -> tuple[str, ...] | None:
    """Read one module-level tuple-of-strings literal without importing it."""
    if root is None:
        return None
    try:
        source = (_as_path(root) / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [
            item.id for item in node.targets if isinstance(item, ast.Name)
        ]
        if name not in targets:
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError):
            return None
        if isinstance(value, (list, tuple)) and all(
            isinstance(item, str) for item in value
        ):
            return tuple(value)
    return None


def _as_path(root: Path) -> Path:
    return Path(root)


def _normalise(changed_files: Iterable[str]) -> tuple[str, ...]:
    """Worktree-relative POSIX paths, deduplicated, order preserved."""
    names: list[str] = []
    for item in changed_files:
        if not isinstance(item, str):
            continue
        candidate = item.strip().replace("\\", "/").lstrip("./")
        if not candidate or candidate in names:
            continue
        names.append(candidate)
    return tuple(names)


def _targeted_tests(
    changed: tuple[str, ...],
    root: Path | None,
    exists: Callable[[Path | None, str], bool],
) -> tuple[str, ...]:
    """Test modules a changed path maps to mechanically, never by name guessing.

    Two mappings only, both structural: a changed test module is its own test,
    and a changed `src/alx/…` module has the conventional test paths derived
    from its own path. A candidate counts only when it exists on disk, so a
    module with no test yields nothing rather than a command that would error.
    """
    tests: list[str] = []
    for relative in changed:
        path = PurePosixPath(relative)
        if path.suffix != ".py":
            continue
        candidates: list[str] = []
        if path.name.startswith("test_"):
            candidates.append(path.as_posix())
        if path.parts[:2] == ("src", "alx"):
            module_parts = PurePosixPath(relative).with_suffix("").parts[2:]
            if module_parts:
                candidates.append(f"tests/test_{'_'.join(module_parts)}.py")
                candidates.append(f"tests/test_{path.stem}.py")
        for candidate in candidates:
            if candidate not in tests and exists(root, candidate):
                tests.append(candidate)
    return tuple(tests)


def _path_exists(root: Path | None, relative: str) -> bool:
    if root is None:
        return False
    try:
        return (_as_path(root) / relative).is_file()
    except OSError:
        return False


def required_verification(
    changed_files: Iterable[str], root: Path | None = None
) -> VerificationPolicy:
    """The checks this exact changed-file set requires. Deterministic and total.

    `root` is the assigned worktree. It is consulted only to read the two gate
    declarations and to confirm a candidate test file exists; the policy is
    otherwise a pure function of the paths.
    """
    changed = _normalise(changed_files)
    checks: list[VerificationCheck] = [
        VerificationCheck(
            "diff_check", DIFF_CHECK, "every change is checked for a malformed diff"
        )
    ]

    documents = _governed_documents(root)
    governance_paths = tuple(
        name
        for name in changed
        if name in documents or name.startswith("governance/")
    )
    if governance_paths:
        checks.append(
            VerificationCheck(
                "governance_gate",
                GOVERNANCE_GATE,
                "changed a canonical governance document: "
                + ", ".join(governance_paths[:8]),
            )
        )

    source_root = _architecture_root(root)
    architecture_paths = tuple(
        name
        for name in changed
        if name == _ARCHITECTURE_MANIFEST
        or name.startswith(f"{source_root}/")
    )
    if architecture_paths:
        checks.append(
            VerificationCheck(
                "architecture_gate",
                ARCHITECTURE_GATE,
                "changed a path the architecture gate governs: "
                + ", ".join(architecture_paths[:8]),
            )
        )

    python_paths = tuple(name for name in changed if name.endswith(".py"))
    if python_paths:
        targeted = _targeted_tests(changed, root, _path_exists)
        if targeted:
            checks.append(
                VerificationCheck(
                    "pytest_targeted",
                    ("python", "-m", "pytest", "-q", *targeted),
                    "changed Python modules map to these tests: "
                    + ", ".join(targeted[:8]),
                )
            )
        else:
            # A Python change with nothing to target is the one case that still
            # earns the whole suite: the change is executable, its blast radius
            # is unknown, and repository policy requires runtime verification of
            # executable changes. A *non*-Python change never reaches this line.
            checks.append(
                VerificationCheck(
                    "pytest_full",
                    FULL_SUITE,
                    "changed Python modules with no safe targeted test mapping: "
                    + ", ".join(python_paths[:8]),
                )
            )

    return VerificationPolicy(tuple(checks))


__all__ = [
    "ARCHITECTURE_GATE",
    "DIFF_CHECK",
    "FULL_SUITE",
    "GOVERNANCE_GATE",
    "VerificationCheck",
    "VerificationEvidence",
    "VerificationPolicy",
    "required_verification",
]
