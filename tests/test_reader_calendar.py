"""D-039: every BHL reader's schedule, combined into one calendar.

The calendar is the base AL/X will use to know which event each reader should
be running at any moment. These tests prove schedules are read for every
reader in the configured products, checked for mechanical faults without being
corrected, combined into one consistent snapshot, and read back by room, reader
and time, with what is running now.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.readers import (  # noqa: E402
    READER_READ_PERMISSION,
    READER_SEND_PERMISSION,
    build_reader_runtime,
)
from alx.config import ReaderSettings  # noqa: E402
from alx.contracts import CapabilityResultState, ExecutionOutcome, SideEffect  # noqa: E402
from alx.contracts.readers import ReaderAccessError, ReaderDevice  # noqa: E402
from alx.providers.behaviorlive import BehaviorLiveConfig  # noqa: E402
from alx.providers.particle import ParticleCloud  # noqa: E402
from alx.providers.reader_calendar import SQLiteReaderCalendar  # noqa: E402
from alx.tools.readers import (  # noqa: E402
    DEFINITIONS,
    MAX_MESSAGE_BYTES,
    READ_READER_CALENDAR,
    READ_READER_LOG,
    REFRESH_READER_CALENDAR,
    SEND_READER_SCHEDULE,
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

    def test_an_outage_keeps_each_reader_s_last_schedule(self) -> None:
        good = Configs({"c471cf5a": config("c471cf5a"), "ab2d5218": config("ab2d5218", mode=0)})
        self.executors(Fleet({45984: [device("c471cf5a"), device("ab2d5218")]}), good)[
            REFRESH_READER_CALENDAR]({})
        later = NOW + timedelta(minutes=5)
        outage = Configs({"c471cf5a": ReaderAccessError("connection_failed"),
                          "ab2d5218": ReaderAccessError("schedule_unreadable")})
        result = self.executors(Fleet({45984: [device("c471cf5a"), device("ab2d5218")]}),
                                outage, at=later)[REFRESH_READER_CALENDAR]({})
        self.assertEqual(result.values["session_count"], 4)
        for reader in result.values["readers"]:
            self.assertEqual(reader["event_count"], 2)
            self.assertEqual(reader["room"], "Majestic")
            self.assertEqual(reader["schedule_as_of"], NOW.isoformat())
            self.assertIn("previous_schedule_kept", reader["problems"])
        self.assertEqual(result.values["problems"], ())

    def test_a_reader_behaviorlive_no_longer_configures_loses_its_schedule(self) -> None:
        fleet = Fleet({45984: [device("c471cf5a")]})
        self.executors(fleet, Configs({"c471cf5a": config("c471cf5a")}))[REFRESH_READER_CALENDAR]({})
        result = self.executors(fleet, Configs({}))[REFRESH_READER_CALENDAR]({})
        self.assertEqual(result.values["session_count"], 0)
        self.assertEqual(result.values["readers"][0]["problems"],
                         ("schedule_unavailable:reader_not_configured",))

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

    def test_only_sending_changes_the_world(self) -> None:
        self.assertEqual({item.capability_id: item.side_effect for item in DEFINITIONS}, {
            REFRESH_READER_CALENDAR: SideEffect.NONE, READ_READER_CALENDAR: SideEffect.NONE,
            SEND_READER_SCHEDULE: SideEffect.EFFECTFUL, READ_READER_LOG: SideEffect.NONE})


class Reader:
    """A reader that answers each schedule message as the protocol says."""

    def __init__(self, answers=None, fail_at=None, error="device_timeout"):
        self.messages = []
        self.answers = answers or {}
        self.fail_at = fail_at
        self.error = error

    def call_function(self, product_id, device_id, function, argument):
        position = len(self.messages)
        self.messages.append((product_id, device_id, function, json.loads(argument)))
        if position == self.fail_at:
            raise ReaderAccessError(self.error)
        return self.answers.get(position, 0)


class SendScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.calendar = SQLiteReaderCalendar(Path(directory.name) / "calendar.sqlite3")
        self.addCleanup(self.calendar.close)

    def send(self, reader, arguments=None, events=None, at=NOW, mode=0):
        execute = build_reader_executors(
            Fleet({45984: [device("ab2d5218")]}),
            Configs({"ab2d5218": config("ab2d5218", mode=mode, events=events)}),
            self.calendar, (45984,), lambda: "call-1", clock=lambda: at, control=reader)
        execute[REFRESH_READER_CALENDAR]({})
        return execute[SEND_READER_SCHEDULE]({"reader_uid": "ab2d5218", **(arguments or {})})

    def test_a_reader_gets_begin_each_event_and_commit(self) -> None:
        reader = Reader()
        result = self.send(reader)
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(result.values["event_ids"], (3462, 1099))
        ops = [message[3] for message in reader.messages]
        self.assertEqual([item["op"] for item in ops], ["begin", "event", "event", "commit"])
        self.assertEqual(ops[0], {"op": "begin", "v": result.values["version"], "n": 2,
                                  "m": 0, "r": "Majestic", "o": -4})
        self.assertEqual(ops[1]["id"], 3462)
        self.assertEqual(ops[1]["st"], int(datetime(2026, 10, 7, 14, 5, tzinfo=UTC).timestamp()))
        self.assertEqual({item["v"] for item in ops}, {result.values["version"]})
        self.assertEqual({message[:3] for message in reader.messages},
                         {(45984, device("ab2d5218").device_id, "schedule")})
        record = self.calendar.sent("ab2d5218")
        self.assertEqual(record["version"], result.values["version"])
        # IN reader: each window closes halfway through its event.
        self.assertEqual([item["until"] for item in record["held"]],
                         [int(datetime(2026, 10, 7, 14, 35, tzinfo=UTC).timestamp()),
                          int(datetime(2026, 10, 7, 16, 20, tzinfo=UTC).timestamp())])
        self.assertEqual((ops[1]["a"], ops[1]["u"]),
                         (int(datetime(2026, 10, 7, 4, tzinfo=UTC).timestamp()),
                          int(datetime(2026, 10, 7, 14, 35, tzinfo=UTC).timestamp())))
        self.assertEqual(ops[2]["a"], ops[1]["u"])

    def test_the_same_schedule_always_has_the_same_version(self) -> None:
        first, second = self.send(Reader()), self.send(Reader())
        changed = self.send(Reader(), events=[
            event(3462, "2026-10-07T14:05:00+00:00", "2026-10-07T15:10:00+00:00")])
        self.assertEqual(first.values["version"], second.values["version"])
        self.assertNotEqual(first.values["version"], changed.values["version"])

    def test_ended_events_are_not_sent(self) -> None:
        reader = Reader()
        result = self.send(reader, at=datetime(2026, 10, 7, 15, 10, tzinfo=UTC))
        self.assertEqual(result.values["event_ids"], (1099,))

    def test_overlaps_return_to_al_x_until_she_leaves_one_out(self) -> None:
        overlapping = [
            event(1, "2026-10-07T14:00:00+00:00", "2026-10-07T15:00:00+00:00"),
            event(2, "2026-10-07T14:30:00+00:00", "2026-10-07T15:30:00+00:00")]
        reader = Reader()
        refused = self.send(reader, events=overlapping)
        self.assertEqual(refused.failure["code"], "events_overlap")
        self.assertEqual(refused.failure["events"], ("1,2",))
        self.assertEqual(reader.messages, [])
        sent = self.send(reader, {"leave_out": [2]}, events=overlapping,
                         at=datetime(2026, 10, 7, 14, 10, tzinfo=UTC))
        self.assertEqual(sent.values["event_ids"], (1,))
        self.assertEqual(sent.values["left_out"], (2,))

    def test_leaving_out_an_unknown_event_is_unusable(self) -> None:
        self.assertEqual(self.send(Reader(), {"leave_out": [99]}).failure["code"],
                         "arguments_unusable")

    def test_an_unknown_reader_is_named(self) -> None:
        execute = build_reader_executors(
            Fleet({45984: []}), Configs({}), self.calendar, (45984,), lambda: "call-1",
            clock=lambda: NOW, control=Reader())
        execute[REFRESH_READER_CALENDAR]({})
        self.assertEqual(execute[SEND_READER_SCHEDULE]({"reader_uid": "ab2d5218"})
                         .failure["code"], "reader_unknown")

    def test_a_drop_before_the_commit_leaves_the_old_schedule(self) -> None:
        reader = Reader(fail_at=1, error="device_offline")
        result = self.send(reader)
        self.assertEqual(result.failure["code"], "device_offline")
        self.assertFalse(result.failure["commit_unconfirmed"])
        self.assertIsNone(result.outcome)
        self.assertEqual(self.calendar.sent("ab2d5218"), {})

    def test_an_unanswered_commit_is_ambiguous(self) -> None:
        reader = Reader(fail_at=3)
        result = self.send(reader)
        self.assertEqual(result.failure["code"], "device_timeout")
        self.assertTrue(result.failure["commit_unconfirmed"])
        self.assertEqual(result.outcome, ExecutionOutcome.AMBIGUOUS)

    def test_a_reader_refusal_stops_the_send(self) -> None:
        reader = Reader(answers={2: -3})
        result = self.send(reader)
        self.assertEqual(result.failure["code"], "reader_refused")
        self.assertEqual((result.failure["message"], result.failure["return_value"]), (2, -3))
        self.assertEqual(len(reader.messages), 3)

    def test_long_text_is_cut_to_what_the_reader_stores(self) -> None:
        reader = Reader()
        self.send(reader, events=[event(5, "2026-10-07T16:00:00+00:00",
                                        "2026-10-07T17:00:00+00:00", title="é" * 300)])
        message = reader.messages[1][3]
        self.assertEqual(len(message["t"]), 64)
        self.assertLessEqual(len(json.dumps(message, ensure_ascii=False).encode()),
                             MAX_MESSAGE_BYTES)

    def test_a_reader_with_nothing_left_today_gets_an_empty_schedule(self) -> None:
        reader = Reader()
        result = self.send(reader, at=datetime(2026, 10, 7, 23, 0, tzinfo=UTC))
        self.assertEqual(result.values["event_ids"], ())
        self.assertEqual([item[3]["op"] for item in reader.messages], ["begin", "commit"])
        self.assertEqual(reader.messages[0][3]["n"], 0)


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

    def test_a_function_call_posts_the_argument_and_returns_its_value(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"id": "a" * 24, "connected": True, "return_value": 0}
        with patch("httpx.post", return_value=response) as post:
            self.assertEqual(ParticleCloud("token").call_function(
                45984, "a" * 24, "schedule", '{"op":"commit"}'), 0)
        self.assertEqual(post.call_args.args[0],
                         f"https://api.particle.io/v1/products/45984/devices/{'a' * 24}/schedule")
        self.assertEqual(post.call_args.kwargs["data"], {"arg": '{"op":"commit"}'})

    def test_particle_s_answers_for_unreachable_readers_are_named(self) -> None:
        for status, code in ((404, "device_offline"), (400, "function_not_exposed"),
                             (408, "device_timeout")):
            with self.subTest(status=status):
                with patch("httpx.post", return_value=Mock(status_code=status)):
                    with self.assertRaises(ReaderAccessError) as raised:
                        ParticleCloud("token").call_function(45984, "a" * 24, "schedule", "{}")
                self.assertEqual(raised.exception.code, code)

    def test_a_device_id_is_checked_before_it_reaches_a_url(self) -> None:
        with patch("httpx.post") as post:
            with self.assertRaises(ReaderAccessError):
                ParticleCloud("token").call_function(45984, "../x", "schedule", "{}")
        post.assert_not_called()

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

    def test_reading_and_sending_have_their_own_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_reader_runtime(ReaderSettings.from_environment({
                "PARTICLE_ACCESS_TOKEN": "t", "ALX_READER_PRODUCT_IDS": "44781, 45984"}),
                Path(directory), lambda: "c")
            runtime.calendar.close()
        self.assertEqual({key: policy.permission_references
                          for key, policy in runtime.policies.items()}, {
            REFRESH_READER_CALENDAR: frozenset({READER_READ_PERMISSION}),
            READ_READER_CALENDAR: frozenset({READER_READ_PERMISSION}),
            SEND_READER_SCHEDULE: frozenset({READER_SEND_PERMISSION}),
            READ_READER_LOG: frozenset({READER_READ_PERMISSION}),
            "particle_api_request": frozenset({"particle.full"}),
            "read_particle_usage": frozenset({"particle.full"})})
        self.assertFalse(runtime.policies[SEND_READER_SCHEDULE].approval_required)


if __name__ == "__main__":
    unittest.main()
