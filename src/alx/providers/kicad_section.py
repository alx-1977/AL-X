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
        kind = drawing.GetShape()
        if kind == pcbnew.SHAPE_T_ARC:
            # Through start, middle and end: the circle they lie on, sampled.
            (ax, ay), (bx, by), (cx, cy) = [(mm(q.x), mm(q.y)) for q in
                                            (drawing.GetStart(), drawing.GetArcMid(), drawing.GetEnd())]
            d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
            if abs(d) < 1e-12:
                shape = [[ax, ay], [cx, cy]]
            else:
                ux = ((ax*ax + ay*ay) * (by - cy) + (bx*bx + by*by) * (cy - ay) + (cx*cx + cy*cy) * (ay - by)) / d
                uy = ((ax*ax + ay*ay) * (cx - bx) + (bx*bx + by*by) * (ax - cx) + (cx*cx + cy*cy) * (bx - ax)) / d
                r = math.hypot(ax - ux, ay - uy)
                a0, am, a1 = (math.atan2(py - uy, px - ux) for px, py in ((ax, ay), (bx, by), (cx, cy)))
                turn = lambda a: (a + 2 * math.pi) % (2 * math.pi)
                sweep = turn(a1 - a0)
                if turn(am - a0) > sweep:
                    sweep -= 2 * math.pi
                shape = [[ux + r * math.cos(a0 + sweep * k / 24), uy + r * math.sin(a0 + sweep * k / 24)]
                         for k in range(25)]
        elif kind == pcbnew.SHAPE_T_RECT:
            (ax, ay), (bx, by) = (mm(drawing.GetStart().x), mm(drawing.GetStart().y)), \
                                 (mm(drawing.GetEnd().x), mm(drawing.GetEnd().y))
            shape = [[ax, ay], [bx, ay], [bx, by], [ax, by], [ax, ay]]
        elif kind == pcbnew.SHAPE_T_CIRCLE:
            c, r = drawing.GetCenter(), mm(drawing.GetRadius())
            shape = [[mm(c.x) + r * math.cos(2 * math.pi * k / 48), mm(c.y) + r * math.sin(2 * math.pi * k / 48)]
                     for k in range(49)]
        elif kind == pcbnew.SHAPE_T_POLY:
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


# The whole board, as facts: parts with every field, nets with what they join,
# zones and keep-outs. Looking comes before judging (D-074, 2026-11-08: the
# wrong antenna was reviewed because nothing showed her both).
INSPECT_SCRIPT = r"""
import json, sys
from pathlib import Path
import pcbnew

board_path = Path(sys.argv[-1])
args = json.loads((board_path.parent / "section-args.json").read_text())
refs, nets, needle, limit = set(args["refs"]), set(args["nets"]), args["contains"].lower(), args["limit"]
mm = pcbnew.ToMM
board = pcbnew.LoadBoard(str(board_path))
COPPER = {pcbnew.F_Cu: "F", pcbnew.B_Cu: "B"}
for index in range(1, 31):
    layer = getattr(pcbnew, "In%d_Cu" % index, None)
    if layer is not None:
        COPPER[layer] = "In%d" % index

def fields(footprint):
    out = {}
    try:
        for field in footprint.GetFields():
            name, value = field.GetName(), field.GetText()
            if value and name not in ("Reference", "Value"):
                out[name] = value
    except Exception:
        pass
    return out

parts = []
for footprint in board.GetFootprints():
    ref, value = footprint.GetReference(), footprint.GetValue()
    info = fields(footprint)
    text = " ".join([ref, value, footprint.GetFPIDAsString(), *info.values()]).lower()
    pads = [{"number": pad.GetNumber(), "net": pad.GetNetname()} for pad in footprint.Pads()]
    if refs and ref not in refs:
        continue
    if nets and not any(pad["net"] in nets for pad in pads):
        continue
    if needle and needle not in text:
        continue
    p = footprint.GetPosition()
    parts.append({"ref": ref, "value": value, "footprint": footprint.GetFPIDAsString(),
                  "side": "B" if footprint.IsFlipped() else "F", "at": [round(mm(p.x), 3), round(mm(p.y), 3)],
                  "rotation": footprint.GetOrientationDegrees(), "fields": info, "pads": pads})
# Narrowed by parts, the nets shown are the ones those parts touch.
wanted_nets = nets or ({pad["net"] for part in parts for pad in part["pads"]} if (refs or needle) else None)
joins = {}
for footprint in board.GetFootprints():
    for pad in footprint.Pads():
        joins.setdefault(pad.GetNetname(), []).append(footprint.GetReference() + "." + pad.GetNumber())
tracks, vias = {}, {}
for track in board.GetTracks():
    bucket = vias if track.GetClass() == "PCB_VIA" else tracks
    bucket[track.GetNetname()] = bucket.get(track.GetNetname(), 0) + 1
net_rows = []
for name in sorted(set(joins) | set(tracks) | set(vias)):
    if not name or (wanted_nets is not None and name not in wanted_nets):
        continue
    net_rows.append({"name": name, "pads": joins.get(name, []), "tracks": tracks.get(name, 0),
                     "vias": vias.get(name, 0)})
zones = []
# Teardrops are zones too, hundreds on a finished board; listed, they buried
# the real pours and keep-outs (2026-11-10). They are counted instead.
teardrops = 0
for zone in board.Zones():
    if zone.IsTeardropArea():
        teardrops += 1
        continue
    box = zone.GetBoundingBox()
    zones.append({"name": zone.GetZoneName(), "net": zone.GetNetname(), "keepout": bool(zone.GetIsRuleArea()),
                  "layers": [COPPER.get(layer, board.GetLayerName(layer)) for layer in zone.GetLayerSet().Seq()],
                  "extent": [round(mm(box.GetX()), 3), round(mm(box.GetY()), 3),
                             round(mm(box.GetRight()), 3), round(mm(box.GetBottom()), 3)]})
edge = board.GetBoardEdgesBoundingBox()
out = {"board": {"outline": [round(mm(edge.GetX()), 3), round(mm(edge.GetY()), 3),
                             round(mm(edge.GetRight()), 3), round(mm(edge.GetBottom()), 3)],
                 "copper_layers": [COPPER[layer] for layer in sorted(COPPER) if board.IsLayerEnabled(layer)],
                 "parts_total": len(board.GetFootprints()), "teardrops": teardrops},
       "parts": parts[:limit], "nets": net_rows[:limit], "zones": zones[:limit],
       "truncated": {"parts": len(parts) > limit, "nets": len(net_rows) > limit, "zones": len(zones) > limit}}
(board_path.parent / "section.json").write_text(json.dumps(out))
"""


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
    return _run(board, {"window": list(window), **names}, SECTION_SCRIPT, scratch_root,
                kicad_python, execute_python)


def read_overview(
    board: Path,
    arguments: Mapping[str, Any],
    *,
    scratch_root: Path,
    kicad_python: Path,
    execute_python: Callable[[str, Path, Path], Any],
) -> dict[str, Any]:
    """The whole board as facts, read from a copy that is then removed."""
    names = {}
    for key in ("refs", "nets"):
        value = arguments.get(key) or []
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) and v for v in value):
            raise SectionError("arguments_unusable", f"{key} is a list of names")
        names[key] = list(value)
    contains = arguments.get("contains") or ""
    limit = arguments.get("limit", 300)
    if not isinstance(contains, str) or isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 2000:
        raise SectionError("arguments_unusable", "contains is text; limit is 1 to 2000")
    return _run(board, {**names, "contains": contains, "limit": limit}, INSPECT_SCRIPT,
                scratch_root, kicad_python, execute_python)


def _run(board, script_args, script_text, scratch_root, kicad_python, execute_python):
    if not board.is_file() or board.suffix != ".kicad_pcb":
        raise SectionError("board_unavailable", "not a .kicad_pcb file")
    run = scratch_root / ("section-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    try:
        copy = run / "board.kicad_pcb"
        shutil.copyfile(board, copy)
        (run / "section-args.json").write_text(json.dumps(script_args))
        script = run / "section.py"
        script.write_text(script_text)
        executed = execute_python(str(kicad_python), script, copy)
        output = run / "section.json"
        if getattr(executed, "returncode", 1) or not output.is_file():
            raise SectionError("section_failed", (getattr(executed, "stderr", "") or "")[-2000:])
        if output.stat().st_size > MAX_SECTION_BYTES:
            raise SectionError("section_too_large", "choose a smaller window or fewer nets")
        return json.loads(output.read_text())
    finally:
        shutil.rmtree(run, ignore_errors=True)
