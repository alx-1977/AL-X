"""The one `research` capability: the web, paid questions and her notebook (D-074).

Reading a page, searching the web, putting a bounded question to a paid model
and keeping her research notebook are actions of one capability. Each keeps its
own input and result contract, permission and approval rule; the always-present
catalogue carries one line for all of them. AL/X proposes a structured call,
the broker validates it, the safety gate authorises it, and the action runs.
There is no other entry point, because a second way in would be a second
production path to the same outcome.

A paid question spends money, so it carries its own permission. Everything
about what it may cost is enforced inside ResearchSpecialist; nothing here
chooses a model, a tier or a budget.
"""

from __future__ import annotations

from alx.contracts.diagnostics import failure_details

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    Cognition,
    RESEARCH,
    ResearchAction,
    ResearchQuestion,
    SideEffect,
    SpecialistQuestion,
    StructuredSchema,
    ValueKind,
)


from alx.tools.notebook import CONTRACTS as NOTEBOOK_CONTRACTS
from alx.tools.web import PAGE_CONTRACT, SEARCH_CONTRACT


_ASK = ResearchAction.ASK_MODEL.value

_STRING = StructuredSchema(ValueKind.STRING)

_FAILURES = (
    "arguments_unusable",
    # The tier AL/X chose is not enabled in this runtime.
    "cognition_tier_unconfigured",
    # The day's budget cannot cover another request.
    "research_budget_exhausted",
    "research_calls_exhausted",
    # A model with no configured price, or whose worst case exceeds the ceiling.
    "research_model_unavailable",
    # A provider charged more than its enforced bound allowed; research stops.
    "research_ceiling_failed",
    "provider_failed",
)

ASK_CONTRACT = CapabilityDefinition(
    _ASK,
    "Put one bounded research question to a paid model at a chosen cognition "
    "tier and return its structured answer. Spends from the research budget; "
    "answers once and continues nothing.",
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "question_id": _STRING,
            "instruction": _STRING,
            "material": _STRING,
            "cognition": _STRING,
        },
        ("question_id", "instruction", "material"),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "finding": _STRING,
            # Present only when the material was longer than the priced input
            # bound allowed. Its presence says the finding was read from part
            # of the material; what that is worth is AL/X's judgement.
            "material_omitted_characters": StructuredSchema(ValueKind.INTEGER),
        },
        ("finding",),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES,
)

# The answer shape asked of the model. Deliberately one field: this capability
# asks a question and reports what came back, and a richer schema would start
# encoding what research is for. The capability's own result may additionally
# report material the priced input bound could not carry, which is a fact about
# the evidence rather than something the model is asked for.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"finding": {"type": "string"}},
    "required": ["finding"],
    "additionalProperties": False,
}


def _failure_code(error: Exception) -> str:
    """Map a research failure to a declared code, carrying no material."""
    return {
        "ResearchBudgetExceeded": "research_budget_exhausted",
        "ResearchCallsExhausted": "research_calls_exhausted",
        "ResearchModelUnpriced": "research_model_unavailable",
        "ResearchModelUnbounded": "research_model_unavailable",
        "ResearchInputUnbounded": "arguments_unusable",
        "ResearchCeilingFailed": "research_ceiling_failed",
    }.get(type(error).__name__, "provider_failed")


def build_model_question(
    researcher: Any,
    call_id_source: Callable[[], str],
) -> Callable[[Any], CapabilityResult]:
    """Bind the paid-question action to the one prepaid specialist."""

    def ask(arguments: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            raw_tier = str(arguments.get("cognition", Cognition.SURVEY.value))
            question = ResearchQuestion(
                SpecialistQuestion(
                    str(arguments["question_id"]),
                    str(arguments["instruction"]),
                    str(arguments["material"]),
                    ANSWER_SCHEMA,
                ),
                cognition=Cognition(raw_tier),
            )
        except (KeyError, TypeError, ValueError):
            return CapabilityResult(
                call_id,
                _ASK,
                CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        try:
            answer = researcher.answer(question, task_id=call_id)
        except Exception as error:
            code = _failure_code(error)
            if type(error).__name__ == "SpecialistError":
                # The specialist's own codes are already sanitised, and an
                # unconfigured tier must be distinguishable from a provider
                # fault so AL/X can tell "not available" from "went wrong".
                reason = str(getattr(error, "args", ("",))[0] or "")
                code = (
                    "cognition_tier_unconfigured"
                    if reason.startswith("cognition_tier_unconfigured")
                    else "provider_failed"
                )
            return CapabilityResult(
                call_id,
                _ASK,
                CapabilityResultState.FAILED,
                failure={"code": code, **failure_details(error)},
            )
        finding = answer.get("finding") if isinstance(answer, Mapping) else None
        if not isinstance(finding, str) or not finding.strip():
            return CapabilityResult(
                call_id,
                _ASK,
                CapabilityResultState.FAILED,
                failure={"code": "provider_failed"},
            )
        values: dict[str, Any] = {"finding": finding}
        omitted = answer.get("material_omitted_characters")
        if isinstance(omitted, int) and not isinstance(omitted, bool) and omitted > 0:
            # Material that never reached the model is evidence about the
            # answer, so it travels with it. The capability reports the
            # shortfall and draws no conclusion from it.
            values["material_omitted_characters"] = omitted
        return CapabilityResult(
            call_id,
            _ASK,
            CapabilityResultState.SUCCEEDED,
            values,
            # The full answer is available to the Core for the next decision,
            # but the goal store is not the research notebook. Durable
            # continuity is created through the record_entry action.
            durable_values={},
        )

    return ask


# Each action keeps its own input and result contract; the capability is one
# (D-074). These are contracts, not capabilities: nothing registers them.
CONTRACTS = {
    ResearchAction.READ_PAGE: PAGE_CONTRACT,
    ResearchAction.SEARCH_WEB: SEARCH_CONTRACT,
    ResearchAction.ASK_MODEL: ASK_CONTRACT,
    **NOTEBOOK_CONTRACTS,
}
# What changes nothing: the notebook reads need no active goal. A page read and
# a web search stay effectful as they were: each reaches a third party, and a
# search spends.
READ_ACTIONS = (ResearchAction.SEARCH_NOTEBOOK, ResearchAction.READ_THREAD)
# The notebook text that belongs to the notebook store rather than to the goal's
# execution audit (as before, when each notebook job was its own capability).
_NOT_DURABLE = frozenset({"content", "reason", "question", "interest"})


def offered_research_actions(
    *, web: bool, search: bool, model: bool
) -> tuple[ResearchAction, ...]:
    """The actions this runtime can actually perform, in the contracts' order.

    A page read needs web reading enabled, a search its own paid provider and
    ledger, and a paid question an enabled tier and budget. An action whose
    dependency is absent is not offered, so the catalogue never promises a
    call that would fail before doing any work. The notebook is always there.
    """
    absent = set()
    if not web:
        absent.add(ResearchAction.READ_PAGE)
    if not search:
        absent.add(ResearchAction.SEARCH_WEB)
    if not model:
        absent.add(ResearchAction.ASK_MODEL)
    return tuple(item for item in CONTRACTS if item not in absent)


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


def research_definition(offered: Sequence[ResearchAction]) -> CapabilityDefinition:
    """The one `research` capability, describing only the offered actions (D-074).

    Its first sentence is her always-present catalogue line, so it names
    every offered job and nothing that is absent.
    """
    offered = tuple(item for item in CONTRACTS if item in set(offered))
    jobs = [label for action, label in (
        (ResearchAction.READ_PAGE, "read a public web page"),
        (ResearchAction.SEARCH_WEB, "search the web"),
        (ResearchAction.ASK_MODEL, "ask a paid model one bounded question"),
    ) if action in offered]
    jobs.append("keep your research notebook (threads of questions, claims and conclusions)")
    line = "Web and research: " + (
        jobs[0] if len(jobs) == 1 else ", ".join(jobs[:-1]) + " and " + jobs[-1]
    ) + "."
    contracts = [CONTRACTS[item] for item in offered]
    inputs = _union([item.input_schema for item in contracts])
    return CapabilityDefinition(
        RESEARCH,
        line
        + " action names which. "
        + ", ".join(item.value for item in offered if item in READ_ACTIONS)
        + " only read. correct_entry records Friedl's correction and needs his "
        "approval; every other action is yours to use. The result carries the "
        "action's own fields. Each action below lists its fields and the ones it "
        "requires. "
        + " ".join(
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
            extra_properties=False,
        ),
        SideEffect.EFFECTFUL,
        tuple(dict.fromkeys((
            *(code for item in contracts for code in item.possible_failure_codes),
            "result_output_invalid",
        ))),
        durable_input_fields=("action", *(name for name in inputs if name not in _NOT_DURABLE)),
        trace_fields=("action",),
        read_only_when=tuple(
            (("action", item.value),) for item in offered if item in READ_ACTIONS
        ),
    )


def build_research_executors(
    actions: Mapping[ResearchAction, Callable[[Any], CapabilityResult]],
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Any], CapabilityResult]]:
    """The one `research` executor over the actions this runtime composed."""
    offered = tuple(item for item in CONTRACTS if item in actions)

    def failed(code: str, reason: str = "") -> CapabilityResult:
        return CapabilityResult(
            call_id_source(), RESEARCH, CapabilityResultState.FAILED,
            failure={"code": code, **({"reason": reason} if reason else {})},
        )

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        """Run one offered action under its own contract (D-074).

        The action's result passes through unwrapped, with its provenance and
        durable values, so a page's retention or a notebook receipt stays
        where it was.
        """
        try:
            action = ResearchAction(arguments.get("action"))
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
        if result.capability_id != RESEARCH:
            result = replace(result, capability_id=RESEARCH)
        if (result.state is not CapabilityResultState.FAILED
                and not contract.output_schema.accepts(result.values)):
            return failed("result_output_invalid")
        return result

    return {RESEARCH: run}


__all__ = [
    "CONTRACTS",
    "READ_ACTIONS",
    "build_model_question",
    "build_research_executors",
    "offered_research_actions",
    "research_definition",
]
