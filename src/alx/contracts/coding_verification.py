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
import re
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

# The broader suite. `required_verification` does not select it. A missing
# test-file mapping is reported, and the architecture or governance gate still
# runs when the changed path is one of theirs. The command stays so a separate
# broader run can use the same argv the executor already knows.
FULL_SUITE: tuple[str, ...] = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider")

# Modules whose tests are not named after the module. Each target is a file
# that already imports or executes that module. A listed file that is not on
# disk is ignored, so the map cannot schedule a command that would error.
_EXPLICIT_TESTS: dict[str, tuple[str, ...]] = {
    # Coding.
    "src/alx/contracts/coding.py": (
        "tests/test_coding_contract_bounds.py",
        "tests/test_coding_agent.py",
        "tests/test_coding_verification.py",
        "tests/test_coding_checkout.py",
        "tests/test_coding_retry_fuse.py",
    ),
    "src/alx/contracts/coding_verification.py": (
        "tests/test_coding_agent.py",
    ),
    "src/alx/providers/coding_agent.py": (
        "tests/test_coding_recovery.py",
        "tests/test_coding_git_outcome.py",
    ),
    "src/alx/providers/coding_containment.py": (
        "tests/test_coding_agent.py",
    ),
    "src/alx/providers/coding_git.py": (
        "tests/test_coding_git_workspace.py",
        "tests/test_coding_git_outcome.py",
        "tests/test_coding_checkout.py",
        "tests/test_coding_branch_continuation.py",
    ),
    "src/alx/providers/coding_process.py": (
        "tests/test_coding_verification.py",
        "tests/test_coding_agent.py",
        "tests/test_coding_recovery.py",
    ),
    "src/alx/providers/coding_session.py": (
        "tests/test_coding_session_boundary.py",
        "tests/test_coding_agent.py",
    ),
    "src/alx/providers/coding_subscription_session.py": (
        "tests/test_coding_session_boundary.py",
    ),
    "src/alx/providers/coding_workspace.py": (
        "tests/test_coding_checkout.py",
        "tests/test_coding_contract_bounds.py",
        "tests/test_coding_agent.py",
    ),
    "src/alx/tools/coding.py": (
        "tests/test_coding_agent.py",
        "tests/test_coding_contract_bounds.py",
        "tests/test_coding_retry_fuse.py",
    ),
    "src/alx/bootstrap/coding.py": (
        "tests/test_coding_agent.py",
        "tests/test_coding_recovery.py",
        "tests/test_runtime_startup_smoke.py",
    ),
    # Providers.
    "src/alx/providers/cartesia.py": (
        "tests/test_provider_adapters.py",
        "tests/test_provider_failure_sanitisation.py",
    ),
    "src/alx/providers/dhl.py": (
        "tests/test_dhl_reconciliation.py",
    ),
    "src/alx/providers/elevenlabs.py": (
        "tests/test_provider_adapters.py",
    ),
    "src/alx/providers/errors.py": (
        "tests/test_provider_failure_sanitisation.py",
        "tests/test_provider_adapters.py",
    ),
    "src/alx/providers/gated_transcription.py": (
        "tests/test_speech_transmission_gate.py",
    ),
    "src/alx/providers/github_merge.py": (
        "tests/test_merge_authority.py",
    ),
    "src/alx/providers/github_review.py": (
        "tests/test_read_external_review.py",
        "tests/test_review_request.py",
        "tests/test_external_review_handoff.py",
    ),
    "src/alx/providers/icloud_mail.py": (
        "tests/test_mail_poll_lifetime.py",
        "tests/test_mail_store_transitions.py",
        "tests/test_mail_reliability.py",
        "tests/test_mail_reconciliation.py",
        "tests/test_mail_attachments.py",
        "tests/test_mail_review_fixes.py",
    ),
    "src/alx/providers/icloud_mail_send.py": (
        "tests/test_mail_reply.py",
    ),
    "src/alx/providers/mail_poller.py": (
        "tests/test_mail_poll_lifetime.py",
    ),
    "src/alx/providers/openai.py": (
        "tests/test_provider_adapters.py",
        "tests/test_prompt_cache_prefix.py",
    ),
    "src/alx/providers/sandbox_macos/__init__.py": (
        "tests/test_sandbox_macos_backend.py",
        "tests/test_sandbox_bounds.py",
        "tests/test_sandbox_isolation.py",
        "tests/test_sandbox_capability.py",
    ),
    "src/alx/providers/sandbox_macos/launcher.py": (
        "tests/test_sandbox_macos_backend.py",
    ),
    "src/alx/providers/sandbox_macos/runner.py": (
        "tests/test_sandbox_macos_backend.py",
        "tests/test_sandbox_bounds.py",
        "tests/test_sandbox_isolation.py",
        "tests/test_sandbox_capability.py",
    ),
    "src/alx/providers/sandbox_retention.py": (
        "tests/test_sandbox_lifecycle.py",
    ),
    "src/alx/providers/sandbox_workspace.py": (
        "tests/test_sandbox_bounds.py",
        "tests/test_sandbox_lifecycle.py",
        "tests/test_sandbox_capability.py",
        "tests/test_sandbox_macos_backend.py",
    ),
    "src/alx/providers/speech_activity.py": (
        "tests/test_speech_transmission_gate.py",
    ),
    "src/alx/providers/web_fetch.py": (
        "tests/test_web_fetch_bounds.py",
        "tests/test_web_composed_retrieval.py",
    ),
    "src/alx/providers/web_search.py": (
        "tests/test_web_search_capability.py",
    ),
    "src/alx/providers/web_url.py": (
        "tests/test_web_url_boundary.py",
        "tests/test_web_search_capability.py",
        "tests/test_web_composed_retrieval.py",
    ),
    "src/alx/providers/xai.py": (
        "tests/test_provider_adapters.py",
        "tests/test_runtime_startup_smoke.py",
    ),
    "src/alx/providers/xero.py": (
        "tests/test_xero_bill_primitives.py",
    ),
    # Tools.
    "src/alx/tools/dhl.py": (
        "tests/test_dhl_reconciliation.py",
    ),
    "src/alx/tools/mail.py": (
        "tests/test_mail_reply.py",
    ),
    "src/alx/tools/repository.py": (
        "tests/test_merge_authority.py",
    ),
    "src/alx/tools/repository_authority.py": (
        "tests/test_repository_authority.py",
    ),
    "src/alx/tools/review.py": (
        "tests/test_review_request.py",
        "tests/test_read_external_review.py",
    ),
    "src/alx/tools/review_content.py": (
        "tests/test_read_external_review.py",
        "tests/test_review_request.py",
    ),
    "src/alx/tools/sandbox.py": (
        "tests/test_sandbox_capability.py",
    ),
    "src/alx/tools/web.py": (
        "tests/test_web_capabilities.py",
        "tests/test_web_search_capability.py",
        "tests/test_web_untrusted_content.py",
        "tests/test_web_single_path.py",
        "tests/test_web_conversation.py",
        "tests/test_web_composed_retrieval.py",
    ),
    "src/alx/tools/xero.py": (
        "tests/test_xero_bill_primitives.py",
        "tests/test_invoice_capture.py",
    ),
    # Safety.
    "src/alx/safety/gate.py": (
        "tests/test_capability_safety.py",
    ),
    "src/alx/safety/retention.py": (
        "tests/test_retention_provenance.py",
        "tests/test_retention_wiring.py",
    ),
    # Bootstrap and runtime composition.
    "src/alx/bootstrap/autonomous.py": (
        "tests/test_autonomous_integration.py",
        "tests/test_autonomous_recovery.py",
    ),
    "src/alx/bootstrap/dhl.py": (
        "tests/test_dhl_reconciliation.py",
        "tests/test_single_production_path.py",
    ),
    "src/alx/bootstrap/live_voice.py": (
        "tests/test_runtime_startup_smoke.py",
        "tests/test_runtime_launch.py",
    ),
    "src/alx/bootstrap/mail.py": (
        "tests/test_mail_vertical_slice.py",
        "tests/test_mail_reply.py",
    ),
    "src/alx/bootstrap/notebook.py": (
        "tests/test_notebook_runtime.py",
    ),
    "src/alx/bootstrap/providers.py": (
        "tests/test_runtime_config.py",
        "tests/test_provider_adapters.py",
    ),
    "src/alx/bootstrap/reasoning.py": (
        "tests/test_origin_selected_core.py",
        "tests/test_model_reasoner.py",
    ),
    "src/alx/bootstrap/repository.py": (
        "tests/test_merge_authority.py",
    ),
    "src/alx/bootstrap/repository_authority.py": (
        "tests/test_repository_authority.py",
    ),
    "src/alx/bootstrap/research.py": (
        "tests/test_first_research_activation.py",
    ),
    "src/alx/bootstrap/review.py": (
        "tests/test_review_request.py",
    ),
    "src/alx/bootstrap/sandbox.py": (
        "tests/test_sandbox_capability.py",
    ),
    "src/alx/bootstrap/tasks.py": (
        "tests/test_task_status.py",
    ),
    "src/alx/bootstrap/web.py": (
        "tests/test_web_single_path.py",
        "tests/test_web_capabilities.py",
    ),
    "src/alx/bootstrap/xero.py": (
        "tests/test_xero_bill_primitives.py",
        "tests/test_invoice_capture.py",
        "tests/test_single_production_path.py",
    ),
}

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
    baseline: str = ""

    def as_values(self) -> dict[str, object]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "reason": self.reason,
            "ran": self.ran,
            "passed": self.passed,
            "kind": self.kind,
            "findings": list(self.findings),
            "baseline": self.baseline,
        }


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """The checks a job is required to pass, in the order they should run."""

    checks: tuple[VerificationCheck, ...]

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        """The argv forms only. Content and report checks have none."""
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


def pytest_failure_signature(
    output: str, root: Path
) -> tuple[tuple[str, str], ...] | None:
    """Complete pytest failure bodies, with only the checkout location erased.

    Pytest's progress, elapsed time and warning count are not failure meaning.
    Every failure body and its short-summary identity must agree. An unfamiliar
    or incomplete report cannot establish equivalence.
    """
    normalized_output = output.replace(str(root.resolve()), "<checkout>")
    normalized_output = normalized_output.replace(str(root), "<checkout>")
    lines = normalized_output.splitlines()
    starts = [index for index, line in enumerate(lines)
              if line.startswith("=") and line.endswith("=") and
              (" FAILURES " in line or " ERRORS " in line)]
    summaries = [index for index, line in enumerate(lines)
                 if " short test summary info " in line and line.startswith("=")]
    if not starts or len(summaries) != 1 or starts[0] >= summaries[0]:
        return None
    body = lines[starts[0]:summaries[0]]
    # Warnings may follow the failures; they are not part of the verdict.
    for index, line in enumerate(body):
        if " warnings summary " in line and line.startswith("="):
            body = body[:index]
            break
    short = []
    for line in lines[summaries[0] + 1:]:
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            short.append(line)
        elif line.startswith("="):
            break
    if not short or len(body) == 0:
        return None
    # Section headings and details stay verbatim. Only decorative line widths
    # differ when terminal settings change, which the environment key covers.
    headings = [index for index, line in enumerate(body)
                if re.fullmatch(r"_+ .+? _+", line)]
    if len(headings) != len(short):
        return None
    signatures = []
    for position, start in enumerate(headings):
        end = headings[position + 1] if position + 1 < len(headings) else len(body)
        heading = re.fullmatch(r"_+ (.+?) _+", body[start])
        if heading is None:
            return None
        title = heading.group(1)
        is_error = title.startswith("ERROR at ")
        if is_error:
            title = re.sub(r"^ERROR at (?:setup|teardown|call) of ", "", title)
        status, _, node = short[position].partition(" ")
        path, separator, node = node.partition("::")
        if not path or not separator:
            return None
        depth = 0
        for offset, character in enumerate(node):
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0 and node.startswith(" - ", offset):
                node = node[:offset]
                break
        if depth != 0:
            return None
        parts = node.split("::")
        identity = ".".join(parts)
        if (status == "ERROR") != is_error or not identity or \
                title.replace("::", ".") != identity:
            return None
        section = "\n".join(line.rstrip() for line in body[start:end] if line.strip())
        signatures.append((short[position], section))
    return tuple(signatures)


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
    """Test modules a changed path maps to, and the Python paths that map to none.

    A changed test module is its own test. A changed `src/alx/…` module also
    tries the conventional names derived from its path, then the explicit
    table for the areas whose tests are not named after the module. A
    candidate counts only when it exists on disk.

    Mapped and unmapped are both returned. A mapped neighbour does not cover
    an unmapped file, and an unmapped file does not discard the mapped tests.
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
        # A test module sitting next to the file it covers, when that file
        # exists. This is how a small fixture names its own test, and it is
        # the same stem rule the source tree already uses under tests/.
        parent = path.parent.as_posix()
        sibling = (
            f"test_{path.stem}.py"
            if parent in ("", ".")
            else f"{parent}/test_{path.stem}.py"
        )
        if sibling not in candidates:
            candidates.append(sibling)
        if path.parts[:2] == ("src", "alx"):
            module_parts = PurePosixPath(relative).with_suffix("").parts[2:]
            if module_parts:
                candidates.append(f"tests/test_{'_'.join(module_parts)}.py")
                candidates.append(f"tests/test_{path.stem}.py")
        for candidate in _EXPLICIT_TESTS.get(relative, ()):
            if candidate not in candidates:
                candidates.append(candidate)
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
        # Mapped files run their own tests. Unmapped files are named in the
        # evidence and are not a reason to schedule the repository suite.
        # The gates above still run for the paths they govern.
        if targeted:
            checks.append(
                VerificationCheck(
                    "pytest_targeted",
                    ("python", "-m", "pytest", "-q", *targeted),
                    "changed Python modules map to these tests: "
                    + ", ".join(targeted[:8]),
                )
            )
        if unmapped:
            shown = unmapped[:8]
            reason = (
                "changed Python modules have no targeted test mapping "
                "and were not sent to the full suite: " + ", ".join(shown)
            )
            if len(unmapped) > len(shown):
                reason += f", and {len(unmapped) - len(shown)} more"
            checks.append(
                VerificationCheck(
                    "unmapped_python",
                    (),
                    reason,
                    kind="report",
                    ran=True,
                    passed=True,
                    findings=tuple(unmapped[:MAX_CONTENT_FINDINGS]),
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
