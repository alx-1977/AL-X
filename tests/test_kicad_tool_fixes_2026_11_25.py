"""Fixes from AL/X's KiCad failures of 24-25 Nov (P0005 teardrops, P0009 RF switch)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.providers import datasheet_fetch  # noqa: E402
from alx.providers.datasheet_fetch import DatasheetError, fetch_datasheet  # noqa: E402
from alx.providers.kicad_helpers import script_runner  # noqa: E402
from alx.providers.kicad_schematic_helpers import SCHEMATIC_HELPERS  # noqa: E402
from alx.providers.kicad_steps import routing_added  # noqa: E402

BOARD = '(kicad_pcb (version 20260206)%s)'
TRACK = ' (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "A") (uuid "%s"))'


class ScriptNames(unittest.TestCase):
    def test_argv_and_pcbnew_are_in_reach(self) -> None:
        runner = script_runner("print(argv[1])")
        self.assertIn("argv=_sys.argv", runner)
        self.assertIn("pcbnew=_pcbnew", runner)


class HelpNeverKillsTheScript(unittest.TestCase):
    def setUp(self) -> None:
        self.ns = {"__name__": "test"}
        exec(compile(SCHEMATIC_HELPERS, "schematic_helpers", "exec"), self.ns)  # noqa: S102

    def test_a_sheet_method_the_instructions_name_is_found(self) -> None:
        self.assertNotIn("no helper named", self.ns["help"]("Sheet.add_wire"))

    def test_an_unknown_name_lists_the_helpers(self) -> None:
        answer = self.ns["help"]("Sheet.add")
        self.assertIn("no helper named Sheet.add", answer)
        self.assertIn("Sheet.add_wire", answer)

    def test_private_names_are_not_offered(self) -> None:
        self.assertIn("no helper named", self.ns["help"]("_sx"))


class RoutingAdded(unittest.TestCase):
    def test_copper_added_or_not(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            before, after = Path(folder, "a.kicad_pcb"), Path(folder, "b.kicad_pcb")
            before.write_text(BOARD % (TRACK % "1"))
            after.write_text(BOARD % (TRACK % "2"))  # same track, new id: nothing added
            self.assertFalse(routing_added(before, after))
            after.write_text(BOARD % (TRACK % "1" + TRACK.replace("(end 1 0)", "(end 2 0)") % "3"))
            self.assertTrue(routing_added(before, after))
            after.write_text(BOARD % "")  # copper removed only
            self.assertFalse(routing_added(before, after))


class Datasheets(unittest.TestCase):
    def setUp(self) -> None:
        datasheet_fetch._NEXAR_SPENT.clear()

    tearDown = setUp

    def test_a_miss_names_similar_datasheets_already_saved(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "M524MEA-2491516617.pdf").write_bytes(b"%PDF-1.7")
            with self.assertRaises(DatasheetError) as caught:
                fetch_datasheet("M524", "Particle", Path(folder), client=None, nexar=[], digikey=None,
                                download=lambda url: b"")
            self.assertIn("M524MEA-2491516617.pdf", caught.exception.detail)

    def test_a_spent_nexar_key_is_not_asked_again(self) -> None:
        asked = []

        class Client:
            def post(self, url, data=None, json=None, headers=None):
                asked.append(url)
                from types import SimpleNamespace
                if "token" in url:
                    return SimpleNamespace(json=lambda: {"access_token": "t"})
                return SimpleNamespace(json=lambda: {"errors": [{"message": "You have exceeded your part limit of 100."}]})

        with tempfile.TemporaryDirectory() as folder:
            for _ in range(2):
                with self.assertRaises(DatasheetError):
                    fetch_datasheet("X1", "Maker", Path(folder), client=Client(), nexar=[("k", "s")], digikey=None,
                                    download=lambda url: b"")
        self.assertEqual(len(asked), 2)  # token and query, once


def test_find_part_is_asked_afresh_because_the_library_grows_during_a_job():
    # Review of #247: a cached "not found" told her to make a part she had just added.
    from alx.specialists.board_agent import _read_key
    assert _read_key("kicad", {"action": "find_part", "query": "74LVC1G123"}) is None
    assert _read_key("kicad", {"action": "fetch_datasheet", "part": "74LVC1G123"}) is not None



def test_find_part_takes_any_one_filter():
    # Greptile on #247: every filter was required, so {"query": "comparator"} was refused.
    from alx.tools.kicad import FIND_PART
    assert FIND_PART.input_schema.required == ()


if __name__ == "__main__":
    unittest.main()
