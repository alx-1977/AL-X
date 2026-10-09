"""Compose the D-039 reader calendar, or leave it unavailable."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
from alx.tools.particle import DEFINITIONS as PARTICLE_DEFINITIONS
from alx.tools.particle import build_particle_executors
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
# D-045. Full access to Friedl's Particle account, without per-call approval:
# "I want ALX to have FULL access to my Particle account. No need to restrict
# anything please. She will be managing it anyway."
PARTICLE_FULL_PERMISSION = "particle.full"


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
    full = AuthorityPolicy(frozenset({PARTICLE_FULL_PERMISSION}))
    policies = {
        definition.capability_id: send if definition.capability_id == SEND_READER_SCHEDULE else read
        for definition in DEFINITIONS
    }
    policies.update({definition.capability_id: full for definition in PARTICLE_DEFINITIONS})
    executors = dict(build_reader_executors(
        particle,
        BehaviorLiveConfig(settings.config_url, settings.timeout_seconds),
        calendar,
        settings.product_ids,
        call_id_source,
        control=particle,
    ))
    executors.update(build_particle_executors(particle, calendar.log, call_id_source))
    return ReaderRuntime(
        calendar=calendar,
        definitions=DEFINITIONS + PARTICLE_DEFINITIONS,
        policies=policies,
        executors=executors,
        permissions=frozenset({READER_READ_PERMISSION, READER_SEND_PERMISSION,
                               PARTICLE_FULL_PERMISSION}),
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
# A reader's report older than this no longer says what it is doing now. A
# card stands for up to CARD_FRESH_FOR, then a read refreshes it each cycle.
CHECK_FRESH_FOR = timedelta(minutes=40)
REQUEST_EVENT = "roomreader/schedule_request"
# The reader's status card, published by the reader whenever it changes.
STATUS_EVENT = "roomreader/status"
# While a card this recent is held, AL/X does not read the status herself;
# the reader sends a new one whenever something on it changes.
CARD_FRESH_FOR = timedelta(minutes=30)
# Just after a reader moves to its next event, its new card can arrive after
# AL/X's own clock has moved on; the event before stays acceptable this long.
CHANGEOVER_GRACE = timedelta(minutes=2)
# Every event a reader publishes starts with this. Hearing any of them (a
# scan, a battery report, a request) shows the reader is connected; listening
# costs no data operations.
READER_EVENTS = "roomreader"
# Each reader in use is pinged this often. A ping is free of data operations
# and costs about 122 bytes; Particle gives up on an unanswered one after about
# 30 seconds, so a reader that loses power shows offline within ~90 seconds.
PING_INTERVAL_SECONDS = 60
# Heard from or answered a ping within this long: online, whatever Particle's
# device list says (it can lag a lost connection by most of an hour).
PRESENCE_FRESH_FOR = timedelta(minutes=3)
STATUS_VARIABLE = "status"


def _usable_report(report: Any) -> bool:
    """A status report with the fields every judgement relies on, correctly typed."""
    if not isinstance(report, dict):
        return False

    def whole(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    return (isinstance(report.get("v"), str) and whole(report.get("e"))
            and whole(report.get("n")) and whole(report.get("clk")))


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
        # When each reader was last heard from (any event, a ping answered, a
        # status read), and the last ping's answer.
        self._seen: dict[str, datetime] = {}
        self._pinged: dict[str, tuple[bool, datetime]] = {}
        self._last_outcome: dict[str, str] = {}
        self._last_cycle: datetime | None = None
        # Readers whose firmware has no schedule function (V1). Not pushed to
        # again until one of them asks, which only schedule firmware does.
        self._cannot_receive: set[str] = set()
        # Readers with a send under way; never two at once to one reader.
        self._sending: set[str] = set()
        # Each reader's latest card and when it arrived, and the wrong event
        # last recorded for it (so a persisting one is logged once).
        self._cards: dict[str, tuple[dict[str, Any], datetime]] = {}
        self._wrong: dict[str, tuple[Any, Any] | None] = {}
        self._last_card_at: dict[str, datetime] = {}
        self._last_action: tuple[datetime, str] | None = None

    # ---- what the tile reads ---------------------------------------------

    def checks(self, at: datetime) -> dict[str, dict[str, Any]]:
        with self._lock:
            known = set(self._checks) | set(self._seen) | set(self._pinged)
            result: dict[str, dict[str, Any]] = {}
            for uid in known:
                check = dict(self._checks.get(uid, {}))
                if "at" in check:
                    check["fresh"] = at - check["at"] <= CHECK_FRESH_FOR
                presence = self._presence(uid, at)
                if presence is not None:
                    check["online"] = presence
                result[uid] = check
            return result

    def _presence(self, uid: str, at: datetime) -> bool | None:
        """Online from what AL/X observed herself, or None when she has nothing recent."""
        seen = self._seen.get(uid)
        if seen is not None and at - seen <= PRESENCE_FRESH_FOR:
            return True
        pinged = self._pinged.get(uid)
        if pinged is not None and at - pinged[1] <= PRESENCE_FRESH_FOR:
            return pinged[0]
        return None

    def _set_online(self, uid: str, online: bool, at: datetime, via: str) -> None:
        # The change and its log line are one step: pings, events and the
        # regular check run on different threads, and the log must show the
        # transitions in the order they happened.
        with self._lock:
            if self._online.get(uid) == online:
                return
            self._online[uid] = online
            self._calendar.log(at, uid, "online" if online else "offline", {"via": via})

    def _heard(self, uid: str, at: datetime, via: str) -> None:
        with self._lock:
            self._seen[uid] = at
        self._set_online(uid, True, at, via)

    # ---- presence ----------------------------------------------------------

    def ping_cycle(self) -> None:
        """Ping every reader with events in the calendar, in parallel.

        All of the day's readers, not only those with events still to run: the
        BHL tile counts every reader in use today, and its count must be what
        AL/X observed, never Particle's lagging list. Pings are free of data
        operations.
        """
        at = self._now()
        refreshed_at, _, readers, sessions = self._calendar.snapshot(at)
        if not refreshed_at:
            return
        targets = []
        for reader in readers:
            uid = reader.get("reader_uid", "")
            own = [item for item in sessions if item.reader_uid == uid]
            if uid and own and reader.get("device_id"):
                targets.append((uid, reader))
        if not targets:
            return

        def ping(target):
            uid, reader = target
            try:
                return uid, self._particle.ping(int(reader["product_id"]), reader["device_id"])
            except ReaderAccessError:
                return uid, None  # the ping itself failed: nothing learned

        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
            answers = list(pool.map(ping, targets))
        done = self._now()
        for uid, online in answers:
            if online is None:
                continue
            with self._lock:
                self._pinged[uid] = (online, done)
                if online:
                    self._seen[uid] = done
            self._set_online(uid, online, done, "ping")

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
        if len(device_id) < READER_UID_LENGTH:
            return
        uid = device_id[-READER_UID_LENGTH:]
        at = self._now()
        self._heard(uid, at, "event")
        if name == STATUS_EVENT:
            self._card(uid, data, at)
            return
        if name != REQUEST_EVENT:
            return
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
                # Nothing left to run or send, but a reader still showing its
                # last event after that window closed is stuck: judged again
                # on what it last reported, without reading or sending.
                with self._lock:
                    held = self._cards.get(uid)
                    observed = self._presence(uid, at)
                connected = observed if observed is not None else reader.get("online") is True
                # Only on current evidence: a reader that went offline, or whose
                # report is old, is not said to be on the wrong event.
                if held is not None and connected and at - held[1] <= CHECK_FRESH_FOR:
                    self._judge_event(uid, held[0], held[1], own, mode, at)
                continue
            with self._lock:
                observed = self._presence(uid, at)
            online = observed if observed is not None else reader.get("online") is True
            self._set_online(uid, online, at, "observed" if observed is not None else "particle")
            if not online:
                continue
            with self._lock:
                card = self._cards.get(uid)
                card_at = self._last_card_at.get(uid)
            if card is not None and card_at is not None and at - card_at <= CARD_FRESH_FOR:
                report, received_at = card  # a recent card from the reader: no read
            else:
                report, received_at = self._read_status(uid, reader, at), at
            record = self._calendar.sent(uid)
            if report is not None:
                self._judge_event(uid, report, received_at, own, mode, at)
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
        if not _usable_report(report):
            self._note(uid, at, "status_unavailable", {"code": "status_unreadable"})
            return None
        self._calendar.log(at, uid, "status", report)
        self._heard(uid, at, "status")
        self._keep_report(uid, report, at)
        return report

    def _card(self, uid: str, data: str, at: datetime) -> None:
        """A status card the reader published itself: recorded and judged."""
        try:
            report = json.loads(data) if data else None
        except ValueError:
            report = None
        if not _usable_report(report):
            # Not trusted, not kept: the status read stays available.
            self._note(uid, at, "status_unavailable", {"code": "card_unreadable"})
            return
        self._calendar.log(at, uid, "status", {**report, "via": "card"})
        self._keep_report(uid, report, at)
        with self._lock:
            # Only a card the reader sent itself spares the next read; a read
            # does not, so a reader that stops sending cards is read each cycle.
            self._last_card_at[uid] = at
        _, _, readers, sessions = self._calendar.snapshot(at)
        reader = next((item for item in readers if item.get("reader_uid") == uid), None)
        own = [item for item in sessions if item.reader_uid == uid]
        if reader is not None and own and reader.get("mode") in (0, 1):
            self._judge_event(uid, report, at, own, reader["mode"], at)

    def _keep_report(self, uid: str, report: Mapping[str, Any], received_at: datetime) -> None:
        """The reader's latest report, whether its own card or a read, and when."""
        with self._lock:
            current = self._cards.get(uid)
            if current is not None and current[1] > received_at:
                return  # a newer one is already held
            self._cards[uid] = (dict(report), received_at)
            self._checks[uid] = {**self._checks.get(uid, {}), "event": report.get("e"),
                                 "version": report.get("v"), "at": received_at}
            self._last_outcome.pop(uid, None)

    def _judge_event(self, uid: str, report: Mapping[str, Any], received_at: datetime,
                     own: list, mode: int, at: datetime) -> None:
        """Whether the reader runs the event its window says, recorded once per change.

        Only the report still held is judged: a newer card that arrived while
        an older one was being judged has the last word.
        """
        # A report still describing an older schedule than the one AL/X last
        # sent says only that the new one has not landed yet, which delivery
        # handles; which event it runs is judged against the schedule it holds.
        sent = self._calendar.sent(uid)
        if sent and report.get("v") != sent.get("version"):
            return
        reported = report.get("e")
        expected = expected_event(own, mode, at)
        wrong = reported != expected and reported != expected_event(
            own, mode, at - CHANGEOVER_GRACE)
        with self._lock:
            held = self._cards.get(uid)
            if held is not None and held[1] != received_at:
                return
            check = self._checks.setdefault(uid, {"at": at})
            check["wrong"] = wrong
            check["expected"] = expected
            previous = self._wrong.get(uid)
            self._wrong[uid] = (reported, expected) if wrong else None
        if wrong and previous != (reported, expected):
            self._calendar.log(at, uid, "wrong_event",
                               {"expected": expected, "reported": reported})

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
    """Hears the readers, and pings them, for as long as the process runs.

    Every event the readers publish arrives here; any of them shows a reader is
    connected, and a request for a schedule is answered. Each reader in use is
    also pinged every minute, so one that loses power is noticed in about 90
    seconds rather than when Particle's device list catches up.

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
                self._particle.stream_events(product_id, READER_EVENTS, self._monitor.on_event)
                delay = 5.0  # a stream that ran and ended is not a failure
            except ReaderAccessError as error:
                LOGGER.info("Reader request stream for %s ended: %s", product_id, error.code)
                delay = min(delay * 2, 120.0)
            except Exception as error:  # noqa: BLE001 - keep listening
                LOGGER.warning("Reader request stream for %s failed: %s", product_id, error)
                delay = min(delay * 2, 120.0)
            self._sleep(delay)

    def ping_forever(self, rounds: int | None = None) -> None:
        done = 0
        while rounds is None or done < rounds:
            done += 1
            started = time.monotonic()
            try:
                self._monitor.ping_cycle()
            except Exception as error:  # noqa: BLE001 - keep pinging
                LOGGER.warning("Reader ping round failed: %s", error)
            self._sleep(max(1.0, PING_INTERVAL_SECONDS - (time.monotonic() - started)))

    async def run(self) -> None:
        # The event streams and the presence pings, each on its own daemon
        # thread: hearing readers and pinging them are the same job, knowing
        # which readers are connected right now.
        for product_id in self._products:
            threading.Thread(target=self.listen, args=(product_id,), daemon=True,
                             name=f"reader-events-{product_id}").start()
        threading.Thread(target=self.ping_forever, daemon=True, name="reader-pings").start()
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
