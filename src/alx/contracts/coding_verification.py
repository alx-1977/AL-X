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


# The always-required structural checks. Malformed content is malformed whatever
# it touches, and both cost milliseconds, so every job runs them regardless of
# what it changed.
#
# Two checks rather than one, because `git diff --check` inspects *tracked*
# changes only. A file the job newly created is untracked at verification time —
# staging happens later, inside the commit — so a new file carrying leftover
# conflict markers passed the diff check and was committed. Found in review on
# PR #54, and reproduced: `git diff --check` exits 0 on an untracked file whose
# content is plainly broken.
#
# `content_check` closes that by reading the job's own final files directly. It
# is deterministic with one correct answer, so under Law 2 it is code rather
# than a command, and it needs no addition to the command allowlist.
DIFF_CHECK: tuple[str, ...] = ("git", "diff", "--check")

# Markers git's own `--check` looks for. Seven characters at the start of a
# line is the conflict-marker shape git uses; it is matched here the same way.
_CONFLICT_MARKERS = ("<" * 7, "=" * 7, ">" * 7, "|" * 7)

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
_ARCHITECTURE_SCRIPT = "scripts/check_architecture.py"
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
    # "command" runs through the allowlisted executor. "content" is performed
    # in process by `content_violations` below, because reading the job's own
    # files needs no subprocess and no addition to the command allowlist.
    kind: str = "command"
    # What a failed content check found, so the evidence says why rather than
    # leaving Core to infer it. Bounded: the first few findings are enough to
    # act on, and this is durable state.
    findings: tuple[str, ...] = ()

    def as_values(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "reason": self.reason,
            "ran": self.ran,
            "passed": self.passed,
            "kind": self.kind,
            "findings": list(self.findings),
        }


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """The checks a job is required to pass, in the order they should run."""

    checks: tuple[VerificationCheck, ...]

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        """The argv forms only. A content check has none and is excluded."""
        return tuple(
            check.argv for check in self.checks if check.kind == "command"
        )

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
    """Worktree-relative POSIX paths, deduplicated, order preserved.

    `./` prefixes and leading slashes are removed one segment at a time. This
    used to be `lstrip("./")`, which strips a *set of characters* rather than a
    prefix: `.github/workflows/law-gates.yml` came back as
    `github/workflows/…`, so a job changing the CI workflow or the pull-request
    template — both canonical documents — matched nothing and skipped the
    governance gate that exists to protect them. Found in review on PR #54.
    """
    names: list[str] = []
    for item in changed_files:
        if not isinstance(item, str):
            continue
        # Only the relative-path prefixes are removed. `strip()` here changed
        # the path itself: a file genuinely named `broken.py ` became
        # `broken.py`, so the content check inspected a path that did not
        # exist and reported nothing, while `git diff --check` could not see
        # the untracked file either. Malformed content reached the commit
        # unexamined. Git permits leading and trailing spaces in a path.
        # Found in review on PR #54.
        # A backslash is not rewritten: on POSIX `broken\\file.py` is one
        # valid filename, and turning it into `broken/file.py` had the content
        # check skip the real file while `git diff --check` could not see it
        # either. `Path` already treats a backslash as a separator on Windows,
        # so preserving the exact value is right on both. Found in review on
        # PR #54.
        candidate = item
        while candidate.startswith("./") or candidate.startswith("/"):
            candidate = candidate[2:] if candidate.startswith("./") else candidate[1:]
        if not candidate or candidate in names:
            continue
        names.append(candidate)
    return tuple(names)


def _targeted_tests(
    changed: tuple[str, ...],
    root: Path | None,
    exists: Callable[[Path | None, str], bool],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Test modules a changed path maps to mechanically, never by name guessing.

    Returns the mapped tests and the changed Python paths that mapped to
    nothing. Both halves matter: the caller may use targeted tests only when
    *every* changed Python path is covered. Combining the mappings and asking
    only whether any existed let one mapped file speak for an unmapped one, so
    a job changing a covered module and an uncovered one ran the covered
    module's test and called the pair verified. Found in review on PR #54.

    Two mappings only, both structural: a changed test module is its own test,
    and a changed `src/alx/…` module has the conventional test paths derived
    from its own path. A candidate counts only when it exists on disk, so a
    module with no test yields nothing rather than a command that would error.
    """
    tests: list[str] = []
    unmapped: list[str] = []
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
        found = False
        for candidate in candidates:
            if exists(root, candidate):
                found = True
                if candidate not in tests:
                    tests.append(candidate)
        if not found:
            unmapped.append(relative)
    return tuple(tests), tuple(unmapped)


def _path_exists(root: Path | None, relative: str) -> bool:
    if root is None:
        return False
    try:
        return (_as_path(root) / relative).is_file()
    except OSError:
        return False


MAX_CONTENT_FINDINGS = 20
# A job's own source file. Larger than any file the repository holds, and a
# bound rather than an unbounded read of whatever the job produced.
#
# Exceeding it is a finding, not a truncation. Checking only a prefix would let
# a conflict marker past the limit through unexamined while the check reported
# success, which is the failure mode this whole module exists to remove: an
# unverified thing must never read as a verified one. A file this large in a
# bounded repair is itself worth stopping for.
MAX_CONTENT_CHARACTERS = 2_000_000


def content_violations(
    changed_files: Iterable[str], root: Path | None
) -> tuple[str, ...]:
    """Leftover conflict markers and whitespace errors in the job's own files.

    What `git diff --check` reports, computed over the job's final files rather
    than over the tracked diff, so a newly created file is inspected too. Only
    the job's own paths are read: this is a check on the change, not an audit of
    the worktree.

    A file that cannot be read as text is not a finding. Binary content and an
    unreadable path are ordinary, and inventing a violation from them would fail
    honest jobs; the concern here is malformed *text* the job wrote.
    """
    findings: list[str] = []
    if root is None:
        return ()
    for relative in _normalise(changed_files):
        if len(findings) >= MAX_CONTENT_FINDINGS:
            break
        try:
            target = _as_path(root) / relative
            # A symlink is not read. `is_file()` follows one, so a changed
            # symlink inside the worktree would have this read a regular file
            # outside it — whitespace in somebody else's file failing the job's
            # required check, and a path this module has no business reading.
            # Containment is enforced everywhere else in the coding package;
            # this was the one place that bypassed it. Found in review on
            # PR #54. Not a finding either: a symlink the job legitimately
            # created is not malformed text.
            if target.is_symlink() or not target.is_file():
                continue
            resolved = target.resolve()
            if not resolved.is_relative_to(_as_path(root).resolve()):
                continue
            # Read one character past the bound rather than the whole file and
            # slice afterwards: `read_text()` loads everything before any slice
            # applies, so the slice bounded the string and not the read.
            with target.open("r", encoding="utf-8") as handle:
                text = handle.read(MAX_CONTENT_CHARACTERS + 1)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if len(text) > MAX_CONTENT_CHARACTERS:
            # Fails closed: the rest was never examined, so the file cannot be
            # reported clean on the strength of its prefix.
            findings.append(
                f"{relative}: larger than {MAX_CONTENT_CHARACTERS} characters "
                "and was not checked"
            )
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if len(findings) >= MAX_CONTENT_FINDINGS:
                break
            if line.startswith(_CONFLICT_MARKERS):
                findings.append(
                    f"{relative}:{number}: leftover conflict marker"
                )
            elif line.rstrip("\r\n") != line.rstrip():
                findings.append(f"{relative}:{number}: trailing whitespace")
            elif " \t" in line[: len(line) - len(line.lstrip())]:
                # Git's own `--check` reports this by default. Matching what it
                # reports is the point: the two checks answer one question over
                # different halves of the change, so a rule git enforces on a
                # tracked file must not go unenforced on a new one.
                findings.append(f"{relative}:{number}: space before tab in indent")
    return tuple(findings)


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
        ),
        # The same question asked of the job's own files rather than of the
        # tracked diff, so a file the job created is inspected too.
        VerificationCheck(
            "content_check",
            (),
            "every changed file is checked for conflict markers and "
            "whitespace errors, including files the job newly created",
            kind="content",
        ),
    ]

    # A change to a gate script runs that gate. Nothing else selects it: the
    # scripts are not canonical documents and do not sit under the architecture
    # source root, so editing one otherwise escalated to the full suite, which
    # does not run either gate and so never exercised the edit. Found in review
    # on PR #54.
    gate_scripts = {
        _GOVERNANCE_SCRIPT: ("governance_gate", GOVERNANCE_GATE),
        _ARCHITECTURE_SCRIPT: ("architecture_gate", ARCHITECTURE_GATE),
    }

    documents = _governed_documents(root)
    governance_paths = tuple(
        name
        for name in changed
        if name in documents
        or name.startswith("governance/")
        or name == _GOVERNANCE_SCRIPT
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
        or name == _ARCHITECTURE_SCRIPT
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
        targeted, unmapped = _targeted_tests(changed, root, _path_exists)
        # Every changed Python path must be covered, not merely one of them.
        if targeted and not unmapped:
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
                    + ", ".join((unmapped or python_paths)[:8]),
                )
            )

    return VerificationPolicy(tuple(checks))


__all__ = [
    "ARCHITECTURE_GATE",
    "DIFF_CHECK",
    "FULL_SUITE",
    "GOVERNANCE_GATE",
    "MAX_CONTENT_FINDINGS",
    "content_violations",
    "VerificationCheck",
    "VerificationEvidence",
    "VerificationPolicy",
    "required_verification",
]
