"""D-039: the one calendar of BHL room-reader sessions.

`refresh_reader_calendar` finds every reader in the configured Particle
products, reads each one's schedule from BehaviorLive, checks it, and replaces
the calendar with that one consistent snapshot. `read_reader_calendar` reads
it back by room, reader or time, with what each reader should be running now.

The checks state mechanical facts only: a field missing or malformed, an event
ending before it starts, two events overlapping in one reader's schedule, a
duplicated event, an empty schedule, or readers in one room disagreeing. Which
schedule is right, and what to do about a problem, is AL/X's judgement.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy
from alx.contracts.readers import (
    ReaderAccessError,
    ReaderConfigSource,
    ReaderDevice,
    ReaderFleet,
    ReaderSession,
)


REFRESH_READER_CALENDAR = "refresh_reader_calendar"
READ_READER_CALENDAR = "read_reader_calendar"

_STRING = StructuredSchema(ValueKind.STRING)
_OBJECT = StructuredSchema(ValueKind.OBJECT)
_OBJECTS = StructuredSchema(ValueKind.ARRAY, items=_OBJECT)
_STRINGS = StructuredSchema(ValueKind.ARRAY, items=_STRING)
_MAX_SESSIONS_RETURNED = 500
_DEFAULT_WINDOW = timedelta(hours=24)

_FAILURES = (
    "arguments_unusable",
    "connection_failed",
    "permission_denied",
    "product_not_found",
    "rate_limited",
    "request_rejected",
    "response_invalid",
    "calendar_empty",
)

REFRESH_DEFINITION = CapabilityDefinition(
    REFRESH_READER_CALENDAR,
    "Rebuild the one calendar of BHL room-reader sessions: list every reader in the configured Particle products (online state, last heard), read each reader's schedule from BehaviorLive, check it, and replace the calendar with that snapshot. Returns each reader's room, mode (0 IN, 1 OUT), event count and problems, plus problems across readers. Problems are facts (malformed or missing fields, events ending before they start or overlapping, duplicates, an empty or unreadable schedule, readers in one room disagreeing); nothing is corrected.",
    StructuredSchema(ValueKind.OBJECT, {}, (), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT,
        {"refreshed_at": _STRING, "readers": _OBJECTS, "problems": _STRINGS,
         "session_count": StructuredSchema(ValueKind.INTEGER)},
        ("refreshed_at", "readers", "problems", "session_count"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

READ_DEFINITION = CapabilityDefinition(
    READ_READER_CALENDAR,
    "Read the reader calendar as last refreshed: sessions overlapping a window (from/to, ISO 8601; default now to 24 hours ahead), optionally for one room or one reader_uid, and for every matching reader the session it should be running now and the next one. Times are UTC; each session carries its schedule's offset_hours. Refresh first if refreshed_at is old.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"room": _STRING, "reader_uid": _STRING, "from": _STRING, "to": _STRING},
        (),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"refreshed_at": _STRING, "problems": _STRINGS, "readers": _OBJECTS,
         "now_running": _OBJECTS, "sessions": _OBJECTS,
         "truncated": StructuredSchema(ValueKind.BOOLEAN)},
        ("refreshed_at", "problems", "readers", "now_running", "sessions", "truncated"),
        extra_properties=False,
    ),
    SideEffect.NONE,
    _FAILURES,
)

DEFINITIONS = (REFRESH_DEFINITION, READ_DEFINITION)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def parse_schedule(
    reader_uid: str, raw: Mapping[str, Any]
) -> tuple[dict[str, Any], tuple[ReaderSession, ...], tuple[str, ...]]:
    """(header, sessions, problems) for one reader's BehaviorLive config.

    Every malformed event is reported and left out; the rest are kept, so one
    bad entry never hides a reader's whole day.
    """
    problems: list[str] = []
    if raw.get("reader") != reader_uid:
        problems.append(f"reader_mismatch:{raw.get('reader')!r}")
    mode = raw.get("mode")
    if mode not in (0, 1) or isinstance(mode, bool):
        problems.append(f"mode_invalid:{mode!r}")
    room = raw.get("room")
    if not isinstance(room, str) or not room.strip():
        problems.append("room_missing")
        room = ""
    offset = raw.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool):
        problems.append(f"offset_invalid:{offset!r}")
        offset = 0
    events = raw.get("events")
    if not isinstance(events, list):
        problems.append("events_missing")
        events = []
    if not events:
        problems.append("no_events")
    header = {"room": room.strip(), "mode": mode if mode in (0, 1) else -1,
              "offset_hours": offset}
    sessions: list[ReaderSession] = []
    seen: set[int] = set()
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            problems.append(f"event_invalid:#{index}")
            continue
        event_id = event.get("id")
        if not isinstance(event_id, int) or isinstance(event_id, bool):
            problems.append(f"event_id_invalid:#{index}")
            continue
        if event_id in seen:
            problems.append(f"event_duplicated:{event_id}")
            continue
        seen.add(event_id)
        starts, ends = _time(event.get("st")), _time(event.get("en"))
        bad = [name for name, ok in (
            ("st", starts is not None), ("en", ends is not None),
            ("t", isinstance(event.get("t"), str)),
            ("fn", isinstance(event.get("fn"), str)),
            ("ln", isinstance(event.get("ln"), str)),
            ("hbd", isinstance(event.get("hbd"), int) and not isinstance(event.get("hbd"), bool)),
        ) if not ok]
        if bad:
            problems.append(f"event_field_invalid:{event_id}:{','.join(bad)}")
            continue
        if ends <= starts:
            problems.append(f"event_ends_before_start:{event_id}")
            continue
        sessions.append(ReaderSession(
            reader_uid, event_id, header["room"], header["mode"], starts, ends,
            event["t"], event["fn"], event["ln"], event["hbd"], offset,
        ))
    ordered = sorted(sessions, key=lambda item: item.starts_at)
    for before, after in zip(ordered, ordered[1:]):
        if after.starts_at < before.ends_at:
            problems.append(f"events_overlap:{before.event_id},{after.event_id}")
    return header, tuple(ordered), tuple(problems)


def room_problems(
    headers: Mapping[str, Mapping[str, Any]],
    sessions: Sequence[ReaderSession],
) -> tuple[str, ...]:
    """Readers sharing a room whose event lists differ."""
    by_room: dict[str, dict[str, frozenset[int]]] = {}
    for reader_uid, header in headers.items():
        if header.get("room"):
            by_room.setdefault(header["room"], {})[reader_uid] = frozenset(
                item.event_id for item in sessions if item.reader_uid == reader_uid
            )
    problems = []
    for room, readers in sorted(by_room.items()):
        if len(set(readers.values())) > 1:
            problems.append(f"room_schedules_differ:{room}:{','.join(sorted(readers))}")
    return tuple(problems)


def _session(item: ReaderSession) -> dict[str, Any]:
    return {
        "reader_uid": item.reader_uid, "event_id": item.event_id, "room": item.room,
        "mode": item.mode, "starts_at": item.starts_at.isoformat(),
        "ends_at": item.ends_at.isoformat(), "title": item.title,
        "first_name": item.first_name, "last_name": item.last_name,
        "hbd": item.hbd, "offset_hours": item.offset_hours,
    }


def build_reader_executors(
    fleet: ReaderFleet,
    configs: ReaderConfigSource,
    calendar: Any,
    product_ids: Sequence[int],
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    now = clock or (lambda: datetime.now(UTC))

    def failed(capability: str, code: str) -> CapabilityResult:
        return CapabilityResult(call_id_source(), capability,
                                CapabilityResultState.FAILED, failure={"code": code})

    def provenance(at: datetime):
        return RetentionPolicy().non_mail(ContentOrigin.EXTERNAL, at)

    def refresh(arguments: StructuredData) -> CapabilityResult:
        at = now()
        try:
            devices: list[ReaderDevice] = []
            for product_id in product_ids:
                devices.extend(fleet.devices(product_id))
        except ReaderAccessError as error:
            return failed(REFRESH_READER_CALENDAR, error.code)
        summaries: list[dict[str, Any]] = []
        headers: dict[str, Mapping[str, Any]] = {}
        sessions: list[ReaderSession] = []
        for device in sorted(devices, key=lambda item: item.reader_uid):
            summary: dict[str, Any] = {
                "reader_uid": device.reader_uid, "device_name": device.name,
                "product_id": device.product_id, "online": device.online,
                "last_heard": device.last_heard, "room": "", "mode": -1,
                "event_count": 0, "problems": (),
            }
            try:
                raw = configs.config(device.reader_uid)
            except ReaderAccessError as error:
                summary["problems"] = (f"schedule_unavailable:{error.code}",)
                summaries.append(summary)
                continue
            header, parsed, problems = parse_schedule(device.reader_uid, raw)
            headers[device.reader_uid] = header
            sessions.extend(parsed)
            summary.update(room=header["room"], mode=header["mode"],
                           event_count=len(parsed), problems=problems)
            summaries.append(summary)
        across = room_problems(headers, sessions)
        calendar.replace(at, summaries, sessions, across)
        return CapabilityResult(
            call_id_source(), REFRESH_READER_CALENDAR, CapabilityResultState.SUCCEEDED,
            {"refreshed_at": at.isoformat(), "readers": tuple(summaries),
             "problems": across, "session_count": len(sessions)},
            provenance=provenance(at),
        )

    def read(arguments: StructuredData) -> CapabilityResult:
        at = now()
        try:
            start = _time(arguments.get("from")) if arguments.get("from") else at
            if start is None:
                raise ValueError("from")
            end = _time(arguments.get("to")) if arguments.get("to") else start + _DEFAULT_WINDOW
            if end is None or end <= start:
                raise ValueError("window")
            room = arguments.get("room") or ""
            reader_uid = arguments.get("reader_uid") or ""
            if not isinstance(room, str) or not isinstance(reader_uid, str):
                raise ValueError("filter")
        except ValueError:
            return failed(READ_READER_CALENDAR, "arguments_unusable")
        refreshed_at, problems, readers, sessions = calendar.snapshot(at)
        if not refreshed_at:
            return failed(READ_READER_CALENDAR, "calendar_empty")
        chosen = [
            item for item in readers
            if (not room or item.get("room") == room)
            and (not reader_uid or item.get("reader_uid") == reader_uid)
        ]
        uids = {item["reader_uid"] for item in chosen}
        mine = [item for item in sessions if item.reader_uid in uids]
        running = []
        for reader in chosen:
            own = [item for item in mine if item.reader_uid == reader["reader_uid"]]
            current = next((item for item in own if item.starts_at <= at < item.ends_at), None)
            upcoming = next((item for item in own if item.starts_at > at), None)
            running.append({
                "reader_uid": reader["reader_uid"], "room": reader.get("room", ""),
                "mode": reader.get("mode", -1),
                "current": _session(current) if current else {},
                "next": _session(upcoming) if upcoming else {},
            })
        window = [item for item in mine if item.starts_at < end and item.ends_at > start]
        return CapabilityResult(
            call_id_source(), READ_READER_CALENDAR, CapabilityResultState.SUCCEEDED,
            {"refreshed_at": refreshed_at, "problems": problems,
             "readers": tuple(chosen), "now_running": tuple(running),
             "sessions": tuple(_session(item) for item in window[:_MAX_SESSIONS_RETURNED]),
             "truncated": len(window) > _MAX_SESSIONS_RETURNED},
            provenance=provenance(at),
        )

    return {REFRESH_READER_CALENDAR: refresh, READ_READER_CALENDAR: read}
