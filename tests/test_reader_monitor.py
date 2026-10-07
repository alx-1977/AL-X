"""D-043: AL/X keeps every reader on its schedule and records each step.

These tests prove the switching windows follow V1's rule; a reader asking for
its schedule is answered through the one send path; the regular check reads
each reader's status, notices a reader on the wrong event, and delivers a
schedule a reader is not holding, without repeating itself; readers without
schedule firmware are left alone until they ask; the event stream is parsed
and reconnects; and every step can be read back for a report.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap.readers import ReaderMonitor, ReaderRequestListener  # noqa: E402
from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.readers import (  # noqa: E402
    ReaderAccessError,
    ReaderSession,
    active_windows,
    expected_event,
)
from alx.interfaces.reader_tile import tile_source  # noqa: E402
from alx.providers.particle import ParticleCloud  # noqa: E402
from alx.providers.reader_calendar import SQLiteReaderCalendar  # noqa: E402
from alx.tools.readers import READ_READER_LOG, build_reader_executors  # noqa: E402

DEVICE = "e00fce683341b2e6ab2d5218"
UID = DEVICE[-8:]


def at(hour, minute=0):
    return datetime(2026, 10, 7, hour, minute, tzinfo=UTC)


def seconds(moment):
    return int(moment.timestamp())


def session(event_id, start, end, mode=0, uid=UID):
    return ReaderSession(uid, event_id, "Majestic", mode, start, end, "Talk", "Pam",
                         "Beesly", 0, -4)


DAY = (session(1, at(14), at(15)), session(2, at(15, 30), at(16, 30)),
       session(3, at(17), at(18)))


class WindowTests(unittest.TestCase):
    def test_an_in_reader_moves_on_halfway_through_each_event(self) -> None:
        windows = active_windows(DAY, 0)
        # The day opens at local midnight (UTC-4).
        self.assertEqual(windows[1], (seconds(at(4)), seconds(at(14, 30))))
        self.assertEqual(windows[2], (seconds(at(14, 30)), seconds(at(16))))
        self.assertEqual(windows[3], (seconds(at(16)), seconds(at(17, 30))))

    def test_an_out_reader_moves_on_halfway_through_the_next_event(self) -> None:
        windows = active_windows(DAY, 1)
        self.assertEqual(windows[1], (seconds(at(4)), seconds(at(16))))
        self.assertEqual(windows[2], (seconds(at(16)), seconds(at(17, 30))))
        # The last event is held until 30 minutes after it ends.
        self.assertEqual(windows[3], (seconds(at(17, 30)), seconds(at(18, 30))))

    def test_the_expected_event_follows_the_window(self) -> None:
        self.assertEqual(expected_event(DAY, 0, at(14, 40)), 2)
        self.assertEqual(expected_event(DAY, 1, at(14, 40)), 1)
        self.assertEqual(expected_event(DAY, 0, at(19)), 0)


class Particle:
    """A reader on the other end of Particle."""

    def __init__(self, status=None, refuse=None):
        self.calls = []
        self.status = status if status is not None else {"v": "", "e": 0, "n": 0}
        self.refuse = refuse
        self.reads = 0

    def call_function(self, product_id, device_id, function, argument):
        if self.refuse:
            raise ReaderAccessError(self.refuse)
        self.calls.append(json.loads(argument))
        return 0

    def read_variable(self, product_id, device_id, name):
        self.reads += 1
        if isinstance(self.status, Exception):
            raise self.status
        return json.dumps(self.status)


class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.calendar = SQLiteReaderCalendar(Path(directory.name) / "calendar.sqlite3")
        self.addCleanup(self.calendar.close)
        self.now = [at(14, 10)]

    def build(self, particle, online=True, sessions=DAY):
        self.calendar.replace(at(14), [{
            "reader_uid": UID, "device_id": DEVICE, "product_id": 45984, "online": online,
            "room": "Majestic", "mode": 0, "offset_hours": -4,
        }], list(sessions), ())
        clock = lambda: self.now[0]
        executors = build_reader_executors(Mock(), Mock(), self.calendar, (45984,),
                                           lambda: "call-1", clock=clock, control=particle)
        self.executors = executors
        return ReaderMonitor(executors, self.calendar, particle, lambda work: work(),
                             clock=clock)

    def steps(self):
        return [step["kind"] for step in self.calendar.log_between(at(0), at(23))[0]]

    def test_a_request_is_answered_with_the_schedule(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        monitor.on_event("roomreader/schedule_request", DEVICE, '{"v":""}', "t")
        self.assertEqual([call["op"] for call in particle.calls],
                         ["begin", "event", "event", "event", "commit"])
        self.assertEqual(self.steps(),
                         ["schedule_requested", "schedule_delivery", "schedule_sent"])
        self.assertTrue(self.calendar.sent(UID)["held"])

    def test_a_sent_schedule_is_stamped_when_it_finished_with_its_duration(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        original = particle.call_function

        def slow(*arguments):
            self.now[0] += timedelta(seconds=1.2)  # one cellular round trip
            return original(*arguments)

        particle.call_function = slow
        monitor.on_event("roomreader/schedule_request", DEVICE, '{"v":""}', "t")
        sent = [s for s in self.calendar.log_between(at(0), at(23))[0]
                if s["kind"] == "schedule_sent"][0]
        self.assertEqual(sent["detail"]["seconds"], 6.0)
        self.assertEqual(sent["detail"]["messages"], 5)
        self.assertEqual(sent["at"], (at(14, 10) + timedelta(seconds=6)).isoformat())
        self.assertEqual(sent["detail"]["started_at"], at(14, 10).isoformat())

    def test_a_repeated_request_within_a_minute_is_not_answered_twice(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        monitor.on_event("roomreader/schedule_request", DEVICE, '{"v":""}', "t")
        monitor.on_event("roomreader/schedule_request", DEVICE, '{"v":""}', "t")
        self.assertEqual(sum(call["op"] == "commit" for call in particle.calls), 1)

    def test_a_request_during_a_send_does_not_start_a_second_one(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        original = particle.call_function
        nested = []

        def call(*arguments):
            # Mid-send, the same reader asks again (another stream) and the
            # regular check runs: neither may start a second send.
            if not nested:
                nested.append(True)
                self.now[0] += timedelta(minutes=15)
                monitor.on_event("roomreader/schedule_request", DEVICE, "{}", "t")
                monitor.cycle()
            return original(*arguments)

        particle.call_function = call
        monitor.on_event("roomreader/schedule_request", DEVICE, "{}", "t")
        self.assertEqual(sum(c["op"] == "begin" for c in particle.calls), 1)
        self.assertEqual(self.steps().count("schedule_delivery"), 1)

    def test_a_send_that_never_started_does_not_hold_back_the_next(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        original = self.calendar.log
        failed = []

        def log(at, uid, kind, detail=None):
            if kind == "schedule_delivery" and not failed:
                failed.append(True)
                raise RuntimeError("database locked")
            return original(at, uid, kind, detail)

        self.calendar.log = log
        with self.assertRaises(RuntimeError):
            monitor.cycle()
        self.assertEqual(particle.calls, [])
        self.now[0] += timedelta(minutes=5)  # within the ten-minute gap
        monitor.cycle()
        self.assertEqual(sum(c["op"] == "commit" for c in particle.calls), 1)

    def test_a_send_that_began_keeps_its_time(self) -> None:
        particle = Particle(refuse="device_timeout")
        monitor = self.build(particle)
        monitor.cycle()
        self.now[0] += timedelta(minutes=5)
        monitor.cycle()
        self.assertEqual(self.steps().count("schedule_delivery"), 1)

    def test_other_events_are_ignored(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        monitor.on_event("roomreader/scan", DEVICE, "{}", "t")
        self.assertEqual((particle.calls, self.steps()), ([], []))

    def test_the_check_delivers_to_a_reader_not_holding_its_schedule(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        monitor.cycle()
        self.assertIn("schedule_sent", self.steps())
        self.assertEqual(self.steps()[:2], ["online", "status"])

    def test_a_reader_holding_its_schedule_is_left_alone(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        monitor.on_event("roomreader/schedule_request", DEVICE, "{}", "t")
        version = self.calendar.sent(UID)["version"]
        particle.status = {"v": version, "e": 2, "n": 3}  # 14:30: halfway, IN moves on
        particle.calls.clear()
        self.now[0] += timedelta(minutes=20)
        monitor.cycle()
        self.assertEqual(particle.calls, [])
        self.assertNotIn("wrong_event", self.steps())

    def test_a_reader_on_the_wrong_event_is_recorded(self) -> None:
        particle = Particle(status={"v": "x", "e": 3, "n": 3})
        monitor = self.build(particle)
        monitor.cycle()
        wrong = [s for s in self.calendar.log_between(at(0), at(23))[0]
                 if s["kind"] == "wrong_event"]
        self.assertEqual(wrong[0]["detail"], {"expected": 1, "reported": 3})
        self.assertEqual(monitor.checks(self.now[0])[UID]["event"], 3)

    def test_unasked_deliveries_are_spaced_out(self) -> None:
        particle = Particle(refuse="device_timeout")
        monitor = self.build(particle)
        monitor.cycle()
        self.now[0] += timedelta(minutes=5)
        monitor.cycle()
        self.assertEqual(self.steps().count("schedule_delivery"), 1)

    def test_a_reader_without_schedule_firmware_is_left_until_it_asks(self) -> None:
        particle = Particle(status=ReaderAccessError("variable_not_exposed"),
                            refuse="function_not_exposed")
        monitor = self.build(particle)
        monitor.cycle()
        self.now[0] += timedelta(minutes=30)
        monitor.cycle()
        self.assertEqual(self.steps().count("schedule_delivery"), 1)
        # A repeated failure to read status is recorded once.
        self.assertEqual(self.steps().count("status_unavailable"), 1)

    def test_an_offline_reader_is_recorded_once_and_not_read(self) -> None:
        particle = Particle()
        monitor = self.build(particle, online=False)
        monitor.cycle()
        monitor.cycle()
        self.assertEqual((self.steps(), particle.reads), (["offline"], 0))

    def test_the_tile_says_al_x_is_monitoring_and_turns_red_on_a_wrong_event(self) -> None:
        particle = Particle(status={"v": "x", "e": 3, "n": 3})
        monitor = self.build(particle)
        monitor.cycle()
        data = tile_source(self.calendar, lambda: self.now[0], UTC, monitor=monitor)()
        self.assertEqual(data["tone"], "bad")
        self.assertEqual(data["state"]["title"], "1 room reader on the wrong event")
        self.assertTrue(data["alx"]["text"].startswith("monitoring"))

    def test_every_step_can_be_read_back(self) -> None:
        particle = Particle()
        monitor = self.build(particle)
        monitor.on_event("roomreader/schedule_request", DEVICE, '{"v":""}', "t")
        result = self.executors[READ_READER_LOG]({"from": at(0).isoformat(),
                                                  "to": at(23).isoformat()})
        self.assertEqual(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual([step["kind"] for step in result.values["steps"]],
                         ["schedule_requested", "schedule_delivery", "schedule_sent"])
        self.assertEqual(self.executors[READ_READER_LOG]({"reader_uid": "other"})
                         .values["steps"], ())
        self.assertEqual(self.executors[READ_READER_LOG]({"from": "soon"}).failure["code"],
                         "arguments_unusable")


class StreamTests(unittest.TestCase):
    def test_the_event_stream_is_parsed(self) -> None:
        lines = [":ok", "", "event: roomreader/schedule_request",
                 'data: {"data":"{\\"v\\":\\"\\"}","ttl":60,"published_at":"t1","coreid":"'
                 + DEVICE + '"}', "", "event: x", "data: not json", ""]
        response = MagicMock(status_code=200)
        response.iter_lines.return_value = iter(lines)
        stream = MagicMock()
        stream.__enter__.return_value = response
        seen = []
        with patch("httpx.stream", return_value=stream) as opened:
            ParticleCloud("token").stream_events(45984, "roomreader/schedule_request",
                                                 lambda *event: seen.append(event))
        self.assertEqual(seen, [("roomreader/schedule_request", DEVICE, '{"v":""}', "t1")])
        self.assertTrue(opened.call_args.args[1].endswith(
            "/v1/products/45984/events/roomreader%2Fschedule_request"))

    def test_a_dropped_stream_reconnects_with_backoff(self) -> None:
        particle = Mock()
        particle.stream_events.side_effect = ReaderAccessError("connection_failed")
        waits = []
        ReaderRequestListener(particle, (45984,), Mock(), sleep=waits.append).listen(45984, 3)
        self.assertEqual(particle.stream_events.call_count, 3)
        self.assertEqual(waits, [10.0, 20.0, 40.0])

    def test_a_variable_is_read(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"name": "status", "result": '{"v":"x"}'}
        with patch("httpx.get", return_value=response) as get:
            self.assertEqual(ParticleCloud("token").read_variable(45984, DEVICE, "status"),
                             '{"v":"x"}')
        self.assertTrue(get.call_args.args[0].endswith(f"/devices/{DEVICE}/status"))


if __name__ == "__main__":
    unittest.main()
