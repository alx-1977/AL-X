"""Compose the D-039 reader calendar, or leave it unavailable."""

from __future__ import annotations

import asyncio
import logging
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
from alx.tools.readers import (
    DEFINITIONS,
    REFRESH_READER_CALENDAR,
    SEND_READER_SCHEDULE,
    build_reader_executors,
)

LOGGER = logging.getLogger(__name__)


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
    refresh_seconds: int = 300


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
        refresh_seconds=settings.refresh_seconds,
    )


class ReaderCalendarPoller:
    """Refresh the calendar on a timer, through the one refresh path (D-041).

    Purely mechanical, like the mail scan: it reads and records, makes no Core
    call and decides nothing. It keeps the BHL tile true to today without
    anyone asking. A failed refresh leaves the previous calendar in place, and
    the tile's "Updated" time shows how old it is.
    """

    def __init__(
        self,
        refresh: Callable[[StructuredData], CapabilityResult],
        interval_seconds: int,
        with_call_id: Callable[[Callable[[], CapabilityResult]], CapabilityResult],
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._refresh = refresh
        self._interval = interval_seconds
        self._with_call_id = with_call_id

    def refresh_once(self) -> CapabilityResult:
        return self._with_call_id(lambda: self._refresh({}))

    async def run(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self.refresh_once)
                if result.failure is not None:
                    LOGGER.info("Reader calendar refresh failed: %s", result.failure.get("code"))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("Reader calendar refresh failed: %s", error)
            await asyncio.sleep(self._interval)


def reader_poller(runtime: ReaderRuntime, with_call_id) -> ReaderCalendarPoller:
    return ReaderCalendarPoller(
        runtime.executors[REFRESH_READER_CALENDAR], runtime.refresh_seconds, with_call_id)
