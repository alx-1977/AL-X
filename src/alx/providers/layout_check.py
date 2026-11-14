"""A board layout checked against the manufacturer's drawing (2026-11-13).

Friedl, after AL/X twice showed layouts with a clean DRC that differed from
Würth's drawing: the tool measures, not her. The numbers on a datasheet
drawing are text in the PDF with their place on the page, so every one is
listed mechanically and none can be skipped. She says once what each number
measures on the board (her reading); the board is measured here, by KiCad,
and compared. Within 0.05 mm is a match. A number she has not accounted for
keeps the check open. For RF and high-speed analog parts her reading counts
only after Friedl has confirmed it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from alx.providers.kicad_section import _run

TOLERANCE_MM = 0.05
MAX_NUMBERS = 80
AXES = ("x", "y")
ANCHORS = ("centre", "left", "right", "top", "bottom")
# A number followed by one of these is a part value, not a dimension.
UNITS = {"pf", "nf", "uf", "µf", "ph", "nh", "uh", "µh", "mh", "ω", "ohm", "kω", "mω", "hz", "khz",
         "mhz", "ghz", "db", "dbi", "dbm", "v", "mv", "a", "ma", "w", "mw", "%", "°c", "s", "ms"}
_NUMBER = re.compile(r"^\d{1,4}(?:[.,]\d{1,4})?$")
# pdftotext -tsv, one row per word: level 5, then left, top, width and height
# in points and the word. -bbox crashed on every page of an onsemi datasheet
# (poppler 26.04, 14 Nov) where -tsv reads the same boxes.
_WORD_LEVEL = "5"


class LayoutCheckError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def number_id(box: list[float]) -> str:
    """A number's name: where it is printed, in whole points. Stable for one PDF page."""
    return "%d:%d" % (round(box[0]), round(box[1]))


def read_numbers(words: list[tuple[float, float, float, float, str]],
                 section: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """Every number printed inside the section, in reading order, each with its place.

    Tolerances (±0.1, +0.1, -0.2) and counts (4x) are not numbers of their own.
    A number followed closely on its line by a unit (1.8 pF) is a part value.
    """
    x0, y0, x1, y1 = section
    inside = [word for word in words if word[0] >= x0 and word[2] <= x1 and word[1] >= y0 and word[3] <= y1]
    found = []
    for index, (wx0, wy0, wx1, wy1, token) in enumerate(inside):
        if not _NUMBER.match(token):
            continue
        unit = ""
        for nx0, ny0, _, _, following in inside[index + 1:index + 3]:
            # Beside it on its line, or below it where the drawing's text runs sideways.
            beside = abs(ny0 - wy0) < 2 and 0 <= nx0 - wx1 < 6
            below = abs(nx0 - wx0) < 2 and 0 <= ny0 - wy1 < 6
            if (beside or below) and following.casefold() in UNITS:
                unit = following
                break
        box = [round(wx0, 1), round(wy0, 1), round(wx1, 1), round(wy1, 1)]
        found.append({"id": number_id(box), "text": token, "value": float(token.replace(",", ".")),
                      "at": box, "unit": unit})
    found.sort(key=lambda item: (round(item["at"][1] / 4), item["at"][0]))
    return found


def extract_words(document: Path, page: int, *, scratch_root: Path, extractor: Path,
                  execute: Callable[[Path, list[str], Path], Any]) -> list[tuple[float, float, float, float, str]]:
    """The words of one PDF page with their boxes in points, read from a confined copy."""
    if not document.is_file() or document.suffix.lower() != ".pdf":
        raise LayoutCheckError("document_unavailable", "not a PDF file")
    if not extractor.is_file():
        raise LayoutCheckError("engine_unavailable", "no PDF text reader installed")
    run = scratch_root / ("words-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    try:
        shutil.copyfile(document, run / "document.pdf")
        executed = execute(extractor, ["-f", page, "-l", page, "-tsv", "document.pdf", "words.tsv"], run)
        output = run / "words.tsv"
        if getattr(executed, "returncode", 1) or not output.is_file():
            raise LayoutCheckError("page_failed", (getattr(executed, "stderr", "") or "")[-1000:])
        words = []
        # split on newlines only: splitlines would also cut a word at U+2028 or a form feed
        for row in output.read_text(errors="replace").split("\n")[1:]:
            cells = row.split("\t")
            if len(cells) < 12 or cells[0] != _WORD_LEVEL:
                continue
            try:
                left, top, width, height = (float(cell) for cell in cells[6:10])
            except ValueError:
                continue
            text = "\t".join(cells[11:]).strip()
            if text:
                words.append((left, top, left + width, top + height, text))
        return words
    finally:
        shutil.rmtree(run, ignore_errors=True)


def _at(text: str) -> list[float]:
    try:
        x, y = (float(value) for value in text.split(","))
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError
    except ValueError:
        raise ValueError("@x,y is a point in mm from the part's centre, e.g. AE1.4@1.5,6.4:bottom") from None
    return [x, y]


def _feature(point: Any) -> tuple:
    """REF.PAD:anchor, REF.PAD@x,y:edge or REF:centre, e.g. AE2.3:left or AE1.4@1.5,6.4:bottom.

    @x,y (mm from the part's centre) names one arm of a shaped pad: the edge
    of the pad's copper on the line through that point (14 Nov: Würth's
    L-shaped feed pad of 7488918022 is dimensioned arm by arm).
    """
    if not isinstance(point, str) or ":" not in point:
        raise ValueError("a point is REF.PAD:anchor (e.g. AE2.3:left), REF.PAD@x,y:edge or REF:centre")
    name, anchor = point.rsplit(":", 1)
    if anchor not in ANCHORS:
        raise ValueError("an anchor is one of " + ", ".join(ANCHORS))
    name, marker, at = name.partition("@")
    ref, _, pad = name.partition(".")
    if not ref:
        raise ValueError("a point names a part")
    if not pad and anchor != "centre":
        raise ValueError("a part without a pad has only its centre")
    if marker:
        if not pad or anchor == "centre":
            raise ValueError("@x,y names an edge of a pad: left, right, top or bottom")
        return ref, pad, anchor, _at(at)
    return ref, pad, anchor


def measurement(spec: Any) -> dict[str, Any]:
    """Check one reading's measurement and put it in the form the board script reads."""
    if not isinstance(spec, Mapping):
        raise ValueError("measures is an object")
    axis = spec.get("axis", "direct")
    if "distance" in spec:
        points = spec["distance"]
        if not isinstance(points, (list, tuple)) or len(points) != 2:
            raise ValueError("distance is two points")
        if axis not in (*AXES, "direct"):
            raise ValueError("a distance axis is x, y or direct")
        return {"kind": "distance", "from": _feature(points[0]), "to": _feature(points[1]), "axis": axis}
    if axis not in AXES:
        raise ValueError("axis is x or y, in the part's own directions")
    if "pad_size" in spec:
        ref, pad, _ = _feature(str(spec["pad_size"]) + ":centre")
        if not pad:
            raise ValueError("pad_size names a pad, e.g. AE2.3")
        at = spec.get("at")
        if at is None:
            return {"kind": "pad_size", "pad": (ref, pad), "axis": axis}
        # The width of one arm of a shaped pad, on the line through `at`.
        if (not isinstance(at, (list, tuple)) or len(at) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                           for v in at)):
            raise ValueError("at is [x, y] in mm from the part's centre")
        return {"kind": "pad_size", "pad": (ref, pad), "axis": axis, "at": [float(v) for v in at]}
    if "copper_width" in spec:
        at = spec.get("at")
        if (not isinstance(spec["copper_width"], str) or not isinstance(at, (list, tuple)) or len(at) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in at)):
            raise ValueError("copper_width names a net, with at [x, y] in mm from the part's centre")
        return {"kind": "copper_width", "net": spec["copper_width"], "at": [float(v) for v in at],
                "axis": axis}
    if "keepout_size" in spec:
        return {"kind": "keepout_size", "axis": axis}
    raise ValueError("measures is one of distance, pad_size, copper_width or keepout_size")


MEASURE_SCRIPT = r'''
import json, math, sys
from pathlib import Path
import pcbnew

board_path = Path(sys.argv[-1])
args = json.loads((board_path.parent / "section-args.json").read_text())
board = pcbnew.LoadBoard(str(board_path))
mm, nm = pcbnew.ToMM, pcbnew.FromMM
anchor = board.FindFootprintByReference(args["part"])
if anchor is None:
    raise SystemExit("no part %s on this board" % args["part"])
origin = anchor.GetPosition()
degrees = anchor.GetOrientation().AsDegrees()
back = pcbnew.EDA_ANGLE(-degrees, pcbnew.DEGREES_T)
side = pcbnew.B_Cu if anchor.IsFlipped() else pcbnew.F_Cu


def in_frame(shape):
    # The board turned back by the part's angle about its centre, in mm from it.
    turned = pcbnew.SHAPE_POLY_SET(shape)
    turned.Rotate(back, origin)
    return turned


def box_mm(shape):
    frame = in_frame(shape).BBox()
    return (mm(frame.GetX() - origin.x), mm(frame.GetY() - origin.y),
            mm(frame.GetRight() - origin.x), mm(frame.GetBottom() - origin.y))


def pad_shape(ref, number):
    footprint = board.FindFootprintByReference(ref)
    if footprint is None:
        raise ValueError("no part %s on this board" % ref)
    # Every pad of that number together: a shaped pad is often a custom pad
    # beside a tiny one of the same number (7488918022 pad 4).
    # Only copper on the part's own side; a paste-only aperture or a pad on
    # the far side is not this pad (review of D-081). A pad on another copper
    # layer only, as before, stands in when none is on this side.
    pads = [pad for pad in footprint.Pads() if pad.GetNumber() == number]
    if not pads:
        raise ValueError("%s has no pad %s" % (ref, number))
    here = [pad for pad in pads if pad.IsOnLayer(side)]
    shape = pcbnew.SHAPE_POLY_SET()
    if here:
        for pad in here:
            pad.TransformShapeToPolygon(shape, side, 0, nm(0.001), pcbnew.ERROR_INSIDE)
    else:
        copper = [pad for pad in pads if len(pad.GetLayerSet().CuStack())]
        if not copper:
            raise ValueError("%s pad %s has no copper" % (ref, number))
        copper[0].TransformShapeToPolygon(shape, copper[0].GetLayerSet().CuStack()[0], 0, nm(0.001),
                                          pcbnew.ERROR_INSIDE)
    shape.Simplify()
    return shape


def point(feature):
    ref, number, where = feature[:3]
    if len(feature) == 4:
        # One arm of a shaped pad: its edge on the line through the point.
        at = feature[3]
        low, high = run_through(pad_shape(ref, number), at, "x" if where in ("left", "right") else "y")
        return {"left": (low, at[1]), "right": (high, at[1]), "top": (at[0], low), "bottom": (at[0], high)}[where]
    if not number:
        footprint = board.FindFootprintByReference(ref)
        if footprint is None:
            raise ValueError("no part %s on this board" % ref)
        shape = pcbnew.SHAPE_POLY_SET()
        chain = pcbnew.SHAPE_LINE_CHAIN()
        position = footprint.GetPosition()
        for dx, dy in ((0, 0), (1, 0), (1, 1)):
            chain.Append(position.x + dx, position.y + dy)
        chain.SetClosed(True)
        shape.AddOutline(chain)
        x0, y0, _, _ = box_mm(shape)
        return x0, y0
    x0, y0, x1, y1 = box_mm(pad_shape(ref, number))
    return {"centre": ((x0 + x1) / 2, (y0 + y1) / 2), "left": (x0, (y0 + y1) / 2),
            "right": (x1, (y0 + y1) / 2), "top": ((x0 + x1) / 2, y0),
            "bottom": ((x0 + x1) / 2, y1)}[where]


def net_copper(net):
    found = pcbnew.SHAPE_POLY_SET()
    for track in board.GetTracks():
        if track.GetNetname() == net and track.GetLayer() == side and track.GetClass() in ("PCB_TRACK", "PCB_ARC"):
            track.TransformShapeToPolygon(found, side, 0, nm(0.001), pcbnew.ERROR_INSIDE)
    for item in board.GetDrawings():
        if item.GetClass() == "PCB_SHAPE" and item.GetLayer() == side and item.GetNetname() == net:
            item.TransformShapeToPolygon(found, side, 0, nm(0.001), pcbnew.ERROR_INSIDE)
    for zone in board.Zones():
        if not zone.GetIsRuleArea() and zone.GetNetname() == net and zone.IsOnLayer(side):
            filled = zone.GetFilledPolysList(side)
            if filled.OutlineCount():
                found.BooleanAdd(filled)
    for footprint in board.GetFootprints():
        for pad in footprint.Pads():
            if pad.GetNetname() == net and pad.IsOnLayer(side):
                pad.TransformShapeToPolygon(found, side, 0, nm(0.001), pcbnew.ERROR_INSIDE)
    found.Simplify()
    return found


def copper_width(net, at, axis):
    low, high = run_through(net_copper(net), at, axis, net)
    return high - low


def run_through(shape, at, axis, name="pad"):
    """Where copper starts and ends on the line through `at` along the axis, in mm from the part's centre."""
    turned = in_frame(shape)
    cx, cy = origin.x + nm(at[0]), origin.y + nm(at[1])
    probe = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    half, thin = nm(30), nm(0.002)
    corners = ((cx - half, cy - thin), (cx + half, cy - thin), (cx + half, cy + thin), (cx - half, cy + thin)) \
        if axis == "x" else ((cx - thin, cy - half), (cx + thin, cy - half), (cx + thin, cy + half), (cx - thin, cy + half))
    for x, y in corners:
        chain.Append(int(x), int(y))
    chain.SetClosed(True)
    probe.AddOutline(chain)
    turned.BooleanIntersection(probe)
    for index in range(turned.OutlineCount()):
        piece = turned.Outline(index).BBox()
        low, high = (piece.GetX(), piece.GetRight()) if axis == "x" else (piece.GetY(), piece.GetBottom())
        middle = cx if axis == "x" else cy
        if low <= middle <= high:
            base = origin.x if axis == "x" else origin.y
            return mm(low - base), mm(high - base)
    raise ValueError("no %s copper at %s" % (name, at))


def keepout_size(axis):
    best = None
    for zone in board.Zones():
        if not zone.GetIsRuleArea():
            continue
        x0, y0, x1, y1 = box_mm(zone.Outline())
        distance = max(x0, -x1, 0) + max(y0, -y1, 0)
        if best is None or distance < best[0]:
            best = (distance, x1 - x0, y1 - y0)
    if best is None:
        raise ValueError("no keep-out on this board")
    return best[1] if axis == "x" else best[2]


results = {}
for key, spec in args["measures"].items():
    try:
        if spec["kind"] == "distance":
            (ax, ay), (bx, by) = point(spec["from"]), point(spec["to"])
            value = {"x": abs(bx - ax), "y": abs(by - ay), "direct": math.hypot(bx - ax, by - ay)}[spec["axis"]]
        elif spec["kind"] == "pad_size" and spec.get("at") is not None:
            low, high = run_through(pad_shape(*spec["pad"]), spec["at"], spec["axis"])
            value = high - low
        elif spec["kind"] == "pad_size":
            x0, y0, x1, y1 = box_mm(pad_shape(*spec["pad"]))
            value = x1 - x0 if spec["axis"] == "x" else y1 - y0
        elif spec["kind"] == "copper_width":
            value = copper_width(spec["net"], spec["at"], spec["axis"])
        else:
            value = keepout_size(spec["axis"])
        results[key] = {"mm": round(value, 3)}
    except Exception as error:
        results[key] = {"error": str(error)[:200]}
(board_path.parent / "section.json").write_text(json.dumps(results))
'''


def measure_board(board: Path, part: str, measures: Mapping[str, Mapping[str, Any]], *,
                  scratch_root: Path, kicad_python: Path,
                  execute_python: Callable[[str, Path, Path], Any]) -> dict[str, Any]:
    """Each measurement taken by KiCad on a copy of the board, in the part's own directions."""
    return _run(board, {"part": part, "measures": dict(measures)}, MEASURE_SCRIPT, scratch_root,
                kicad_python, execute_python)


def document_digest(document: Path) -> str:
    return hashlib.sha256(document.read_bytes()).hexdigest()


def compare(numbers: list[dict[str, Any]], entries: Mapping[str, Mapping[str, Any]],
            measured: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each number with what it measures, the board's value and its state."""
    rows = []
    for number in numbers:
        entry = entries.get(number["id"]) or {}
        row = {"id": number["id"], "text": number["text"], "value": number["value"], "at": number["at"]}
        if number.get("basis"):
            row["basis"] = number["basis"]
        if number.get("read_from"):
            # Read off a picture by text recognition: she checks it against the drawing.
            row["read_from"] = number["read_from"]
        if number["unit"]:
            row.update(status="not_applicable", reason="part value (%s)" % number["unit"])
        elif entry.get("not_applicable"):
            row.update(status="not_applicable", reason=str(entry["not_applicable"])[:120])
        elif entry.get("measures") is None:
            row.update(status="unaccounted")
        else:
            row["measures"] = entry["label"]
            taken = measured.get(number["id"]) or {}
            if "mm" not in taken:
                row.update(status="unaccounted", error=taken.get("error", "not measured"))
            else:
                row["board_mm"] = taken["mm"]
                off = round(abs(taken["mm"] - number["value"]), 6)
                row["status"] = "matches" if off <= TOLERANCE_MM else "differs"
        rows.append(row)
    return rows


def label(spec: Mapping[str, Any]) -> str:
    """A reading in a few words, for the screen and the report."""
    if "distance" in spec:
        return "%s → %s%s" % (spec["distance"][0], spec["distance"][1],
                              "" if spec.get("axis", "direct") == "direct" else " (%s)" % spec["axis"])
    if "pad_size" in spec:
        where = "" if spec.get("at") is None else " at %g,%g" % tuple(spec["at"])
        return "%s%s %s" % (spec["pad_size"], where, "width" if spec.get("axis") == "x" else "height")
    if "copper_width" in spec:
        return "%s width" % spec["copper_width"]
    return "keep-out %s" % spec.get("axis", "")


__all__ = ["LayoutCheckError", "MEASURE_SCRIPT", "TOLERANCE_MM", "compare", "document_digest",
           "extract_words", "label", "measure_board", "measurement", "number_id", "read_numbers"]
