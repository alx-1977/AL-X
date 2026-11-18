"""JLC assembly files from KiCad's own BOM and position exports (18 Nov 2026).

KiCad writes the BOM (`kicad-cli sch export bom`) and the positions (`kicad-cli pcb export
pos`); this module only puts them in JLC's columns and applies each part's JLC correction.
A correction is a fact about a library part, stored on the FireFli footprint as two fields:

- "JLC Rotation Offset": degrees added to the part's rotation, e.g. "90";
- "JLC Position Offset": "x,y" in mm, in the part's own frame at rotation 0 with y up, turned
  with the part, e.g. "0,-19.6675".

Every step has one correct result, so it is code (Law 2). Which parts have no LCSC number,
which parts were corrected and which sit on the bottom are reported, not decided.
"""

from __future__ import annotations

import csv
import io
import math
import re
from collections.abc import Iterable, Mapping

ROTATION_FIELD = "JLC Rotation Offset"
POSITION_FIELD = "JLC Position Offset"
BOM_FIELDS = "Value,Reference,Footprint,LCSC,${QUANTITY}"
BOM_LABELS = "Comment,Designator,Footprint,LCSC Part #,Quantity"
BOM_GROUP = "Value,Footprint,LCSC"


def _csv(header: Iterable[str], rows: Iterable[Iterable[object]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(list(header))
    writer.writerows(rows)
    return out.getvalue()


def jlc_bom(kicad_bom: str) -> tuple[str, list[str]]:
    """JLC's BOM (Comment, Designator, Footprint, LCSC Part #) from KiCad's grouped BOM, and the
    designators of every line without an LCSC number."""
    rows = list(csv.DictReader(io.StringIO(kicad_bom)))
    out, missing = [], []
    for row in rows:
        footprint = row.get("Footprint", "").split(":", 1)[-1]
        lcsc = row.get("LCSC Part #", "").strip()
        if not lcsc:
            missing.append(row.get("Designator", ""))
        out.append((row.get("Comment", ""), row.get("Designator", ""), footprint, lcsc))
    return _csv(("Comment", "Designator", "Footprint", "LCSC Part #"), out), missing


def correction(footprint_text: str) -> tuple[float, tuple[float, float]] | None:
    """A library footprint's JLC correction, or None when it has none."""
    def field(name):
        found = re.search(r'\(property "%s" "([^"]*)"' % re.escape(name), footprint_text)
        return found.group(1).strip() if found else ""
    rotation, position = field(ROTATION_FIELD), field(POSITION_FIELD)
    if not rotation and not position:
        return None
    try:
        dx, dy = (float(v) for v in position.split(",")) if position else (0.0, 0.0)
        return (float(rotation) if rotation else 0.0, (dx, dy))
    except ValueError as error:
        raise ValueError("unreadable JLC correction: rotation %r, position %r" % (rotation, position)) from error


def jlc_positions(kicad_pos: str, corrections: Mapping[str, tuple[float, tuple[float, float]]]
                  ) -> tuple[str, dict[str, object]]:
    """JLC's CPL (Designator, Mid X, Mid Y, Layer, Rotation) from KiCad's CSV positions.

    corrections: each designator's (rotation, (dx, dy)). Returns the CSV and a report of the
    designators corrected and those on the bottom (JLC's bottom-side rotation is to be
    checked in its preview: nothing here has been proven against a bottom-side order)."""
    rows = list(csv.DictReader(io.StringIO(kicad_pos)))
    out, corrected, bottom = [], [], []
    for row in rows:
        ref = row["Ref"]
        x, y, rotation = float(row["PosX"]), float(row["PosY"]), float(row["Rot"])
        side = "Bottom" if row["Side"].strip().lower() == "bottom" else "Top"
        if side == "Bottom":
            bottom.append(ref)
        fix = corrections.get(ref)
        if fix:
            turn, (dx, dy) = fix
            if side == "Bottom":
                dx = -dx
            angle = math.radians(rotation)
            x += dx * math.cos(angle) - dy * math.sin(angle)
            y += dx * math.sin(angle) + dy * math.cos(angle)
            rotation += turn
            corrected.append(ref)
        rotation %= 360
        out.append((ref, "%.4f" % x, "%.4f" % y, side, "%g" % round(rotation, 4)))
    return (_csv(("Designator", "Mid X", "Mid Y", "Layer", "Rotation"), out),
            {"parts": len(out), "corrected": corrected, "bottom": bottom})


__all__ = ["BOM_FIELDS", "BOM_GROUP", "BOM_LABELS", "POSITION_FIELD", "ROTATION_FIELD", "correction",
           "jlc_bom", "jlc_positions"]
