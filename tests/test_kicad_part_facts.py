"""Part facts read once and kept with the FireFli library (27 Nov 2026).

P0009's B-SoM to M-SoM job made 808 calls, 335 of them reads: the M-SoM datasheet
26 times, Particle's pin maps about 60. A read also reached the worker cut to 8,000
characters while next_offset pointed past all 24,000, so the text between was never seen.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from alx.config.settings import KiCadSettings  # noqa: E402
from alx.contracts import (  # noqa: E402
    CapabilityAttempt, CapabilityAttemptDisposition, CapabilityDefinition, CapabilityResult,
    CapabilityResultState, ModelCompletion, SideEffect, StructuredSchema, ValueKind,
)
from alx.providers.kicad import KiCadProvider  # noqa: E402
from alx.providers.kicad_facts import FactsError, find, part_of_document, save, view  # noqa: E402
from alx.specialists.worker import MechanicalWorker  # noqa: E402
from alx.tools.board_agent import BOARD_AGENT_KICAD_ACTIONS  # noqa: E402
from alx.tools.kicad import KICAD, build_kicad_executors  # noqa: E402

PIN36 = {"number": "36", "name": "MOSI", "function": "SPI1 MOSI (D?)", "page": 12}


class FactsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.library = Path(self.folder.name)

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_saved_facts_come_back_by_part_alias_and_datasheet_name(self) -> None:
        path, _, counts = save(self.library, "M524MEA", source="/ds/M524MEA-2491516617.pdf",
                               aliases=["M-SoM"], pins=[PIN36],
                               notes=[{"topic": "boot", "text": "RGBR low at boot enters trap mode", "page": 30}])
        self.assertEqual(counts, {"pins": 1, "notes": 1, "aliases": 1})
        self.assertEqual(path.name, "M524MEA.json")
        for name in ("M524MEA", "m524mea", "M-SoM", "msom", part_of_document("/x/M524MEA-2491516617.pdf")):
            self.assertEqual(find(self.library, name)[0], path, name)
        self.assertIsNone(find(self.library, "B524"))
        shown = view(*find(self.library, "M-SoM"))
        self.assertEqual(shown["pins"]["36"], "MOSI | SPI1 MOSI (D?) (M524MEA-2491516617.pdf p.12)")
        self.assertEqual(shown["notes"], ["boot: RGBR low at boot enters trap mode (M524MEA-2491516617.pdf p.30)"])

    def test_a_pin_given_again_replaces_and_a_known_note_is_not_doubled(self) -> None:
        save(self.library, "M524MEA", source="/ds/a.pdf", pins=[PIN36],
             notes=[{"text": "3V3 needs 500 mA", "page": 5}])
        _, data, counts = save(self.library, "M524MEA", source="/ds/a.pdf",
                               pins=[dict(PIN36, function="SPI1 MOSI, D12")],
                               notes=[{"text": "3V3  needs 500 mA", "page": 5}])
        self.assertEqual(counts["notes"], 0)
        self.assertEqual(data["pins"]["36"]["function"], "SPI1 MOSI, D12")
        self.assertEqual(len(data["notes"]), 1)
        self.assertEqual(data["sources"], ["/ds/a.pdf"])

    def test_notes_differing_only_in_a_decimal_point_or_sign_are_both_kept(self) -> None:
        notes = [{"text": "IO at 1.8 V", "page": 4}, {"text": "IO at 18 V", "page": 4},
                 {"text": "+5 V rail", "page": 4}, {"text": "-5 V rail", "page": 4}]
        _, data, counts = save(self.library, "X1", source="/ds/x.pdf", notes=notes)
        self.assertEqual(counts["notes"], 4)
        self.assertEqual(len(data["notes"]), 4)

    def test_a_part_with_many_pins_comes_in_pieces(self) -> None:
        pins = [{"number": str(n), "name": "GPIO%d" % n, "function": "f" * 250, "page": 9} for n in range(1, 201)]
        path, data, _ = save(self.library, "BIG", source="/ds/big.pdf", pins=pins,
                             notes=[{"text": "keep-out under antenna", "page": 30}])
        seen, start, pieces = [], 0, 0
        while True:
            piece = view(path, data, start)
            pieces += 1
            self.assertEqual(piece["notes"] != [], start == 0)
            self.assertLessEqual(sum(len(v) for v in piece["pins"].values()), 15_000)
            seen += list(piece["pins"])
            if "next_start" not in piece:
                break
            start = piece["next_start"]
        self.assertEqual(seen, [str(n) for n in range(1, 201)])
        self.assertGreater(pieces, 1)

    def test_every_fact_names_its_page_and_source(self) -> None:
        with self.assertRaisesRegex(FactsError, "page"):
            save(self.library, "X1", source="/ds/x.pdf", pins=[{"number": "1", "name": "VDD"}])
        with self.assertRaisesRegex(FactsError, "source"):
            save(self.library, "X1", pins=[PIN36])
        with self.assertRaisesRegex(FactsError, "longer"):
            save(self.library, "X1", source="/ds/x.pdf", notes=[{"text": "x" * 2000, "page": 1}])
        self.assertFalse((self.library / "facts" / "X1.json").exists())

    def test_an_alias_cannot_point_at_two_parts(self) -> None:
        save(self.library, "M524MEA", aliases=["SoM"])
        with self.assertRaisesRegex(FactsError, "another part"):
            save(self.library, "B524MEA", aliases=["SoM"])


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.library = root / "lib"
        (self.library / "catalogue").mkdir(parents=True)
        (self.library / "catalogue" / "FireFli_ICs.csv").write_text(
            "Reference,Value,Footprint,Datasheet,Description,Manufacturer,MPN,LCSC,Note\n"
            "U,S-82D1AAD-A8T2U7,FireFli_ICs:X,,Battery protection,ABLIC,S-82D1AAD-A8T2U7,C3681739,\n"
            "U,NLA9306,FireFli_ICs:Y,,Level shifter,onsemi,NLA9306,C1,\n")
        settings = KiCadSettings.from_environment({"ALX_KICAD_DESIGN_ROOTS": str(root / "designs")}, root / "storage")
        self.provider = KiCadProvider(settings, lambda exe, args: subprocess.run([exe, *args], capture_output=True,
                                                                                 text=True))
        self.provider._firefli_library = lambda: self.library
        counter = iter(range(1, 1000))
        self.call = build_kicad_executors(self.provider, lambda: "c%d" % next(counter))[KICAD]

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_save_then_read_through_the_tool(self) -> None:
        missing = self.call({"action": "part_facts", "part": "S-82D1AAD-A8T2U7"})
        self.assertEqual(missing.state, CapabilityResultState.SUCCEEDED)
        self.assertFalse(missing.values["result"]["found"])
        self.assertIn("save", missing.values["result"]["hint"])
        saved = self.call({"action": "part_facts", "part": "S-82D1AAD-A8T2U7", "source": "/ds/S-82D1A.pdf",
                           "pins": [{"number": "1", "name": "VDD", "page": 3}],
                           "notes": [{"topic": "thresholds", "text": "VCU 4.28 V, VDL 3.0 V", "page": 7}]})
        self.assertEqual(saved.state, CapabilityResultState.SUCCEEDED, saved.failure)
        self.assertEqual(saved.values["result"]["saved"], {"pins": 1, "notes": 1, "aliases": 0})
        again = self.call({"action": "part_facts", "part": "s82d1aad-a8t2u7"})
        self.assertEqual(again.values["result"]["pins"], {"1": "VDD (S-82D1A.pdf p.3)"})
        refused = self.call({"action": "part_facts", "part": "X", "pins": [{"number": "1", "name": "A", "page": 1}]})
        self.assertEqual(refused.failure["code"], "arguments_unusable")
        self.assertIn("source", refused.failure["recovery"]["detail"])

    def test_find_part_marks_parts_with_facts(self) -> None:
        save(self.library, "S-82D1AAD-A8T2U7", description="Li-ion protection")
        parts = self.call({"action": "find_part", "query": "ABLIC"}).values["result"]["parts"]
        self.assertTrue(parts[0]["facts_kept"])
        parts = self.call({"action": "find_part", "query": "onsemi"}).values["result"]["parts"]
        self.assertNotIn("facts_kept", parts[0])

    def test_a_datasheets_first_read_carries_its_facts_or_the_reminder(self) -> None:
        pdf = Path(self.folder.name) / "S-82D1AAD-A8T2U7-0123abcd.pdf"
        pdf.write_bytes(b"%PDF")  # never parsed here: the reader is stood in for below
        reads = {"kind": "pdf", "page_count": 40, "pages": []}
        import alx.providers.kicad as module
        original = module.read_reference
        module.read_reference = lambda arguments, **_: dict(reads)
        try:
            first = self.provider.read_reference({"path": str(pdf)})
            self.assertIn("part_facts", first["hint"])
            save(self.library, "S-82D1AAD-A8T2U7", source=str(pdf), notes=[{"text": "VCU 4.28 V", "page": 7}])
            first = self.provider.read_reference({"path": str(pdf)})
            # A pointer only, so the document's own text always arrives whole (review of #253).
            self.assertEqual(first["facts_held"]["notes"], 1)
            self.assertIn("part_facts", first["facts_held"]["hint"])
            later = self.provider.read_reference({"path": str(pdf), "offset": 5000})
            self.assertNotIn("facts_held", later)
            self.assertNotIn("hint", later)
        finally:
            module.read_reference = original

    def test_fetch_finds_facts_by_the_part_number_itself(self) -> None:
        # A part number is not a path: LM317T/NOPB is not NOPB (review of #253).
        save(self.library, "LM317T/NOPB", description="regulator")
        save(self.library, "AP2112K-3.3", description="LDO")
        self.assertEqual(self.provider._facts_kept("LM317T/NOPB")["part"], "LM317T/NOPB")
        self.assertEqual(self.provider._facts_kept("AP2112K-3.3")["part"], "AP2112K-3.3")
        self.assertIsNone(self.provider._facts_kept("NOPB"))

    def test_the_board_worker_holds_the_action(self) -> None:
        self.assertIn("part_facts", BOARD_AGENT_KICAD_ACTIONS)


class WholeReadTests(unittest.TestCase):
    """A read reaches the worker whole, so next_offset follows text she has seen."""

    def test_a_long_read_is_not_cut_but_other_results_still_are(self) -> None:
        # 22,500 characters, over 40,000 once escaped as ASCII (review of #253: still cut at 40,000).
        text = "pin ±0.5 °C – µA\n" * 1250
        obj = StructuredSchema(ValueKind.OBJECT)
        kicad = CapabilityDefinition("kicad", "Board work.", obj, obj, SideEffect.EFFECTFUL)

        def dispatch(call):
            values = {"result": {"kind": "text", "text": text, "next_offset": len(text)}}
            return CapabilityAttempt(call, CapabilityAttemptDisposition.EXECUTED, True,
                                     CapabilityResult(call.call_id, call.capability_id,
                                                      CapabilityResultState.SUCCEEDED, values))

        class Model:
            def __init__(self):
                self.steps = [{"name": "call_capability", "input": {"capability_id": "kicad", "arguments_json":
                               json.dumps({"action": action, "path": "/x"})}} for action in ("read_reference", "inspect")]
                self.requests = []

            def complete(self, request):
                self.requests.append(request)
                if self.steps:
                    return ModelCompletion("fake", "sonnet", {}, {}, tool_use=self.steps.pop(0))
                return ModelCompletion("fake", "sonnet", {"status": "done", "results": [], "open_points": [],
                                                          "files": []}, {})

        model = Model()
        MechanicalWorker(model, dispatch, (kicad,)).run("Read it.", "job", 10)
        read, other = (json.loads(r.tool_result) for r in model.requests[1:3])
        self.assertEqual(read["values"]["result"]["text"], text)
        self.assertNotIsInstance(other["values"]["result"]["text"], str)


if __name__ == "__main__":
    unittest.main()
