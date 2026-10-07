"""Composition root for the first permanent local voice-to-Core runtime."""

from __future__ import annotations

import argparse
import asyncio
from contextvars import ContextVar
import logging
from uuid import uuid4
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from alx.bootstrap.providers import build_runtime_providers, verify_core_claude_identity
from alx.bootstrap.mail import (
    build_mail_runtime,
    build_mail_send_runtime,
    captured_invoice_filing_scopes,
    mail_post_reply_standing_scopes,
)
from alx.bootstrap.research import build_research_runtime
from alx.bootstrap.sandbox import build_sandbox_runtime
from alx.bootstrap.coding import build_coding_runtime
from alx.bootstrap.pull_request_checks import build_pull_request_checks_runtime
from alx.bootstrap.repository import build_repository_runtime
from alx.bootstrap.repository_authority import build_repository_authority_runtime
from alx.bootstrap.review import build_review_runtime
from alx.bootstrap.tasks import build_task_runtime
from alx.contracts.cognition import CognitionOrigin
from alx.contracts.continuity import CognitionOpportunity
from alx.contracts.task import ExternalTask, TaskState
from alx.contracts.trace import TraceSubsystem
from alx.providers.review_status import subject_reference
from alx.bootstrap.web import build_web_runtime
from alx.bootstrap.autonomous import (
    AutonomousCognitionRunner,
    InputBoundHolds,
    LedgerSpendAuthority,
    OccasionSpendRelay,
)
from alx.bootstrap.continuity import build_continuity_runtime
from alx.contracts.notebook import OPEN_NOTEBOOK_THREAD_LIMIT
from alx.tools import OPEN_THOUGHT_LIMIT, PENDING_REVISIT_LIMIT
from alx.bootstrap.notebook import build_notebook_runtime
from alx.bootstrap.reasoning import (
    OriginSelectedReasoner,
    autonomous_input_ceiling,
    build_model_reasoner,
)
from alx.bootstrap.xero import (
    BILL_EXECUTION_CAPABILITIES,
    BILL_TASK_CAPABILITIES,
    build_supplier_invoice_extractor,
    build_xero_runtime,
)
from alx.bootstrap.dhl import build_dhl_runtime
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.config import (
    merge_settings,
    repository_runtime_settings,
    review_settings,
    autonomous_cognition_daily_budget_usd,
    autonomous_commissioning_limit,
    autonomous_due_check_seconds,
    AUTONOMOUS_MAX_OUTPUT_TOKENS,
    ConfigurationError,
    LiveVoiceSettings,
    LlamaParseSettings,
    ReaderSettings,
    MailSendSettings,
    MailSettings,
    RuntimeSettings,
    XeroSettings,
)
from alx.continuity.completed_work_source import CompletedWorkSource
from alx.continuity.plan_source import PlanAttentionSource, PlanWorkers
from alx.continuity.mail_source import MailCognitionSource
from alx.continuity.occasions import CombinedOccasionSource
from alx.bootstrap.documents import build_document_runtime
from alx.bootstrap.readers import build_reader_runtime
from alx.continuity.runtime_source import RuntimeStartedSource
from alx.contracts.repository_authority import valid_sha
from alx.continuity import (
    DueCognitionSource,
    FutureCognitionSource,
    SQLiteOpportunityLedger,
)
from alx.observability import ConfiguredPricingWorstCase
from alx.providers import MailPoller
from alx.observability.autonomous_budget import SQLiteAutonomousLedger
from alx.conversation import ConversationGateway, ConversationNotFound, SQLiteConversationStore
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.interfaces import (
    LiveVoiceServer,
    VoiceActivityStatus,
    VoiceDiagnosticFeed,
    VoiceSession,
)
from alx.observability import BudgetExceeded, SandboxBudget, SQLiteUsageRecorder
from alx.observability.usage import bill_budget_for
from alx.memories import SQLiteMemoryStore
from alx.safety import AuthorityContext, SafetyGate


def _bill_budget_for_turn(
    settings: RuntimeSettings, autonomous_opportunity_id: str
):
    """Choose the bill ceiling from the provider executing this turn.

    The occasion relay is populated only while the autonomous runner is inside
    its Core turn. Voice and observed-mail turns use the conversational
    reasoner, while a populated occasion uses the separately configured
    autonomous reasoner. An impossible autonomous turn without its configured
    provider gets the strict default rather than the subscription allowance.
    """
    if autonomous_opportunity_id:
        provider = (
            settings.autonomous.provider
            if settings.autonomous is not None
            else ""
        )
    else:
        provider = settings.reasoning.provider
    return bill_budget_for(provider)


LOGGER = logging.getLogger(__name__)


def load_environment(path: Path, inherited: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read simple KEY=VALUE settings while preserving process-level overrides."""
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "").isalnum() or not key[0].isalpha():
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    values.update(inherited if inherited is not None else os.environ)
    return values


def _completed(attempt) -> bool:
    """True only when a capture actually finished its work."""
    result = getattr(attempt, "result", None)
    values = getattr(result, "values", None)
    return bool(values and values.get("completed") is True)


def _coding_repository_root(
    repository_root: Path, authority_root: Path | None
) -> Path | None:
    """Use the checkout AL/X can later publish, recover, and switch.

    No repository authority means no coding: nothing could switch a failed
    job's feature branch back to main, so the process checkout is not a
    fallback.
    """
    if authority_root is None:
        return None
    try:
        if repository_root.resolve() == authority_root.resolve():
            return repository_root.resolve()
    except OSError:
        return None
    return None


def migrate_legacy_conversations(
    goal_store: SQLiteGoalStore,
    conversation_store: SQLiteConversationStore,
) -> None:
    """Move pre-refactor turns once without keeping goals as their owner."""
    grouped: dict[str, list] = {}
    for turn in goal_store.legacy_conversation_turns():
        grouped.setdefault(turn.conversation_id, []).append(turn)
    for conversation_id, turns in grouped.items():
        try:
            conversation_store.load(conversation_id)
            continue
        except ConversationNotFound:
            pass
        related = [
            item for item in goal_store.list_goals()
            if item.conversation_id == conversation_id
        ]
        retention = max(item.retention_until for item in related)
        snapshot = conversation_store.create(conversation_id, retention)
        for turn in turns:
            snapshot = conversation_store.append(turn, retention, snapshot.revision)



def _reviewer_name(review_runtime: Any) -> str:
    """The reviewer a composed runtime actually uses.

    Read from the provider rather than from configuration. Selection accepts
    a name case-insensitively and falls back to the default on an unknown one,
    so the configured string and the composed reviewer can differ — and the
    task store, the observer registry and the poller all have to agree on one
    name or the review is recorded under a service nothing watches.
    """
    provider = getattr(review_runtime, "provider", None)
    return str(getattr(provider, "reviewer", "") or "")


def _watch_review(
    task_runtime: Any,
    conversation_id: str,
    number: int,
    head_sha: str,
    requested_at: datetime,
    reviewer: str,
) -> str:
    """Wait for this requested review through the one task observer.

    Failing to record must not fail the request: the review has already been
    asked for, and losing visibility of it is worse reported than raised.
    """
    if not head_sha:
        return "head_unconfirmed"
    if task_runtime is None:
        return "observer_unavailable"
    try:
        return task_runtime.poller.wait(
            ExternalTask(
                # Two same-second requests are still distinct occasions. A
                # timestamp identifier collided and silently inherited the
                # earlier row's handoff state, so identity is now collision-safe.
                task_id=f"review:{number}:{uuid4().hex}",
                kind="external_review",
                # The configured reviewer, so the watcher and the
                # record name the same one.
                service=reviewer,
                subject_reference=subject_reference(number, head_sha),
                state=TaskState.REQUESTED,
                requested_at=requested_at,
                conversation_id=conversation_id,
            )
        )
    except Exception as error:  # noqa: BLE001 - visibility, not correctness
        LOGGER.warning(
            "A requested review could not be watched: %s", type(error).__name__
        )
        return "observer_unavailable"


# The tree this process imported: `scripts/alx` extracts committed main here,
# so the laws, identity and frontend AL/X runs under are the ones merged with
# her code, never whatever a feature branch in the checkout currently holds.
CODE_ROOT = Path(__file__).resolve().parents[3]


def running_commit_of(code_root: Path, repository_reader: Any) -> str:
    """The commit this process is running.

    scripts/alx runs committed main from a snapshot directory named by its
    commit, so the code root's own name is the answer. The checkout's HEAD
    can be a feature branch by then, so it is only used when the runtime
    runs straight from the checkout.
    """
    if valid_sha(code_root.name):
        return code_root.name
    return "" if repository_reader is None else repository_reader.head_commit()


async def run(repository_root: Path) -> None:
    environment = load_environment(repository_root / ".env")
    provider_settings = RuntimeSettings.from_environment(environment)
    voice_settings = LiveVoiceSettings.from_environment(environment)
    merge_configuration = merge_settings(environment)
    repository_runtime_configuration = repository_runtime_settings(environment)
    review_configuration = review_settings(environment)
    storage_root = voice_settings.storage_root
    if not storage_root.is_absolute():
        storage_root = repository_root / storage_root
    storage_root.mkdir(parents=True, exist_ok=True)

    diagnostics = VoiceDiagnosticFeed()
    activity = VoiceActivityStatus(diagnostics)
    usage = SQLiteUsageRecorder(storage_root / "reasoning-usage.sqlite3")
    # The Core names the conversation on every budget check, so a dispatch can
    # arm the ceiling for the task that is actually running. A context
    # variable, as the call below is: a planned step runs on a background
    # worker while a turn dispatches on another thread, and each must see its
    # own conversation and call, never whichever was named last.
    current_conversation_id: ContextVar[str] = ContextVar(
        "alx_current_conversation_id", default=""
    )

    # Whether the turn now reaching the Core came from Friedl. Set by the
    # transport, which is the only place that knows; the Core is never told
    # which reasoner or which origin is spending, and must not be.
    person_turn_in_progress = [False]

    def budget_check(conversation_id: str) -> None:
        """Stop a runaway task, and convert that stop into bounded recovery.

        A ceiling that only ever raises leaves the conversation deadlocked:
        the Core checkpoints, the transport keeps listening, and every later
        turn re-raises against the same exhausted window, so the next thing
        Friedl says fails before it is heard. Declaring recovery here gives
        the next turns the configured allowance and nothing more. It is
        idempotent, so being stopped repeatedly never buys a further one.

        The allowance is declared only for a person turn. It exists so Friedl
        is not left talking to a conversation that cannot answer, and a
        background turn does not need it: nobody is waiting on one. On
        2026-09-08 the opposite happened - a stopped task checkpointed in a
        millisecond, background turns cycled through the allowance in the gap
        before Friedl typed, and the conversation could no longer reason at
        all. Reserving it is what keeps the recovery for the person it is for.
        """
        current_conversation_id.set(conversation_id)
        try:
            usage.check(
                conversation_id, allow_recovery=person_turn_in_progress[0]
            )
        except BudgetExceeded:
            if person_turn_in_progress[0]:
                usage.enter_recovery(conversation_id)
            raise

    def telemetry(task_id: str, values: Mapping[str, Any]) -> None:
        """Development panel and durable record see the same measurement."""
        diagnostics.publish(task_id, values)
        usage.record(task_id, values)

    providers = build_runtime_providers(provider_settings, telemetry)
    # AL/X Core reasons as her own Claude account. Confirmed before anything is
    # composed or served: a wrong or missing login stops startup here.
    verify_core_claude_identity(providers)
    # The most input an autonomous turn may carry, derived once from the
    # conversational Core's recorded context window, because the autonomous
    # instance is that same Core. Refuses to start on a model with no window.
    autonomous_ceiling = (
        None if providers.autonomous is None
        else autonomous_input_ceiling(
            provider_settings.reasoning.provider,
            provider_settings.reasoning.model,
        )
    )
    goal_store = SQLiteGoalStore(storage_root / "goals.sqlite3")
    conversation_store = SQLiteConversationStore(storage_root / "conversations.sqlite3")
    migrate_legacy_conversations(goal_store, conversation_store)
    memory_store = SQLiteMemoryStore(storage_root / "memories.sqlite3")
    registry = CapabilityRegistry()
    # Which subsystem each capability belongs to, for the operator trace,
    # recorded as each runtime registers its own. Composition is the one place
    # that knows which runtime contributed a capability; nothing infers it.
    capability_subsystems: dict[str, TraceSubsystem] = {}

    def register(definitions: Any, subsystem: TraceSubsystem) -> None:
        for definition in definitions:
            registry.register(definition)
            capability_subsystems[definition.capability_id] = subsystem
    current_call_id: ContextVar[str] = ContextVar("alx_current_call_id", default="")
    # Set only on a planned step's background worker. Such a step belongs to
    # no occasion: whatever occasion is running on the Core thread meanwhile
    # is someone else's, and must not choose its budget.
    planned_dispatch: ContextVar[bool] = ContextVar("alx_planned_dispatch", default=False)

    def bind_planned_dispatch(conversation_id: str) -> None:
        """The whole ambient context a background planned step runs under."""
        current_conversation_id.set(conversation_id)
        planned_dispatch.set(True)
    current_goal_state: ContextVar[Any] = ContextVar(
        "alx_current_goal_state", default=None
    )
    mail_settings = MailSettings.from_environment(environment)
    mail_runtime = build_mail_runtime(
        mail_settings,
        storage_root,
        current_call_id.get,
    )
    register(mail_runtime.definitions, TraceSubsystem.MAIL)
    policies = dict(mail_runtime.policies)
    executors = dict(mail_runtime.executors)
    permissions = set(mail_runtime.permissions)

    def notebook_provenance(source_references, _recorded_at):
        """Resolve cited goal artifacts without copying their evidence.

        A goal snapshot's provenance is a conservative union of the material
        used to produce it. That makes a notebook entry citing any of its
        artifacts inherit D-013 whenever mail contributed. Unknown references
        return None and the notebook capability refuses the write.
        """
        state = current_goal_state.get()
        if state is None:
            return None
        known = {
            *(f"attempt:{item.call.call_id}" for item in state.attempts
              if item.call is not None),
            *(f"evidence:{item.evidence_id}" for item in state.evidence),
            *(f"decision:{item.record_id}" for item in state.decisions),
            *(f"correction:{item.record_id}" for item in state.corrections),
            *(f"progress:{item.record_id}" for item in state.progress),
        }
        if any(reference not in known for reference in source_references):
            return None
        provenance = goal_store.load(state.goal_id).provenance
        if provenance is None:
            return None
        return tuple(provenance for _reference in source_references)

    notebook_runtime = build_notebook_runtime(
        storage_root,
        voice_settings.goal_retention_days,
        current_call_id.get,
        provenance_of=notebook_provenance,
    )
    register(notebook_runtime.definitions, TraceSubsystem.NOTEBOOK)
    policies.update(notebook_runtime.policies)
    executors.update(notebook_runtime.executors)
    permissions.update(notebook_runtime.permissions)

    opportunity_ledger = SQLiteOpportunityLedger(
        storage_root / "cognition-opportunities.sqlite3"
    )
    # D-024: AL/X may ask for another cognition opportunity later. Requests
    # are created and withdrawn durably here; the occasion sources composed
    # below honour them only while an autonomous Core is configured.
    continuity_runtime = build_continuity_runtime(
        storage_root,
        voice_settings.goal_retention_days,
        current_call_id.get,
        # The conversation the executing Core turn belongs to, taken from the
        # runtime rather than from AL/X. A capability argument could name any
        # thread, and a thought would then be able to attach itself to a
        # conversation it did not arise in.
        conversation_id_source=current_conversation_id.get,
        # So she can close an undelivered occasion once she has decided what
        # to do about it. Only she may: nothing expires it.
        occasions=opportunity_ledger,
        # The same condition that enables every occasion source below. When
        # it is false her requests are still kept, and each receipt says no
        # occasion will arise from it in this runtime.
        autonomous_available=providers.autonomous is not None,
    )
    register(continuity_runtime.definitions, TraceSubsystem.THOUGHTS)
    policies.update(continuity_runtime.policies)
    executors.update(continuity_runtime.executors)
    permissions.update(continuity_runtime.permissions)

    # D-024 Phases 2 and 5. The ledgers and the source are constructed here,
    # once, so the paid path is real rather than something that only exists
    # in tests. The due-cognition tick below polls every source; each one
    # produces nothing unless an autonomous Core is configured, which is
    # Friedl's to switch on (EX-001 names the only permitted configuration).
    #
    # The source is enabled only when an autonomous Core is actually
    # configured. Without one an autonomous turn would be refused at the
    # reasoner anyway, so producing occasions nobody can answer would spend a
    # reservation to reach a guaranteed refusal.
    autonomous_budget = SQLiteAutonomousLedger(
        storage_root / "autonomous-cognition-spend.sqlite3",
        autonomous_cognition_daily_budget_usd(environment),
        ConfiguredPricingWorstCase(),
    )
    # Carries what the reasoning boundary spends back to the occasion ledger,
    # so every dollar is inspectable per occasion and not only per day.
    occasion_spend = OccasionSpendRelay()
    cognition_source = FutureCognitionSource(
        continuity_runtime.store,
        opportunity_ledger,
        enabled=providers.autonomous is not None,
    )
    # Restart-safe continuation. A process that stopped between claiming an
    # occasion and recording its outcome left a durable claim behind, and
    # without this the request would stay pending while every later scan
    # skipped it. Recovery reads persisted state only: an occasion with no
    # dispatched reservation cannot have reached the provider and is reclaimed;
    # one that did is retained for inspection rather than replayed.
    reclaimed = cognition_source.recover(autonomous_budget)
    if reclaimed:
        LOGGER.info(
            "Reclaimed %d cognition occasion(s) left claimed by a stopped run",
            len(reclaimed),
        )
    LOGGER.info(
        "Autonomous cognition composed: enabled=%s daily_budget_usd=%.4f",
        cognition_source.enabled,
        autonomous_cognition_daily_budget_usd(environment),
    )

    # Paid research reaches AL/X as one capability through the same broker and
    # safety gate as everything else. It is absent unless a cognition tier is
    # enabled and a budget configured, so a runtime that has not been told it
    # may spend cannot propose a research call at all.
    research_runtime = build_research_runtime(
        provider_settings.research,
        storage_root,
        current_call_id.get,
        telemetry,
    )
    if research_runtime is None:
        LOGGER.info("Research is not enabled: no paid research capability")
    else:
        register(research_runtime.definitions, TraceSubsystem.RESEARCH)
        policies.update(research_runtime.policies)
        executors.update(research_runtime.executors)
        permissions.update(research_runtime.permissions)

    # D-025 authorises reading the public web. It is a separate authority from
    # research spending: this buys no model tokens, and research.spend reaches
    # no network. Absent unless this runtime was told it may read.
    web_runtime = build_web_runtime(
        voice_settings.web_read_enabled,
        current_call_id.get,
        voice_settings.web_search,
        storage_root,
    )
    if web_runtime is not None:
        register(web_runtime.definitions, TraceSubsystem.WEB)
        policies.update(web_runtime.policies)
        executors.update(web_runtime.executors)
        permissions.update(web_runtime.permissions)

    # D-027 authorises isolated experimentation. The sandbox root is kept
    # apart from the runtime storage root, which holds goals, memories and a
    # private key. The repository, that storage root and the user's private
    # keys are denied to the confined process explicitly.
    #
    # A relative sandbox root is resolved against the repository, exactly as
    # the runtime storage root above is. Passed through as configured, it was
    # interpreted against the process working directory: launching the service
    # from elsewhere silently created a different workspace and a different
    # ledger, so a session lost its history and the day's spend started again
    # from zero.
    sandbox_workspace_root = voice_settings.sandbox.workspace_root
    sandbox_ledger_path = voice_settings.sandbox.ledger_path
    if sandbox_workspace_root is not None and not sandbox_workspace_root.is_absolute():
        sandbox_workspace_root = repository_root / sandbox_workspace_root
    if sandbox_ledger_path is not None and not sandbox_ledger_path.is_absolute():
        sandbox_ledger_path = repository_root / sandbox_ledger_path
    sandbox_runtime = build_sandbox_runtime(
        voice_settings.sandbox.is_usable,
        sandbox_workspace_root,
        sandbox_ledger_path,
        current_call_id.get,
        denied_read_paths=(
            repository_root,
            storage_root,
            Path.home() / ".ssh",
        ),
        budget=SandboxBudget(
            voice_settings.sandbox.daily_runs,
            voice_settings.sandbox.daily_wall_seconds,
        ),
    )
    if sandbox_runtime is not None:
        register(sandbox_runtime.definitions, TraceSubsystem.SANDBOX)
        policies.update(sandbox_runtime.policies)
        executors.update(sandbox_runtime.executors)
        permissions.update(sandbox_runtime.permissions)

    # Requesting an external review is effectful and may spend review credits,
    # so its policy requires an approval grounded in Friedl's own turn.
    # Compose the sole waiter before publishing the capabilities. The callback
    # runs only after composition; neither a holder nor a second observer is needed.
    review_runtime = build_review_runtime(
        review_configuration.is_usable,
        review_configuration.repository,
        review_configuration.token,
        current_call_id.get,
        reviewer=review_configuration.reviewer,
        started=lambda number, sha, requested_at: _watch_review(
            task_runtime,
            current_conversation_id.get(),
            number,
            sha,
            requested_at,
            # The resolved reviewer, read from the provider that was actually
            # composed — never the configured string. Selection normalises
            # case and falls back on an unknown name, so `CodeRabbit` and a
            # typo both compose the coderabbit provider while the raw value
            # says otherwise. The watcher registers its observer under the
            # provider's own name, and the poller finds an observer by the
            # task's service: recording the raw value meant a lookup that
            # matched nothing, and a review nobody ever polled.
            _reviewer_name(review_runtime),
        ),
        # Whether an earlier task already took the verdict of this exact
        # head. Asked only when the request joined a finished round, which is
        # the one case where no further review of that head is coming.
        delivered=lambda number, sha, requested_at: (
            task_runtime is not None
            and task_runtime.store.verdict_already_consumed(
                _reviewer_name(review_runtime),
                subject_reference(number, sha),
                requested_at.isoformat(),
            )
        ),
    )
    task_runtime = build_task_runtime(
        storage_root,
        review_configuration.repository,
        review_configuration.token,
        # The frontend dispatches on `code`; a diagnostic without one renders
        # as "Server diagnostic · unknown", which is what the watcher's lines
        # became. The code names the event and the payload carries only
        # identifiers, a state and a duration, as D-012 requires.
        lambda conversation_id, values: diagnostics.publish(
            conversation_id, {"code": "task.status", **values}
        ),
        # Completion is recorded durably by the watcher. Turning it into a
        # Core turn is the completed-work source's job, through the same
        # runner, ledger and lock as every other occasion. Writing an
        # opportunity here instead left a ledger row nothing consumed, so the
        # Core was never woken.
        lambda task: None,
        # The reviewer being watched, reused rather than rebuilt. Without it
        # the watcher is never composed, and a review AL/X successfully
        # requests is recorded nowhere, polled by nothing, and never handed
        # back to her: the request succeeds and the result never arrives.
        review_provider=review_runtime.provider if review_runtime else None,
    )

    if task_runtime is None:
        # Do not spend review credits when completion cannot be watched.
        review_runtime = None
    if review_runtime is not None:
        register(review_runtime.definitions, TraceSubsystem.REVIEW)
        policies.update(review_runtime.policies)
        executors.update(review_runtime.executors)
        permissions.update(review_runtime.permissions)

    # AL/X's repository authority. Hers, never the Coding Agent's: a job
    # commits on the feature branch prepared in the canonical checkout and
    # cannot reach a remote by
    # construction, so what is published, merged, reset or deleted is decided
    # after she has seen what the job produced.
    #
    # One capability carrying one enumerated operation, replacing the narrow
    # publication and canonical-sync capabilities that preceded it. Those were
    # each correct and each was a separate thing to ask for; the surface was
    # the problem rather than the safety, and a capability per verb meant an
    # ordinary git question needed a merge before she could answer it.
    repository_runtime = build_repository_authority_runtime(
        repository_runtime_configuration.is_usable,
        repository_runtime_configuration.root,
        repository_runtime_configuration.repository_identity,
        repository_runtime_configuration.timeout_seconds,
        current_call_id.get,
        merge_configuration.token,
    )
    if repository_runtime is not None:
        register(repository_runtime.definitions, TraceSubsystem.GIT)
        policies.update(repository_runtime.policies)
        executors.update(repository_runtime.executors)
        permissions.update(repository_runtime.permissions)

    # Friedl delegated routine merge authorisation to AL/X. She reads an
    # external review of the current head and decides; this executes that
    # decision against the exact revision she judged.
    merge_runtime = build_repository_runtime(
        merge_configuration.is_usable,
        merge_configuration.repository,
        merge_configuration.token,
        current_call_id.get,
        repository_runtime=repository_runtime,
    )
    if merge_runtime is not None:
        register(merge_runtime.definitions, TraceSubsystem.GITHUB)
        policies.update(merge_runtime.policies)
        executors.update(merge_runtime.executors)
        permissions.update(merge_runtime.permissions)

    # Reading check results is not merging and not requesting a review.
    # Those stay behind their own switches. This read is available whenever
    # the repository and token they already use are configured, and holding
    # it grants neither permission.
    checks_runtime = build_pull_request_checks_runtime(
        merge_configuration.repository,
        merge_configuration.token,
        current_call_id.get,
    )
    if checks_runtime is not None:
        register(checks_runtime.definitions, TraceSubsystem.GITHUB)
        policies.update(checks_runtime.policies)
        executors.update(checks_runtime.executors)
        permissions.update(checks_runtime.permissions)

    # D-028 authorises one bounded coding job in the configured checkout. It is
    # a separate authority from sandbox.execute: the sandbox cannot touch a
    # repository, and this cannot merge, push, deploy or request a review.
    # D-033 fixes that checkout to the canonical repository. The capability
    # creates and switches its feature branch there before implementation.
    # Composed after repository authority and only against its root: a job
    # that fails leaves its feature branch for AL/X to recover and switch
    # back to main, and without that authority nothing could.
    coding_repository = _coding_repository_root(
        repository_root,
        repository_runtime.root if repository_runtime is not None else None,
    )
    if provider_settings.coding.enabled and coding_repository is None:
        LOGGER.warning(
            "Coding checkout lacks repository authority: no coding capability"
        )
    coding_runtime = build_coding_runtime(
        provider_settings.coding.enabled,
        providers.coding,
        current_call_id.get,
        session=providers.coding_session,
        reviewer=providers.coding_reviewer,
        activity_sink=activity.set,
        telemetry_sink=activity.publish_coding,
        repository=coding_repository,
        goal_state_source=current_goal_state.get,
    )
    if coding_runtime is not None:
        register(coding_runtime.definitions, TraceSubsystem.CODING)
        policies.update(coding_runtime.policies)
        executors.update(coding_runtime.executors)
        permissions.update(coding_runtime.permissions)

    # D-016 authorises the narrowly scoped supplier-bill capability. Missing
    # configuration leaves Xero absent without weakening mail or voice.
    # D-038. Reading what a mailed document states needs only mail and
    # LlamaParse, so it is composed apart from Xero.
    try:
        document_runtime = build_document_runtime(
            LlamaParseSettings.from_environment(environment),
            mail_runtime.source,
            current_call_id.get,
        )
    except ConfigurationError as error:
        LOGGER.info("Mail document reading unavailable: %s", error)
        document_runtime = None
    if document_runtime is not None:
        register(document_runtime.definitions, TraceSubsystem.MAIL)
        policies.update(document_runtime.policies)
        executors.update(document_runtime.executors)
        permissions.update(document_runtime.permissions)

    # D-039. The one calendar of BHL room-reader sessions.
    try:
        reader_runtime = build_reader_runtime(
            ReaderSettings.from_environment(environment), storage_root,
            current_call_id.get,
        )
    except ConfigurationError as error:
        LOGGER.info("Reader calendar unavailable: %s", error)
        reader_runtime = None
    if reader_runtime is not None:
        register(reader_runtime.definitions, TraceSubsystem.READERS)
        policies.update(reader_runtime.policies)
        executors.update(reader_runtime.executors)
        permissions.update(reader_runtime.permissions)

    xero_approval_ttl_seconds: int | None = None
    try:
        xero_settings = XeroSettings.from_environment(environment)
    except ConfigurationError as error:
        LOGGER.info("Xero unavailable: %s", error)
    else:
        # Extraction is a bounded LlamaCloud document read. When LlamaParse
        # is not configured the extractor stays absent and capture is not
        # advertised: answering it through the Core or the generic specialist
        # is the expensive path this exists to avoid.
        extractor = None
        try:
            extractor = build_supplier_invoice_extractor(
                LlamaParseSettings.from_environment(environment)
            )
        except ConfigurationError as error:
            LOGGER.info("Supplier invoice extraction unavailable: %s", error)
        xero_runtime = build_xero_runtime(
            xero_settings,
            storage_root,
            mail_runtime.source,
            current_call_id.get,
            extractor,
        )
        register(xero_runtime.definitions, TraceSubsystem.XERO)
        policies.update(xero_runtime.policies)
        executors.update(xero_runtime.executors)
        permissions.update(xero_runtime.permissions)
        xero_approval_ttl_seconds = xero_settings.approval_ttl_seconds

        # A DHL import posts to Xero, so its one capability is built with the
        # same adapter and the same authority as any other bill write.
        dhl_runtime = build_dhl_runtime(
            mail_runtime.source,
            xero_runtime.adapter,
            current_call_id.get,
            xero_settings.import_vat_account,
            xero_settings.customs_duty_account,
            xero_settings.clearance_account,
            xero_settings.dhl_supplier_name,
            xero_settings.unattended_bill_writes,
        )
        register(dhl_runtime.definitions, TraceSubsystem.DHL)
        policies.update(dhl_runtime.policies)
        executors.update(dhl_runtime.executors)
        permissions.update(dhl_runtime.permissions)

    # Replying is authorised by DECISIONS.md D-011 and configured separately, so
    # a runtime without send settings reads mail without being able to send it.
    approval_ttl_seconds: int | None = None
    try:
        send_settings = MailSendSettings.from_environment(environment)
    except ConfigurationError as error:
        LOGGER.info("Mail sending unavailable: %s", error)
    else:
        approval_ttl_seconds = send_settings.approval_ttl_seconds
        send_definitions, send_policies, send_executors, send_permissions = (
            build_mail_send_runtime(
                send_settings, mail_runtime.source, current_call_id.get
            )
        )
        register(send_definitions, TraceSubsystem.MAIL)
        policies.update(send_policies)
        executors.update(send_executors)
        permissions.update(send_permissions)

    broker = CapabilityBroker(
        registry, SafetyGate(policies), executors,
        trace=diagnostics.trace, subsystems=capability_subsystems,
    )

    def dispatch(call, state):
        current_call_id.set(call.call_id)
        goal_state_token = current_goal_state.set(state)
        # Reaching for any bill capability declares the task routine, so the
        # ceiling applies from the first one rather than from the commit.
        if call.capability_id in BILL_TASK_CAPABILITIES:
            # The ceiling counts reasoning calls, so it follows the provider
            # executing this turn. The occasion relay is already the durable
            # boundary between autonomous and conversational turns; no model
            # selection or authority changes here.
            usage.set_budget(
                current_conversation_id.get(),
                _bill_budget_for_turn(
                    provider_settings,
                    "" if planned_dispatch.get()
                    else occasion_spend.current_opportunity_id(),
                ),
            )
        try:
            attempt = broker.dispatch(
                call,
                AuthorityContext(
                    principal_reference=voice_settings.primary_person_id,
                    granted_permission_references=frozenset(permissions),
                    evaluated_at=datetime.now(UTC),
                    approvals=() if state is None else state.approvals,
                    standing_scopes=(
                        *mail_post_reply_standing_scopes(state),
                        *captured_invoice_filing_scopes(state),
                    ),
                ),
            )
        finally:
            current_goal_state.reset(goal_state_token)
        # A finished bill closes its ceiling window, so the next invoice gets
        # its own. Only a completed capture counts: settling after a refusal
        # or a returned ambiguity would hand the same bill a fresh ceiling and
        # let it keep reasoning.
        if call.capability_id in BILL_EXECUTION_CAPABILITIES and _completed(attempt):
            usage.settle(current_conversation_id.get())
        return attempt

    # The shortest configured approval window governs, so a capability cannot
    # inherit a longer one from another integration.
    approval_windows = tuple(
        value for value in (approval_ttl_seconds, xero_approval_ttl_seconds)
        if value is not None
    )

    # EX-001, time-boxed: one Core answers Friedl, another answers a turn
    # nobody asked for. Selection is one expression over CognitionOrigin, here
    # and nowhere else. The origin boundary exists whether or not the
    # experiment is configured: absent an autonomous Core an autonomous turn is
    # refused, never answered by the conversational model, because a silent
    # fallback would spend on a Core nobody selected and record the result as
    # if the experiment had run.
    conversational_reasoner = build_model_reasoner(
        providers.reasoning, CODE_ROOT
    )
    reasoner = OriginSelectedReasoner(
        conversational_reasoner,
        None if providers.autonomous is None
        else build_model_reasoner(
            providers.autonomous,
            CODE_ROOT,
            AUTONOMOUS_MAX_OUTPUT_TOKENS,
            autonomous_ceiling,
            # The bounds and the budget arrive together; ModelReasoner refuses
            # a partial combination, so a bounded autonomous reasoner that
            # could dispatch without withdrawing anything cannot be built.
            LedgerSpendAuthority(
                autonomous_budget,
                provider_settings.autonomous.provider,
                provider_settings.autonomous.model,
                occasion_spend,
                # Links each reservation to the occasion it serves. Without it
                # reservations are stored anonymously and recovery cannot tell
                # a dispatched turn from an undispatched claim, which would let
                # it replay a call that may already have been billed.
                occasion_spend.current_opportunity_id,
            ),
        ),
    )

    # One condition for both halves of D-036's continuation: the Core may
    # install a plan, and a plan may return to her, only together.
    plan_continuation = providers.autonomous is not None
    process_started_at = datetime.now(UTC)
    repository_reader = (
        None if repository_runtime is None else repository_runtime.authority
    )
    running_commit = running_commit_of(CODE_ROOT, repository_reader)

    def runtime_facts() -> dict[str, str]:
        return {
            "started_at": process_started_at.isoformat(timespec="seconds"),
            "running_commit": running_commit,
            "main_commit": (
                "" if repository_reader is None else repository_reader.main_commit()
            ),
        }

    core = CoreAgent(
        goal_store,
        reasoner,
        dispatch,
        registry.list_definitions(),
        memory_store,
        plan_continuation=plan_continuation,
        # A planned step on a background worker names its goal's conversation
        # for the executors, exactly as a reasoning step's budget check does.
        bind_dispatch=bind_planned_dispatch,
        # The operator's live execution trace: each reasoning call's purpose,
        # plan and goal transitions, refusals.
        trace=diagnostics.trace,
        # A background occasion has no person turn; its response reaches the
        # principal, whose relationship memories she is shown on every turn.
        principal_person_id=voice_settings.primary_person_id,
        runtime_facts=runtime_facts,
        approval_ttl_seconds=min(approval_windows) if approval_windows else None,
        budget_check=budget_check,
        # Read from the policies themselves, so a capability that requires an
        # approval grounded in Friedl's turn is bound to one action per turn
        # without anything naming it here. Adding such a capability later
        # inherits the rule; nothing has to remember to list it.
        #
        # A policy that allows a standing scope is excluded. Those are
        # authorised by a scope that stays valid across turns rather than by
        # what Friedl just said, so one instruction does not spend them:
        # binding them stopped mail cleanup after a single message, refusing
        # the second trash or mark-seen of a turn whose authority was never
        # the turn in the first place.
        turn_bound_capabilities=frozenset(
            capability_id
            for capability_id, policy in policies.items()
            if policy.approval_required and not policy.standing_scope_allowed
        ),
        # The other half of the same reading: capabilities that reach outside
        # but need only permission. Without it the Core cannot tell that an
        # approval was never required, so a volunteered one is validated
        # against Friedl's latest turn and a background read is refused.
        approval_free_capabilities=frozenset(
            capability_id
            for capability_id, policy in policies.items()
            if not policy.approval_required
        ),
        # One bounded, recency-ordered list, from the one continuity store,
        # for every turn. There is deliberately no separate assembly for an
        # unprompted turn: a second builder would decide what she is like when
        # nobody is watching.
        open_thoughts=lambda: continuity_runtime.store.open_thoughts(
            OPEN_THOUGHT_LIMIT
        ),
        # Her pending revisits, bounded the same way, for every turn. So she
        # can see what she already asked to come back to, and withdraw what
        # another revisit or finished work has made unnecessary.
        pending_revisits=lambda: continuity_runtime.store.pending()[
            :PENDING_REVISIT_LIMIT
        ],
        # Her open enquiries, from the one notebook store. Context only: a
        # thread never creates an occasion, and nothing here schedules a
        # return to one. Continuity of interest; opportunity stays hers to
        # ask for through request_future_cognition.
        open_notebook_threads=lambda: notebook_runtime.store.open_threads(
            OPEN_NOTEBOOK_THREAD_LIMIT
        ),
        undelivered_responses=lambda: opportunity_ledger.undelivered(),
        # A refused goal proposal left no trace on 2026-09-04, so a live
        # rejection could not be diagnosed. Mechanical facts to the log only:
        # which references were cited, and which rule refused them.
        record_goal_rejection=lambda record: LOGGER.info(
            "Goal proposal rejected: %s", record,
        ),
    )
    gateway = ConversationGateway(
        core,
        conversation_store,
        contextual_events=mail_runtime.source.contextual_events,
    )
    # One Core-turn lock for the whole runtime. Person turns and autonomous
    # turns serialize through this same object: AL/X has one Core, so she has
    # one turn at a time regardless of what caused the turn.
    core_turn_lock = asyncio.Lock()
    session = VoiceSession(
        gateway,
        providers.speech_to_text,
        providers.text_to_speech,
        voice_settings.primary_person_id,
        voice_settings.core_step_budget,
        voice_settings.goal_retention_days,
        diagnostics=diagnostics,
        core_turn_lock=core_turn_lock,
        turn_origin_sink=lambda person: person_turn_in_progress.__setitem__(0, person),
        activity=activity,
    )
    server = LiveVoiceServer(
        session,
        voice_settings.host,
        voice_settings.port,
        provider_settings.speech_to_text.sample_rate_hz,
        CODE_ROOT / "src/alx/interfaces/assets",
    )
    # Every kind of occasion reaches the Core through one producer, one
    # runner and one tick. A finished external task joins the matured requests
    # here rather than bringing a second tick, which would be a competing
    # production path to the same outcome.
    occasion_sources: list[Any] = [cognition_source]
    # A plan that needs her is offered through the same runner. Its
    # attention, offers and cap live on the plan; when automatic offers are
    # exhausted it is announced on the console as a structural notice, which
    # costs no reasoning call. There is no startup recovery: the first tick
    # reconciles every plan exactly as every later tick does.
    plan_source = PlanAttentionSource(
        goal_store, opportunity_ledger, enabled=plan_continuation,
        notify=lambda conversation_id, values: diagnostics.publish(
            conversation_id, {"code": "plan.attention", **values}
        ),
        # An offer is paid only if the spend ledger recorded that it reached
        # the provider; a budget stop or a disabled reasoner costs nothing.
        spend=autonomous_budget,
        ready=core.plan_evidence_ready,
    )
    occasion_sources.append(plan_source)
    plan_workers = PlanWorkers(
        core, core_turn_lock, plan_source,
        # The one capability with its own cancel: a running coding job.
        cancel_dispatch=(
            None if coding_runtime is None
            else lambda job: coding_runtime.agent.cancel(job.call.call_id)
        ),
        reconcile_replies=gateway.reconcile_plan_announcements,
    )
    # Observed mail joins them for the same reason, and to end the same
    # coupling the due-cognition tick was built to avoid. Mail used to reach
    # the Core only through a generator a live voice session drained, so
    # whether AL/X could think about a message depended on whether a browser
    # was open. Watching the mailbox was already a property of the process;
    # now thinking about what it found is too.
    # Each message continues the thread its own identifier headers name, so
    # unrelated correspondence does not share a history, unfinished goals or
    # autonomous responses. The producer derives that per observation; nothing
    # here chooses a thread.
    mail_cognition_source = MailCognitionSource(
        mail_runtime.source,
        opportunity_ledger,
        enabled=providers.autonomous is not None,
    )
    # The same restart-safe recovery its siblings get. A claim left behind by a
    # stopped run would hide an observation that had already arrived, and
    # nothing would ever raise it again. Done before the runner starts, so no
    # occasion is offered from a half-recovered ledger.
    reclaimed_mail = mail_cognition_source.recover(autonomous_budget)
    if reclaimed_mail:
        LOGGER.info(
            "Reclaimed %d mail occasion(s) left claimed by a stopped run",
            len(reclaimed_mail),
        )
    occasion_sources.append(mail_cognition_source)
    if task_runtime is not None:
        completed_work_source = CompletedWorkSource(
            task_runtime.store,
            opportunity_ledger,
            enabled=providers.autonomous is not None,
        )
        # Finished external work needs the same restart-safe recovery the
        # matured requests get, and for a sharper reason: a claim left behind
        # by a stopped run would hide a result that had already arrived, and
        # nothing would ever raise it again. Done before the runner starts, so
        # no occasion is offered from a half-recovered ledger.
        reclaimed_work = completed_work_source.recover(autonomous_budget)
        if reclaimed_work:
            LOGGER.info(
                "Reclaimed %d completed-work occasion(s) left claimed"
                " by a stopped run",
                len(reclaimed_work),
            )
        occasion_sources.append(completed_work_source)
    # One occasion for this process starting, so work held "until a restart"
    # resumes without Friedl having to prompt it.
    runtime_started_source = RuntimeStartedSource(
        opportunity_ledger, process_started_at,
        enabled=providers.autonomous is not None,
    )
    runtime_started_source.recover(autonomous_budget)
    occasion_sources.append(runtime_started_source)
    occasion_source: Any = (
        occasion_sources[0]
        if len(occasion_sources) == 1
        else CombinedOccasionSource(*occasion_sources)
    )

    # Occasions too large for the autonomous input bound are held durably and
    # released when the bound changes or the retry interval passes, rather
    # than rebuilt and refused on every tick.
    autonomous_holds = (
        None if autonomous_ceiling is None
        else InputBoundHolds(opportunity_ledger, autonomous_ceiling)
    )
    autonomous_runner = AutonomousCognitionRunner(
        occasion_source,
        opportunity_ledger,
        gateway,
        voice_settings.core_step_budget,
        voice_settings.goal_retention_days,
        response_transport=server,
        spend_observer=occasion_spend,
        commissioning_limit=autonomous_commissioning_limit(environment),
        holds=autonomous_holds,
        trace=diagnostics.trace,
    )
    due_cognition = DueCognitionSource(
        occasion_source,
        autonomous_runner,
        core_turn_lock,
        autonomous_due_check_seconds(environment),
        reopen=None if autonomous_holds is None else autonomous_holds.reopen,
        advance_plans=plan_workers.advance,
    )
    # Watching the mailbox is not a property of whether Friedl has a browser
    # open, so the scan lives here beside the transport rather than inside a
    # voice exchange. It is purely mechanical: it discovers, advances the
    # cursor and reconciles into durable state, and makes no Core call. What
    # it finds reaches AL/X through the one delivery path a session already
    # owns.

    mail_store_lock = asyncio.Lock()
    mail_poller = MailPoller(
        mail_runtime.source,
        mail_settings.poll_seconds,
        mail_store_lock,
    )
    try:
        async with asyncio.TaskGroup() as runtime_tasks:
            runtime_tasks.create_task(server.serve_forever())
            runtime_tasks.create_task(due_cognition.run())
            runtime_tasks.create_task(mail_poller.run())
            if task_runtime is not None:
                runtime_tasks.create_task(task_runtime.poller.run())
            if sandbox_runtime is not None:
                # D-027's retention deadline is a property of time passing, not
                # of anything happening. Swept only at composition and before
                # each experiment, an idle runtime kept the last session's
                # bytes indefinitely.
                runtime_tasks.create_task(sandbox_runtime.retention.run())
    finally:
        # Planned steps run outside the Core lock, so the lock alone cannot
        # say they are done. Stop starting them and wait for each started one
        # to record its result before anything below closes a store.
        await plan_workers.stop()
        # Cancelling the producer does not stop work already running inside
        # asyncio.to_thread: the coroutine unwinds while the worker keeps going.
        # Closing the stores here would then pull SQLite connections out from
        # under a Core turn mid-write. Taking the shared lock waits for whatever
        # critical section is executing to reach its own durable boundary, and
        # nothing new can start because the producer is already cancelled.
        async with core_turn_lock:
            pass
        # The same wait for the mail scan: cancelling its task does not stop a
        # scan already running on a worker thread, and closing the observation
        # store under one would pull SQLite out from beneath a write.
        async with mail_store_lock:
            pass
        mail_runtime.observations.close()
        conversation_store.close()
        memory_store.close()
        notebook_runtime.store.close()
        if web_runtime is not None:
            web_runtime.provider.close()
            if web_runtime.searcher is not None:
                web_runtime.searcher.close()
        goal_store.close()


def main(argv: list[str] | None = None) -> None:
    # The checkout is named, not inferred from this file: the launcher runs
    # code extracted from committed main, which lives outside the checkout
    # whose .env, storage and repository authority the runtime serves.
    parser = argparse.ArgumentParser(prog="alx.bootstrap.live_voice")
    parser.add_argument("--checkout", type=Path, required=True)
    repository_root = parser.parse_args(argv).checkout.resolve()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(repository_root))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
