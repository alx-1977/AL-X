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
from alx.interfaces.reader_tile import compose_tile, tile_source  # noqa: E402
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


class ComposeTileTests(unittest.TestCase):
    def test_no_tile_without_a_calendar_or_without_events_today(self) -> None:
        self.assertIsNone(tile(at(14, 30), refreshed=""))
        self.assertIsNone(tile(at(14, 30), sessions=()))
        self.assertIsNone(tile(at(14, 30, day=8)))

    def test_green_only_when_everything_is_confirmed(self) -> None:
        data = tile(at(14, 10), readers=ONLINE, sent=HOLDING)
        self.assertEqual(data["tone"], "ok")
        self.assertEqual(data["state"], {"title": "All systems normal",
                                         "detail": "Event under way until 15:05 · next starts 15:30"})
        self.assertEqual([chip["tone"] for chip in data["chips"]], ["ok", "ok"])

    def test_online_but_never_sent_a_schedule_is_yellow(self) -> None:
        data = tile(at(14, 10), readers=ONLINE)
        self.assertEqual(data["tone"], "warn")
        self.assertEqual(data["state"]["title"], "Schedules not confirmed")

    def test_a_reader_offline_in_session_is_red(self) -> None:
        data = tile(at(14, 30), sent=HOLDING)
        self.assertEqual(data["tone"], "bad")
        self.assertEqual(data["state"]["title"], "1 room reader offline")
        self.assertEqual((data["chips"][0]["value"], data["chips"][0]["tone"]), ("1/2", "bad"))

    def test_offline_turns_red_30_minutes_before_its_event(self) -> None:
        self.assertEqual(tile(at(13, 34), sent=HOLDING)["tone"], "warn")
        self.assertEqual(tile(at(13, 35), sent=HOLDING)["tone"], "bad")

    def test_offline_between_distant_events_is_yellow(self) -> None:
        sessions = (session("c471cf5a", 1, at(9), at(10)), session("c471cf5a", 2, at(16), at(17)))
        data = tile(at(12), sessions=sessions,
                    sent={"c471cf5a": held(*sessions)})
        self.assertEqual((data["tone"], data["chips"][0]["tone"]), ("warn", "warn"))
        self.assertEqual(data["state"]["detail"], "Next event at 16:00")

    def test_an_event_moved_since_it_was_sent_is_not_confirmed(self) -> None:
        moved = (DAY[0], DAY[1],
                 session("ab2d5218", 2, at(15, 45), at(17, 10), title="Supervision"), DAY[3])
        data = tile(at(14, 10), sessions=moved, readers=ONLINE, sent=HOLDING)
        self.assertEqual((data["tone"], data["state"]["title"]),
                         ("warn", "Schedules not confirmed"))

    def test_an_event_moved_earlier_still_runs_on_the_reader(self) -> None:
        accepted = session("ab2d5218", 1, at(14), at(15))
        later = session("ab2d5218", 2, at(16), at(17))
        moved = (session("ab2d5218", 1, at(12), at(13)), later)
        data = tile(at(14, 10), sessions=moved, readers=ONLINE[:1],
                    sent={"ab2d5218": held(accepted, later)})
        self.assertEqual(data["state"]["title"], "Schedules not confirmed")

    def test_an_event_deleted_from_the_calendar_still_runs_on_the_reader(self) -> None:
        kept = session("ab2d5218", 2, at(16), at(17))
        data = tile(at(14, 10), sessions=(kept,), readers=ONLINE[:1],
                    sent={"ab2d5218": held(session("ab2d5218", 1, at(14), at(15)), kept)})
        self.assertEqual(data["state"]["title"], "Schedules not confirmed")

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
        self.assertEqual(data["tone"], "warn")
        self.assertEqual(data["state"]["title"], "BHL link not answering")
        self.assertEqual(data["chips"][1]["tone"], "warn")
        self.assertEqual(data["chips"][1]["label"],
                         "Schedules last read from BehaviorLive at 14:00")

    def test_a_kept_schedule_counts_from_when_it_was_read(self) -> None:
        readers = ({**ONLINE[0], "schedule_as_of": "2026-10-07T13:40:00+00:00"}, ONLINE[1])
        self.assertEqual(tile(at(14, 10), readers=readers, sent=HOLDING)["state"]["title"],
                         "BHL link not answering")

    def test_before_the_first_event(self) -> None:
        data = tile(at(9), readers=ONLINE, sent=HOLDING, refreshed="2026-10-07T09:00:00+00:00")
        self.assertEqual(data["state"]["detail"], "First event at 14:05")

    def test_after_the_last_event_the_day_is_finished(self) -> None:
        data = tile(at(20), readers=ONLINE, sent=HOLDING, refreshed="2026-10-07T20:00:00+00:00")
        self.assertEqual(data["state"]["detail"], "Today's events have finished")
        self.assertEqual(data["tone"], "ok")

    def test_today_is_the_event_s_own_day(self) -> None:
        # 01:00 UTC on the 8th is still the evening of the 7th at -4 hours.
        evening = (session("ab2d5218", 9, at(0, 30, day=8), at(2, day=8)),)
        self.assertIsNotNone(tile(at(23, day=7), sessions=evening))
        self.assertIsNone(tile(at(23, day=8), sessions=evening))

    def test_a_full_day_summarises_rooms_not_events(self) -> None:
        sessions = []
        for room in range(9):
            uid = f"{room:08x}"
            sessions.append(session(uid, 100 + room, at(14), at(15), room=f"R{room}"))
            if room < 6:
                sessions.append(session(uid, 200 + room, at(15, 30), at(16), room=f"R{room}"))
        data = tile(at(14, 30), sessions=tuple(sessions), readers=())
        self.assertEqual(data["context"], "15 events today · 9 rooms")
        self.assertEqual(data["state"]["detail"],
                         "9 rooms in session · next starts 15:30 (6 rooms) · schedules not confirmed")
        self.assertEqual(data["chips"][0]["value"], "0/9")

    def test_al_x_says_she_is_not_monitoring_yet(self) -> None:
        self.assertEqual(tile(at(14, 30))["alx"], {"text": "not monitoring yet", "idle": True})

    def test_the_tile_reads_the_stored_calendar_and_what_was_sent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = SQLiteReaderCalendar(Path(directory) / "calendar.sqlite3")
            calendar.replace(at(14), list(ONLINE), list(DAY), ())
            for uid in ("ab2d5218", "c471cf5a"):
                calendar.record_sent(uid, "v1", at(13), (1, 2), HOLDING[uid]["held"])
            data = tile_source(calendar, lambda: at(14, 10), UTC)()
            calendar.close()
        self.assertEqual(data["tone"], "ok")


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
        self.assertEqual(tile(at(14, 10), readers=ONLINE, sent={"ab2d5218": record,
                                                                "c471cf5a": HOLDING["c471cf5a"]})
                         ["state"]["title"], "Schedules not confirmed")

    def test_a_legacy_record_is_not_confirmed_after_the_last_event(self) -> None:
        # CodeRabbit on #124: an empty legacy record and an empty remainder
        # must not confirm each other.
        legacy = {"ab2d5218": {"version": "v0", "held": None},
                  "c471cf5a": {"version": "v0", "held": None}}
        data = tile(at(20), readers=ONLINE, sent=legacy,
                    refreshed="2026-10-07T20:00:00+00:00")
        self.assertEqual(data["state"]["title"], "Schedules not confirmed")

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

    def test_the_main_page_carries_the_tile(self) -> None:
        page = (ASSETS / "index.html").read_text()
        self.assertIn('href="/tile.css"', page)
        self.assertIn('id="tiles"', page)
        self.assertIn('src="/reader-tile.js"', page)
        self.assertEqual(self.get(self.server(None), "/reader-tile.js").status_code, 200)


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
