"""Friedl's layout rules, checked mechanically on a board copy (2026-11-12).

One rule so far, his: copper wider than the pad it leaves or meets joins it
through a taper (teardrop), never a step. For each pad, a thin band just
outside each of its edges is intersected with the same net's tracks and
copper shapes; copper there wider than the edge is a step. Pours (zones) are
not checked: a ground pour meets every ground pad wider than the pad by
design. What a finding means, and whether to change it, is hers to judge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from alx.providers.kicad_section import _run

MAX_FINDINGS = 20

RULES_SCRIPT = r'''
import json, sys
from pathlib import Path
import pcbnew

board_path = Path(sys.argv[-1])
board = pcbnew.LoadBoard(str(board_path))
mm, nm = pcbnew.ToMM, pcbnew.FromMM
RING, SLACK = nm(0.01), 0.05
copper = {}
for track in board.GetTracks():
    if track.GetClass() != "PCB_TRACK":
        continue
    shape = pcbnew.SHAPE_POLY_SET()
    track.TransformShapeToPolygon(shape, track.GetLayer(), 0, nm(0.005), pcbnew.ERROR_INSIDE)
    copper.setdefault((track.GetNetname(), track.GetLayer()), pcbnew.SHAPE_POLY_SET()).BooleanAdd(shape)
for item in board.GetDrawings():
    if item.GetClass() != "PCB_SHAPE" or not pcbnew.IsCopperLayer(item.GetLayer()) or not item.GetNetname():
        continue
    shape = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(shape, item.GetLayer(), 0, nm(0.005), pcbnew.ERROR_INSIDE)
    copper.setdefault((item.GetNetname(), item.GetLayer()), pcbnew.SHAPE_POLY_SET()).BooleanAdd(shape)


findings = []
for footprint in board.GetFootprints():
    for pad in footprint.Pads():
        net = pad.GetNetname()
        if not net or net.startswith("unconnected-") or pad.GetOrientationDegrees() % 90:
            continue
        if min(pad.GetBoundingBox().GetWidth(), pad.GetBoundingBox().GetHeight()) < nm(0.1):
            continue
        for layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            near = copper.get((net, layer))
            if near is None or not pad.IsOnLayer(layer):
                continue
            outline = pcbnew.SHAPE_POLY_SET()
            pad.TransformShapeToPolygon(outline, layer, 0, nm(0.005), pcbnew.ERROR_INSIDE)
            if outline.OutlineCount() < 1:
                continue
            # Only the copper that touches this pad: a track merely passing near
            # it is not joining it.
            touching = pcbnew.SHAPE_POLY_SET()
            for index in range(near.OutlineCount()):
                piece = pcbnew.SHAPE_POLY_SET()
                piece.AddOutline(near.Outline(index))
                overlap = pcbnew.SHAPE_POLY_SET(piece)
                overlap.BooleanIntersection(outline)
                if overlap.OutlineCount():
                    touching.BooleanAdd(piece)
            if touching.OutlineCount() < 1:
                continue
            # Each piece of copper outside the pad leaves it by the edge it lies
            # beyond; its width just past that edge is what joins the pad. A
            # taper starts there at the pad's own width, a step at its full width.
            box = outline.BBox()
            cx, cy = box.Centre().x, box.Centre().y
            half_w, half_h = max(box.GetWidth() / 2, 1), max(box.GetHeight() / 2, 1)
            outside = pcbnew.SHAPE_POLY_SET(touching)
            outside.BooleanSubtract(outline)
            for index in range(outside.OutlineCount()):
                piece = pcbnew.SHAPE_POLY_SET()
                piece.AddOutline(outside.Outline(index))
                where = piece.BBox()
                dx = (where.Centre().x - cx) / half_w
                dy = (where.Centre().y - cy) / half_h
                vertical = abs(dy) >= abs(dx)
                # Copper that runs along the edge more than it leaves it (a pad
                # sitting on the side of wider copper) is not a joint.
                depth = where.GetHeight() if vertical else where.GetWidth()
                span = where.GetWidth() if vertical else where.GetHeight()
                if span > depth:
                    continue
                if vertical:
                    edge = box.GetBottom() if dy > 0 else box.GetY()
                    lo, hi = (edge, edge + RING) if dy > 0 else (edge - RING, edge)
                    corners = ((where.GetX(), lo), (where.GetRight(), lo), (where.GetRight(), hi), (where.GetX(), hi))
                    pad_width, side = box.GetWidth(), "bottom" if dy > 0 else "top"
                else:
                    edge = box.GetRight() if dx > 0 else box.GetX()
                    lo, hi = (edge, edge + RING) if dx > 0 else (edge - RING, edge)
                    corners = ((lo, where.GetY()), (hi, where.GetY()), (hi, where.GetBottom()), (lo, where.GetBottom()))
                    pad_width, side = box.GetHeight(), "right" if dx > 0 else "left"
                strip = pcbnew.SHAPE_POLY_SET()
                chain = pcbnew.SHAPE_LINE_CHAIN()
                for x, y in corners:
                    chain.Append(int(x), int(y))
                chain.SetClosed(True)
                strip.AddOutline(chain)
                strip.BooleanIntersection(piece)
                if strip.OutlineCount() < 1:
                    continue
                cut = strip.BBox()
                width = cut.GetWidth() if side in ("top", "bottom") else cut.GetHeight()
                if mm(width) > mm(pad_width) + SLACK:
                    findings.append({"rule": "taper_where_copper_is_wider_than_its_pad",
                                     "pad": "%s.%s" % (footprint.GetReference(), pad.GetNumber()),
                                     "net": net, "side": side, "pad_mm": round(mm(pad_width), 3),
                                     "copper_mm": round(mm(width), 3)})
(board_path.parent / "section.json").write_text(json.dumps({"findings": findings}))
'''


def check_layout_rules(board: Path, *, scratch_root: Path, kicad_python: Path,
                       execute_python: Callable[[str, Path, Path], Any]) -> list[dict[str, Any]]:
    """Every place a board breaks Friedl's layout rules, read from a copy."""
    return list(_run(board, {}, RULES_SCRIPT, scratch_root, kicad_python, execute_python)["findings"])[:MAX_FINDINGS]


__all__ = ["RULES_SCRIPT", "check_layout_rules"]
