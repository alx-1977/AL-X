"""The one `code` capability: coding jobs, git, pull requests and reviews (D-074).

Running and stopping a coding job, operating on her checkout, reading a pull
request's checks, requesting and reading an external review, merging a
reviewed pull request, restarting onto merged main and running a sandbox
experiment are actions of one capability. Each keeps its own input and result
contract, permission and approval rule; the always-present catalogue carries
one line for all of them. AL/X proposes a structured call, the broker
validates it, the safety gate authorises it, and the action runs. There is no
other entry point, because a second way in would be a second production path
to the same outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Mapping

from alx.contracts import (
    CODE,
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    CodeAction,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.tools.coding import CONTRACTS as CODING_CONTRACTS
from alx.tools.pull_request_checks import CONTRACT as CHECKS_CONTRACT
from alx.tools.repository import CONTRACT as MERGE_CONTRACT
from alx.tools.repository_authority import CONTRACT as GIT_CONTRACT
from alx.tools.review import BEYOND_ALLOWANCE_CONTRACT, REQUEST_CONTRACT
from alx.tools.review_content import CONTRACT as READ_REVIEW_CONTRACT
from alx.tools.sandbox import CONTRACT as EXPERIMENT_CONTRACT
from alx.tools.self_restart import CONTRACT as RESTART_CONTRACT


_STRING = StructuredSchema(ValueKind.STRING)

# Each action keeps its own input and result contract; the capability is one
# (D-074). These are contracts, not capabilities: nothing registers them.
CONTRACTS = {
    **CODING_CONTRACTS,
    CodeAction.GIT: GIT_CONTRACT,
    CodeAction.READ_CHECKS: CHECKS_CONTRACT,
    CodeAction.REQUEST_REVIEW: REQUEST_CONTRACT,
    CodeAction.REQUEST_REVIEW_BEYOND_ALLOWANCE: BEYOND_ALLOWANCE_CONTRACT,
    CodeAction.READ_REVIEW: READ_REVIEW_CONTRACT,
    CodeAction.MERGE: MERGE_CONTRACT,
    CodeAction.RESTART: RESTART_CONTRACT,
    CodeAction.EXPERIMENT: EXPERIMENT_CONTRACT,
}
assert tuple(CONTRACTS) == tuple(CodeAction)
# What changes nothing: reading checks needs no active goal. Reading a review
# stays effectful as it was: it reaches the reviewer.
READ_ACTIONS = (CodeAction.READ_CHECKS,)
# Reading again changes nothing, so a plan may wait on these.
OBSERVATION_ACTIONS = (CodeAction.READ_CHECKS, CodeAction.READ_REVIEW)
# Commit messages, pull request text and merge metadata are her wording
# published elsewhere, so the Core's check on text Friedl has not heard applies.
TRANSMITTING_ACTIONS = (CodeAction.GIT, CodeAction.MERGE)
# The only action that needs Friedl's approval of the exact call: a paid review
# past his allowance (2026-10-14).
APPROVAL_ACTIONS = (CodeAction.REQUEST_REVIEW_BEYOND_ALLOWANCE,)
# A sandbox program stays in its workspace and run manifest, never in the
# goal's execution audit, as when the experiment was its own capability.
_NOT_DURABLE = frozenset({"source", "entry_filename", "wall_seconds"})

_JOBS = (
    ((CodeAction.RUN_JOB, CodeAction.STOP_JOB), "run and stop coding jobs"),
    ((CodeAction.GIT,), "git on your checkout"),
    ((CodeAction.READ_CHECKS,), "pull request checks"),
    ((CodeAction.REQUEST_REVIEW, CodeAction.READ_REVIEW), "external reviews"),
    ((CodeAction.MERGE,), "merge reviewed pull requests"),
    ((CodeAction.RESTART,), "restart onto merged main"),
    ((CodeAction.EXPERIMENT,), "sandbox experiments"),
)


def _union(schemas: Sequence[StructuredSchema]) -> dict[str, StructuredSchema]:
    """Every field any offered action takes or returns; a field shaped two ways is any value."""
    fields: dict[str, StructuredSchema] = {}
    for schema in schemas:
        for name, field in schema.properties.items():
            if name in fields and fields[name] != field:
                fields[name] = StructuredSchema(ValueKind.ANY)
            else:
                fields.setdefault(name, field)
    return fields


def _alternatives(offered: Sequence[CodeAction], chosen: Sequence[CodeAction]):
    return tuple((("action", item.value),) for item in offered if item in chosen)


def code_definition(offered: Sequence[CodeAction]) -> CapabilityDefinition:
    """The one `code` capability, describing only the offered actions (D-074).

    Its first sentence is her always-present catalogue line, so it names
    every offered job and nothing that is absent.
    """
    offered = tuple(item for item in CONTRACTS if item in set(offered))
    jobs = [label for actions, label in _JOBS if any(item in offered for item in actions)]
    line = "Code: " + (
        jobs[0] if len(jobs) == 1 else ", ".join(jobs[:-1]) + " and " + jobs[-1]
    ) + "."
    contracts = [CONTRACTS[item] for item in offered]
    inputs = _union([item.input_schema for item in contracts])

    def named(chosen: Sequence[CodeAction]) -> str:
        return ", ".join(item.value for item in offered if item in chosen)

    notes = [line, "action names which."]
    if named(READ_ACTIONS):
        notes.append(f"{named(READ_ACTIONS)} only reads.")
    if named(OBSERVATION_ACTIONS):
        notes.append(f"A plan may wait on {named(OBSERVATION_ACTIONS)}.")
    if named(APPROVAL_ACTIONS):
        notes.append(f"{named(APPROVAL_ACTIONS)} needs Friedl's approval of the exact call; "
                     "every other action is yours to use.")
    notes.append("The result carries the action's own fields. Each action below lists "
                 "its fields and the ones it requires.")
    return CapabilityDefinition(
        CODE,
        " ".join(notes) + " " + " ".join(
            f"[{name.value}] fields: {', '.join(contract.input_schema.properties) or 'none'}; "
            f"required: {', '.join(contract.input_schema.required) or 'none'}. {contract.purpose}"
            for name, contract in zip(offered, contracts)
        ),
        StructuredSchema(
            ValueKind.OBJECT,
            {"action": _STRING, **inputs},
            ("action",),
            extra_properties=False,
        ),
        StructuredSchema(
            ValueKind.OBJECT,
            _union([item.output_schema for item in contracts]),
            # A git operation reports operation-specific fields beside its own.
            extra_properties=any(item.output_schema.extra_properties for item in contracts),
        ),
        SideEffect.EFFECTFUL,
        tuple(dict.fromkeys((
            *(code for item in contracts for code in item.possible_failure_codes),
            "result_output_invalid",
        ))),
        durable_input_fields=("action", *(name for name in inputs if name not in _NOT_DURABLE)),
        trace_fields=tuple(dict.fromkeys(
            ("action", *(name for item in contracts for name in item.trace_fields)))),
        read_only_when=_alternatives(offered, READ_ACTIONS),
        transmits_authored_text_when=_alternatives(offered, TRANSMITTING_ACTIONS),
        plan_observation_when=_alternatives(offered, OBSERVATION_ACTIONS),
    )


def build_code_executors(
    actions: Mapping[CodeAction, Callable[[Any], CapabilityResult]],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Any], CapabilityResult]]:
    """The one `code` executor over the actions this runtime composed."""
    offered = tuple(item for item in CONTRACTS if item in actions)

    def failed(code: str, reason: str = "") -> CapabilityResult:
        return CapabilityResult(
            call_id_source(), CODE, CapabilityResultState.FAILED,
            failure={"code": code, **({"reason": reason} if reason else {})},
        )

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        """Run one offered action under its own contract (D-074).

        The action's result passes through unwrapped, with its provenance,
        durable values and outcome, so what other code reads from it (a job's
        branch, a merge's commit, a review's judgement flag) stays where it was.
        """
        try:
            action = CodeAction(arguments.get("action"))
        except ValueError:
            action = None
        if action not in offered:
            return failed("arguments_unusable",
                          "action must be one of " + ", ".join(item.value for item in offered))
        contract = CONTRACTS[action]
        values = {key: value for key, value in arguments.items() if key != "action"}
        fields = contract.input_schema
        unknown = sorted(set(values) - set(fields.properties))
        missing = sorted(set(fields.required) - set(values))
        if unknown or missing:
            return failed("arguments_unusable", "; ".join(
                part for part in (
                    unknown and f"{action.value} does not take {', '.join(unknown)}",
                    missing and f"{action.value} requires {', '.join(missing)}",
                ) if part))
        result = actions[action](values)
        if result.capability_id != CODE:
            result = replace(result, capability_id=CODE)
        if (result.state is not CapabilityResultState.FAILED
                and not contract.output_schema.accepts(result.values)):
            return failed("result_output_invalid")
        return result

    return {CODE: run}


__all__ = [
    "APPROVAL_ACTIONS",
    "CONTRACTS",
    "OBSERVATION_ACTIONS",
    "READ_ACTIONS",
    "TRANSMITTING_ACTIONS",
    "build_code_executors",
    "code_definition",
]
