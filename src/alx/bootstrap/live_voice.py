"""Composition root for the first permanent local voice-to-Core runtime."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from alx.bootstrap.providers import build_runtime_providers
from alx.bootstrap.reasoning import build_model_reasoner
from alx.capabilities import CapabilityBroker, CapabilityRegistry
from alx.config import LiveVoiceSettings, RuntimeSettings
from alx.contracts import GoalStatus
from alx.conversation import ConversationGateway
from alx.core import CoreAgent
from alx.goals import SQLiteGoalStore
from alx.interfaces import LiveVoiceServer, VoiceSession
from alx.memories import SQLiteMemoryStore
from alx.safety import AuthorityContext, SafetyGate


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


def locate_active_goal(store: SQLiteGoalStore, conversation_id: str) -> str | None:
    """Select the latest Core-active goal using durable state, never wording."""
    candidates = [
        snapshot
        for snapshot in store.list_goals()
        if snapshot.turns
        and snapshot.turns[-1].conversation_id == conversation_id
        and snapshot.state.status in (GoalStatus.ACTIVE, GoalStatus.AWAITING_INPUT)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.turns[-1].occurred_at).state.goal_id


async def run(repository_root: Path) -> None:
    environment = load_environment(repository_root / ".env")
    provider_settings = RuntimeSettings.from_environment(environment)
    voice_settings = LiveVoiceSettings.from_environment(environment)
    storage_root = voice_settings.storage_root
    if not storage_root.is_absolute():
        storage_root = repository_root / storage_root
    storage_root.mkdir(parents=True, exist_ok=True)

    providers = build_runtime_providers(provider_settings)
    goal_store = SQLiteGoalStore(storage_root / "goals.sqlite3")
    memory_store = SQLiteMemoryStore(storage_root / "memories.sqlite3")
    registry = CapabilityRegistry()
    broker = CapabilityBroker(registry, SafetyGate({}), {})

    def dispatch(call, state):
        return broker.dispatch(
            call,
            AuthorityContext(
                principal_reference=voice_settings.primary_person_id,
                granted_permission_references=frozenset(),
                evaluated_at=datetime.now(UTC),
                approvals=state.approvals,
            ),
        )

    core = CoreAgent(
        goal_store,
        build_model_reasoner(providers.reasoning, repository_root),
        dispatch,
        registry.list_definitions(),
        memory_store,
    )
    gateway = ConversationGateway(
        core,
        lambda conversation_id: locate_active_goal(goal_store, conversation_id),
    )
    session = VoiceSession(
        gateway,
        providers.speech_to_text,
        providers.text_to_speech,
        voice_settings.primary_person_id,
        voice_settings.core_step_budget,
        voice_settings.goal_retention_days,
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
        memory_store.close()
        goal_store.close()


def main() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    try:
        asyncio.run(run(repository_root))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
