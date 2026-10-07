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
from alx.tools.readers import DEFINITIONS, build_reader_executors


# D-039. Reading readers and their schedules is its own permission; device
# actions, when they come, will be another.
READER_READ_PERMISSION = "readers.read"


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
    policy = AuthorityPolicy(frozenset({READER_READ_PERMISSION}))
    return ReaderRuntime(
        calendar=calendar,
        definitions=DEFINITIONS,
        policies={definition.capability_id: policy for definition in DEFINITIONS},
        executors=build_reader_executors(
            ParticleCloud(settings.access_token, settings.timeout_seconds),
            BehaviorLiveConfig(settings.config_url, settings.timeout_seconds),
            calendar,
            settings.product_ids,
            call_id_source,
        ),
        permissions=frozenset({READER_READ_PERMISSION}),
    )
