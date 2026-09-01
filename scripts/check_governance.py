#!/usr/bin/env python3
"""Fail when AL/X's canonical governance layer is missing or silently changed."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


REQUIRED_FILES = (
    ".greptile/config.json",
    ".greptile/files.json",
    ".greptile/rules.md",
    ".github/CODEOWNERS",
    ".github/copilot-instructions.md",
    ".github/pull_request_template.md",
    ".github/workflows/law-gates.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "IDENTITY_AND_MEMORY.md",
    "LAWS_OF_ALX.md",
    "architecture/boundaries.toml",
    "docs/ARCHITECTURE_BLUEPRINT.md",
    "docs/FOUNDATION_PROOF.md",
    "docs/LAW_ENFORCEMENT.md",
    "docs/TECHNICAL_PLAN.md",
    "governance/DECISIONS.md",
    "governance/EXCEPTIONS.md",
    "governance/GREPTILE.sha256",
    "governance/IDENTITY_AND_MEMORY.sha256",
    "governance/LAWS_OF_ALX.sha256",
)

GREPTILE_RULE_IDS = {
    "alx-one-production-path",
    "alx-single-reasoning-authority",
    "alx-dynamic-reasoning",
    "alx-no-unapproved-hardcoding",
    "alx-one-conversation-path",
    "alx-primitive-tool-boundary",
    "alx-durable-goal-loop",
    "alx-explicit-exceptions-only",
    "alx-governed-capability-invention",
}

GREPTILE_CONTEXT_FILES = {
    "LAWS_OF_ALX.md",
    "docs/LAW_ENFORCEMENT.md",
    "docs/ARCHITECTURE_BLUEPRINT.md",
    "docs/FOUNDATION_PROOF.md",
    "architecture/boundaries.toml",
    "governance/EXCEPTIONS.md",
}


def _read(root: Path, relative_path: str, violations: list[str]) -> str:
    path = root / relative_path
    if not path.is_file():
        violations.append(f"missing required file: {relative_path}")
        return ""
    return path.read_text(encoding="utf-8")


def _require_markers(
    text: str, relative_path: str, markers: tuple[str, ...], violations: list[str]
) -> None:
    for marker in markers:
        if marker not in text:
            violations.append(f"{relative_path}: missing required marker: {marker}")


def _check_law_checksum(root: Path, violations: list[str]) -> None:
    law_path = root / "LAWS_OF_ALX.md"
    checksum_path = root / "governance/LAWS_OF_ALX.sha256"
    if not law_path.is_file() or not checksum_path.is_file():
        return

    checksum_line = checksum_path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"([0-9a-f]{64})  LAWS_OF_ALX\.md", checksum_line)
    if not match:
        violations.append("governance/LAWS_OF_ALX.sha256: invalid checksum record")
        return

    actual = hashlib.sha256(law_path.read_bytes()).hexdigest()
    if actual != match.group(1):
        violations.append(
            "LAWS_OF_ALX.md differs from its approved checksum; an explicit owner-approved amendment and checksum update are required"
        )


def _check_identity_checksum(root: Path, violations: list[str]) -> None:
    identity_path = root / "IDENTITY_AND_MEMORY.md"
    checksum_path = root / "governance/IDENTITY_AND_MEMORY.sha256"
    if not identity_path.is_file() or not checksum_path.is_file():
        return

    checksum_line = checksum_path.read_text(encoding="utf-8").strip()
    match = re.fullmatch(
        r"([0-9a-f]{64})  IDENTITY_AND_MEMORY\.md", checksum_line
    )
    if not match:
        violations.append(
            "governance/IDENTITY_AND_MEMORY.sha256: invalid checksum record"
        )
        return

    actual = hashlib.sha256(identity_path.read_bytes()).hexdigest()
    if actual != match.group(1):
        violations.append(
            "IDENTITY_AND_MEMORY.md differs from its approved checksum; explicit owner approval and checksum update are required"
        )


def _check_exceptions(text: str, violations: list[str]) -> None:
    headings = re.findall(r"^## (EX-[A-Z0-9-]+)\b", text, flags=re.MULTILINE)
    empty_marker = "No exceptions have been approved."
    if not headings:
        if empty_marker not in text:
            violations.append(
                "governance/EXCEPTIONS.md: must state that none are approved or contain structured EX-* records"
            )
        return

    if empty_marker in text:
        violations.append(
            "governance/EXCEPTIONS.md: cannot claim no exceptions while EX-* records exist"
        )

    required_fields = (
        "Law:",
        "Scope:",
        "Necessity:",
        "Alternatives:",
        "Risks and safeguards:",
        "Approved by Friedl:",
        "Approval date:",
        "Expiry or review condition:",
    )
    sections = re.split(r"^## (?=EX-[A-Z0-9-]+\b)", text, flags=re.MULTILINE)[1:]
    for section in sections:
        identifier = section.splitlines()[0].strip()
        for field in required_fields:
            if field not in section:
                violations.append(
                    f"governance/EXCEPTIONS.md: {identifier} missing field: {field}"
                )


def _check_env_is_ignored(root: Path, violations: list[str]) -> None:
    gitignore = _read(root, ".gitignore", violations)
    rules = {
        line.strip()
        for line in gitignore.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if ".env" not in rules:
        violations.append(".gitignore: .env must be ignored explicitly")

    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", ".env"],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode == 0:
        violations.append(".env is tracked by git and must not be committed")


def _load_json(path: Path, relative_path: str, violations: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        violations.append(f"{relative_path}: invalid or unreadable JSON: {error}")
        return {}
    if not isinstance(value, dict):
        violations.append(f"{relative_path}: top-level value must be an object")
        return {}
    return value


def _check_greptile(root: Path, violations: list[str]) -> None:
    config_path = root / ".greptile/config.json"
    files_path = root / ".greptile/files.json"
    rules_path = root / ".greptile/rules.md"
    if not all(path.is_file() for path in (config_path, files_path, rules_path)):
        return

    config = _load_json(config_path, ".greptile/config.json", violations)
    if config.get("strictness") != 1:
        violations.append(".greptile/config.json: strictness must remain 1")
    if config.get("skipReview") != "AUTOMATIC":
        violations.append(
            ".greptile/config.json: automatic reviews must remain disabled"
        )
    if config.get("triggerOnUpdates") is not False:
        violations.append(
            ".greptile/config.json: automatic commit re-reviews must remain disabled"
        )
    if config.get("triggerOnDrafts") is not False:
        violations.append(
            ".greptile/config.json: automatic draft reviews must remain disabled"
        )
    if config.get("statusCheck") is not True:
        violations.append(
            ".greptile/config.json: manual reviews must publish the required status check"
        )
    instructions = config.get("instructions", "")
    for marker in (
        "independent constitutional reviewer",
        "AL/X LAW VIOLATION — BLOCKING",
        "Never invent, infer, or approve an exception",
    ):
        if marker not in instructions:
            violations.append(
                f".greptile/config.json: instructions missing required marker: {marker}"
            )

    rules = config.get("rules", [])
    if not isinstance(rules, list):
        violations.append(".greptile/config.json: rules must be an array")
        rules = []
    rules_by_id = {
        rule.get("id"): rule
        for rule in rules
        if isinstance(rule, dict) and isinstance(rule.get("id"), str)
    }
    missing_rules = GREPTILE_RULE_IDS - rules_by_id.keys()
    if missing_rules:
        violations.append(
            ".greptile/config.json: missing constitutional rules: "
            + ", ".join(sorted(missing_rules))
        )
    for rule_id in GREPTILE_RULE_IDS & rules_by_id.keys():
        rule = rules_by_id[rule_id]
        if rule.get("severity") != "high" or rule.get("enabled") is False:
            violations.append(
                f".greptile/config.json: {rule_id} must remain enabled at high severity"
            )
    one_path = rules_by_id.get("alx-one-production-path", {})
    one_path_text = one_path.get("rule", "") if isinstance(one_path, dict) else ""
    for marker in (
        "exactly one authoritative implementation path",
        "must be deleted",
        "Tests must prove the competing path is absent",
    ):
        if marker not in one_path_text:
            violations.append(
                ".greptile/config.json: alx-one-production-path missing required "
                f"marker: {marker}"
            )

    context = _load_json(files_path, ".greptile/files.json", violations)
    context_entries = context.get("files", [])
    context_paths = {
        entry.get("path")
        for entry in context_entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    missing_context = GREPTILE_CONTEXT_FILES - context_paths
    if missing_context:
        violations.append(
            ".greptile/files.json: missing canonical context: "
            + ", ".join(sorted(missing_context))
        )

    rules_text = rules_path.read_text(encoding="utf-8")
    _require_markers(
        rules_text,
        ".greptile/rules.md",
        (
            "`LAWS_OF_ALX.md` is the sole canonical law text.",
            "exactly one authoritative implementation path",
            "Require deletion, not concealment or redirection.",
            "Who is deciding meaning",
            "AL/X LAW VIOLATION — BLOCKING",
            "Do not create or infer exceptions.",
        ),
        violations,
    )

    checksum_path = root / "governance/GREPTILE.sha256"
    if not checksum_path.is_file():
        return
    expected_paths = {
        ".greptile/config.json",
        ".greptile/files.json",
        ".greptile/rules.md",
    }
    recorded: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (\.greptile/(?:config\.json|files\.json|rules\.md))", line)
        if not match:
            violations.append("governance/GREPTILE.sha256: invalid checksum record")
            continue
        recorded[match.group(2)] = match.group(1)
    if recorded.keys() != expected_paths:
        violations.append(
            "governance/GREPTILE.sha256: expected checksums for all three Greptile files"
        )
        return
    for relative_path, expected_digest in recorded.items():
        actual_digest = hashlib.sha256((root / relative_path).read_bytes()).hexdigest()
        if actual_digest != expected_digest:
            violations.append(
                f"{relative_path} differs from its approved checksum; explicit owner approval and checksum update are required"
            )


def check_repository(root: Path) -> list[str]:
    root = root.resolve()
    violations: list[str] = []

    for relative_path in REQUIRED_FILES:
        if not (root / relative_path).is_file():
            violations.append(f"missing required file: {relative_path}")

    laws = _read(root, "LAWS_OF_ALX.md", violations)
    law_numbers = [int(value) for value in re.findall(r"^## Law (\d+)\b", laws, re.MULTILINE)]
    if law_numbers != [0, 1, 2, 3]:
        violations.append("LAWS_OF_ALX.md: expected exactly Laws 0 through 3 in order")
    _require_markers(
        laws,
        "LAWS_OF_ALX.md",
        (
            "Law 0 — One outcome. One production path.",
            "One outcome. One path. Everything else is removed.",
            "Law 1 — AL/X decides meaning",
            "Law 2 — Code executes known procedures",
            "Law 3 — Ambiguity returns to AL/X",
            "Ideas are permissive. Experimentation is isolated. Deployment is governed.",
        ),
        violations,
    )

    # The three-law rewrite left the enforcement specification describing gates
    # for laws that no longer exist, and both gates passed anyway. A document
    # that binds implementers must not contradict the canonical law text.
    enforcement = _read(root, "docs/LAW_ENFORCEMENT.md", violations)
    stale = sorted(
        {
            int(value)
            for value in re.findall(r"^\| (\d+)", enforcement, re.MULTILINE)
        }
        - set(law_numbers)
    )
    if stale:
        violations.append(
            "docs/LAW_ENFORCEMENT.md: defines gates for laws that do not exist "
            f"in LAWS_OF_ALX.md: {', '.join(str(item) for item in stale)}"
        )
    enforced = {
        int(value) for value in re.findall(r"^\| (\d+)", enforcement, re.MULTILINE)
    }
    missing = sorted(set(law_numbers) - enforced)
    if missing:
        violations.append(
            "docs/LAW_ENFORCEMENT.md: no gate is defined for law(s) "
            f"{', '.join(str(item) for item in missing)}"
        )
    for number, title in re.findall(r"^## Law (\d+) — (.+)$", laws, re.MULTILINE):
        if f"| {number} — {title.strip()} |" not in enforcement:
            violations.append(
                f"docs/LAW_ENFORCEMENT.md: its row for law {number} does not "
                "carry that law's title"
            )

    # Greptile reviews against the laws this file names. Instructing it to
    # review "all 19 Laws" after the rewrite would have produced an invalid
    # constitutional review, and no gate noticed. Any live document that names
    # a law must name one that exists; governance/DECISIONS.md is excluded
    # because it records decisions as they were approved at the time.
    for relative_path in (
        ".greptile/rules.md",
        ".greptile/config.json",
        "AGENTS.md",
        "CLAUDE.md",
        "README.md",
        "TODO.md",
        "IDENTITY_AND_MEMORY.md",
        "docs/ARCHITECTURE_BLUEPRINT.md",
        "docs/FOUNDATION_PROOF.md",
        "docs/TECHNICAL_PLAN.md",
        "docs/MAIL_RETENTION_PROPOSAL.md",
        "docs/PERSISTENT_RESEARCH_NOTEBOOK_BRIEF.md",
        "docs/XERO_EMAIL_BILLS_IMPLEMENTATION.md",
    ):
        path = root / relative_path
        if not path.is_file():
            continue
        document = path.read_text(encoding="utf-8")
        named = {
            int(value)
            for value in re.findall(r"\bLaws? (\d+)\b", document)
        }
        unknown = sorted(named - set(law_numbers))
        if unknown:
            violations.append(
                f"{relative_path}: names law(s) that do not exist in "
                f"LAWS_OF_ALX.md: {', '.join(str(item) for item in unknown)}"
            )
        counted = re.search(r"\ball (\d+) Laws\b", document)
        if counted and int(counted.group(1)) != len(law_numbers):
            violations.append(
                f"{relative_path}: claims {counted.group(1)} laws exist, "
                f"but LAWS_OF_ALX.md has {len(law_numbers)}"
            )

    blueprint = _read(root, "docs/ARCHITECTURE_BLUEPRINT.md", violations)
    if "process_DHL_invoice_workflow` would encode a journey" in blueprint:
        violations.append(
            "docs/ARCHITECTURE_BLUEPRINT.md: its prohibited-capability example "
            "predates Law 2 and contradicts it"
        )

    agents = _read(root, "AGENTS.md", violations)
    _require_markers(
        agents,
        "AGENTS.md",
        (
            "LAWS_OF_ALX.md",
            "IDENTITY_AND_MEMORY.md",
            "docs/LAW_ENFORCEMENT.md",
            "docs/ARCHITECTURE_BLUEPRINT.md",
            "docs/FOUNDATION_PROOF.md",
            "preserve exactly one production path",
            "Git history is the archive for removed implementations.",
        ),
        violations,
    )

    pull_request = _read(root, ".github/pull_request_template.md", violations)
    _require_markers(
        pull_request,
        ".github/pull_request_template.md",
        (
            "Production outcome and its one authoritative path:",
            "Superseded production entry points searched and deleted",
            "replacement tests prove no competing path remains",
        ),
        violations,
    )

    claude = _read(root, "CLAUDE.md", violations)
    _require_markers(claude, "CLAUDE.md", ("AGENTS.md",), violations)

    copilot = _read(root, ".github/copilot-instructions.md", violations)
    _require_markers(
        copilot,
        ".github/copilot-instructions.md",
        ("AGENTS.md", "LAWS_OF_ALX.md", "docs/LAW_ENFORCEMENT.md"),
        violations,
    )

    architecture = _read(root, "docs/ARCHITECTURE_BLUEPRINT.md", violations)
    _require_markers(
        architecture,
        "docs/ARCHITECTURE_BLUEPRINT.md",
        (
            "**Status:** Accepted by Friedl on 2026-08-26",
            "We structure AL/X's capabilities, safety boundaries, and memory—not her reasoning path.",
        ),
        violations,
    )

    proof = _read(root, "docs/FOUNDATION_PROOF.md", violations)
    _require_markers(
        proof,
        "docs/FOUNDATION_PROOF.md",
        ("**Status:** Approved for implementation by Friedl on 2026-08-26",),
        violations,
    )

    identity = _read(root, "IDENTITY_AND_MEMORY.md", violations)
    _require_markers(
        identity,
        "IDENTITY_AND_MEMORY.md",
        (
            "**Status:** Approved by Friedl on 2026-08-27",
            "Never diminish the person you are speaking to",
            "Be genuine rather than performative",
            "Allow yourself to evolve",
            "Origin 01 — Why I exist",
            "Origin 04 — My history begins here",
            "must not be reduced to a rigid score",
        ),
        violations,
    )

    decisions = _read(root, "governance/DECISIONS.md", violations)
    _require_markers(
        decisions,
        "governance/DECISIONS.md",
        (
            "D-001 — Foundation architecture accepted",
            "D-002 — Runtime model evaluation order",
            "D-003 — Reuse the original video background",
        ),
        violations,
    )

    exceptions = _read(root, "governance/EXCEPTIONS.md", violations)
    _check_exceptions(exceptions, violations)
    _check_greptile(root, violations)
    _check_law_checksum(root, violations)
    _check_identity_checksum(root, violations)
    _check_env_is_ignored(root, violations)

    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)

    violations = check_repository(args.root)
    if violations:
        print("AL/X governance gate failed:")
        for violation in violations:
            print(f"- {violation}")
        return 1

    print("AL/X governance gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
