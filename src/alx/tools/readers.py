"""D-039: the one calendar of BHL room-reader sessions.

`refresh_reader_calendar` finds every reader in the configured Particle
products, reads each one's schedule from BehaviorLive, checks it, and replaces
the calendar with that one consistent snapshot. `read_reader_calendar` reads
it back by room, reader or time, with what each reader should be running now.

`send_reader_schedule` (D-040) sends one reader its sessions from that
calendar through a Particle function, one short message at a time, and the
reader only switches to the new schedule once every message has arrived; the
protocol is `docs/READER_SCHEDULE_PROTOCOL.md`.

The checks state mechanical facts only: a field missing or malformed, an event
ending before it starts, two events overlapping in one reader's schedule, a
duplicated event, an empty schedule, or readers in one room disagreeing. Which
schedule is right, and what to do about a problem, is AL/X's judgement.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    ContentOrigin,
    ExecutionOutcome,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.provenance import RetentionPolicy
from alx.contracts.readers import (
    ReaderAccessError,
    ReaderConfigSource,
    ReaderControl,
    ReaderDevice,
    ReaderFleet,
    ReaderSession,
    session_fingerprint,
)


REFRESH_READER_CALENDAR = "refresh_reader_calendar"
READ_READER_CALENDAR = "read_reader_calendar"
SEND_READER_SCHEDULE = "send_reader_schedule"

# The reader-side contract, docs/READER_SCHEDULE_PROTOCOL.md. Messages stay
# under every Device OS's function-argument limit (622 bytes before 6.3), and
# display text is cut to what the reader stores.
SCHEDULE_FUNCTION = "schedule"
MAX_MESSAGE_BYTES = 600
MAX_SCHEDULE_EVENTS = 32
TITLE_CHARACTERS = 64
NAME_CHARACTERS = 32

_STRING = StructuredSchema(ValueKind.STRING)
_OBJECT = StructuredSchema(ValueKind.OBJECT)
_OBJECTS = StructuredSchema(ValueKind.ARRAY, items=_OBJECT)
_STRINGS = StructuredSchema(ValueKind.ARRAY, items=_STRING)
_MAX_SESSIONS_RETURNED = 500
_DEFAULT_WINDOW = timedelta(hours=24)

# BehaviorLive answering that a reader has no schedule is a fact about the
# reader, not an outage, so its previous schedule is not kept.
_SCHEDULE_ABSENT = frozenset({"reader_not_configured", "reader_uid_invalid"})

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
    "Rebuild the one calendar of BHL room-reader sessions: list every reader in the configured Particle products (online state, last heard), read each reader's schedule from BehaviorLive, check it, and replace the calendar with that snapshot. Returns each reader's room, mode (0 IN, 1 OUT), event count, schedule_as_of and problems, plus problems across readers. A schedule that cannot be fetched keeps the one last read (problem previous_schedule_kept) unless BehaviorLive says the reader has none. Problems are facts (malformed or missing fields, events ending before they start or overlapping, duplicates, an empty or unreadable schedule, readers in one room disagreeing); nothing is corrected.",
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

SEND_DEFINITION = CapabilityDefinition(
    SEND_READER_SCHEDULE,
    "Send one reader its schedule from the calendar as last refreshed: every session for that reader_uid not yet ended, less any event IDs in leave_out, with its room, mode and offset. The reader keeps its previous schedule unless the whole new one arrives, and confirms each message. Refuses overlapping events (choose which to leave out), more than 32 events, or a reader without room and mode. A reader that cannot be reached keeps its previous schedule; if it stopped answering on the final message it may hold either.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"reader_uid": _STRING,
         "leave_out": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.INTEGER))},
        ("reader_uid",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {"reader_uid": _STRING, "version": _STRING, "sent_at": _STRING,
         "calendar_refreshed_at": _STRING,
         "event_ids": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.INTEGER)),
         "left_out": StructuredSchema(ValueKind.ARRAY, items=StructuredSchema(ValueKind.INTEGER))},
        ("reader_uid", "version", "sent_at", "calendar_refreshed_at", "event_ids", "left_out"),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    _FAILURES + (
        "reader_unknown",
        "reader_schedule_unusable",
        "events_overlap",
        "schedule_too_long",
        "event_too_large",
        "device_offline",
        "device_timeout",
        "function_not_exposed",
        "reader_refused",
    ),
)

DEFINITIONS = (REFRESH_DEFINITION, READ_DEFINITION, SEND_DEFINITION)


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


def _message(fields: Mapping[str, Any]) -> str:
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def schedule_messages(
    room: str, mode: int, offset_hours: int, sessions: Sequence[ReaderSession]
) -> tuple[str, tuple[str, ...]]:
    """(version, messages) for one reader: begin, one per event, commit.

    The version is a digest of exactly what the reader will hold, so the same
    schedule always has the same version and any change gives a new one.
    """
    events = [
        {"id": item.event_id, "st": int(item.starts_at.timestamp()),
         "en": int(item.ends_at.timestamp()), "t": item.title[:TITLE_CHARACTERS],
         "fn": item.first_name[:NAME_CHARACTERS], "ln": item.last_name[:NAME_CHARACTERS],
         "hbd": item.hbd}
        for item in sessions
    ]
    header = {"n": len(events), "m": mode, "r": room[:TITLE_CHARACTERS], "o": offset_hours}
    version = hashlib.sha256(_message({**header, "events": events}).encode()).hexdigest()[:8]
    messages = [_message({"op": "begin", "v": version, **header})]
    messages.extend(
        _message({"op": "event", "v": version, "i": index, **event})
        for index, event in enumerate(events)
    )
    messages.append(_message({"op": "commit", "v": version}))
    return version, tuple(messages)


def build_reader_executors(
    fleet: ReaderFleet,
    configs: ReaderConfigSource,
    calendar: Any,
    product_ids: Sequence[int],
    call_id_source: Callable[[], str],
    clock: Callable[[], datetime] | None = None,
    control: ReaderControl | None = None,
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
        # A schedule that cannot be fetched now keeps the one last read, so a
        # BehaviorLive outage never empties a reader's day. Only BehaviorLive
        # saying the reader has no schedule removes it.
        previous_at, _, previous_readers, previous_sessions = calendar.snapshot(at)
        previous = {item.get("reader_uid"): item for item in previous_readers}
        summaries: list[dict[str, Any]] = []
        headers: dict[str, Mapping[str, Any]] = {}
        sessions: list[ReaderSession] = []
        for device in sorted(devices, key=lambda item: item.reader_uid):
            summary: dict[str, Any] = {
                "reader_uid": device.reader_uid, "device_id": device.device_id,
                "device_name": device.name,
                "product_id": device.product_id, "online": device.online,
                "last_heard": device.last_heard, "room": "", "mode": -1,
                "offset_hours": 0, "event_count": 0, "problems": (),
                "schedule_as_of": at.isoformat(),
            }
            try:
                raw = configs.config(device.reader_uid)
            except ReaderAccessError as error:
                summary["problems"] = (f"schedule_unavailable:{error.code}",)
                kept = previous.get(device.reader_uid)
                if kept is not None and error.code not in _SCHEDULE_ABSENT:
                    carried = [item for item in previous_sessions
                               if item.reader_uid == device.reader_uid]
                    sessions.extend(carried)
                    headers[device.reader_uid] = {"room": kept.get("room", "")}
                    summary.update(
                        room=kept.get("room", ""), mode=kept.get("mode", -1),
                        offset_hours=kept.get("offset_hours", 0), event_count=len(carried),
                        schedule_as_of=kept.get("schedule_as_of") or previous_at,
                        problems=(f"schedule_unavailable:{error.code}", "previous_schedule_kept"),
                    )
                summaries.append(summary)
                continue
            header, parsed, problems = parse_schedule(device.reader_uid, raw)
            headers[device.reader_uid] = header
            sessions.extend(parsed)
            summary.update(room=header["room"], mode=header["mode"],
                           offset_hours=header["offset_hours"],
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
                "last_sent": calendar.sent(reader["reader_uid"]),
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

    def send(arguments: StructuredData) -> CapabilityResult:
        at = now()
        reader_uid = arguments.get("reader_uid")
        leave_out = arguments.get("leave_out") or ()
        if (not isinstance(reader_uid, str) or not reader_uid
                or not isinstance(leave_out, (list, tuple))
                or any(not isinstance(item, int) or isinstance(item, bool) for item in leave_out)):
            return failed(SEND_READER_SCHEDULE, "arguments_unusable")
        refreshed_at, _, readers, sessions = calendar.snapshot(at)
        if not refreshed_at:
            return failed(SEND_READER_SCHEDULE, "calendar_empty")
        reader = next((item for item in readers if item.get("reader_uid") == reader_uid), None)
        if reader is None or not reader.get("device_id"):
            return failed(SEND_READER_SCHEDULE, "reader_unknown")
        if reader.get("mode") not in (0, 1) or not reader.get("room"):
            return failed(SEND_READER_SCHEDULE, "reader_schedule_unusable")
        own = [item for item in sessions if item.reader_uid == reader_uid and item.ends_at > at]
        unknown = set(leave_out) - {item.event_id for item in own}
        if unknown:
            return failed(SEND_READER_SCHEDULE, "arguments_unusable")
        chosen = sorted((item for item in own if item.event_id not in set(leave_out)),
                        key=lambda item: item.starts_at)
        overlapping = [
            f"{before.event_id},{after.event_id}"
            for before, after in zip(chosen, chosen[1:]) if after.starts_at < before.ends_at
        ]
        if overlapping:
            return CapabilityResult(
                call_id_source(), SEND_READER_SCHEDULE, CapabilityResultState.FAILED,
                failure={"code": "events_overlap", "events": tuple(overlapping)},
            )
        if len(chosen) > MAX_SCHEDULE_EVENTS:
            return failed(SEND_READER_SCHEDULE, "schedule_too_long")
        version, messages = schedule_messages(
            reader["room"], reader["mode"], int(reader.get("offset_hours") or 0), chosen)
        if any(len(message.encode()) > MAX_MESSAGE_BYTES for message in messages):
            return failed(SEND_READER_SCHEDULE, "event_too_large")
        if control is None:
            return failed(SEND_READER_SCHEDULE, "permission_denied")
        for position, message in enumerate(messages):
            final = position == len(messages) - 1
            try:
                answer = control.call_function(
                    int(reader["product_id"]), reader["device_id"], SCHEDULE_FUNCTION, message)
            except ReaderAccessError as error:
                # Before the commit the reader still holds its previous
                # schedule. A commit that was sent but went unanswered may
                # have taken: only the reader's status can say which.
                return CapabilityResult(
                    call_id_source(), SEND_READER_SCHEDULE, CapabilityResultState.FAILED,
                    failure={"code": error.code, "version": version,
                             "commit_unconfirmed": final and error.code == "device_timeout"},
                    outcome=(ExecutionOutcome.AMBIGUOUS
                             if final and error.code == "device_timeout" else None),
                )
            if answer != 0:
                return CapabilityResult(
                    call_id_source(), SEND_READER_SCHEDULE, CapabilityResultState.FAILED,
                    failure={"code": "reader_refused", "version": version,
                             "message": position, "return_value": answer},
                )
        event_ids = tuple(item.event_id for item in chosen)
        calendar.record_sent(reader_uid, version, at, event_ids,
                             tuple(session_fingerprint(item) for item in chosen))
        return CapabilityResult(
            call_id_source(), SEND_READER_SCHEDULE, CapabilityResultState.SUCCEEDED,
            {"reader_uid": reader_uid, "version": version, "sent_at": at.isoformat(),
             "calendar_refreshed_at": refreshed_at, "event_ids": event_ids,
             "left_out": tuple(leave_out)},
            provenance=provenance(at),
        )

    return {REFRESH_READER_CALENDAR: refresh, READ_READER_CALENDAR: read,
            SEND_READER_SCHEDULE: send}
