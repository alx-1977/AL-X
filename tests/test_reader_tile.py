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
from alx.contracts.readers import ReaderSession  # noqa: E402
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


def tile(now, sessions=DAY, readers=READERS, refreshed=REFRESHED):
    return compose_tile((refreshed, (), readers, sessions), now, UTC)


class ComposeTileTests(unittest.TestCase):
    def test_no_tile_without_a_calendar_or_without_events_today(self) -> None:
        self.assertIsNone(tile(at(14, 30), refreshed=""))
        self.assertIsNone(tile(at(14, 30), sessions=()))
        self.assertIsNone(tile(at(14, 30, day=8)))

    def test_a_running_event_is_named_with_its_room_and_end(self) -> None:
        data = tile(at(14, 30))
        self.assertEqual(data["state"], {"title": "Event under way",
                                         "detail": "Ethics · Majestic · until 15:05"})
        self.assertEqual(data["subtitle"], "2 events today")
        self.assertEqual(data["place"], "Majestic")
        self.assertEqual(data["activity"], "Updated 14:00")

    def test_between_events_the_next_one_is_named(self) -> None:
        self.assertEqual(tile(at(15, 15))["state"],
                         {"title": "Next event at 15:30", "detail": "Supervision · Majestic"})

    def test_before_the_first_event_the_tile_is_already_there(self) -> None:
        self.assertEqual(tile(at(9))["state"]["title"], "Next event at 14:05")

    def test_after_the_last_event_the_day_is_finished(self) -> None:
        self.assertEqual(tile(at(20))["state"],
                         {"title": "Today's events have finished", "detail": "Last ended at 17:10"})

    def test_today_is_the_event_s_own_day(self) -> None:
        # 01:00 UTC on the 8th is still the evening of the 7th at -4 hours.
        evening = (session("ab2d5218", 9, at(0, 30, day=8), at(2, day=8)),)
        self.assertIsNotNone(tile(at(23, day=7), sessions=evening))
        self.assertIsNone(tile(at(23, day=8), sessions=evening))

    def test_today_s_readers_online_are_counted(self) -> None:
        reader_fact = tile(at(14, 30))["facts"][0]
        self.assertEqual((reader_fact["value"], reader_fact["tone"]), ("1/2", "attention"))
        everyone = tuple({**item, "online": True} for item in READERS)
        self.assertEqual(tile(at(14, 30), readers=everyone)["facts"][0]["tone"], "ok")

    def test_updated_is_the_oldest_schedule_shown(self) -> None:
        readers = ({**READERS[0], "schedule_as_of": "2026-10-07T13:00:00+00:00"},
                   {**READERS[1], "schedule_as_of": "2026-10-07T14:00:00+00:00"})
        self.assertEqual(tile(at(14, 30), readers=readers)["activity"], "Updated 13:00")

    def test_unmonitored_hardware_is_shown_as_such(self) -> None:
        facts = tile(at(14, 30))["facts"]
        self.assertEqual([(item["label"], item["tone"]) for item in facts[1:]],
                         [("Registration Scanners", "disabled"), ("PSUs", "disabled")])

    def test_two_rooms_running_at_once(self) -> None:
        other = (session("deadbeef", 5, at(14), at(16), title="Law", room="Royal"),)
        self.assertEqual(tile(at(14, 30), sessions=DAY + other)["state"]["title"],
                         "2 events under way")

    def test_the_tile_reads_the_stored_calendar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            calendar = SQLiteReaderCalendar(Path(directory) / "calendar.sqlite3")
            calendar.replace(at(14), list(READERS), list(DAY), ())
            data = tile_source(calendar, lambda: at(14, 30), UTC)()
            calendar.close()
        self.assertEqual(data["state"]["title"], "Event under way")


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
