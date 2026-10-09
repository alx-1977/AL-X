"""The BHL tile: present whenever the reader calendar has an event today (D-041).

Friedl asked for a standing reminder that a BHL event day is under way, so
he does not have to remember it, small enough to stay out of the way. The
tile is a view of facts AL/X already holds, coloured by rules Friedl set
(D-041, amended 2026-10-07 and 2026-10-09):

- red: a reader whose event is running, or starts within 30 minutes, is
  offline; or a reader runs an event other than the one it should;
- yellow: anything else not confirmed: a reader offline with no event close,
  a reader whose schedule from AL/X, as it can still run it, is not exactly
  what the calendar has left, a reader on battery below 20 %, a reader whose
  signal is below 30 %, or schedules that could not be read from
  BehaviorLive for 15 minutes;
- green: none of these.

The main screen shows only a status bar across the top: the colour and one
line ("All OK", "1 error · 2 warnings"). Opened, it shows one small trace per reader with a problem:
what is wrong, for how long, what AL/X has seen and done, and who acts
next. It makes no judgement beyond those rules, and says AL/X is acting only
on what the reader monitor does on its own. "Today" is the event's own day,
taken in its schedule's offset. Times shown are in this machine's local
time, Friedl's.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, tzinfo
from typing import Any

from alx.contracts.readers import MODE_IN, ReaderSession, expected_event, holds_current


# Friedl's rules, 2026-10-07 and 2026-10-09.
WARNING_BEFORE_EVENT = timedelta(minutes=30)
LINK_STALE_AFTER = timedelta(minutes=15)
BATTERY_LOW_PERCENT = 20
WEAK_SIGNAL_PERCENT = 30  # the reader's own POOR_SIGNAL_PERCENT
# What a trace shows of a reader's log: its last few steps, a step repeated
# back to back shown once (an offline reader is logged offline again after
# each restart of AL/X), from enough of the log to fill them.
TRACE_LINES = 5
TRACE_READ = 20
MODE_NAMES = {0: "IN", 1: "OUT"}
# Who acts next on a problem: AL/X, a person, or the reader by itself.
ALX, TECHNICIAN, READER = "alx", "tech", "reader"


def _event_day(moment: datetime, offset_hours: int):
    return (moment + timedelta(hours=offset_hours)).date()


def _clock(moment: datetime, local: tzinfo | None, seconds: bool = False) -> str:
    return moment.astimezone(local).strftime("%H:%M:%S" if seconds else "%H:%M")


def _count(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _percent(value: Any) -> int | None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        return None
    return min(100, int(value))


def describe_step(step: Mapping[str, Any], local: tzinfo | None,
                  now: datetime | None = None) -> list[str]:
    """One reader log step as a trace line: time, words, tone.

    The time carries the weekday when the step was not today.
    """
    kind = step.get("kind", "")
    detail = step.get("detail") or {}
    via = detail.get("via")
    tone = ""
    if kind == "online":
        text = {"ping": "online · answered a ping"}.get(via, "online · heard from")
        tone = "ok"
    elif kind == "offline":
        text = "offline · ping unanswered" if via == "ping" else "offline"
        tone = "error"
    elif kind == "status":
        parts = ["card" if via == "card" else "status read", f"ev {detail.get('e')}"]
        battery, signal = _percent(detail.get("bat")), _percent(detail.get("sig"))
        if battery is not None:
            parts.append(f"bat {battery}%")
        if signal is not None:
            parts.append(f"sig {signal}%")
        text = " · ".join(parts)
    elif kind == "wrong_event":
        text = f"on event {detail.get('reported')} · expected {detail.get('expected')}"
        tone = "error"
    elif kind == "schedule_requested":
        text = "reader asked for its schedule"
    elif kind == "schedule_delivery":
        text = "sending schedule"
        tone = "alx"
    elif kind == "schedule_sent":
        count = len(detail.get("event_ids") or ())
        text = f"schedule {detail.get('version')} confirmed · {_count(count, 'event')}"
        tone = "ok"
    elif kind in ("schedule_send_failed", "schedule_not_sent"):
        text = f"schedule not sent · {str(detail.get('code', '')).replace('_', ' ')}"
        tone = "warn"
    elif kind == "status_unavailable":
        text = f"status unavailable · {str(detail.get('code', '')).replace('_', ' ')}"
        tone = "warn"
    else:
        text = kind.replace("_", " ")
    try:
        moment = datetime.fromisoformat(step["at"]).astimezone(local)
    except (KeyError, TypeError, ValueError):
        return ["", text, tone]
    if now is not None and moment.date() != now.astimezone(local).date():
        return [moment.strftime("%a %H:%M"), text, tone]
    return [moment.strftime("%H:%M:%S"), text, tone]


def trace_lines(steps: Sequence[Mapping[str, Any]], local: tzinfo | None,
                now: datetime) -> list[list[str]]:
    """The last TRACE_LINES lines, a line repeated back to back shown once
    (at its latest time, with how many times)."""
    lines: list[list[str]] = []
    repeats: list[int] = []
    for step in steps:
        line = describe_step(step, local, now)
        if lines and lines[-1][1:] == line[1:]:
            lines[-1][0] = line[0]
            repeats[-1] += 1
            continue
        lines.append(line)
        repeats.append(1)
    for line, count in zip(lines, repeats):
        if count > 1:
            line[1] = f"{line[1]} ×{count}"
    return lines[-TRACE_LINES:]


def compose_tile(
    snapshot: tuple[str, Sequence[str], Sequence[Mapping[str, Any]], Sequence[ReaderSession]],
    now: datetime,
    local: tzinfo | None = None,
    sent: Mapping[str, Mapping[str, Any]] | None = None,
    # D-043: what the readers themselves last reported, and what AL/X
    # observed of them, from the reader monitor. Absent when it is not
    # running: then nothing is said to be under way on AL/X's side.
    checks: Mapping[str, Mapping[str, Any]] | None = None,
    # A reader's last log steps, oldest first, and when a logged condition
    # began (calendar.log_started); asked only for readers with a problem.
    log_of: Callable[[str], Sequence[Mapping[str, Any]]] | None = None,
    started: Callable[..., str | None] | None = None,
    # Whether the monitor's regular check is running; None: whenever the
    # monitor's observations are given.
    monitoring: bool | None = None,
) -> dict[str, Any] | None:
    """The tile's data, or None when the calendar has no event today."""
    refreshed_at, _problems, readers, sessions = snapshot
    if not refreshed_at:
        return None
    today = [item for item in sessions
             if _event_day(item.starts_at, item.offset_hours) == _event_day(now, item.offset_hours)]
    if not today:
        return None
    sent = sent or {}
    if monitoring is None:
        monitoring = checks is not None
    observed = checks or {}
    summary = {item.get("reader_uid"): item for item in readers}
    in_use = sorted({item.reader_uid for item in today})

    def check(uid: str) -> Mapping[str, Any]:
        value = observed.get(uid)
        return value if isinstance(value, Mapping) else {}

    def is_online(uid: str) -> bool:
        # D-043: what AL/X observed herself (any event heard, a ping
        # answered) outranks Particle's device list, which can lag a lost
        # connection by most of an hour.
        seen = check(uid).get("online")
        if isinstance(seen, bool):
            return seen
        return summary.get(uid, {}).get("online") is True

    traces: list[dict[str, Any]] = []
    for uid in in_use:
        own = sorted((item for item in sessions if item.reader_uid == uid),
                     key=lambda item: item.starts_at)
        own_today = [item for item in own if item in today]
        mode = summary.get(uid, {}).get("mode", MODE_IN)
        held = check(uid)
        online = is_online(uid)
        running = next((item for item in own_today if item.starts_at <= now < item.ends_at), None)
        close = next((item for item in own_today
                      if item.starts_at - WARNING_BEFORE_EVENT <= now < item.ends_at), None)

        def since(kind: str, cleared_by: str = "", uid: str = uid) -> str | None:
            return started(uid, kind, cleared_by) if started is not None else None

        issue: tuple[str, str, str, str, str | None] | None = None  # tone, text, who, action, since
        if not online and close is not None:
            text = ("Offline · event running" if running is not None
                    else f"Offline · event at {_clock(close.starts_at, local)}")
            issue = ("bad", text, TECHNICIAN, "Check the reader in the room",
                     since("offline", "online"))
        elif online and held.get("fresh") is True and held.get("wrong") is True:
            issue = ("bad", "On the wrong event", TECHNICIAN, "Check the reader in the room",
                     since("wrong_event"))
        elif not online:
            issue = ("warn", "Offline", ALX, "Pinging it every minute" if monitoring else "",
                     since("offline", "online"))
        elif not holds_current(sent.get(uid) or {}, own, mode, now):
            # The monitor sends a reader that is online its schedule again,
            # every ten minutes until it holds it; not one that cannot take
            # a schedule (V1 firmware).
            if held.get("cannot_receive") is True:
                issue = ("warn", "Schedule not confirmed", TECHNICIAN,
                         "It cannot take a schedule from AL/X", None)
            else:
                issue = ("warn", "Schedule not confirmed", ALX,
                         "Sending the schedule" if monitoring else "",
                         since("schedule_delivery", "schedule_sent"))
        else:
            battery, signal = _percent(held.get("bat")), _percent(held.get("sig"))
            if held.get("pwr") == "bat" and battery is not None and battery < BATTERY_LOW_PERCENT:
                issue = ("warn", f"Battery low · {battery}%", TECHNICIAN, "Plug it into USB", None)
            elif signal is not None and signal < WEAK_SIGNAL_PERCENT:
                # Nothing AL/X does: the reader keeps its scans until
                # they are delivered.
                issue = ("warn", f"Weak signal · {signal}%", READER,
                         "Keeps scans until sent", None)
        if issue is None:
            continue

        tone, text, who, action, began = issue
        steps = list(log_of(uid)) if log_of is not None else []
        current = expected_event(own, mode, now)
        shown = next((item for item in own if item.event_id == current), None) or running or next(
            (item for item in own_today if item.starts_at > now), None)
        event = None
        if shown is not None:
            if shown.starts_at <= now < shown.ends_at:
                when = f"until {_clock(shown.ends_at, local)}"
            elif shown.starts_at > now:
                when = f"at {_clock(shown.starts_at, local)}"
            else:
                when = f"ended {_clock(shown.ends_at, local)}"
            event = {"title": shown.title, "when": when}
        power = None
        if held.get("pwr") in ("usb", "bat"):
            power = {"source": "usb" if held["pwr"] == "usb" else "battery",
                     "percent": _percent(held.get("bat"))}
        traces.append({
            "uid": uid,
            # One room has an IN and an OUT reader.
            "room": " · ".join(part for part in (
                (own_today[0].room if own_today else "") or uid, MODE_NAMES.get(mode, ""))
                if part),
            "tone": tone,
            "event": event,
            # Last known; an offline reader reports nothing new.
            "power": power,
            "signal": _percent(held.get("sig")),
            "issue": {"text": text, "who": who, "action": action},
            "since": began,
            "trace": trace_lines(steps, local, now),
        })

    read_times = [datetime.fromisoformat(summary.get(uid, {}).get("schedule_as_of") or refreshed_at)
                  for uid in in_use]
    last_read = min([datetime.fromisoformat(refreshed_at), *read_times])
    if now - last_read > LINK_STALE_AFTER:
        traces.append({
            "uid": "", "room": "BehaviorLive", "tone": "warn", "event": None,
            "power": None, "signal": None,
            "issue": {"text": f"Schedules last read {_clock(last_read, local)}", "who": ALX,
                      "action": "Reading them again every 5 minutes"},
            "since": last_read.isoformat(), "trace": [],
        })

    traces.sort(key=lambda item: (item["tone"] != "bad", item["room"]))
    errors = sum(item["tone"] == "bad" for item in traces)
    warnings = len(traces) - errors
    parts = []
    if errors:
        parts.append(_count(errors, "error"))
    if warnings:
        parts.append(_count(warnings, "warning"))
    return {
        "tone": "bad" if errors else ("warn" if warnings else "ok"),
        "name": "BHL",
        "title": " · ".join(parts) or "All OK",
        "readers": traces,
    }


def tile_source(
    calendar: Any, clock: Callable[[], datetime], local: tzinfo | None = None,
    monitor: Any = None,
) -> Callable[[], dict[str, Any] | None]:
    def current() -> dict[str, Any] | None:
        at = clock()
        snapshot = calendar.snapshot(at)
        sent = {item["reader_uid"]: calendar.sent(item["reader_uid"]) for item in snapshot[2]}
        checks = monitor.checks(at) if monitor is not None else None
        return compose_tile(snapshot, at, local, sent, checks,
                            lambda uid: calendar.log_latest(uid, TRACE_READ),
                            calendar.log_started,
                            monitoring=monitor is not None and monitor.running(at))

    return current
