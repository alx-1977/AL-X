"""Compose the D-039 reader calendar, or leave it unavailable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from alx.config import ReaderSettings
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers.behaviorlive import BehaviorLiveConfig
from alx.providers.particle import ParticleCloud
from alx.providers.reader_calendar import SQLiteReaderCalendar
from alx.safety import AuthorityPolicy
from alx.tools.readers import DEFINITIONS, SEND_READER_SCHEDULE, build_reader_executors


# D-039. Reading readers and their schedules is its own permission.
READER_READ_PERMISSION = "readers.read"
# D-040. Sending a reader its schedule is another, without per-send approval:
# Friedl authorised whatever AL/X needs to keep the readers on the right events.
READER_SEND_PERMISSION = "readers.send"


@dataclass(frozen=True, slots=True)
class ReaderRuntime:
    calendar: SQLiteReaderCalendar
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_reader_runtime(
    settings: ReaderSettings,
    storage_root: Path,
    call_id_source: Callable[[], str],
) -> ReaderRuntime | None:
    if not settings.is_usable:
        return None
    calendar = SQLiteReaderCalendar(storage_root / "reader-calendar.sqlite3")
    read = AuthorityPolicy(frozenset({READER_READ_PERMISSION}))
    send = AuthorityPolicy(frozenset({READER_SEND_PERMISSION}))
    particle = ParticleCloud(settings.access_token, settings.timeout_seconds)
    return ReaderRuntime(
        calendar=calendar,
        definitions=DEFINITIONS,
        policies={
            definition.capability_id: send if definition.capability_id == SEND_READER_SCHEDULE else read
            for definition in DEFINITIONS
        },
        executors=build_reader_executors(
            particle,
            BehaviorLiveConfig(settings.config_url, settings.timeout_seconds),
            calendar,
            settings.product_ids,
            call_id_source,
            control=particle,
        ),
        permissions=frozenset({READER_READ_PERMISSION, READER_SEND_PERMISSION}),
    )
