"""Friedl's layout rules, checked mechanically on a board copy (2026-11-12).

One rule so far, his: copper wider than the pad it leaves or meets joins it
through a taper (teardrop), never a step. Every copper layer is checked, and
each pad is measured in its own directions, so a turned part reads the same
as a straight one. A track or arc ending on a pad is a step when it is wider
than the pad across the track's direction. A copper shape touching a pad is
a step on an edge it covers whole when the copper joined there is wider than
that edge, unless it runs along the pad further than it leaves it (the pad
then sits on the side of wider copper, which is no joint). Pours (zones) are
not checked: a ground pour meets every ground pad wider than the pad by
design. What a finding means, and whether to change it, is hers to judge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from alx.providers.kicad_section import _run

MAX_FINDINGS = 20

RULES_SCRIPT = r'''
import json, math, sys
from pathlib import Path
import pcbnew

board_path = Path(sys.argv[-1])
board = pcbnew.LoadBoard(str(board_path))
mm, nm = pcbnew.ToMM, pcbnew.FromMM
RING, SLACK, REACH, DEPTH = nm(0.01), 0.05, nm(5), nm(20)
LAYERS = list(board.GetEnabledLayers().CuStack())

# Copper shapes by net and layer (a feed rectangle, a pad extension).
shapes = {}
for item in board.GetDrawings():
    if item.GetClass() == "PCB_SHAPE" and pcbnew.IsCopperLayer(item.GetLayer()) and item.GetNetname():
        shape = pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(shape, item.GetLayer(), 0, nm(0.005), pcbnew.ERROR_INSIDE)
        shapes.setdefault((item.GetNetname(), item.GetLayer()), pcbnew.SHAPE_POLY_SET()).BooleanAdd(shape)
tracks = [track for track in board.GetTracks() if track.GetClass() in ("PCB_TRACK", "PCB_ARC")]


def box(x0, y0, x1, y1):
    shape = pcbnew.SHAPE_POLY_SET()
    chain = pcbnew.SHAPE_LINE_CHAIN()
    for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
        chain.Append(int(x), int(y))
    chain.SetClosed(True)
    shape.AddOutline(chain)
    return shape


def cut(shape, area):
    result = pcbnew.SHAPE_POLY_SET(shape)
    result.BooleanIntersection(area)
    return result


def pieces(shape):
    for index in range(shape.OutlineCount()):
        piece = pcbnew.SHAPE_POLY_SET()
        piece.AddOutline(shape.Outline(index))
        yield piece


def finding(footprint, pad, layer, where, pad_width, copper_width):
    return {"rule": "taper_where_copper_is_wider_than_its_pad",
            "pad": "%s.%s" % (footprint.GetReference(), pad.GetNumber()), "net": pad.GetNetname(),
            "layer": board.GetLayerName(layer), "where": where,
            "pad_mm": round(mm(pad_width), 3), "copper_mm": round(mm(copper_width), 3)}


findings = []
for footprint in board.GetFootprints():
    for pad in footprint.Pads():
        net = pad.GetNetname()
        if not net or net.startswith("unconnected-"):
            continue
        for layer in LAYERS:
            if not pad.IsOnLayer(layer):
                continue
            outline = pcbnew.SHAPE_POLY_SET()
            pad.TransformShapeToPolygon(outline, layer, 0, nm(0.005), pcbnew.ERROR_INSIDE)
            if outline.OutlineCount() < 1:
                continue
            # Measured in the pad's own directions: the pad, and copper near it,
            # are turned back by the pad's angle about its centre.
            centre, angle = pad.GetPosition(), pad.GetOrientation()
            turn = -angle.AsRadians()
            cos, sin = math.cos(turn), math.sin(turn)
            back = pcbnew.EDA_ANGLE(-angle.AsDegrees(), pcbnew.DEGREES_T)
            square = pcbnew.SHAPE_POLY_SET(outline)
            square.Rotate(back, centre)
            frame = square.BBox()
            w, h = frame.GetWidth(), frame.GetHeight()
            if min(w, h) < nm(0.1):
                continue
            # Tracks and arcs touching the pad, wherever they end (review of
            # #238: a wide track starting just off the pad's centre still
            # overlaps it): wider than the pad across the track's own
            # direction is a step.
            for track in tracks:
                if track.GetNetname() != net or track.GetLayer() != layer:
                    continue
                body = pcbnew.SHAPE_POLY_SET()
                track.TransformShapeToPolygon(body, layer, 0, nm(0.005), pcbnew.ERROR_INSIDE)
                if cut(body, outline).OutlineCount() < 1:
                    continue
                arc = track.GetClass() == "PCB_ARC"
                ends = ((track.GetStart(), track.GetMid() if arc else track.GetEnd()),
                        (track.GetEnd(), track.GetMid() if arc else track.GetStart()))
                end, toward = min(ends, key=lambda pair: math.hypot(pair[0].x - centre.x,
                                                                      pair[0].y - centre.y))
                dx, dy = toward.x - end.x, toward.y - end.y
                length = math.hypot(dx, dy)
                if length == 0:
                    continue
                ux, uy = (dx * cos - dy * sin) / length, (dx * sin + dy * cos) / length
                across = w * abs(uy) + h * abs(ux)
                if mm(track.GetWidth()) > mm(across) + SLACK:
                    findings.append(finding(footprint, pad, layer, "track", across, track.GetWidth()))
            # Copper shapes touching the pad, piece by piece and edge by edge.
            near = shapes.get((net, layer))
            if near is None:
                continue
            touching = pcbnew.SHAPE_POLY_SET()
            for piece in pieces(near):
                if cut(piece, outline).OutlineCount():
                    piece.Rotate(back, centre)
                    touching.BooleanAdd(piece)
            if touching.OutlineCount() < 1:
                continue
            l, t, r, b = frame.GetX(), frame.GetY(), frame.GetRight(), frame.GetBottom()
            edges = {"top": ((l, t - RING, r, t), (l - REACH, t - RING, r + REACH, t), (l, t - DEPTH, r, t), True),
                     "bottom": ((l, b, r, b + RING), (l - REACH, b, r + REACH, b + RING), (l, b, r, b + DEPTH), True),
                     "left": ((l - RING, t, l, b), (l - RING, t - REACH, l, b + REACH), (l - DEPTH, t, l, b), False),
                     "right": ((r, t, r + RING, b), (r, t - REACH, r + RING, b + REACH), (r, t, r + DEPTH, b), False)}
            for piece in pieces(touching):
                steps, partial, alongside, joint = [], [], False, False
                for side, (own, strip, slab, horizontal) in edges.items():
                    edge = w if horizontal else h
                    crossing = cut(piece, box(*own))
                    if crossing.OutlineCount() < 1:
                        continue
                    joined = pcbnew.SHAPE_POLY_SET()
                    for part in pieces(cut(piece, box(*strip))):
                        if cut(part, box(*own)).OutlineCount():
                            joined.BooleanAdd(part)
                    span = joined.BBox()
                    width = span.GetWidth() if horizontal else span.GetHeight()
                    covered = crossing.BBox()
                    cover = covered.GetWidth() if horizontal else covered.GetHeight()
                    if mm(width) <= mm(edge) + SLACK:
                        # No wider than the edge, across all of it: a proper
                        # joint, as where a taper starts.
                        joint = joint or cover >= 0.95 * edge
                        continue
                    out = cut(piece, box(*slab)).BBox()
                    depth = out.GetHeight() if horizontal else out.GetWidth()
                    if width <= depth:
                        # It leaves the pad wider than the edge: a step, even
                        # where it covers only part of the edge (review of #238).
                        steps.append(finding(footprint, pad, layer, side, edge, width))
                        continue
                    if cover >= 0.95 * edge:
                        # It runs along a whole edge further than it leaves:
                        # the pad sits on the side of this piece of copper,
                        # which is no joint. Only this piece is passed; another
                        # piece on another edge is still checked (review of #238).
                        alongside = True
                    else:
                        # Wider copper meeting part of an edge, as at a corner:
                        # a step too (review of #239).
                        partial.append(finding(footprint, pad, layer, side, edge, width))
                if not alongside:
                    # One finding per piece of copper: where it leaves the pad,
                    # or else where it meets part of an edge - unless it joins
                    # the pad properly across a whole edge and only grazes the
                    # others where it starts inside the pad (C61's taper).
                    findings.extend(steps[:1] or ([] if joint else partial[:1]))
(board_path.parent / "section.json").write_text(json.dumps({"findings": findings}))
'''


def check_layout_rules(board: Path, *, scratch_root: Path, kicad_python: Path,
                       execute_python: Callable[[str, Path, Path], Any]) -> dict[str, Any]:
    """Every place a board breaks Friedl's layout rules, read from a copy.

    At most MAX_FINDINGS are listed; how many more there were is said, so a
    long list is never mistaken for a complete one.
    """
    found = list(_run(board, {}, RULES_SCRIPT, scratch_root, kicad_python, execute_python)["findings"])
    return {"findings": found[:MAX_FINDINGS], "omitted": max(0, len(found) - MAX_FINDINGS)}


__all__ = ["RULES_SCRIPT", "check_layout_rules"]
