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
    restricted_external_imports: dict[str, frozenset[str]]
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
        restricted_external_imports={
            dependency: frozenset(owners)
            for dependency, owners in data["restricted_external_imports"].items()
        },
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


# Diagnostics that would carry payload-bearing runtime state out of AL/X.
# Recorded as governance decision D-012, the diagnostics privacy boundary.
# A provider request holds a mail body, Friedl's speech, or AL/X's own words;
# an exception object, a traceback frame, or captured locals can carry any of
# them. AL/X may report sanitised codes, identifiers and durations, never the
# state itself.
PROHIBITED_DIAGNOSTIC_CALLS = {
    # Renders a traceback, which prints source lines and can capture locals.
    "format_exception": "traceback rendering",
    "format_exc": "traceback rendering",
    "print_exception": "traceback rendering",
    "print_exc": "traceback rendering",
    "format_tb": "traceback rendering",
    "print_tb": "traceback rendering",
    "extract_tb": "traceback frame extraction",
    "extract_stack": "traceback frame extraction",
    "format_stack": "traceback frame extraction",
    "print_stack": "traceback frame extraction",
    "TracebackException": "traceback rendering",
    "StackSummary": "traceback frame extraction",
    # Logs the active exception and its traceback.
    "exception": "logging an exception with its traceback",
    # Hands exception state to an error-reporting or observability sink.
    "capture_exception": "error-reporting sink",
    "capture_message": "error-reporting sink",
    "record_exception": "error-reporting sink",
    "set_exception": "error-reporting sink",
    "notify_exception": "error-reporting sink",
}

# Passing a live exception to a logger, rather than a sanitised code.
PROHIBITED_DIAGNOSTIC_KEYWORDS = {
    "exc_info": "logging an exception with its traceback",
    "capture_locals": "capturing frame locals",
    "stack_info": "logging a stack trace",
}

# Replacing the interpreter's own handler, which renders a full traceback.
PROHIBITED_DIAGNOSTIC_ATTRIBUTES = {
    "excepthook": "installing an exception hook",
    "unraisablehook": "installing an exception hook",
    "threading_excepthook": "installing an exception hook",
}


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
        normalised = _normalise_identifier(identifier)
        tokens = _identifier_tokens(identifier)
        forbidden = {
            item
            for item in self.rules.forbidden_source_names
            if (
                item in tokens
                or normalised.startswith(item + "_")
                or normalised.endswith("_" + item)
            )
        }
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
        allowed_owners = self.rules.restricted_external_imports.get(top_level)
        if allowed_owners is not None and self.owner not in allowed_owners:
            self._add(
                node,
                f"restricted dependency {top_level!r} is allowed only in "
                + ", ".join(sorted(allowed_owners)),
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

    def _check_diagnostics(self, node: ast.Call) -> None:
        """Reject diagnostics that would export payload-bearing state (D-012)."""
        if isinstance(node.func, ast.Attribute):
            called = node.func.attr
        elif isinstance(node.func, ast.Name):
            called = node.func.id
        else:
            called = ""
        route = PROHIBITED_DIAGNOSTIC_CALLS.get(called)
        if route is not None:
            self._add(
                node,
                f"prohibited diagnostic ({route}): {called} may carry private "
                "runtime state; report a sanitised code instead (D-012)",
            )
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            route = PROHIBITED_DIAGNOSTIC_KEYWORDS.get(keyword.arg)
            if route is not None:
                self._add(
                    node,
                    f"prohibited diagnostic ({route}): {keyword.arg}= may carry "
                    "private runtime state; report a sanitised code instead (D-012)",
                )

    def visit_Call(self, node: ast.Call) -> None:
        self._check_diagnostics(node)
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
        for target in node.targets:
            for child in ast.walk(target):
                if isinstance(child, ast.Attribute):
                    route = PROHIBITED_DIAGNOSTIC_ATTRIBUTES.get(child.attr)
                    if route is not None:
                        self._add(
                            node,
                            f"prohibited diagnostic ({route}): {child.attr} renders "
                            "a full traceback; handle failures with sanitised "
                            "codes instead (D-012)",
                        )
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


# Law 1: anything presented as coming from AL/X must be authored by the
# authoritative reasoning path. A transport, frontend, provider, tool or
# recovery handler may emit technical codes, diagnostics and structural UI
# state; it may not compose conversational wording on her behalf. A recovery
# handler once added a fixed sentence — "That turn failed before I could
# answer..." — to the voice error event, which is a second assistant voice.
#
# Terminal logs and diagnostic panel codes stay allowed, and so do structural
# phases such as listening, thinking, speaking and error.
CONVERSATIONAL_EVENT_FIELDS = frozenset(
    {"notice", "message", "text", "content", "speech", "utterance", "say",
     "prose", "wording", "explanation", "apology", "prompt_text"}
)

# The one value that may reach speech synthesis. It carries the reasoner's own
# words; anything else would be a composed fallback.
AUTHORITATIVE_RESPONSE_SOURCES = frozenset({"response", "outcome.response"})


def _voice_event_violations(path: Path, relative: str, tree: ast.AST) -> list[Violation]:
    """Reject conversational fields added to a phase/error event payload."""
    found: list[Violation] = []
    for node in ast.walk(tree):
        # message["notice"] = "..."  — a composed field on an event mapping.
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)
                    and target.slice.value in CONVERSATIONAL_EVENT_FIELDS
                    and _contains_string_literal(node.value)
                ):
                    found.append(
                        Violation(
                            relative,
                            node.lineno,
                            "voice event may not carry composed wording: "
                            f"{target.slice.value!r} is conversational output "
                            "and must come from the authoritative reasoner",
                        )
                    )
        # {"type": "phase", "notice": "..."} — the same field in a literal.
        if isinstance(node, ast.Dict):
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if not keys & {"type", "value", "reason"}:
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in CONVERSATIONAL_EVENT_FIELDS
                    and _contains_string_literal(value)
                ):
                    found.append(
                        Violation(
                            relative,
                            node.lineno,
                            "voice event may not carry composed wording: "
                            f"{key.value!r} is conversational output and must "
                            "come from the authoritative reasoner",
                        )
                    )
    return found


def _speech_synthesis_violations(relative: str, tree: ast.AST) -> list[Violation]:
    """Only the authoritative Core response may be synthesised."""
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if called != "synthesize" or not node.args:
            continue
        spoken = node.args[0]
        if isinstance(spoken, ast.Name):
            name = spoken.id
        elif isinstance(spoken, ast.Attribute):
            name = f"{getattr(spoken.value, 'id', '')}.{spoken.attr}".lstrip(".")
        else:
            name = None
        if name is None or name not in AUTHORITATIVE_RESPONSE_SOURCES:
            found.append(
                Violation(
                    relative,
                    node.lineno,
                    "only the authoritative Core response may reach speech "
                    "synthesis; a literal, fallback or capability result may not",
                )
            )
    return found


# The complete set of neutral system states a phase label may name. A label is
# structural UI state, not AL/X speaking, so first-person or user-directed
# wording is prohibited here: "I hear you" and "Something interrupted me" both
# read as her voice when she has not reasoned at all. "AL/X" is her name rather
# than a sentence, so it is permitted as the unknown-phase fallback.
NEUTRAL_PHASE_LABELS = frozenset(
    {
        "Ready",
        "Listening",
        "Hearing",
        "Thinking",
        "Speaking",
        "Error",
        "Disconnected",
        "AL/X",
    }
)


def _phase_label_violations(path: Path, relative: str, text: str) -> list[Violation]:
    """Every phase label must be one of the approved neutral states."""
    found: list[Violation] = []
    block = re.search(r"phaseLabels\s*=\s*\{(.*?)\}", text, re.S)
    if block is not None:
        for number, line in enumerate(block.group(1).splitlines()):
            match = re.search(r'["\']([^"\']*)["\']\s*,?\s*$', line.strip())
            if match is None:
                continue
            label = match.group(1)
            if label not in NEUTRAL_PHASE_LABELS:
                found.append(
                    Violation(
                        relative,
                        text[: block.start()].count("\n") + number + 2,
                        f"phase label {label!r} is not a neutral system state; "
                        "a label may not carry first-person or user-directed "
                        "wording, which reads as AL/X speaking",
                    )
                )
    # The fallback assigned when a phase is unknown is a label too.
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(
            r"phaseLabels\[[^\]]*\]\s*\?\?\s*[\"\']([^\"\']*)[\"\']", line
        )
        if match and match.group(1) not in NEUTRAL_PHASE_LABELS:
            found.append(
                Violation(
                    relative,
                    number,
                    f"phase fallback {match.group(1)!r} is not a neutral system "
                    "state",
                )
            )
    return found


def _frontend_violations(root: Path) -> list[Violation]:
    """The frontend renders authoritative state; it does not author AL/X.

    JavaScript has no AST here, so this is structural rather than a search for
    one sentence: any assignment that puts a transported field onto a visible
    AL/X surface is rejected, whatever the field is called and whatever it
    says. The diagnostics panel is explicitly allowed — it is a technical log,
    not AL/X speaking — and so is a fixed structural phase label.
    """
    assets = root / "src/alx/interfaces/assets"
    if not assets.exists():
        return []

    # Surfaces the person reads as AL/X, rather than as a technical panel.
    surfaces = ("status", "transcript", "response", "reply", "bubble")
    found: list[Violation] = []
    # The status element's initial value is a label the person reads before any
    # phase arrives, so it is held to the same rule.
    for markup in sorted(assets.glob("*.html")):
        relative = str(markup.relative_to(root))
        for number, line in enumerate(
            markup.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = re.search(r'id="status"[^>]*>([^<]*)<', line)
            if match and match.group(1).strip() not in NEUTRAL_PHASE_LABELS:
                found.append(
                    Violation(
                        relative,
                        number,
                        f"status text {match.group(1).strip()!r} is not a "
                        "neutral system state",
                    )
                )

    for path in sorted(assets.glob("*.js")):
        relative = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        found.extend(_phase_label_violations(path, relative, text))
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("//"):
                continue
            match = re.search(
                r"\b(" + "|".join(surfaces) + r")\s*\.\s*"
                r"(?:textContent|innerText|innerHTML)\s*=\s*(.+)$",
                stripped,
            )
            if match is None:
                continue
            assigned = match.group(2).strip().rstrip(";")
            # A fixed structural label, or a lookup of one, is allowed.
            if assigned.startswith("phaseLabels"):
                continue
            # Anything carried in from the transport is not.
            if re.search(r"\bmessage\b|\bevent\b|\bdata\b|\bpayload\b", assigned):
                found.append(
                    Violation(
                        relative,
                        number,
                        "frontend may not render transported prose as AL/X: "
                        f"{match.group(1)} must show structural state, and "
                        "conversational wording must come from the reasoner",
                    )
                )
    return found


# EX-001 authorises the Sol/Luna split as a time-boxed experiment: two Cores,
# one answering Friedl and one answering a turn nobody asked for. The whole
# point is that the choice is provenance, never meaning. If selection ever keys
# on a topic, a capability, a goal, notebook state, a keyword, content,
# importance or a domain, deterministic code has begun deciding which AL/X
# shows up before she has reasoned at all — which EX-001 explicitly prohibits
# and which is Law 1 phrase routing one level up.
#
# So the gate is narrow and blunt: only `origin` may steer the two reasoners,
# and only composition may hold both. A reviewer cannot be relied on to notice
# a third branch added months from now.
AUTONOMOUS_SELECTION_OWNER = "bootstrap/reasoning.py"
AUTONOMOUS_SELECTION_COMPOSER = "bootstrap/live_voice.py"

# Anything a selection must never read. These are the shapes a model router
# takes when it stops being about provenance.
SEMANTIC_SELECTION_TOKENS = frozenset(
    {
        "topic", "topics", "subject", "keyword", "keywords", "content",
        "capability", "capabilities", "goal", "goals", "notebook", "research",
        "memory", "memories", "intent", "importance", "priority", "urgency",
        "domain", "category", "sentiment", "score", "threshold", "interest",
    }
)


def _autonomous_selection_violations(
    relative_text: str, tree: ast.AST
) -> list[Violation]:
    """Prove the experimental Core choice stays a choice about provenance.

    Two rules. Only the composer and the reasoner module may name both
    reasoners at all, so no third place can grow its own selection. And inside
    the one selecting expression, the only thing consulted is the origin.
    """
    posix = relative_text.replace("\\", "/")
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != "OriginSelectedReasoner":
            continue
        if not posix.endswith(AUTONOMOUS_SELECTION_OWNER):
            violations.append(
                Violation(
                    relative_text,
                    node.lineno,
                    "the experimental origin selection may live only in "
                    + AUTONOMOUS_SELECTION_OWNER,
                )
            )
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Attribute):
                continue
            token = inner.attr.lower()
            if token in SEMANTIC_SELECTION_TOKENS:
                violations.append(
                    Violation(
                        relative_text,
                        inner.lineno,
                        "model selection may read only the cognition origin; "
                        f"reading {inner.attr!r} would select on meaning",
                    )
                )
    if posix.endswith(AUTONOMOUS_SELECTION_OWNER) or posix.endswith(
        AUTONOMOUS_SELECTION_COMPOSER
    ):
        return violations
    names = {
        node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    if "OriginSelectedReasoner" in names:
        violations.append(
            Violation(
                relative_text,
                0,
                "only composition may reference the experimental origin "
                "selection; a second selection site is a model router",
            )
        )
    return violations


NOTE_INTERPRETATION_METHODS = frozenset(
    {
        "lower", "upper", "casefold", "startswith", "endswith", "find",
        "rfind", "index", "split", "rsplit", "partition", "search", "match",
        "count", "replace", "strip", "encode", "translate", "title",
    }
)


# A carried thought is AL/X's own unfinished thinking, and it is opaque for the
# same reason her private note is: the moment code reads it, code has begun
# deciding what she meant and when it should be raised. Only files that
# legitimately hold her words are checked, so ordinary uses of the English word
# "content" elsewhere are not swept up.
OPAQUE_CONTENT_MODULES = (
    "continuity/store.py",
    "tools/continuity.py",
    "continuity/source.py",
    "bootstrap/autonomous.py",
)


def _note_interpretation_violations(
    relative_text: str, tree: ast.AST
) -> list[Violation]:
    """Prove nothing reads the private note or a carried thought.

    Catches string inspection applied to anything named `note`, and any
    conditional whose test is a bare note value. Storing, passing and returning
    it are all untouched, because those are transport rather than reading.
    """
    posix = relative_text.replace("\\", "/")
    opaque_here = posix.endswith(OPAQUE_CONTENT_MODULES)
    violations: list[Violation] = []

    def names_a_note(node: ast.AST) -> bool:
        # See through a conversion such as str(values["note"]): wrapping the
        # note in a cast does not make reading it something else.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("str", "repr", "format")
            and node.args
        ):
            return names_a_note(node.args[0])
        opaque_names = ("note", "content") if opaque_here else ("note",)
        if isinstance(node, ast.Name):
            return node.id in opaque_names or node.id.endswith("_note")
        if isinstance(node, ast.Attribute):
            return node.attr in opaque_names or node.attr.endswith("_note")
        if isinstance(node, ast.Subscript):
            key = node.slice
            return isinstance(key, ast.Constant) and key.value in opaque_names
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                node.func.attr in NOTE_INTERPRETATION_METHODS
                and names_a_note(node.func.value)
            ):
                violations.append(
                    Violation(
                        relative_text,
                        node.lineno,
                        f"AL/X's own words may not be inspected: .{node.func.attr}()"
                        " reads what she wrote",
                    )
                )
        if isinstance(node, (ast.If, ast.IfExp)) and names_a_note(node.test):
            violations.append(
                Violation(
                    relative_text,
                    node.lineno,
                    "deterministic code may not branch on AL/X's own words",
                )
            )
        if isinstance(node, ast.Compare) and names_a_note(node.left):
            # `note is None` and `isinstance(note, str)` ask whether a note
            # exists and whether it is well-formed. Neither reads what it says,
            # and a contract must be able to validate its own shape. Comparing
            # a note to a value is different: that is reading it.
            if all(
                isinstance(operator, (ast.Is, ast.IsNot))
                and isinstance(comparator, ast.Constant)
                and comparator.value is None
                for operator, comparator in zip(node.ops, node.comparators)
            ):
                continue
            violations.append(
                Violation(
                    relative_text,
                    node.lineno,
                    "deterministic code may not compare AL/X's own words",
                )
            )
    return violations


# D-024: the opportunity source notices that something objective happened. It
# may not read goals, notebook entries, memories, research or the private note,
# and it may not rank, filter or defer an occasion by what the occasion might
# be about. A filter there is the cheapest second mind to build and the most
# damaging, because it decides what AL/X never gets to consider.
#
# Imports are the enforceable half: a module that cannot reach her state cannot
# form an opinion about it.
OPPORTUNITY_SOURCE_MODULE = "continuity/source.py"
OPPORTUNITY_SOURCE_FORBIDDEN_IMPORTS = frozenset(
    {"goals", "memories", "research", "specialists", "tools"}
)
OPPORTUNITY_SOURCE_FORBIDDEN_READS = frozenset(
    {
        "topic", "importance", "priority", "urgency", "category", "sentiment",
        "score", "interest", "intent", "staleness", "sender", "subject",
        "keywords", "content", "summary",
    }
)


def _opportunity_source_violations(
    relative_text: str, tree: ast.AST
) -> list[Violation]:
    """Prove the opportunity source stays a clock, not a critic."""
    posix = relative_text.replace("\\", "/")
    if not posix.endswith(OPPORTUNITY_SOURCE_MODULE):
        return []
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = [alias.name for alias in node.names]
            for candidate in (module, *names):
                parts = candidate.split(".")
                if "alx" in parts:
                    owner_index = parts.index("alx") + 1
                    owner = parts[owner_index] if owner_index < len(parts) else ""
                    if owner in OPPORTUNITY_SOURCE_FORBIDDEN_IMPORTS:
                        violations.append(
                            Violation(
                                relative_text,
                                node.lineno,
                                "the opportunity source may not read AL/X's "
                                f"state: importing {owner!r} lets it form an "
                                "opinion about what deserves thought",
                            )
                        )
        if isinstance(node, ast.Attribute) and node.attr in OPPORTUNITY_SOURCE_FORBIDDEN_READS:
            violations.append(
                Violation(
                    relative_text,
                    node.lineno,
                    "the opportunity source notices objective events only; "
                    f"reading {node.attr!r} would filter on meaning",
                )
            )
    return violations


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
        violations.extend(_autonomous_selection_violations(relative_text, tree))
        violations.extend(_note_interpretation_violations(relative_text, tree))
        violations.extend(_opportunity_source_violations(relative_text, tree))
        if owner == "interfaces":
            violations.extend(_voice_event_violations(path, relative_text, tree))
            violations.extend(_speech_synthesis_violations(relative_text, tree))

    violations.extend(_frontend_violations(root))
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
