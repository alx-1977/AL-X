"""Read one window of a KiCad board as plain geometry, for the viewer (D-073).

The board is copied into AL/X's own storage and read there by KiCad's Python,
inside the same containment as every KiCad script; nothing runs in Friedl's
design folders. The result is geometry only - what to show and what it means
is decided by AL/X.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_SECTION_BYTES = 4_000_000
MAX_NAMES = 64

# KiCad 10's bundled Python (3.9). Layers are read by number, never by name:
# boards converted from Altium name theirs "Top Layer", "Board Shape" and so on.
SECTION_SCRIPT = r'''
import json, math, sys
from pathlib import Path
import pcbnew

board_path = Path(sys.argv[-1])
args = json.loads((board_path.parent / "section-args.json").read_text())
x0, y0, x1, y1 = args["window"]
refs, nets, planes = set(args["refs"]), set(args["nets"]), set(args["planes"])
mm, nm = pcbnew.ToMM, pcbnew.FromMM
board = pcbnew.LoadBoard(str(board_path))
COPPER = {pcbnew.F_Cu: "F", pcbnew.B_Cu: "B"}
for index in range(1, 31):
    layer = getattr(pcbnew, "In%d_Cu" % index, None)
    if layer is not None:
        COPPER[layer] = "In%d" % index

def inside(x, y):
    return x0 <= x <= x1 and y0 <= y <= y1

def crosses(shape):
    xs, ys = [p[0] for p in shape], [p[1] for p in shape]
    return min(xs) <= x1 and max(xs) >= x0 and min(ys) <= y1 and max(ys) >= y0

window = pcbnew.SHAPE_POLY_SET()
outline = pcbnew.SHAPE_LINE_CHAIN()
for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
    outline.Append(nm(x), nm(y))
outline.SetClosed(True)
window.AddOutline(outline)

def polys(shape):
    out = []
    for i in range(shape.OutlineCount()):
        o = shape.Outline(i)
        out.append({"outer": [[mm(o.CPoint(j).x), mm(o.CPoint(j).y)] for j in range(o.PointCount())],
                    "holes": [[[mm(shape.Hole(i, h).CPoint(j).x), mm(shape.Hole(i, h).CPoint(j).y)]
                               for j in range(shape.Hole(i, h).PointCount())] for h in range(shape.HoleCount(i))]})
    return out

def points(track):
    if track.GetClass() == "PCB_ARC":
        c = track.GetCenter(); r = mm(track.GetRadius())
        a0 = math.atan2(mm(track.GetStart().y - c.y), mm(track.GetStart().x - c.x))
        a1 = math.atan2(mm(track.GetEnd().y - c.y), mm(track.GetEnd().x - c.x))
        am = math.atan2(mm(track.GetMid().y - c.y), mm(track.GetMid().x - c.x))
        turn = lambda a: (a + 2 * math.pi) % (2 * math.pi)
        sweep, mid = turn(a1 - a0), turn(am - a0)
        if mid > sweep:
            sweep -= 2 * math.pi
        return [[mm(c.x) + r * math.cos(a0 + sweep * k / 8), mm(c.y) + r * math.sin(a0 + sweep * k / 8)]
                for k in range(9)]
    return [[mm(track.GetStart().x), mm(track.GetStart().y)], [mm(track.GetEnd().x), mm(track.GetEnd().y)]]

out = {"window": [x0, y0, x1, y1], "tracks": [], "vias": [], "pads": [], "parts": [],
       "planes": [], "keepouts": [], "edges": []}
for track in board.GetTracks():
    net = track.GetNetname()
    if track.GetClass() == "PCB_VIA":
        p = track.GetPosition()
        if net in nets and inside(mm(p.x), mm(p.y)):
            out["vias"].append({"at": [mm(p.x), mm(p.y)], "diameter": mm(track.GetWidth(pcbnew.F_Cu)), "net": net})
        continue
    if net not in nets or track.GetLayer() not in COPPER:
        continue
    shape = points(track)
    if crosses(shape):
        out["tracks"].append({"points": shape, "width": mm(track.GetWidth()),
                              "layer": COPPER[track.GetLayer()], "net": net})
for footprint in board.GetFootprints():
    ref = footprint.GetReference()
    if ref not in refs:
        continue
    p = footprint.GetPosition()
    side = pcbnew.B_Cu if footprint.IsFlipped() else pcbnew.F_Cu
    out["parts"].append({"ref": ref, "value": footprint.GetValue(), "at": [mm(p.x), mm(p.y)],
                         "rotation": footprint.GetOrientationDegrees(),
                         "side": "B" if footprint.IsFlipped() else "F"})
    for pad in footprint.Pads():
        shape = pcbnew.SHAPE_POLY_SET()
        pad.TransformShapeToPolygon(shape, side, 0, nm(0.005), pcbnew.ERROR_INSIDE)
        out["pads"].append({"ref": ref, "number": pad.GetNumber(), "net": pad.GetNetname(),
                            "polygons": polys(shape)})
for zone in board.Zones():
    for layer, name in COPPER.items():
        if not zone.IsOnLayer(layer):
            continue
        if zone.GetIsRuleArea():
            shape = pcbnew.SHAPE_POLY_SET(zone.Outline())
            shape.BooleanIntersection(window)
            if shape.OutlineCount():
                out["keepouts"].append({"layer": name, "polygons": polys(shape)})
        elif zone.GetNetname() in planes:
            filled = zone.GetFilledPolysList(layer)
            if filled is None:
                continue
            shape = pcbnew.SHAPE_POLY_SET(filled)
            shape.BooleanIntersection(window)
            if shape.OutlineCount():
                out["planes"].append({"layer": name, "net": zone.GetNetname(), "polygons": polys(shape)})
for drawing in board.GetDrawings():
    if drawing.GetLayer() != pcbnew.Edge_Cuts:
        continue
    try:
        if drawing.GetShape() == pcbnew.SHAPE_T_ARC:
            shape = [[mm(q.x), mm(q.y)] for q in (drawing.GetStart(), drawing.GetArcMid(), drawing.GetEnd())]
        elif drawing.GetShape() == pcbnew.SHAPE_T_POLY:
            for item in polys(drawing.GetPolyShape()):
                if crosses(item["outer"]):
                    out["edges"].append(item["outer"] + item["outer"][:1])
            continue
        else:
            shape = [[mm(drawing.GetStart().x), mm(drawing.GetStart().y)],
                     [mm(drawing.GetEnd().x), mm(drawing.GetEnd().y)]]
    except Exception:
        continue
    if crosses(shape):
        out["edges"].append(shape)
(board_path.parent / "section.json").write_text(json.dumps(out))
'''


class SectionError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def read_section(
    board: Path,
    arguments: Mapping[str, Any],
    *,
    scratch_root: Path,
    kicad_python: Path,
    execute_python: Callable[[str, Path, Path], Any],
) -> dict[str, Any]:
    """Copy one board into scratch storage, read a window of it, remove the copy."""
    window = arguments.get("window")
    if (not isinstance(window, (list, tuple)) or len(window) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in window)
            or window[0] >= window[2] or window[1] >= window[3]):
        raise SectionError("arguments_unusable", "window is [x0, y0, x1, y1] in mm, x0 < x1, y0 < y1")
    names = {}
    for key in ("refs", "nets", "planes"):
        value = arguments.get(key) or []
        if (not isinstance(value, (list, tuple)) or len(value) > MAX_NAMES
                or not all(isinstance(v, str) and v for v in value)):
            raise SectionError("arguments_unusable", f"{key} is a list of up to {MAX_NAMES} names")
        names[key] = list(value)
    if not board.is_file() or board.suffix != ".kicad_pcb":
        raise SectionError("board_unavailable", "not a .kicad_pcb file")
    run = scratch_root / ("section-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    try:
        copy = run / "board.kicad_pcb"
        shutil.copyfile(board, copy)
        (run / "section-args.json").write_text(json.dumps({"window": list(window), **names}))
        script = run / "section.py"
        script.write_text(SECTION_SCRIPT)
        executed = execute_python(str(kicad_python), script, copy)
        output = run / "section.json"
        if getattr(executed, "returncode", 1) or not output.is_file():
            raise SectionError("section_failed", (getattr(executed, "stderr", "") or "")[-2000:])
        if output.stat().st_size > MAX_SECTION_BYTES:
            raise SectionError("section_too_large", "choose a smaller window or fewer nets")
        return json.loads(output.read_text())
    finally:
        shutil.rmtree(run, ignore_errors=True)
