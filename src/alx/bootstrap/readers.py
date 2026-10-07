"""Compose the D-039 reader calendar, or leave it unavailable."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from alx.config import ReaderSettings
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.readers import (
    READER_UID_LENGTH,
    ReaderAccessError,
    expected_event,
    holds_current,
    still_to_run,
)
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
    particle: Any = None
    product_ids: tuple[int, ...] = ()


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
        particle=particle,
        product_ids=tuple(settings.product_ids),
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
        monitor: "ReaderMonitor | None" = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._refresh = refresh
        self._interval = interval_seconds
        self._with_call_id = with_call_id
        self._monitor = monitor

    def refresh_once(self) -> CapabilityResult:
        return self._with_call_id(lambda: self._refresh({}))

    async def run(self) -> None:
        while True:
            try:
                result = await asyncio.to_thread(self.refresh_once)
                if result.failure is not None:
                    LOGGER.info("Reader calendar refresh failed: %s", result.failure.get("code"))
                # D-043: with today's calendar current, check every reader in
                # use and deliver any schedule it is not holding.
                if self._monitor is not None:
                    await asyncio.to_thread(self._monitor.cycle)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning("Reader calendar refresh failed: %s", error)
            await asyncio.sleep(self._interval)


# A reader asks again on every new connection; answering more often than this
# for one reader would only repeat the same schedule.
REQUEST_ANSWER_GAP = timedelta(seconds=60)
# Unasked deliveries to one reader are spaced at least this far apart, so a
# reader that keeps refusing is not called every cycle.
AUTOMATIC_DELIVERY_GAP = timedelta(minutes=10)
# A reader's own report older than this no longer says what it is doing now.
CHECK_FRESH_FOR = timedelta(minutes=15)
REQUEST_EVENT = "roomreader/schedule_request"
STATUS_VARIABLE = "status"


class ReaderMonitor:
    """Keeps every reader in use on its schedule, and records each step (D-043).

    Mechanical, like the calendar refresh: it answers a reader's request with
    the schedule the calendar holds, delivers a schedule a reader is not
    holding, reads what each reader reports, and logs all of it. Every
    delivery goes through the one send path, which refuses anything that
    needs AL/X's judgement (overlapping events) and logs the refusal; what to
    do then is hers. It makes no Core call.
    """

    def __init__(self, executors: Mapping[str, Callable[[StructuredData], CapabilityResult]],
                 calendar: Any, particle: Any,
                 with_call_id: Callable[[Callable[[], CapabilityResult]], CapabilityResult],
                 clock: Callable[[], datetime] | None = None) -> None:
        self._send = executors[SEND_READER_SCHEDULE]
        self._calendar = calendar
        self._particle = particle
        self._with_call_id = with_call_id
        self._now = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._last_delivery: dict[str, datetime] = {}
        self._checks: dict[str, dict[str, Any]] = {}
        self._online: dict[str, bool] = {}
        self._last_outcome: dict[str, str] = {}
        self._last_cycle: datetime | None = None
        # Readers whose firmware has no schedule function (V1). Not pushed to
        # again until one of them asks, which only schedule firmware does.
        self._cannot_receive: set[str] = set()
        # Readers with a send under way; never two at once to one reader.
        self._sending: set[str] = set()
        self._last_action: tuple[datetime, str] | None = None

    # ---- what the tile reads ---------------------------------------------

    def checks(self, at: datetime) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {uid: {**check, "fresh": at - check["at"] <= CHECK_FRESH_FOR}
                    for uid, check in self._checks.items()}

    def activity(self, at: datetime, local: Any = None) -> dict[str, Any] | None:
        with self._lock:
            if self._last_cycle is None or at - self._last_cycle > 3 * CHECK_FRESH_FOR:
                return None
            if self._last_action is None:
                return {"text": "monitoring"}
            when, what = self._last_action
            return {"text": f"monitoring · {what} {when.astimezone(local).strftime('%H:%M')}"}

    # ---- a reader asking ---------------------------------------------------

    def on_event(self, name: str, device_id: str, data: str, published_at: str) -> None:
        if name != REQUEST_EVENT or len(device_id) < READER_UID_LENGTH:
            return
        uid = device_id[-READER_UID_LENGTH:]
        at = self._now()
        try:
            holding = json.loads(data).get("v", "") if data else ""
        except (ValueError, AttributeError):
            holding = ""
        self._calendar.log(at, uid, "schedule_requested",
                           {"holding": holding, "published_at": published_at})
        with self._lock:
            self._cannot_receive.discard(uid)
            last = self._last_delivery.get(uid)
            if last is not None and at - last < REQUEST_ANSWER_GAP:
                return
            if not self._reserve(uid, at):
                return
        self._deliver(uid, "requested", at)

    # ---- the regular check -------------------------------------------------

    def cycle(self) -> None:
        at = self._now()
        refreshed_at, _, readers, sessions = self._calendar.snapshot(at)
        if not refreshed_at:
            return
        for reader in readers:
            uid = reader.get("reader_uid", "")
            own = [item for item in sessions if item.reader_uid == uid]
            if not uid or not own or reader.get("mode") not in (0, 1):
                continue
            mode = reader["mode"]
            if not still_to_run(own, mode, at):
                continue  # nothing left today for this reader
            online = reader.get("online") is True
            if self._online.get(uid) != online:
                self._online[uid] = online
                self._calendar.log(at, uid, "online" if online else "offline", {})
            if not online:
                continue
            report = self._read_status(uid, reader, at)
            expected = expected_event(own, mode, at)
            record = self._calendar.sent(uid)
            if report is not None and report.get("e") != expected:
                self._calendar.log(at, uid, "wrong_event",
                                   {"expected": expected, "reported": report.get("e")})
            stale = not holds_current(record, own, mode, at) or (
                report is not None and record and report.get("v") != record.get("version"))
            if stale:
                with self._lock:
                    last = self._last_delivery.get(uid)
                    due = (uid not in self._cannot_receive
                           and (last is None or at - last >= AUTOMATIC_DELIVERY_GAP)
                           and self._reserve(uid, at))
                if due:
                    self._deliver(uid, "not_holding_current_schedule", at)
        with self._lock:
            self._last_cycle = at

    def _read_status(self, uid: str, reader: Mapping[str, Any],
                     at: datetime) -> dict[str, Any] | None:
        try:
            raw = self._particle.read_variable(int(reader["product_id"]), reader["device_id"],
                                               STATUS_VARIABLE)
            report = json.loads(raw) if isinstance(raw, str) else None
            if not isinstance(report, dict):
                raise ValueError("status")
        except ReaderAccessError as error:
            self._note(uid, at, "status_unavailable", {"code": error.code})
            return None
        except (ValueError, KeyError, TypeError):
            self._note(uid, at, "status_unavailable", {"code": "status_unreadable"})
            return None
        self._calendar.log(at, uid, "status", report)
        with self._lock:
            self._checks[uid] = {"event": report.get("e"), "version": report.get("v"),
                                 "at": at}
            self._last_outcome.pop(uid, None)
        return report

    def _note(self, uid: str, at: datetime, kind: str, detail: Mapping[str, Any]) -> None:
        """Log a repeated failure once, not on every cycle."""
        key = f"{kind}:{json.dumps(dict(detail), sort_keys=True)}"
        with self._lock:
            if self._last_outcome.get(uid) == key:
                return
            self._last_outcome[uid] = key
        self._calendar.log(at, uid, kind, detail)

    def _reserve(self, uid: str, at: datetime) -> bool:
        """Claim the next send to this reader. Call with the lock held.

        The listener threads and the regular check run at once, so deciding to
        send and recording that a send is under way must be one step: two
        sends interleaving their begin/event/commit messages would each wipe
        the other's half-received schedule on the reader.
        """
        if uid in self._sending:
            return False
        self._sending.add(uid)
        self._last_delivery[uid] = at
        return True

    def _deliver(self, uid: str, trigger: str, at: datetime) -> None:
        """Send a reader its schedule. The caller has reserved it (_reserve)."""
        started = False
        try:
            self._calendar.log(at, uid, "schedule_delivery", {"trigger": trigger})
            started = True
            result = self._with_call_id(lambda: self._send({"reader_uid": uid}))
        finally:
            with self._lock:
                self._sending.discard(uid)
                # Nothing reached the reader, so nothing should hold back the
                # next attempt. Once a send has begun, its time stands, so a
                # partly delivered schedule is not immediately repeated.
                if not started and self._last_delivery.get(uid) == at:
                    del self._last_delivery[uid]
        if result.failure is None:
            with self._lock:
                self._last_action = (at, "schedule sent")
        elif result.failure.get("code") == "function_not_exposed":
            with self._lock:
                self._cannot_receive.add(uid)
        # The send path logs what was sent, or why not.


class ReaderRequestListener:
    """Hears readers asking for their schedule, for as long as the process runs.

    One Particle event stream per product, each on its own daemon thread, so a
    stalled stream never holds anything else up and the process can stop at
    any time. A dropped stream reconnects with backoff.
    """

    def __init__(self, particle: Any, product_ids: Sequence[int], monitor: ReaderMonitor,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._particle = particle
        self._products = tuple(product_ids)
        self._monitor = monitor
        self._sleep = sleep

    def listen(self, product_id: int, rounds: int | None = None) -> None:
        delay = 5.0
        done = 0
        while rounds is None or done < rounds:
            done += 1
            try:
                self._particle.stream_events(product_id, REQUEST_EVENT, self._monitor.on_event)
                delay = 5.0  # a stream that ran and ended is not a failure
            except ReaderAccessError as error:
                LOGGER.info("Reader request stream for %s ended: %s", product_id, error.code)
                delay = min(delay * 2, 120.0)
            except Exception as error:  # noqa: BLE001 - keep listening
                LOGGER.warning("Reader request stream for %s failed: %s", product_id, error)
                delay = min(delay * 2, 120.0)
            self._sleep(delay)

    async def run(self) -> None:
        for product_id in self._products:
            threading.Thread(target=self.listen, args=(product_id,), daemon=True,
                             name=f"reader-requests-{product_id}").start()
        await asyncio.Event().wait()


def reader_monitor(runtime: ReaderRuntime, with_call_id) -> ReaderMonitor:
    return ReaderMonitor(runtime.executors, runtime.calendar, runtime.particle, with_call_id)


def reader_poller(runtime: ReaderRuntime, with_call_id,
                  monitor: ReaderMonitor | None = None) -> ReaderCalendarPoller:
    return ReaderCalendarPoller(
        runtime.executors[REFRESH_READER_CALENDAR], runtime.refresh_seconds, with_call_id,
        monitor)


def reader_listener(runtime: ReaderRuntime, monitor: ReaderMonitor) -> ReaderRequestListener:
    return ReaderRequestListener(runtime.particle, runtime.product_ids, monitor)
