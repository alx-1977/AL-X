"""D-041: the BHL tile is present whenever the calendar has an event today.

Friedl wants a standing reminder on an event day without having to ask for
it. These tests prove the tile appears only on a day with events (the event's
own day), says what is running or next from the calendar alone, counts one
event once however many readers carry it, reports today's readers online, and
is served to the page; and that the calendar is refreshed in the background
through the one refresh path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.readers import ReaderCalendarPoller  # noqa: E402
from alx.config import ReaderSettings  # noqa: E402
from alx.contracts import CapabilityResult, CapabilityResultState  # noqa: E402
from alx.contracts.readers import (  # noqa: E402
    ReaderSession,
    active_windows,
    session_fingerprint,
)
from alx.interfaces.reader_tile import compose_tile, tile_source, trace_lines  # noqa: E402
from alx.interfaces.server import LiveVoiceServer  # noqa: E402
from alx.providers.reader_calendar import SQLiteReaderCalendar  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "src/alx/interfaces/assets"
REFRESHED = "2026-10-07T14:00:00+00:00"


def session(reader, event_id, start, end, title="Ethics", room="Majestic", offset=-4):
    return ReaderSession(reader, event_id, room, 0, start, end, title, "Pam", "Beesly", 0, offset)


def at(hour, minute=0, day=7):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


DAY = (
    session("ab2d5218", 1, at(14, 5), at(15, 5)),
    session("c471cf5a", 1, at(14, 5), at(15, 5)),
    session("ab2d5218", 2, at(15, 30), at(17, 10), title="Supervision"),
    session("c471cf5a", 2, at(15, 30), at(17, 10), title="Supervision"),
)
READERS = (
    {"reader_uid": "ab2d5218", "online": True},
    {"reader_uid": "c471cf5a", "online": False},
)
ONLINE = tuple({**item, "online": True} for item in READERS)
def held(*sessions, mode=0):
    """The record of a reader that accepted exactly these sessions as its day."""
    windows = active_windows(sessions, mode)
    return {"held": tuple({"fp": session_fingerprint(item, windows[item.event_id]),
                           "until": windows[item.event_id][1]} for item in sessions)}


HOLDING = {uid: held(*(item for item in DAY if item.reader_uid == uid))
           for uid in ("ab2d5218", "c471cf5a")}


def tile(now, sessions=DAY, readers=READERS, refreshed=REFRESHED, sent=None):
    return compose_tile((refreshed, (), readers, sessions), now, UTC, sent)


def problems(data):
    """Each reader with a problem, and what the problem is."""
    return {item["uid"]: item["issue"]["text"] for item in data["readers"] if item["uid"]}


class ComposeTileTests(unittest.TestCase):
    def test_no_tile_without_a_calendar_or_without_events_today(self) -> None:
        self.assertIsNone(tile(at(14, 30), refreshed=""))
        self.assertIsNone(tile(at(14, 30), sessions=()))
        self.assertIsNone(tile(at(14, 30, day=8)))

    def test_green_only_when_everything_is_confirmed(self) -> None:
        data = tile(at(14, 10), readers=ONLINE, sent=HOLDING)
        self.assertEqual((data["tone"], data["name"], data["title"], data["readers"]),
                         ("ok", "BHL", "All OK", []))

    def test_online_but_never_sent_a_schedule_is_yellow(self) -> None:
        data = tile(at(14, 10), readers=ONLINE)
        self.assertEqual((data["tone"], data["title"]), ("warn", "2 warnings"))
        self.assertEqual(problems(data), {"ab2d5218": "Schedule not confirmed",
                                          "c471cf5a": "Schedule not confirmed"})

    def test_a_reader_offline_in_session_is_red(self) -> None:
        data = tile(at(14, 30), sent=HOLDING, refreshed=at(14, 30).isoformat())
        self.assertEqual((data["tone"], data["title"]), ("bad", "1 error"))
        self.assertEqual(problems(data), {"c471cf5a": "Offline · event running"})
        reader = data["readers"][0]
        self.assertEqual(reader["issue"]["who"], "tech")
        self.assertEqual((reader["room"], reader["event"]),
                         ("Majestic · IN", {"title": "Ethics", "when": "until 15:05"}))

    def test_offline_turns_red_30_minutes_before_its_event(self) -> None:
        self.assertEqual(tile(at(13, 34), sent=HOLDING)["tone"], "warn")
        data = tile(at(13, 35), sent=HOLDING)
        self.assertEqual(data["tone"], "bad")
        self.assertEqual(problems(data), {"c471cf5a": "Offline · event at 14:05"})

    def test_offline_between_distant_events_is_yellow(self) -> None:
        sessions = (session("c471cf5a", 1, at(9), at(10)), session("c471cf5a", 2, at(16), at(17)))
        data = tile(at(12), sessions=sessions,
                    sent={"c471cf5a": held(*sessions)})
        self.assertEqual(data["tone"], "warn")
        self.assertEqual(problems(data), {"c471cf5a": "Offline"})

    def test_an_event_moved_since_it_was_sent_is_not_confirmed(self) -> None:
        moved = (DAY[0], DAY[1],
                 session("ab2d5218", 2, at(15, 45), at(17, 10), title="Supervision"), DAY[3])
        data = tile(at(14, 10), sessions=moved, readers=ONLINE, sent=HOLDING)
        self.assertEqual(data["tone"], "warn")
        self.assertEqual(problems(data), {"ab2d5218": "Schedule not confirmed"})

    def test_an_event_moved_earlier_still_runs_on_the_reader(self) -> None:
        accepted = session("ab2d5218", 1, at(14), at(15))
        later = session("ab2d5218", 2, at(16), at(17))
        moved = (session("ab2d5218", 1, at(12), at(13)), later)
        data = tile(at(14, 10), sessions=moved, readers=ONLINE[:1],
                    sent={"ab2d5218": held(accepted, later)})
        self.assertEqual(problems(data), {"ab2d5218": "Schedule not confirmed"})

    def test_an_event_deleted_from_the_calendar_still_runs_on_the_reader(self) -> None:
        kept = session("ab2d5218", 2, at(16), at(17))
        data = tile(at(14, 10), sessions=(kept,), readers=ONLINE[:1],
                    sent={"ab2d5218": held(session("ab2d5218", 1, at(14), at(15)), kept)})
        self.assertEqual(problems(data), {"ab2d5218": "Schedule not confirmed"})

    def test_events_that_ended_on_both_sides_do_not_count(self) -> None:
        done = session("ab2d5218", 1, at(9), at(10))
        kept = session("ab2d5218", 2, at(16), at(17))
        data = tile(at(14, 10), sessions=(done, kept), readers=ONLINE[:1],
                    sent={"ab2d5218": held(done, kept)})
        self.assertEqual(data["tone"], "ok")

    def test_a_change_the_reader_would_never_see_stays_confirmed(self) -> None:
        long_title = "T" * 64
        sent = (session("ab2d5218", 1, at(14, 5), at(15, 5), title=long_title + " (draft)"),)
        now = (session("ab2d5218", 1, at(14, 5), at(15, 5), title=long_title + " (final)"),)
        record = {"ab2d5218": held(sent[0])}
        data = tile(at(14, 10), sessions=now, readers=ONLINE, sent=record)
        self.assertEqual(data["tone"], "ok")

    def test_a_stale_bhl_link_is_yellow(self) -> None:
        data = tile(at(14, 16), readers=ONLINE, sent=HOLDING)
        self.assertEqual((data["tone"], data["title"]), ("warn", "1 warning"))
        link = data["readers"][0]
        self.assertEqual((link["room"], link["issue"]["text"]),
                         ("BehaviorLive", "Schedules last read 14:00"))

    def test_a_kept_schedule_counts_from_when_it_was_read(self) -> None:
        readers = ({**ONLINE[0], "schedule_as_of": "2026-10-07T13:40:00+00:00"}, ONLINE[1])
        data = tile(at(14, 10), readers=readers, sent=HOLDING)
        self.assertEqual(data["readers"][0]["issue"]["text"], "Schedules last read 13:40")

    def test_before_the_first_and_after_the_last_event_all_is_ok(self) -> None:
        self.assertEqual(tile(at(9), readers=ONLINE, sent=HOLDING,
                              refreshed="2026-10-07T09:00:00+00:00")["title"], "All OK")
        self.assertEqual(tile(at(20), readers=ONLINE, sent=HOLDING,
                              refreshed="2026-10-07T20:00:00+00:00")["title"], "All OK")

    def test_today_is_the_event_s_own_day(self) -> None:
        # 01:00 UTC on the 8th is still the evening of the 7th at -4 hours.
        evening = (session("ab2d5218", 9, at(0, 30, day=8), at(2, day=8)),)
        self.assertIsNotNone(tile(at(23, day=7), sessions=evening))
        self.assertIsNone(tile(at(23, day=8), sessions=evening))

    def test_many_readers_count_once_each_errors_first(self) -> None:
        sessions = []
        for room in range(9):
            uid = f"{room:08x}"
            sessions.append(session(uid, 100 + room, at(16), at(17), room=f"R{room}"))
        readers = tuple({"reader_uid": f"{room:08x}", "online": room >= 3} for room in range(9))
        data = tile(at(16, 30), sessions=tuple(sessions), readers=readers,
                    refreshed=at(16, 30).isoformat())
        self.assertEqual(data["title"], "3 errors · 6 warnings")
        self.assertEqual([item["tone"] for item in data["readers"]], ["bad"] * 3 + ["warn"] * 6)

    def test_without_the_monitor_al_x_is_not_said_to_act(self) -> None:
        data = tile(at(14, 10), readers=ONLINE)
        self.assertEqual({item["issue"]["action"] for item in data["readers"]}, {""})

    def test_with_the_monitor_al_x_sends_the_schedule(self) -> None:
        data = compose_tile((REFRESHED, (), ONLINE, DAY), at(14, 10), UTC, None, {})
        self.assertEqual({(item["issue"]["who"], item["issue"]["action"])
                          for item in data["readers"]}, {("alx", "Sending the schedule")})

    def test_a_reader_on_the_wrong_event_is_red(self) -> None:
        checks = {"ab2d5218": {"online": True, "fresh": True, "wrong": True}}
        data = compose_tile((REFRESHED, (), ONLINE, DAY), at(14, 10), UTC, HOLDING, checks)
        self.assertEqual((data["tone"], problems(data)),
                         ("bad", {"ab2d5218": "On the wrong event"}))

    def test_low_battery_and_weak_signal_are_yellow(self) -> None:
        checks = {"ab2d5218": {"online": True, "bat": 14, "pwr": "bat", "sig": 80},
                  "c471cf5a": {"online": True, "bat": 113, "pwr": "usb", "sig": 18}}
        data = compose_tile((REFRESHED, (), ONLINE, DAY), at(14, 10), UTC, HOLDING, checks)
        self.assertEqual(problems(data), {"ab2d5218": "Battery low · 14%",
                                          "c471cf5a": "Weak signal · 18%"})
        weak = data["readers"][1]
        self.assertEqual((weak["power"], weak["signal"]),
                         ({"source": "usb", "percent": 100}, 18))

    def test_low_battery_on_usb_is_not_a_problem(self) -> None:
        checks = {"ab2d5218": {"online": True, "bat": 14, "pwr": "usb", "sig": 80}}
        data = compose_tile((REFRESHED, (), ONLINE, DAY), at(14, 10), UTC, HOLDING, checks)
        self.assertEqual(data["readers"], [])

    def test_a_trace_shows_the_reader_s_last_steps(self) -> None:
        steps = (
            {"at": "2026-10-07T14:20:00+00:00", "kind": "status",
             "detail": {"e": 1, "bat": 96, "sig": 52, "via": "card"}},
            {"at": "2026-10-07T14:25:00+00:00", "kind": "offline", "detail": {"via": "ping"}},
        )
        asked = []

        def log_of(uid):
            asked.append(uid)
            return steps

        data = compose_tile((REFRESHED, (), READERS, DAY), at(14, 30), UTC, HOLDING, {}, log_of,
                            lambda uid, kind, cleared_by="": f"{uid} {kind} {cleared_by}")
        reader = data["readers"][0]
        self.assertEqual(reader["trace"], [["14:20:00", "card · ev 1 · bat 96% · sig 52%", ""],
                                           ["14:25:00", "offline · ping unanswered", "error"]])
        self.assertEqual(reader["since"], "c471cf5a offline online")
        # Only the reader with a problem has its log read.
        self.assertEqual(asked, ["c471cf5a"])

    def test_repeats_are_shown_once_and_older_days_carry_the_weekday(self) -> None:
        steps = [{"at": at(19, day=5).isoformat(), "kind": "offline", "detail": {"via": "ping"}},
                 {"at": at(21, day=6).isoformat(), "kind": "offline", "detail": {"via": "ping"}},
                 {"at": at(14, 25).isoformat(), "kind": "offline", "detail": {"via": "ping"}},
                 {"at": at(14, 26).isoformat(), "kind": "schedule_sent",
                  "detail": {"version": "v1", "event_ids": [1]}}]
        self.assertEqual(trace_lines(steps, UTC, at(14, 30)),
                         [["14:25:00", "offline · ping unanswered ×3", "error"],
                          ["14:26:00", "schedule v1 confirmed · 1 event", "ok"]])
        self.assertEqual(trace_lines(steps[:1], UTC, at(14, 30)),
                         [["Mon 19:00", "offline · ping unanswered", "error"]])

    def test_the_out_reader_is_named_as_such(self) -> None:
        readers = ({**ONLINE[0], "mode": 0}, {**ONLINE[1], "mode": 1})
        data = tile(at(14, 10), readers=readers)
        self.assertEqual(sorted(item["room"] for item in data["readers"]),
                         ["Majestic · IN", "Majestic · OUT"])

    def test_a_problem_began_at_its_first_logged_step(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = SQLiteReaderCalendar(Path(directory) / "calendar.sqlite3")
            uid = "c471cf5a"
            calendar.log(at(13), uid, "schedule_sent", {})
            calendar.log(at(14), uid, "schedule_delivery", {})
            calendar.log(at(14, 10), uid, "schedule_delivery", {})
            calendar.log(at(14, 5), uid, "offline", {})
            calendar.log(at(14, 6), uid, "online", {})
            calendar.log(at(14, 7), uid, "offline", {})
            for minute in range(10):
                calendar.log(at(14, 20 + minute), uid, "status", {})
            found = (calendar.log_started(uid, "schedule_delivery", "schedule_sent"),
                     calendar.log_started(uid, "offline", "online"),
                     calendar.log_started(uid, "wrong_event"))
            calendar.log(at(15), uid, "schedule_sent", {})
            cleared = calendar.log_started(uid, "schedule_delivery", "schedule_sent")
            calendar.close()
        self.assertEqual(found, (at(14).isoformat(), at(14, 7).isoformat(), None))
        self.assertIsNone(cleared)

    def test_a_malformed_reading_is_unknown_not_a_failure(self) -> None:
        checks = {"ab2d5218": {"online": True, "bat": float("nan"), "pwr": "bat",
                               "sig": float("inf")}}
        data = compose_tile((REFRESHED, (), ONLINE, DAY), at(14, 10), UTC, HOLDING, checks)
        self.assertEqual(data["readers"], [])

    def test_weak_signal_is_the_reader_s_own_doing_not_al_x_s(self) -> None:
        checks = {"ab2d5218": {"online": True, "sig": 18}}
        data = compose_tile((REFRESHED, (), ONLINE, DAY), at(14, 10), UTC, HOLDING, checks)
        self.assertEqual(data["readers"][0]["issue"]["who"], "reader")

    def test_the_tile_reads_the_stored_calendar_and_what_was_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = SQLiteReaderCalendar(Path(directory) / "calendar.sqlite3")
            calendar.replace(at(14), list(ONLINE), list(DAY), ())
            for uid in ("ab2d5218", "c471cf5a"):
                calendar.record_sent(uid, "v1", at(13), (1, 2), HOLDING[uid]["held"])
            data = tile_source(calendar, lambda: at(14, 10), UTC)()
            calendar.close()
        self.assertEqual(data["tone"], "ok")

    def test_the_last_steps_come_from_the_reader_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = SQLiteReaderCalendar(Path(directory) / "calendar.sqlite3")
            for minute in range(8):
                calendar.log(at(14, minute), "c471cf5a", "status", {"e": minute})
            calendar.log(at(14, 8), "ab2d5218", "status", {"e": 99})
            steps = calendar.log_latest("c471cf5a", 5)
            calendar.close()
        self.assertEqual([step["detail"]["e"] for step in steps], [3, 4, 5, 6, 7])


class SentRecordTests(unittest.TestCase):
    def test_a_record_from_before_this_check_confirms_nothing(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.sqlite3"
            with sqlite3.connect(path) as old:
                old.execute("CREATE TABLE sent_schedules (reader_uid TEXT PRIMARY KEY, "
                            "version TEXT NOT NULL, sent_at TEXT NOT NULL, "
                            "event_ids_json TEXT NOT NULL)")
                old.execute("INSERT INTO sent_schedules VALUES ('ab2d5218', 'v0', 't', '[1, 2]')")
            old.close()
            calendar = SQLiteReaderCalendar(path)
            record = calendar.sent("ab2d5218")
            calendar.close()
        self.assertIsNone(record["held"])
        self.assertEqual(problems(tile(at(14, 10), readers=ONLINE,
                                       sent={"ab2d5218": record,
                                             "c471cf5a": HOLDING["c471cf5a"]})),
                         {"ab2d5218": "Schedule not confirmed"})

    def test_a_legacy_record_is_not_confirmed_after_the_last_event(self) -> None:
        # CodeRabbit on #124: an empty legacy record and an empty remainder
        # must not confirm each other.
        legacy = {"ab2d5218": {"version": "v0", "held": None},
                  "c471cf5a": {"version": "v0", "held": None}}
        data = tile(at(20), readers=ONLINE, sent=legacy,
                    refreshed="2026-10-07T20:00:00+00:00")
        self.assertEqual(set(problems(data).values()), {"Schedule not confirmed"})

    def test_a_genuinely_empty_day_sent_is_confirmed_after_the_last_event(self) -> None:
        empty = {"ab2d5218": {"version": "v1", "held": ()},
                 "c471cf5a": {"version": "v1", "held": ()}}
        data = tile(at(20), readers=ONLINE, sent=empty,
                    refreshed="2026-10-07T20:00:00+00:00")
        self.assertEqual(data["tone"], "ok")


class ServingTests(unittest.TestCase):
    def server(self, reader_tile):
        return LiveVoiceServer(Mock(), "127.0.0.1", 8765, 16000, ASSETS, reader_tile=reader_tile)

    def get(self, server, path):
        return server._serve_asset(Mock(), Mock(path=path))

    def test_the_page_gets_the_tile_or_none(self) -> None:
        for source, expected in ((lambda: {"title": "BHL"}, {"title": "BHL"}),
                                 (lambda: None, None), (None, None)):
            with self.subTest(expected=expected):
                response = self.get(self.server(source), "/reader-tile.json")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(json.loads(response.body), {"tile": expected})

    def test_a_failing_tile_source_is_an_error_not_an_empty_day(self) -> None:
        def broken():
            raise RuntimeError("calendar unavailable")
        response = self.get(self.server(broken), "/reader-tile.json")
        # The page keeps its tile on a non-OK answer (reader-tile.js).
        self.assertEqual(response.status_code, 503)
        self.assertIn("if (!response.ok) return;", (ASSETS / "reader-tile.js").read_text())

    def test_the_page_asks_every_few_seconds(self) -> None:
        self.assertIn("const EVERY_MS = 5_000;", (ASSETS / "reader-tile.js").read_text())

    def test_the_main_page_carries_the_tile(self) -> None:
        page = (ASSETS / "index.html").read_text()
        self.assertIn('href="/reader-traces.css"', page)
        self.assertIn('src="/reader-tile.js"', page)
        self.assertIn("from '/reader-traces.js'", (ASSETS / "reader-tile.js").read_text())
        for path in ("/reader-tile.js", "/reader-traces.js", "/reader-traces.css",
                     "/reader-traces", "/reader-traces-fixtures.js"):
            with self.subTest(path=path):
                self.assertEqual(self.get(self.server(None), path).status_code, 200)


class PollerTests(unittest.TestCase):
    def test_a_refresh_runs_under_its_own_call_id(self) -> None:
        seen = []

        def refresh(arguments):
            seen.append(arguments)
            return CapabilityResult("reader-refresh-1", "refresh_reader_calendar",
                                    CapabilityResultState.SUCCEEDED, {})

        wrapped = []

        def with_call_id(work):
            wrapped.append(True)
            return work()

        result = ReaderCalendarPoller(refresh, 300, with_call_id).refresh_once()
        self.assertEqual((seen, wrapped, result.state),
                         ([{}], [True], CapabilityResultState.SUCCEEDED))

    def test_a_failing_refresh_does_not_stop_the_poller(self) -> None:
        calls = []

        def refresh(arguments):
            calls.append(1)
            raise RuntimeError("network")

        async def two_ticks():
            poller = ReaderCalendarPoller(refresh, 1, lambda work: work())
            task = asyncio.create_task(poller.run())
            await asyncio.sleep(1.2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(two_ticks())
        self.assertEqual(len(calls), 2)

    def test_the_interval_is_five_minutes_unless_set(self) -> None:
        base = {"PARTICLE_ACCESS_TOKEN": "t", "ALX_READER_PRODUCT_IDS": "45984"}
        self.assertEqual(ReaderSettings.from_environment(base).refresh_seconds, 300)
        self.assertEqual(ReaderSettings.from_environment(
            {**base, "ALX_READER_REFRESH_SECONDS": "60"}).refresh_seconds, 60)


if __name__ == "__main__":
    unittest.main()
