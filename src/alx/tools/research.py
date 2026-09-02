"""One language-blind primitive for putting a bounded research question.

Research is reached the way every other capability is: AL/X proposes a
structured call, the broker validates it, the safety gate authorises it, and
the executor runs it. There is no direct entry point, because a second way in
would be a second production path to the same outcome.

The capability spends money, so it carries its own permission. Everything about
what a research call may cost is enforced inside ResearchSpecialist; nothing
here chooses a model, a tier or a budget.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    Cognition,
    ResearchQuestion,
    SideEffect,
    SpecialistQuestion,
    StructuredSchema,
    ValueKind,
)


ASK_RESEARCH_QUESTION = "ask_research_question"

_STRING = StructuredSchema(ValueKind.STRING)

_FAILURES = (
    "arguments_unusable",
    # The tier AL/X chose is not enabled in this runtime.
    "cognition_tier_unconfigured",
    # The day's budget cannot cover another request.
    "research_budget_exhausted",
    # A model with no configured price, or whose worst case exceeds the ceiling.
    "research_model_unavailable",
    # A provider charged more than its enforced bound allowed; research stops.
    "research_ceiling_failed",
    "provider_failed",
)

DEFINITION = CapabilityDefinition(
    ASK_RESEARCH_QUESTION,
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
        "ResearchModelUnpriced": "research_model_unavailable",
        "ResearchModelUnbounded": "research_model_unavailable",
        "ResearchInputUnbounded": "arguments_unusable",
        "ResearchCeilingFailed": "research_ceiling_failed",
    }.get(type(error).__name__, "provider_failed")


def build_research_executors(
    researcher: Any,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Any], CapabilityResult]]:
    """Bind the one research primitive to the one prepaid specialist."""

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
                ASK_RESEARCH_QUESTION,
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
                ASK_RESEARCH_QUESTION,
                CapabilityResultState.FAILED,
                failure={"code": code},
            )
        finding = answer.get("finding") if isinstance(answer, Mapping) else None
        if not isinstance(finding, str) or not finding.strip():
            return CapabilityResult(
                call_id,
                ASK_RESEARCH_QUESTION,
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
            ASK_RESEARCH_QUESTION,
            CapabilityResultState.SUCCEEDED,
            values,
            # The full answer is available to the Core for the next decision,
            # but the goal store is not the research notebook. Durable
            # continuity is created through record_research_entry.
            durable_values={},
        )

    return {ASK_RESEARCH_QUESTION: ask}
