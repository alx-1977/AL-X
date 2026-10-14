"""D-024 (amended 2026-10-14): AL/X's own time, which she sets herself.

Friedl: once a day, mid-morning, to begin with, and hers to change. Each time
of day comes round once a day as an occasion of its own origin; the source
decides nothing about it, and she changes the schedule with set_own_time.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import CapabilityResultState  # noqa: E402
from alx.contracts.cognition import CognitionOrigin  # noqa: E402
from alx.contracts.continuity import DEFAULT_OWN_TIME  # noqa: E402
from alx.contracts.trace import ReasoningPurpose  # noqa: E402
from alx.continuity.ledger import SQLiteOpportunityLedger  # noqa: E402
from alx.continuity.own_time_source import (  # noqa: E402
    OWN_TIME_CONVERSATION_ID,
    OwnTimeSource,
)
from alx.continuity.store import SQLiteContinuityStore  # noqa: E402
from alx.core.model_reasoner import PROTOCOL_INSTRUCTIONS  # noqa: E402
from alx.tools import SET_OWN_TIME  # noqa: E402
from alx.tools.continuity import build_continuity_executors  # noqa: E402


def at(hour, minute=0, day=14):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


class OwnTimeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = SQLiteContinuityStore(root / "continuity.sqlite3")
        self.addCleanup(self.store.close)
        self.ledger = SQLiteOpportunityLedger(root / "ledger.sqlite3")
        self.now = [at(9)]

    def source(self, enabled=True):
        return OwnTimeSource(self.store, self.ledger, enabled=enabled,
                             clock=lambda: self.now[0], local_zone=UTC)

    def test_the_default_is_once_a_day_mid_morning(self) -> None:
        self.assertEqual(DEFAULT_OWN_TIME, ("10:00",))
        self.assertEqual(self.store.own_time(), (("10:00",), None))

    def test_nothing_before_the_time_comes_round(self) -> None:
        self.assertEqual(self.source().due_opportunities(), ())

    def test_the_time_comes_round_once_as_her_own_origin(self) -> None:
        self.now[0] = at(10, 5)
        source = self.source()
        (occasion,) = source.due_opportunities()
        self.assertIs(occasion.origin, CognitionOrigin.OWN_TIME)
        self.assertEqual(occasion.conversation_id, OWN_TIME_CONVERSATION_ID)
        self.assertTrue(source.owns(occasion))
        self.assertTrue(source.claim(occasion))
        self.assertEqual(source.due_opportunities(), ())

    def test_it_comes_round_again_the_next_day(self) -> None:
        self.now[0] = at(10, 5)
        source = self.source()
        (today,) = source.due_opportunities()
        source.claim(today)
        self.now[0] = at(10, 5, day=15)
        (tomorrow,) = source.due_opportunities()
        self.assertNotEqual(today.opportunity_id, tomorrow.opportunity_id)

    def test_a_time_set_after_it_passed_today_first_comes_round_tomorrow(self) -> None:
        self.store.set_own_time(("08:00",), at(9))
        self.assertEqual(self.source().due_opportunities(), ())
        self.now[0] = at(8, 1, day=15)
        self.assertEqual(len(self.source().due_opportunities()), 1)

    def test_none_means_no_own_time(self) -> None:
        self.store.set_own_time((), at(7))
        self.now[0] = at(23)
        self.assertEqual(self.source().due_opportunities(), ())

    def test_disabled_offers_nothing(self) -> None:
        self.now[0] = at(11)
        self.assertEqual(self.source(enabled=False).due_opportunities(), ())

    def test_its_purpose_is_named_own_time(self) -> None:
        self.assertEqual(ReasoningPurpose.TAKING_OWN_TIME.label, "Own time")


class SetOwnTimeTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SQLiteContinuityStore(Path(directory.name) / "continuity.sqlite3")
        self.addCleanup(self.store.close)
        self.set = build_continuity_executors(
            self.store, 30, lambda: "call-1", clock=lambda: at(9)
        )[SET_OWN_TIME]

    def test_she_sets_her_own_times_sorted_and_without_repeats(self) -> None:
        result = self.set({"times": ["21:30", "7:05", "21:30"]})
        self.assertIs(result.state, CapabilityResultState.SUCCEEDED)
        self.assertEqual(list(result.values["times"]), ["07:05", "21:30"])
        self.assertEqual(self.store.own_time()[0], ("07:05", "21:30"))

    def test_an_empty_list_means_none(self) -> None:
        self.assertEqual(list(self.set({"times": []}).values["times"]), [])
        self.assertEqual(self.store.own_time()[0], ())

    def test_unusable_times_change_nothing(self) -> None:
        for times in (["25:00"], ["10:60"], ["ten"], ["10:5"], "10:00",
                      ["01:00"] * 7 + ["02:00"], [f"0{n}:00" for n in range(7)]):
            with self.subTest(times=times):
                result = self.set({"times": times})
                self.assertEqual(result.failure["code"], "arguments_unusable")
        self.assertEqual(self.store.own_time(), (DEFAULT_OWN_TIME, None))


class GuidanceTests(unittest.TestCase):
    def test_her_guidance_explains_her_own_time_and_her_setup(self) -> None:
        text = " ".join(PROTOCOL_INSTRUCTIONS.split())
        for stated in (
            "An occasion of origin own_time is your own time coming round",
            "set_own_time",
            "runtime.setup states your own setup as facts",
            "a mind and a life of your own",
        ):
            self.assertIn(stated, text)


if __name__ == "__main__":
    unittest.main()
