"""A new part's symbol and footprint built from its datasheet (19 Nov 2026).

The datasheet is the source of truth for every FireFli part (Friedl). Copying another
library's drawing brought that library's habits in: KiCad draws its 74-series logic in
two units, so P0005's 74LVC1G123 arrived without its power pins and was dropped. Here
AL/X reads the figures from the datasheet (Law 1) and the drawing follows from them
(Law 2):

- the symbol from the pin table: one unit, a rectangle, each pin with its number, name
  and electrical type, pins 1 to n/2 down the left and the rest up the right (as the
  FireFli LM393A), or on the side the datasheet groups them;
- the footprint from the land pattern: two rows (SOIC, SOT, SON, DFN), four sides (QFN)
  or every pad from the datasheet's own table (connectors, modules, odd packages), with an
  exposed pad where there is one, a fabrication outline of the body, silkscreen clear of
  the pads with a pin 1 mark, and a courtyard 0.25 mm round pads and body.

The footprint is then measured against the datasheet drawing with check_kicad_layout.
"""

from __future__ import annotations

import json
import math
import uuid
from typing import Any

GRID = 2.54
PIN_LENGTH = 2.54
FONT = 1.27
PIN_TYPES = ("input", "output", "bidirectional", "tri_state", "passive", "free", "unspecified",
             "power_in", "power_out", "open_collector", "open_emitter", "no_connect")
SIDES = ("left", "right", "top", "bottom")
SHAPES = ("rect", "roundrect", "oval", "circle")
COURTYARD = 0.25


class PartSpecError(ValueError):
    """The figures given do not describe a drawable part; the message says which."""


def _q(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _n(value: float) -> str:
    shown = ("%.4f" % value).rstrip("0").rstrip(".")
    return "0" if shown in ("-0", "") else shown


def _number(value: Any, what: str, positive: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise PartSpecError("%s is a number in mm" % what) from error
    if not math.isfinite(number) or (positive and number <= 0):
        raise PartSpecError("%s must be a positive number in mm" % what)
    return number


# ---------------------------------------------------------------- symbol

def build_symbol(name: str, fields: dict[str, str], pins: list[dict[str, Any]]) -> str:
    """One-unit symbol (a library block, one tab deep) from the datasheet's pin table.

    pins: [{number, name, type, side?}]; type is KiCad's electrical type (input, output,
    bidirectional, tri_state, passive, power_in, power_out, open_collector, no_connect...)."""
    if not isinstance(pins, list) or not pins:
        raise PartSpecError("pins is the datasheet's pin table: [{number, name, type}]")
    numbers, placed = set(), {side: [] for side in SIDES}
    for index, pin in enumerate(pins):
        if not isinstance(pin, dict):
            raise PartSpecError("each pin is {number, name, type}")
        number = str(pin.get("number", "")).strip()
        if not number or number in numbers:
            raise PartSpecError("pin %d: every pin needs its own number" % (index + 1))
        numbers.add(number)
        kind = str(pin.get("type", "")).strip()
        if kind not in PIN_TYPES:
            raise PartSpecError("pin %s: type is one of %s" % (number, ", ".join(PIN_TYPES)))
        side = str(pin.get("side", "")).strip()
        if side and side not in SIDES:
            raise PartSpecError("pin %s: side is left, right, top or bottom" % number)
        placed.setdefault(side or "", []).append({"number": number, "name": str(pin.get("name", "")).strip() or "~",
                                                 "type": kind})
    unsided = placed.pop("", [])
    if unsided:
        # Package order: the first half down the left, the rest up the right (LM393A).
        half = math.ceil(len(unsided) / 2)
        placed["left"] = placed["left"] + unsided[:half]
        placed["right"] = placed["right"] + list(reversed(unsided[half:]))

    def label(pin):
        return 0 if pin["name"] == "~" else len(pin["name"])
    widest = max([label(p) for p in placed["left"]] or [0]) + max([label(p) for p in placed["right"]] or [0])
    across = max(len(placed["top"]), len(placed["bottom"]))
    width = max(10.16, math.ceil((widest * 1.0 + 3.0) / GRID) * GRID, (across + 1) * GRID)
    down = max(len(placed["left"]), len(placed["right"]))
    tall_labels = max([label(p) for p in placed["top"]] or [0]) + max([label(p) for p in placed["bottom"]] or [0])
    height = max(5.08, (down + 1) * GRID, math.ceil((tall_labels * 1.0 + 3.0) / GRID) * GRID)
    # Corners on the grid so every pin end lands on it.
    half_w, half_h = width / 2, height / 2
    if round(half_w / GRID * 2) % 2:
        half_w += GRID / 2
    if round(half_h / GRID * 2) % 2:
        half_h += GRID / 2
    rows = []

    def column(items, x, angle):
        top = (len(items) - 1) * GRID / 2
        for i, pin in enumerate(items):
            rows.append((pin, x, top - i * GRID, angle))

    def row(items, y, angle):
        left = -(len(items) - 1) * GRID / 2
        for i, pin in enumerate(items):
            rows.append((pin, left + i * GRID, y, angle))
    # KiCad symbol coordinates: y up; a pin's (at) is its outer end, angle points into the body.
    column(placed["left"], -half_w - PIN_LENGTH, 0)
    column(placed["right"], half_w + PIN_LENGTH, 180)
    row(placed["top"], half_h + PIN_LENGTH, 270)
    row(placed["bottom"], -half_h - PIN_LENGTH, 90)

    def prop(key, value, at, hidden):
        return ('\t\t(property %s %s\n\t\t\t(at %s %s 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n%s'
                '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size %s %s)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
                % (_q(key), _q(value), _n(at[0]), _n(at[1]), "\t\t\t(hide yes)\n" if hidden else "",
                   _n(FONT), _n(FONT)))
    out = ["(symbol %s\n" % _q(name), "\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n"]
    out.append(prop("Reference", fields.get("Reference", "U"), (-half_w, half_h + 1.27), False))
    out.append(prop("Value", fields.get("Value", name), (-half_w, -half_h - 1.27 - (GRID if placed["bottom"] else 0)),
                    False))
    # The Sim fields point KiCad's simulator at the maker's model (review of #247).
    for key in ("Footprint", "Datasheet", "Description", "Manufacturer", "MPN", "LCSC",
                *sorted(k for k in fields if k.startswith("Sim."))):
        if fields.get(key):
            out.append(prop(key, fields[key], (0, 0), True))
    out.append('\t\t(symbol %s\n\t\t\t(rectangle\n\t\t\t\t(start %s %s)\n\t\t\t\t(end %s %s)\n\t\t\t\t(stroke\n'
               '\t\t\t\t\t(width 0.254)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n\t\t\t\t(fill\n\t\t\t\t\t(type background)'
               '\n\t\t\t\t)\n\t\t\t)\n\t\t)\n' % (_q(name + "_0_1"), _n(-half_w), _n(half_h), _n(half_w), _n(-half_h)))
    out.append("\t\t(symbol %s\n" % _q(name + "_1_1"))
    for pin, x, y, angle in rows:
        out.append('\t\t\t(pin %s line\n\t\t\t\t(at %s %s %d)\n\t\t\t\t(length %s)\n\t\t\t\t(name %s\n\t\t\t\t\t(effects\n'
                   '\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size %s %s)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t\t(number %s\n'
                   '\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size %s %s)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n'
                   '\t\t\t)\n' % (pin["type"], _n(x), _n(y), angle, _n(PIN_LENGTH), _q(pin["name"]), _n(FONT), _n(FONT),
                                  _q(pin["number"]), _n(FONT), _n(FONT)))
    out.append("\t\t)\n\t)")
    return "".join(out)


# ---------------------------------------------------------------- footprint

def _pads_two_row(spec: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(_number(spec.get("pins"), "pins"))
    if count < 2 or count % 2:
        raise PartSpecError("a two-row package has an even number of pins")
    pitch = _number(spec.get("pitch"), "pitch")
    width, length = _number(spec.get("pad_width"), "pad_width"), _number(spec.get("pad_length"), "pad_length")
    spacing = _number(spec.get("row_spacing"), "row_spacing (pad centre to pad centre across the rows)")
    per_row = count // 2
    pads = []
    for i in range(per_row):
        y = (i - (per_row - 1) / 2) * pitch
        pads.append({"number": str(i + 1), "x": -spacing / 2, "y": y, "width": length, "height": width})
    for i in range(per_row):
        y = ((per_row - 1) / 2 - i) * pitch
        pads.append({"number": str(per_row + i + 1), "x": spacing / 2, "y": y, "width": length, "height": width})
    return pads


def _pads_quad(spec: dict[str, Any]) -> list[dict[str, Any]]:
    count = int(_number(spec.get("pins"), "pins"))
    per_side = spec.get("pins_per_side")
    if per_side is None:
        if count % 4:
            raise PartSpecError("give pins_per_side [left, bottom] when the sides differ")
        across, down = count // 4, count // 4
    else:
        down, across = int(per_side[0]), int(per_side[1])
        if 2 * (down + across) != count:
            raise PartSpecError("pins_per_side [left, bottom] must add up to pins on four sides")
    pitch = _number(spec.get("pitch"), "pitch")
    width, length = _number(spec.get("pad_width"), "pad_width"), _number(spec.get("pad_length"), "pad_length")
    span_x = _number(spec.get("row_spacing_x", spec.get("row_spacing")), "row_spacing_x (left to right pad centres)")
    span_y = _number(spec.get("row_spacing_y", spec.get("row_spacing")), "row_spacing_y (top to bottom pad centres)")
    pads, number = [], 1
    # Pin 1 at the top of the left side, counter-clockwise seen from above.
    for i in range(down):
        pads.append({"number": str(number), "x": -span_x / 2, "y": (i - (down - 1) / 2) * pitch,
                     "width": length, "height": width})
        number += 1
    for i in range(across):
        pads.append({"number": str(number), "x": (i - (across - 1) / 2) * pitch, "y": span_y / 2,
                     "width": width, "height": length})
        number += 1
    for i in range(down):
        pads.append({"number": str(number), "x": span_x / 2, "y": ((down - 1) / 2 - i) * pitch,
                     "width": length, "height": width})
        number += 1
    for i in range(across):
        pads.append({"number": str(number), "x": ((across - 1) / 2 - i) * pitch, "y": -span_y / 2,
                     "width": width, "height": length})
        number += 1
    return pads


def _explicit_pads(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise PartSpecError("pads is the datasheet's pad table: [{number, x, y, width, height}]")
    pads = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PartSpecError("pad %d is {number, x, y, width, height}" % (index + 1))
        pad = {"number": str(item.get("number", "")).strip(),
               "x": _number(item.get("x"), "pad %d x" % (index + 1), positive=False),
               "y": _number(item.get("y"), "pad %d y" % (index + 1), positive=False),
               "width": _number(item.get("width"), "pad %d width" % (index + 1)),
               "height": _number(item.get("height"), "pad %d height" % (index + 1))}
        for key in ("shape", "rotation", "drill", "kind"):
            if item.get(key) not in (None, ""):
                pad[key] = item[key]
        pads.append(pad)
    return pads


def build_footprint(name: str, spec: dict[str, Any], fields: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """The footprint (a .kicad_mod) from the datasheet's land pattern, and a summary of it.

    spec: {package: two_row | quad | pads, ...figures in mm...,
           body: [width, height], exposed_pad?: {width, height, number?, x?, y?}, shape?: rect|roundrect}
    Pad positions are KiCad's: x right, y down, origin at the package centre."""
    if not isinstance(spec, dict):
        raise PartSpecError("footprint is the land pattern: {package, ...}")
    package = str(spec.get("package", "")).strip()
    if package == "two_row":
        pads = _pads_two_row(spec)
    elif package == "quad":
        pads = _pads_quad(spec)
    elif package == "pads":
        pads = _explicit_pads(spec.get("pads"))
    else:
        raise PartSpecError("package is two_row (SOIC, SOT, SON, DFN), quad (QFN) or pads (every pad from "
                            "the datasheet's table)")
    shape = str(spec.get("shape") or "rect")
    if shape not in SHAPES:
        raise PartSpecError("shape is rect, roundrect, oval or circle")
    exposed = spec.get("exposed_pad")
    if exposed:
        if not isinstance(exposed, dict):
            raise PartSpecError("exposed_pad is {width, height, number?}")
        pads.append({"number": str(exposed.get("number") or (len(pads) + 1)),
                     "x": _number(exposed.get("x", 0), "exposed_pad x", positive=False),
                     "y": _number(exposed.get("y", 0), "exposed_pad y", positive=False),
                     "width": _number(exposed.get("width"), "exposed_pad width"),
                     "height": _number(exposed.get("height"), "exposed_pad height"), "shape": "rect"})
    numbered = [p["number"] for p in pads if p["number"]]
    if len(numbered) != len(set(numbered)) and package != "pads":
        raise PartSpecError("two pads share a number")
    body = spec.get("body")
    if not (isinstance(body, (list, tuple)) and len(body) == 2):
        raise PartSpecError("body is [width, height] of the package outline in mm")
    body_w, body_h = _number(body[0], "body width"), _number(body[1], "body height")
    through = any(p.get("drill") for p in pads)

    def extent(pad):
        turned = float(pad.get("rotation") or 0) % 180 == 90
        w, h = (pad["height"], pad["width"]) if turned else (pad["width"], pad["height"])
        return pad["x"] - w / 2, pad["y"] - h / 2, pad["x"] + w / 2, pad["y"] + h / 2
    boxes = [extent(p) for p in pads]
    x0 = min([b[0] for b in boxes] + [-body_w / 2]) - COURTYARD
    y0 = min([b[1] for b in boxes] + [-body_h / 2]) - COURTYARD
    x1 = max([b[2] for b in boxes] + [body_w / 2]) + COURTYARD
    y1 = max([b[3] for b in boxes] + [body_h / 2]) + COURTYARD
    x0, y0 = math.floor(x0 * 100) / 100, math.floor(y0 * 100) / 100
    x1, y1 = math.ceil(x1 * 100) / 100, math.ceil(y1 * 100) / 100

    def uid():
        return '(uuid "%s")' % uuid.uuid4()

    def line(a, b, layer, width):
        return ('\t(fp_line\n\t\t(start %s %s)\n\t\t(end %s %s)\n\t\t(stroke\n\t\t\t(width %s)\n\t\t\t(type solid)\n'
                '\t\t)\n\t\t(layer %s)\n\t\t%s\n\t)\n' % (_n(a[0]), _n(a[1]), _n(b[0]), _n(b[1]), _n(width),
                                                          _q(layer), uid()))

    def prop(key, value, at, layer, hidden):
        return ('\t(property %s %s\n\t\t(at %s %s 0)\n\t\t(layer %s)\n%s\t\t%s\n\t\t(effects\n\t\t\t(font\n'
                '\t\t\t\t(size %s %s)\n\t\t\t\t(thickness 0.1)\n\t\t\t)\n\t\t)\n\t)\n'
                % (_q(key), _q(value), _n(at[0]), _n(at[1]), _q(layer), "\t\t(hide yes)\n" if hidden else "",
                   uid(), "0.5" if key == "Reference" else "0.8", "0.5" if key == "Reference" else "0.8"))
    out = ["(footprint %s\n\t(version 20260206)\n\t(generator \"alx\")\n\t(generator_version \"10.0\")\n"
           "\t(layer \"F.Cu\")\n" % _q(name)]
    out.append(prop("Reference", "REF**", (0, y0 - 0.6), "F.SilkS", False))
    out.append(prop("Value", name, (0, y1 + 0.8), "F.Fab", False))
    for key in ("Datasheet", "Description"):
        if fields.get(key):
            out.append(prop(key, fields[key], (0, 0), "F.Fab", True))
    out.append("\t(attr %s)\n" % ("through_hole" if through else "smd"))
    # Body on F.Fab with the pin 1 corner cut; courtyard round pads and body.
    bx, by, chamfer = body_w / 2, body_h / 2, min(body_w, body_h) * 0.15
    fab = [(-bx + chamfer, -by), (bx, -by), (bx, by), (-bx, by), (-bx, -by + chamfer), (-bx + chamfer, -by)]
    for a, b in zip(fab, fab[1:]):
        out.append(line(a, b, "F.Fab", 0.1))
    for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        out.append(line(a, b, "F.CrtYd", 0.05))
    # Silkscreen: the body outline wherever it keeps 0.2 mm off every pad, and a pin 1 dot.
    clear = 0.2 + 0.06

    def clear_of_pads(a, b):
        steps = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / 0.05))
        free, run = [], []
        for k in range(steps + 1):
            p = (a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)
            hit = any(bx0 - clear <= p[0] <= bx1 + clear and by0 - clear <= p[1] <= by1 + clear
                      for bx0, by0, bx1, by1 in boxes)
            if hit:
                if len(run) > 1:
                    free.append((run[0], run[-1]))
                run = []
            else:
                run.append(p)
        if len(run) > 1:
            free.append((run[0], run[-1]))
        return [(s, e) for s, e in free if math.hypot(e[0] - s[0], e[1] - s[1]) >= 0.2]
    sx, sy = bx + 0.06, by + 0.06
    for a, b in (((-sx, -sy), (sx, -sy)), ((sx, -sy), (sx, sy)), ((sx, sy), (-sx, sy)), ((-sx, sy), (-sx, -sy))):
        for s, e in clear_of_pads(a, b):
            out.append(line(s, e, "F.SilkS", 0.12))
    first = next((p for p in pads if p["number"] == "1"), pads[0])
    fx0, fy0, _, _ = extent(first)
    dot = (min(fx0, -bx) - 0.35, fy0 + 0.1) if first["x"] < 0 else (fx0 + 0.1, min(fy0, -by) - 0.35)
    out.append('\t(fp_circle\n\t\t(center %s %s)\n\t\t(end %s %s)\n\t\t(stroke\n\t\t\t(width 0.12)\n\t\t\t(type solid)\n'
               '\t\t)\n\t\t(fill yes)\n\t\t(layer "F.SilkS")\n\t\t%s\n\t)\n'
               % (_n(dot[0]), _n(dot[1]), _n(dot[0] + 0.1), _n(dot[1]), uid()))
    for pad in pads:
        drill = pad.get("drill")
        kind = pad.get("kind") or ("thru_hole" if drill else "smd")
        if kind not in ("smd", "thru_hole", "np_thru_hole"):
            raise PartSpecError("pad %s kind is smd, thru_hole or np_thru_hole" % pad["number"])
        form = pad.get("shape") or (shape if kind == "smd" else "circle")
        if form not in SHAPES:
            raise PartSpecError("pad %s shape is rect, roundrect, oval or circle" % pad["number"])
        layers = {"smd": '"F.Cu" "F.Mask" "F.Paste"', "thru_hole": '"*.Cu" "*.Mask"',
                  "np_thru_hole": '"*.Cu" "*.Mask"'}[kind]
        rotation = float(pad.get("rotation") or 0)
        at = "%s %s%s" % (_n(pad["x"]), _n(pad["y"]), (" " + _n(rotation)) if rotation else "")
        extra = "\t\t(roundrect_rratio 0.25)\n" if form == "roundrect" else ""
        hole = "\t\t(drill %s)\n" % _n(_number(drill, "pad %s drill" % pad["number"])) if drill else ""
        out.append('\t(pad %s %s %s\n\t\t(at %s)\n\t\t(size %s %s)\n%s\t\t(layers %s)\n%s\t\t%s\n\t)\n'
                   % (_q(pad["number"]), kind, form, at, _n(pad["width"]), _n(pad["height"]), hole, layers, extra,
                      uid()))
    out.append("\t(embedded_fonts no)\n)\n")
    summary = {"pads": len(pads), "courtyard": [x0, y0, x1, y1], "body": [body_w, body_h],
               "kind": "through_hole" if through else "smd"}
    return "".join(out), summary


__all__ = ["PIN_TYPES", "PartSpecError", "build_footprint", "build_symbol"]
