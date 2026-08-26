#!/usr/bin/env python3
"""Fail when AL/X's canonical governance layer is missing or silently changed."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path


REQUIRED_FILES = (
    ".github/CODEOWNERS",
    ".github/copilot-instructions.md",
    ".github/pull_request_template.md",
    ".github/workflows/law-gates.yml",
    "AGENTS.md",
    "CLAUDE.md",
    "LAWS_OF_ALX.md",
    "architecture/boundaries.toml",
    "docs/ARCHITECTURE_BLUEPRINT.md",
    "docs/FOUNDATION_PROOF.md",
    "docs/LAW_ENFORCEMENT.md",
    "docs/TECHNICAL_PLAN.md",
    "governance/DECISIONS.md",
    "governance/EXCEPTIONS.md",
    "governance/LAWS_OF_ALX.sha256",
)


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


def check_repository(root: Path) -> list[str]:
    root = root.resolve()
    violations: list[str] = []

    for relative_path in REQUIRED_FILES:
        if not (root / relative_path).is_file():
            violations.append(f"missing required file: {relative_path}")

    laws = _read(root, "LAWS_OF_ALX.md", violations)
    law_numbers = [int(value) for value in re.findall(r"^### Law (\d+)\b", laws, re.MULTILINE)]
    if law_numbers != list(range(1, 19)):
        violations.append("LAWS_OF_ALX.md: expected exactly Laws 1 through 18 in order")

    agents = _read(root, "AGENTS.md", violations)
    _require_markers(
        agents,
        "AGENTS.md",
        (
            "LAWS_OF_ALX.md",
            "docs/LAW_ENFORCEMENT.md",
            "docs/ARCHITECTURE_BLUEPRINT.md",
            "docs/FOUNDATION_PROOF.md",
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
    _check_law_checksum(root, violations)
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
