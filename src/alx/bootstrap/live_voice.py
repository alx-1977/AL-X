"""Composition root for the first permanent local voice-to-Core runtime."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from alx.bootstrap.providers import build_runtime_providers
from alx.bootstrap.mail import (
    build_mail_runtime,
    build_mail_send_runtime,
    captured_invoice_filing_scopes,
    mail_post_reply_standing_scopes,
)
from alx.bootstrap.research import build_research_runtime
from alx.bootstrap.autonomous import LedgerSpendAuthority, OccasionSpendRelay
from alx.bootstrap.continuity import build_continuity_runtime
from alx.tools import OPEN_THOUGHT_LIMIT
from alx.bootstrap.notebook import build_notebook_runtime
from alx.bootstrap.reasoning import OriginSelectedReasoner, build_model_reasoner
from alx.bootstrap.xero import (
    BILL_EXECUTION_CAPABILITIES,
    BILL_TASK_CAPABILITIES,
    build_xero_runtime,
)
from alx.bootstrap.dhl import build_dhl_runtime
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.config import (
    AUTONOMOUS_MAX_INPUT_TOKENS,
    autonomous_cognition_daily_budget_usd,
    AUTONOMOUS_MAX_OUTPUT_TOKENS,
    ConfigurationError,
    LiveVoiceSettings,
    MailSendSettings,
    MailSettings,
    RuntimeSettings,
    XeroSettings,
)
from alx.continuity import (
    FutureCognitionSource,
    SQLiteOpportunityLedger,
)
from alx.observability import ConfiguredPricingWorstCase
from alx.observability.autonomous_budget import SQLiteAutonomousLedger
from alx.conversation import ConversationGateway, ConversationNotFound, SQLiteConversationStore
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.interfaces import LiveVoiceServer, VoiceDiagnosticBuffer, VoiceSession
from alx.observability import BudgetExceeded, XERO_BILL_BUDGET, SQLiteUsageRecorder
from alx.specialists import ModelSpecialist, extract_invoice
from alx.memories import SQLiteMemoryStore
from alx.safety import AuthorityContext, SafetyGate


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


async def run(repository_root: Path) -> None:
    environment = load_environment(repository_root / ".env")
    provider_settings = RuntimeSettings.from_environment(environment)
    voice_settings = LiveVoiceSettings.from_environment(environment)
    storage_root = voice_settings.storage_root
    if not storage_root.is_absolute():
        storage_root = repository_root / storage_root
    storage_root.mkdir(parents=True, exist_ok=True)

    diagnostics = VoiceDiagnosticBuffer()
    usage = SQLiteUsageRecorder(storage_root / "reasoning-usage.sqlite3")
    # The Core names the conversation on every budget check, so a dispatch can
    # arm the ceiling for the task that is actually running.
    current_conversation_id = [""]

    def budget_check(conversation_id: str) -> None:
        """Stop a runaway task, and convert that stop into bounded recovery.

        A ceiling that only ever raises leaves the conversation deadlocked:
        the Core checkpoints, the transport keeps listening, and every later
        turn re-raises against the same exhausted window, so the next thing
        Friedl says fails before it is heard. Declaring recovery here gives
        the next turns the configured allowance and nothing more. It is
        idempotent, so being stopped repeatedly never buys a further one.
        """
        current_conversation_id[0] = conversation_id
        try:
            usage.check(conversation_id)
        except BudgetExceeded:
            usage.enter_recovery(conversation_id)
            raise

    def telemetry(task_id: str, values: Mapping[str, Any]) -> None:
        """Development panel and durable record see the same measurement."""
        diagnostics.publish(task_id, values)
        usage.record(task_id, values)

    providers = build_runtime_providers(provider_settings, telemetry)
    goal_store = SQLiteGoalStore(storage_root / "goals.sqlite3")
    conversation_store = SQLiteConversationStore(storage_root / "conversations.sqlite3")
    migrate_legacy_conversations(goal_store, conversation_store)
    memory_store = SQLiteMemoryStore(storage_root / "memories.sqlite3")
    registry = CapabilityRegistry()
    current_call_id = [""]
    current_goal_state: ContextVar[Any] = ContextVar(
        "alx_current_notebook_goal_state", default=None
    )
    mail_runtime = build_mail_runtime(
        MailSettings.from_environment(environment),
        storage_root,
        lambda: current_call_id[0],
    )
    for definition in mail_runtime.definitions:
        registry.register(definition)
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
        lambda: current_call_id[0],
        provenance_of=notebook_provenance,
    )
    for definition in notebook_runtime.definitions:
        registry.register(definition)
    policies.update(notebook_runtime.policies)
    executors.update(notebook_runtime.executors)
    permissions.update(notebook_runtime.permissions)

    # D-024: AL/X may ask for another cognition opportunity later. Phase 4
    # creates and withdraws those requests durably; nothing honours them until
    # the opportunity source exists, so this is inert on its own.
    continuity_runtime = build_continuity_runtime(
        storage_root,
        voice_settings.goal_retention_days,
        lambda: current_call_id[0],
    )
    for definition in continuity_runtime.definitions:
        registry.register(definition)
    policies.update(continuity_runtime.policies)
    executors.update(continuity_runtime.executors)
    permissions.update(continuity_runtime.permissions)

    # D-024 Phases 2 and 5, composed but inert. The ledgers and the source are
    # constructed here, once, so the paid path is real rather than something
    # that only exists in tests. Nothing polls the source and nothing calls
    # run_due(): activation is Phase 8 and is Friedl's to switch on.
    #
    # The source is enabled only when an autonomous Core is actually
    # configured. Without one an autonomous turn would be refused at the
    # reasoner anyway, so producing occasions nobody can answer would spend a
    # reservation to reach a guaranteed refusal.
    opportunity_ledger = SQLiteOpportunityLedger(
        storage_root / "cognition-opportunities.sqlite3"
    )
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
        lambda: current_call_id[0],
        telemetry,
    )
    if research_runtime is None:
        LOGGER.info("Research is not enabled: no paid research capability")
    else:
        for definition in research_runtime.definitions:
            registry.register(definition)
        policies.update(research_runtime.policies)
        executors.update(research_runtime.executors)
        permissions.update(research_runtime.permissions)

    # D-016 authorises the narrowly scoped supplier-bill capability. Missing
    # configuration leaves Xero absent without weakening mail or voice.
    xero_approval_ttl_seconds: int | None = None
    try:
        xero_settings = XeroSettings.from_environment(environment)
    except ConfigurationError as error:
        LOGGER.info("Xero unavailable: %s", error)
    else:
        # Extraction is a bounded question, so it goes to a specialist with
        # its own model and reasoning effort. When no specialist is configured
        # the extractor stays absent and capture refuses: answering it through
        # the Core is the expensive path this exists to avoid.
        specialist = (
            None if providers.specialist is None
            else ModelSpecialist(providers.specialist)
        )
        xero_runtime = build_xero_runtime(
            xero_settings,
            storage_root,
            mail_runtime.source,
            lambda: current_call_id[0],
            None if specialist is None
            else (
                lambda text, context_line: extract_invoice(
                    specialist, text, context_line
                )
            ),
        )
        for definition in xero_runtime.definitions:
            registry.register(definition)
        policies.update(xero_runtime.policies)
        executors.update(xero_runtime.executors)
        permissions.update(xero_runtime.permissions)
        xero_approval_ttl_seconds = xero_settings.approval_ttl_seconds

        # A DHL import posts to Xero, so its one capability is built with the
        # same adapter and the same authority as any other bill write.
        dhl_runtime = build_dhl_runtime(
            mail_runtime.source,
            xero_runtime.adapter,
            lambda: current_call_id[0],
            xero_settings.import_vat_account,
            xero_settings.customs_duty_account,
            xero_settings.clearance_account,
            xero_settings.dhl_supplier_name,
            xero_settings.unattended_bill_writes,
        )
        for definition in dhl_runtime.definitions:
            registry.register(definition)
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
                send_settings, mail_runtime.source, lambda: current_call_id[0]
            )
        )
        for definition in send_definitions:
            registry.register(definition)
        policies.update(send_policies)
        executors.update(send_executors)
        permissions.update(send_permissions)

    broker = CapabilityBroker(registry, SafetyGate(policies), executors)

    def dispatch(call, state):
        current_call_id[0] = call.call_id
        goal_state_token = current_goal_state.set(state)
        # Reaching for any bill capability declares the task routine, so the
        # ceiling applies from the first one rather than from the commit.
        if call.capability_id in BILL_TASK_CAPABILITIES:
            usage.set_budget(current_conversation_id[0], XERO_BILL_BUDGET)
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
            usage.settle(current_conversation_id[0])
        return attempt

    # EX-001, time-boxed: one Core answers Friedl, another answers a turn
    # nobody asked for. Selection is one expression over CognitionOrigin, here
    # and nowhere else. The origin boundary exists whether or not the
    # experiment is configured: absent an autonomous Core an autonomous turn is
    # refused, never answered by the conversational model, because a silent
    # fallback would spend on a Core nobody selected and record the result as
    # if the experiment had run.
    conversational_reasoner = build_model_reasoner(
        providers.reasoning, repository_root
    )
    reasoner = OriginSelectedReasoner(
        conversational_reasoner,
        None if providers.autonomous is None
        else build_model_reasoner(
            providers.autonomous,
            repository_root,
            AUTONOMOUS_MAX_OUTPUT_TOKENS,
            AUTONOMOUS_MAX_INPUT_TOKENS,
            # The bounds and the budget arrive together; ModelReasoner refuses
            # a partial combination, so a bounded autonomous reasoner that
            # could dispatch without withdrawing anything cannot be built.
            LedgerSpendAuthority(
                autonomous_budget,
                provider_settings.autonomous.provider,
                provider_settings.autonomous.model,
                occasion_spend,
            ),
        ),
    )

    core = CoreAgent(
        goal_store,
        reasoner,
        dispatch,
        registry.list_definitions(),
        memory_store,
        approval_ttl_seconds=min(approval_windows) if approval_windows else None,
        budget_check=budget_check,
        # One bounded, recency-ordered list, from the one continuity store,
        # for every turn. There is deliberately no separate assembly for an
        # unprompted turn: a second builder would decide what she is like when
        # nobody is watching.
        open_thoughts=lambda: continuity_runtime.store.open_thoughts(
            OPEN_THOUGHT_LIMIT
        ),
    )
    gateway = ConversationGateway(
        core,
        conversation_store,
        contextual_events=mail_runtime.source.contextual_events,
    )
    session = VoiceSession(
        gateway,
        providers.speech_to_text,
        providers.text_to_speech,
        voice_settings.primary_person_id,
        voice_settings.core_step_budget,
        voice_settings.goal_retention_days,
        diagnostics=diagnostics,
        event_source=mail_runtime.source,
    )
    server = LiveVoiceServer(
        session,
        voice_settings.host,
        voice_settings.port,
        provider_settings.speech_to_text.sample_rate_hz,
        repository_root / "src/alx/interfaces/assets",
    )
    try:
        await server.serve_forever()
    finally:
        mail_runtime.observations.close()
        conversation_store.close()
        memory_store.close()
        notebook_runtime.store.close()
        goal_store.close()


def main() -> None:
    repository_root = Path(__file__).resolve().parents[3]
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
