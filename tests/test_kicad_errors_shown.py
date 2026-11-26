"""Every DRC error comes with a close-up to show Friedl (26 Nov: three were reported, never shown)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityResult, CapabilityResultState,
)
from alx.providers.kicad_errors import errors_to_show  # noqa: E402
from alx.specialists.board_agent import BoardJobAgent  # noqa: E402
from test_board_agent import KICAD, SessionModel, call, report  # noqa: E402

CLEARANCE = {"type": "clearance", "severity": "error",
             "description": "Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1000 mm)",
             "items": ["Track [/Charger.Charger_INT] on F.Cu, length 10.6000 mm",
                       "Via [/BHL_RoomReaderV2_uC/Radio_Select] on F.Cu - B.Cu"], "at": [118.851, 117.554]}
WARNING = {"type": "via_dangling", "severity": "warning", "description": "Via is not connected",
           "items": ["Via [4V2] on F.Cu - B.Cu"], "at": [195.176, 72.654]}


class ErrorsToShowTests(unittest.TestCase):
    def test_each_error_has_a_view_of_its_spot_with_its_nets_in_red(self):
        shown = errors_to_show("w" * 32, [CLEARANCE, WARNING])
        self.assertEqual(shown["count"], 1)
        view = shown["shown"][0]["view"]
        self.assertEqual(view["work_copy_id"], "w" * 32)
        self.assertEqual(view["window"], [116.351, 115.054, 121.351, 120.054])
        self.assertEqual(view["nets"], ["/BHL_RoomReaderV2_uC/Radio_Select", "/Charger.Charger_INT"])
        self.assertEqual(set(view["marks"].values()), {"bad"})

    def test_every_placed_error_is_shown(self):
        shown = errors_to_show("w" * 32, [CLEARANCE] * 9 + [dict(CLEARANCE, at=None)])
        self.assertEqual((shown["count"], len(shown["shown"])), (10, 9))

    def test_the_parts_of_a_pad_error_are_drawn(self):
        pads = dict(CLEARANCE, items=["Pad 2 [GND] of U12 on F.Cu", "Pad 1 [VCC] of C3 on F.Cu"])
        view = errors_to_show("w" * 32, [pads])["shown"][0]["view"]
        self.assertEqual(view["refs"], ["C3", "U12"])
        self.assertEqual(errors_to_show("w" * 32, [CLEARANCE])["shown"][0]["view"]["refs"], [])


class ReportCarriesErrorsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def run_job(self, revision: str):
        def dispatch(item):
            values = {"action": "check", "result": {
                "work_copy_id": "w" * 32, "work_copy_revision": revision, "totals": {"total": 1},
                "errors_to_show": errors_to_show("w" * 32, [CLEARANCE])}}
            return CapabilityAttempt(item, CapabilityAttemptDisposition.EXECUTED, True, CapabilityResult(
                item.call_id, item.capability_id, CapabilityResultState.SUCCEEDED, values))
        model = SessionModel([call({"action": "check", "work_copy_id": "w" * 32}), report("step_ready", "routing")])
        agent = BoardJobAgent(model, dispatch, (KICAD,), Path(self.folder.name))
        return agent.work(job_id=None, brief="Route the RF nets.", message=None, max_calls=10)

    def test_the_report_carries_the_errors_of_the_revision_it_reports(self):
        outcome = self.run_job("r1")
        self.assertEqual(outcome["errors_to_show"]["count"], 1)
        self.assertEqual(outcome["errors_to_show"]["shown"][0]["at"], CLEARANCE["at"])
        json.dumps(outcome)

    def test_errors_of_another_revision_are_not_carried(self):
        self.assertNotIn("errors_to_show", self.run_job("r0"))


if __name__ == "__main__":
    unittest.main()
