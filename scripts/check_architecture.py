#!/usr/bin/env python3
"""Static AL/X boundary checks for violations that can be detected objectively."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


LANGUAGE_IDENTIFIERS = {
    "input_text",
    "message",
    "message_text",
    "natural_language",
    "prompt",
    "query",
    "raw_message",
    "raw_prompt",
    "raw_text",
    "spoken_text",
    "text",
    "transcript",
    "user_input",
    "user_message",
    "user_prompt",
    "user_text",
    "utterance",
}

RAW_LANGUAGE_FORBIDDEN_BOUNDARIES = {"capabilities", "goals", "safety", "tools"}


@dataclass(frozen=True)
class Rules:
    source_root: str
    boundaries: frozenset[str]
    allowed_imports: dict[str, frozenset[str]]
    provider_only_imports: frozenset[str]
    forbidden_source_names: frozenset[str]


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    message: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        return f"{location}: {self.message}"


def load_rules(root: Path) -> Rules:
    config_path = root / "architecture/boundaries.toml"
    with config_path.open("rb") as handle:
        data = tomllib.load(handle)

    if data.get("schema_version") != 1:
        raise ValueError("architecture/boundaries.toml: unsupported schema_version")

    boundaries = frozenset(data["boundaries"])
    allowed = {
        owner: frozenset(targets)
        for owner, targets in data["allowed_imports"].items()
    }
    if set(allowed) != set(boundaries):
        raise ValueError(
            "architecture/boundaries.toml: every boundary needs one allowed_imports entry"
        )
    unknown_targets = set().union(*allowed.values()) - set(boundaries)
    if unknown_targets:
        raise ValueError(
            "architecture/boundaries.toml: unknown import targets: "
            + ", ".join(sorted(unknown_targets))
        )

    return Rules(
        source_root=data["source_root"],
        boundaries=boundaries,
        allowed_imports=allowed,
        provider_only_imports=frozenset(data["provider_only_imports"]),
        forbidden_source_names=frozenset(data["forbidden_source_names"]),
    )


def _normalise_identifier(value: str) -> str:
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()


def _identifier_tokens(value: str) -> set[str]:
    normalised = _normalise_identifier(value)
    return {normalised, *normalised.split("_")}


def _is_language_identifier(value: str) -> bool:
    return _normalise_identifier(value) in LANGUAGE_IDENTIFIERS


def _contains_language_identifier(node: ast.AST) -> bool:
    return any(
        isinstance(child, (ast.Name, ast.arg))
        and _is_language_identifier(child.id if isinstance(child, ast.Name) else child.arg)
        for child in ast.walk(node)
    )


def _contains_string_literal(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant) and isinstance(child.value, str)
        for child in ast.walk(node)
    )


def _internal_target(module_name: str | None) -> str | None:
    if not module_name:
        return None
    parts = module_name.split(".")
    if parts[0] != "alx" or len(parts) < 2:
        return None
    return parts[1]


class SourceVisitor(ast.NodeVisitor):
    def __init__(self, relative_path: str, owner: str, rules: Rules) -> None:
        self.relative_path = relative_path
        self.owner = owner
        self.rules = rules
        self.violations: list[Violation] = []

    def _add(self, node: ast.AST, message: str) -> None:
        self.violations.append(
            Violation(self.relative_path, getattr(node, "lineno", 0), message)
        )

    def _check_identifier(self, node: ast.AST, identifier: str) -> None:
        tokens = _identifier_tokens(identifier)
        forbidden = tokens & self.rules.forbidden_source_names
        if forbidden:
            self._add(
                node,
                "forbidden routing/workflow identifier: " + ", ".join(sorted(forbidden)),
            )

    def _check_import(self, node: ast.AST, module_name: str, level: int = 0) -> None:
        if level:
            self._add(
                node,
                "relative imports are prohibited so architecture dependencies remain inspectable",
            )
            return

        top_level = module_name.split(".", 1)[0]
        if top_level in self.rules.provider_only_imports and self.owner != "providers":
            self._add(
                node,
                f"model-provider dependency {top_level!r} is allowed only in providers",
            )

        target = _internal_target(module_name)
        if target is None or target == self.owner:
            return
        if target not in self.rules.boundaries:
            self._add(node, f"import targets unknown AL/X boundary {target!r}")
            return
        if target not in self.rules.allowed_imports[self.owner]:
            self._add(
                node,
                f"forbidden dependency: {self.owner} may not import {target}",
            )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_import(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._check_import(node, node.module or "", node.level)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_identifier(node, node.name)
        self._check_arguments(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_identifier(node, node.name)
        self._check_arguments(node)
        self.generic_visit(node)

    def _check_arguments(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self.owner not in RAW_LANGUAGE_FORBIDDEN_BOUNDARIES:
            return
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg:
            arguments.append(node.args.vararg)
        if node.args.kwarg:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if _is_language_identifier(argument.arg):
                self._add(
                    argument,
                    f"raw-language parameter {argument.arg!r} is prohibited in {self.owner}",
                )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_identifier(node, node.name)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._check_identifier(node, node.id)

    def visit_arg(self, node: ast.arg) -> None:
        self._check_identifier(node, node.arg)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._check_identifier(node, node.attr)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        operands = [node.left, *node.comparators]
        has_language = any(_contains_language_identifier(value) for value in operands)
        has_literal = any(_contains_string_literal(value) for value in operands)
        if has_language and has_literal:
            self._add(node, "raw-language comparison to fixed text is prohibited")
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        if _contains_language_identifier(node.subject) and any(
            _contains_string_literal(case.pattern) for case in node.cases
        ):
            self._add(node, "raw-language match against fixed text is prohibited")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in {"startswith", "endswith"} and _contains_language_identifier(
                node.func.value
            ):
                self._add(node, f"raw-language {method} routing is prohibited")
            if method in {"findall", "fullmatch", "match", "search"} and any(
                _contains_language_identifier(argument) for argument in node.args
            ):
                self._add(node, "regular-expression or pattern routing of raw language is prohibited")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Dict) and any(
            _contains_string_literal(key) for key in node.value.keys if key is not None
        ):
            for target in node.targets:
                names = [child.id for child in ast.walk(target) if isinstance(child, ast.Name)]
                if any(
                    _identifier_tokens(name) & {"action", "actions", "command", "commands", "intent", "intents", "route", "routes"}
                    for name in names
                ):
                    self._add(node, "fixed text-to-action mapping is prohibited")
                    break
        self.generic_visit(node)


def check_source(root: Path, rules: Rules | None = None) -> list[Violation]:
    root = root.resolve()
    rules = rules or load_rules(root)
    source_root = root / rules.source_root
    if not source_root.exists():
        return []

    violations: list[Violation] = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        relative_text = str(path.relative_to(root))
        if len(relative.parts) == 1:
            if relative.name != "__init__.py":
                violations.append(
                    Violation(relative_text, 0, "source must live inside an approved boundary")
                )
            continue

        owner = relative.parts[0]
        if owner not in rules.boundaries:
            violations.append(
                Violation(relative_text, 0, f"unknown architecture boundary: {owner}")
            )
            continue

        path_tokens: set[str] = set()
        for part in relative.parts:
            path_tokens.update(_identifier_tokens(part.removesuffix(".py")))
        forbidden_names = path_tokens & rules.forbidden_source_names
        if forbidden_names:
            violations.append(
                Violation(
                    relative_text,
                    0,
                    "forbidden routing/workflow source name: "
                    + ", ".join(sorted(forbidden_names)),
                )
            )

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative_text)
        except (OSError, UnicodeDecodeError, SyntaxError) as error:
            line = error.lineno if isinstance(error, SyntaxError) and error.lineno else 0
            violations.append(Violation(relative_text, line, f"cannot inspect source: {error}"))
            continue

        visitor = SourceVisitor(relative_text, owner, rules)
        visitor.visit(tree)
        violations.extend(visitor.violations)

    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args(argv)

    try:
        rules = load_rules(args.root)
        violations = check_source(args.root, rules)
    except (KeyError, OSError, ValueError, tomllib.TOMLDecodeError) as error:
        print(f"AL/X architecture gate configuration failed: {error}")
        return 1

    if violations:
        print("AL/X architecture gate failed:")
        for violation in violations:
            print(f"- {violation.render()}")
        return 1

    print("AL/X architecture gate passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
