"""Tests address the one `research` capability by action (D-074)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import RESEARCH, ApprovalScope, CapabilityCall, ResearchAction  # noqa: E402
from alx.tools.notebook import build_notebook_executors  # noqa: E402
from alx.tools.research import (  # noqa: E402
    build_model_question,
    build_research_executors,
    research_definition,
)
from alx.tools.web import build_page_reader, build_web_searcher  # noqa: E402

READ_PAGE = ResearchAction.READ_PAGE.value
SEARCH_WEB = ResearchAction.SEARCH_WEB.value
ASK_MODEL = ResearchAction.ASK_MODEL.value
OPEN_THREAD = ResearchAction.OPEN_THREAD.value
RECORD_ENTRY = ResearchAction.RECORD_ENTRY.value
REVISE_ENTRY = ResearchAction.REVISE_ENTRY.value
CORRECT_ENTRY = ResearchAction.CORRECT_ENTRY.value
SEARCH_NOTEBOOK = ResearchAction.SEARCH_NOTEBOOK.value
READ_THREAD = ResearchAction.READ_THREAD.value
SET_STATUS = ResearchAction.SET_STATUS.value
DELETE = ResearchAction.DELETE.value

# The notebook actions, in the order the capability offers them.
NOTEBOOK = tuple(__import__("alx.tools.notebook", fromlist=["CONTRACTS"]).CONTRACTS)


def definition(*actions: ResearchAction | str):
    """The one capability offering these actions (all when none are named)."""
    return research_definition(tuple(ResearchAction(item) for item in actions) or tuple(ResearchAction))


def research_arguments(action: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"action": action, **(arguments or {})}


def research_call(call_id: str, action: str, arguments: Mapping[str, Any] | None = None,
                  approval_id: str | None = None) -> CapabilityCall:
    return CapabilityCall(call_id, RESEARCH, research_arguments(action, arguments), approval_id)


def research_scope(action: str, arguments: Mapping[str, Any]) -> ApprovalScope:
    return ApprovalScope(RESEARCH, research_arguments(action, arguments))


def is_research(call: Any, action: str) -> bool:
    return (call is not None and call.capability_id == RESEARCH
            and call.arguments.get("action") == action)


def _addressed(run: Callable, action: str) -> Callable[[Mapping[str, Any]], Any]:
    return lambda arguments: run({"action": action, **arguments})


def page_reader(fetcher: Any, call_id_source: Callable[[], str]) -> Callable:
    """The read_page action, called through the one capability."""
    run = build_research_executors(
        {ResearchAction.READ_PAGE: build_page_reader(fetcher, call_id_source)}, call_id_source
    )[RESEARCH]
    return _addressed(run, READ_PAGE)


def web_searcher(searcher: Any, ledger: Any, call_id_source: Callable[[], str],
                 provider_name: str) -> Callable:
    """The search_web action, called through the one capability."""
    run = build_research_executors(
        {ResearchAction.SEARCH_WEB: build_web_searcher(searcher, ledger, call_id_source, provider_name)},
        call_id_source,
    )[RESEARCH]
    return _addressed(run, SEARCH_WEB)


def model_question(researcher: Any, call_id_source: Callable[[], str]) -> Callable:
    """The ask_model action, called through the one capability."""
    run = build_research_executors(
        {ResearchAction.ASK_MODEL: build_model_question(researcher, call_id_source)}, call_id_source
    )[RESEARCH]
    return _addressed(run, ASK_MODEL)


def notebook(store: Any, retention_days: int, call_id_source: Callable[[], str],
             **options: Any) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """Every notebook action, each called through the one capability, by action name."""
    run = build_research_executors(
        build_notebook_executors(store, retention_days, call_id_source, **options), call_id_source
    )[RESEARCH]
    return {item.value: _addressed(run, item.value) for item in NOTEBOOK}


# Settings under which no paid question is built: no tier is enabled.
NO_PAID_RESEARCH = __import__("types").SimpleNamespace(enabled_tiers=frozenset())


def research_runtime(storage_root: Any, call_id_source: Callable[[], str], *,
                     settings: Any = NO_PAID_RESEARCH, retention_days: int = 3650,
                     web_read_enabled: bool = False, **options: Any):
    """The composed runtime as start-up builds it; the notebook only, by default."""
    from alx.bootstrap.research import build_research_runtime
    return build_research_runtime(settings, Path(storage_root), call_id_source,
                                  retention_days=retention_days,
                                  web_read_enabled=web_read_enabled, **options)
