"""D-039: every BHL reader's schedule, combined into one calendar.

The calendar is the base AL/X will use to know which event each reader should
be running at any moment. These tests prove schedules are read for every
reader in the configured products, checked for mechanical faults without being
corrected, combined into one consistent snapshot, and read back by room, reader
and time, with what is running now.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.readers import READER_READ_PERMISSION, build_reader_runtime  # noqa: E402
from alx.config import ReaderSettings  # noqa: E402
from alx.contracts import CapabilityResultState, SideEffect  # noqa: E402
from alx.contracts.readers import ReaderAccessError, ReaderDevice  # noqa: E402
from alx.providers.behaviorlive import BehaviorLiveConfig  # noqa: E402
from alx.providers.particle import ParticleCloud  # noqa: E402
from alx.providers.reader_calendar import SQLiteReaderCalendar  # noqa: E402
from alx.tools.readers import (  # noqa: E402
    DEFINITIONS,
    READ_READER_CALENDAR,
    REFRESH_READER_CALENDAR,
    build_reader_executors,
    parse_schedule,
)

NOW = datetime(2026, 10, 7, 14, 30, tzinfo=UTC)


def event(event_id, start, end, title="Session"):
    return {"id": event_id, "t": title, "st": start, "en": end,
            "fn": "Pam", "ln": "Beesly", "hbd": 0}


def config(reader, mode=1, room="Majestic", events=None):
    return {"reader": reader, "mode": mode, "room": room, "offset": -4,
            "events": events if events is not None else [
                event(3462, "2026-10-07T14:05:00+00:00", "2026-10-07T15:05:00+00:00"),
                event(1099, "2026-10-07T15:30:00+00:00", "2026-10-07T17:10:00+00:00"),
            ]}


class Fleet:
    def __init__(self, devices):
        self.by_product = devices

    def devices(self, product_id):
        if product_id not in self.by_product:
            raise ReaderAccessError("product_not_found")
        return tuple(self.by_product[product_id])


class Configs:
    def __init__(self, configs):
        self.configs = configs

    def config(self, reader_uid):
        value = self.configs.get(reader_uid)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise ReaderAccessError("reader_not_configured")
        return value


def device(uid, product=45984, online=True):
    return ReaderDevice("0a10aced2021" + "0" * 4 + uid, f"reader-{uid}", product, online, "")


class ParseScheduleTests(unittest.TestCase):
    def test_a_good_schedule_has_no_problems(self) -> None:
        header, sessions, problems = parse_schedule("c471cf5a", config("c471cf5a"))
        self.assertEqual(problems, ())
        self.assertEqual(header, {"room": "Majestic", "mode": 1, "offset_hours": -4})
        self.assertEqual([item.event_id for item in sessions], [3462, 1099])
        self.assertEqual(sessions[0].starts_at, datetime(2026, 10, 7, 14, 5, tzinfo=UTC))

    def test_faults_are_reported_and_the_rest_kept(self) -> None:
        raw = config("c471cf5a", events=[
            event(1, "2026-10-07T14:00:00+00:00", "2026-10-07T15:00:00+00:00"),
            event(2, "2026-10-07T14:30:00+00:00", "2026-10-07T15:30:00+00:00"),
            event(3, "2026-10-07T16:00:00+00:00", "2026-10-07T15:00:00+00:00"),
            event(1, "2026-10-07T18:00:00+00:00", "2026-10-07T19:00:00+00:00"),
            {**event(4, "2026-10-07T20:00:00", "2026-10-07T21:00:00+00:00")},
            "not an event",
        ])
        _header, sessions, problems = parse_schedule("c471cf5a", raw)
        self.assertEqual([item.event_id for item in sessions], [1, 2])
        self.assertIn("events_overlap:1,2", problems)
        self.assertIn("event_ends_before_start:3", problems)
        self.assertIn("event_duplicated:1", problems)
        self.assertIn("event_field_invalid:4:st", problems)
        self.assertIn("event_invalid:#5", problems)

    def test_the_client_s_empty_or_wrong_header_is_named(self) -> None:
        _h, _s, problems = parse_schedule("c471cf5a", {
            "reader": "deadbeef", "mode": 3, "room": "", "offset": "x", "events": []})
        for problem in ("reader_mismatch:'deadbeef'", "mode_invalid:3", "room_missing",
                        "offset_invalid:'x'", "no_events"):
            self.assertIn(problem, problems)


class RefreshAndReadTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.calendar = SQLiteReaderCalendar(Path(directory.name) / "calendar.sqlite3")
        self.addCleanup(self.calendar.close)

    def executors(self, fleet, configs, products=(45984,), at=NOW):
        return build_reader_executors(fleet, configs, self.calendar, products,
                                      lambda: "call-1", clock=lambda: at)

    def test_every_reader_in_every_product_becomes_one_calendar(self) -> None:
        execute = self.executors(
            Fleet({45984: [device("c471cf5a"), device("ab2d5218")], 44781: []}),
            Configs({"c471cf5a": config("c471cf5a"), "ab2d5218": config("ab2d5218", mode=0)}),
            products=(44781, 45984),
        )
        result = execute[REFRESH_READER_CALENDAR]({})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["session_count"], 4)
        self.assertEqual([item["reader_uid"] for item in result.values["readers"]],
                         ["ab2d5218", "c471cf5a"])
        self.assertEqual(result.values["problems"], ())

    def test_readers_in_one_room_that_disagree_are_flagged(self) -> None:
        execute = self.executors(
            Fleet({45984: [device("c471cf5a"), device("ab2d5218")]}),
            Configs({"c471cf5a": config("c471cf5a"),
                     "ab2d5218": config("ab2d5218", events=[
                         event(9, "2026-10-07T14:00:00+00:00", "2026-10-07T15:00:00+00:00")])}),
        )
        problems = execute[REFRESH_READER_CALENDAR]({}).values["problems"]
        self.assertEqual(problems, ("room_schedules_differ:Majestic:ab2d5218,c471cf5a",))

    def test_an_unreachable_schedule_is_named_and_the_rest_still_load(self) -> None:
        execute = self.executors(
            Fleet({45984: [device("c471cf5a"), device("ab2d5218")]}),
            Configs({"c471cf5a": config("c471cf5a"),
                     "ab2d5218": ReaderAccessError("connection_failed")}),
        )
        readers = execute[REFRESH_READER_CALENDAR]({}).values["readers"]
        self.assertEqual(readers[0]["problems"], ("schedule_unavailable:connection_failed",))
        self.assertEqual(readers[1]["event_count"], 2)

    def test_what_each_reader_should_be_running_now(self) -> None:
        execute = self.executors(Fleet({45984: [device("c471cf5a")]}),
                                 Configs({"c471cf5a": config("c471cf5a")}))
        execute[REFRESH_READER_CALENDAR]({})
        result = execute[READ_READER_CALENDAR]({"room": "Majestic"})
        (running,) = result.values["now_running"]
        self.assertEqual(running["current"]["event_id"], 3462)
        self.assertEqual(running["next"]["event_id"], 1099)
        self.assertEqual(running["mode"], 1)

    def test_a_reader_between_events_has_no_current_event(self) -> None:
        execute = self.executors(Fleet({45984: [device("c471cf5a")]}),
                                 Configs({"c471cf5a": config("c471cf5a")}),
                                 at=datetime(2026, 10, 7, 15, 15, tzinfo=UTC))
        execute[REFRESH_READER_CALENDAR]({})
        (running,) = execute[READ_READER_CALENDAR]({}).values["now_running"]
        self.assertEqual(running["current"], {})
        self.assertEqual(running["next"]["event_id"], 1099)

    def test_reading_before_any_refresh_is_a_declared_failure(self) -> None:
        execute = self.executors(Fleet({45984: []}), Configs({}))
        self.assertEqual(execute[READ_READER_CALENDAR]({}).failure["code"], "calendar_empty")

    def test_a_bad_window_is_unusable(self) -> None:
        execute = self.executors(Fleet({45984: []}), Configs({}))
        execute[REFRESH_READER_CALENDAR]({})
        for arguments in ({"from": "yesterday"}, {"from": "2026-10-07T15:00:00+00:00",
                                                  "to": "2026-10-07T14:00:00+00:00"}):
            with self.subTest(arguments=arguments):
                self.assertEqual(execute[READ_READER_CALENDAR](arguments).failure["code"],
                                 "arguments_unusable")

    def test_a_refresh_replaces_the_calendar_and_survives_restart(self) -> None:
        execute = self.executors(Fleet({45984: [device("c471cf5a")]}),
                                 Configs({"c471cf5a": config("c471cf5a")}))
        execute[REFRESH_READER_CALENDAR]({})
        execute = self.executors(Fleet({45984: [device("c471cf5a")]}),
                                 Configs({"c471cf5a": config("c471cf5a", events=[
                                     event(7, "2026-10-07T16:00:00+00:00",
                                           "2026-10-07T17:00:00+00:00")])}))
        execute[REFRESH_READER_CALENDAR]({})
        _at, _p, _r, sessions = self.calendar.snapshot(NOW)
        self.assertEqual([item.event_id for item in sessions], [7])

    def test_presenter_names_expire_after_the_retention_period(self) -> None:
        execute = self.executors(Fleet({45984: [device("c471cf5a")]}),
                                 Configs({"c471cf5a": config("c471cf5a")}))
        execute[REFRESH_READER_CALENDAR]({})
        _at, _p, _r, sessions = self.calendar.snapshot(NOW + timedelta(days=31))
        self.assertEqual(sessions, ())

    def test_both_capabilities_only_read_the_world(self) -> None:
        self.assertEqual({item.side_effect for item in DEFINITIONS}, {SideEffect.NONE})


class ProviderTests(unittest.TestCase):
    def test_particle_devices_are_read_page_by_page(self) -> None:
        def page(number, ids, total):
            response = Mock(status_code=200)
            response.json.return_value = {
                "devices": [{"id": i, "name": i[-4:], "online": True} for i in ids],
                "meta": {"total_pages": total}}
            return response

        with patch("httpx.get", side_effect=[page(1, ["a" * 24], 2), page(2, ["b" * 24], 2)]) as get:
            devices = ParticleCloud("token").devices(45984)
        self.assertEqual([item.reader_uid for item in devices], ["aaaaaaaa", "bbbbbbbb"])
        self.assertEqual(get.call_args_list[0].kwargs["headers"], {"Authorization": "Bearer token"})

    def test_a_reader_uid_is_checked_before_it_reaches_a_url(self) -> None:
        with patch("httpx.get") as get:
            with self.assertRaises(ReaderAccessError) as raised:
                BehaviorLiveConfig().config("../admin")
        self.assertEqual(raised.exception.code, "reader_uid_invalid")
        get.assert_not_called()

    def test_a_missing_reader_config_is_named(self) -> None:
        with patch("httpx.get", return_value=Mock(status_code=404, content=b"")):
            with self.assertRaises(ReaderAccessError) as raised:
                BehaviorLiveConfig().config("c471cf5a")
        self.assertEqual(raised.exception.code, "reader_not_configured")


class RuntimeTests(unittest.TestCase):
    def test_it_is_absent_without_a_token_or_products(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for environment in ({}, {"PARTICLE_ACCESS_TOKEN": "t"},
                                {"ALX_READER_PRODUCT_IDS": "45984"}):
                with self.subTest(environment=environment):
                    self.assertIsNone(build_reader_runtime(
                        ReaderSettings.from_environment(environment), Path(directory),
                        lambda: "c"))

    def test_it_has_its_own_read_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_reader_runtime(ReaderSettings.from_environment({
                "PARTICLE_ACCESS_TOKEN": "t", "ALX_READER_PRODUCT_IDS": "44781, 45984"}),
                Path(directory), lambda: "c")
            runtime.calendar.close()
        self.assertEqual(set(runtime.policies), {REFRESH_READER_CALENDAR, READ_READER_CALENDAR})
        self.assertEqual({p.permission_references for p in runtime.policies.values()},
                         {frozenset({READER_READ_PERMISSION})})


if __name__ == "__main__":
    unittest.main()
