"""Tested KiCad operations placed ahead of every edit script (2026-11-12).

On 12 November 30 of 83 edit scripts failed, most by guessing KiCad 10's
Python names (SwigPyObject has no attribute ..., wrong pad-size calls). These
helpers are the same operations written once and tested against KiCad 10;
her script can call them or use pcbnew directly. Millimetres and degrees
throughout; layers by number ('F', 'B', 'In1'...), never by name, because
boards converted from Altium name theirs differently.
"""

HELPERS = r'''
# ---- AL/X KiCad helpers (tested on KiCad 10; millimetres, degrees) ----
import sys as _sys
import pcbnew as _pcbnew

BOARD_PATH = _sys.argv[-1]
_LAYERS = {"F": _pcbnew.F_Cu, "B": _pcbnew.B_Cu}
for _index in range(1, 31):
    _layer = getattr(_pcbnew, "In%d_Cu" % _index, None)
    if _layer is not None:
        _LAYERS["In%d" % _index] = _layer


def load():
    """The work copy's board."""
    return _pcbnew.LoadBoard(BOARD_PATH)


def save(board):
    """Save the board back to the work copy."""
    _pcbnew.SaveBoard(BOARD_PATH, board)


def mm(value):
    return _pcbnew.ToMM(value)


def point(x, y):
    return _pcbnew.VECTOR2I(_pcbnew.FromMM(x), _pcbnew.FromMM(y))


def xy(vector):
    return (round(_pcbnew.ToMM(vector.x), 4), round(_pcbnew.ToMM(vector.y), 4))


def layer(name):
    """A copper layer by its short name: 'F', 'B', 'In1' ..."""
    if name not in _LAYERS:
        raise KeyError("copper layer is one of %s" % ", ".join(sorted(_LAYERS)))
    return _LAYERS[name]


def part(board, ref):
    found = board.FindFootprintByReference(ref)
    if found is None:
        raise KeyError("no part %s on this board" % ref)
    return found


def move_part(board, ref, x, y):
    part(board, ref).SetPosition(point(x, y))


def rotate_part(board, ref, degrees):
    """Set the part's rotation (absolute, degrees)."""
    part(board, ref).SetOrientationDegrees(degrees)


def pad(board, ref, number):
    for item in part(board, ref).Pads():
        if item.GetNumber() == str(number):
            return item
    raise KeyError("%s has no pad %s" % (ref, number))


def set_pad(board, ref, number, size=None, at=None):
    """Change a pad: size (w, h) in mm, at (x, y) in board mm."""
    item = pad(board, ref, number)
    if size is not None:
        item.SetSize(_pcbnew.PADSTACK.ALL_LAYERS, point(*size))
    if at is not None:
        item.SetPosition(point(*at))
    return item


def describe(board, ref):
    """Position, rotation, side and every pad of a part, in board mm."""
    found = part(board, ref)
    return {"ref": ref, "at": xy(found.GetPosition()), "rotation": found.GetOrientationDegrees(),
            "side": "B" if found.IsFlipped() else "F",
            "pads": [{"number": item.GetNumber(), "net": item.GetNetname(), "at": xy(item.GetPosition()),
                      "size": xy(item.GetSize(_pcbnew.PADSTACK.ALL_LAYERS))} for item in found.Pads()]}


def tracks(board, net=None):
    """Track segments (not vias), optionally of one net."""
    return [item for item in board.GetTracks()
            if item.GetClass() == "PCB_TRACK" and (net is None or item.GetNetname() == net)]


def vias(board, net=None):
    return [item for item in board.GetTracks()
            if item.GetClass() == "PCB_VIA" and (net is None or item.GetNetname() == net)]


def track_ends(item):
    return (xy(item.GetStart()), xy(item.GetEnd()))


def add_track(board, net, points, width, on="F"):
    """A track through points [(x, y), ...] on copper layer `on`, of `width` mm."""
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    made = []
    for start, end in zip(points, points[1:]):
        segment = _pcbnew.PCB_TRACK(board)
        segment.SetStart(point(*start))
        segment.SetEnd(point(*end))
        segment.SetWidth(_pcbnew.FromMM(width))
        segment.SetLayer(layer(on))
        segment.SetNet(code)
        board.Add(segment)
        made.append(segment)
    return made


def add_via(board, net, at, diameter=0.6, drill=0.3):
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    via = _pcbnew.PCB_VIA(board)
    via.SetPosition(point(*at))
    via.SetWidth(_pcbnew.FromMM(diameter))
    via.SetDrill(_pcbnew.FromMM(drill))
    via.SetNet(code)
    board.Add(via)
    return via


def remove(board, item):
    """Take an item off the board, from whatever holds it.

    Tested on KiCad 10 (2026-11-12): a part's own drawing removed through the
    board stays in the part's list and crashes KiCad when the board is saved,
    and Remove through the part breaks later lookups of that part. Delete,
    through whatever holds the item, is safe. Do not use the item afterwards.
    """
    holder = item.GetParentFootprint()
    (holder if holder is not None else board).Delete(item)


_GRAPHIC_LAYERS = {"F.Silk": _pcbnew.F_SilkS, "B.Silk": _pcbnew.B_SilkS, "F.Fab": _pcbnew.F_Fab,
                   "B.Fab": _pcbnew.B_Fab, "F.Courtyard": _pcbnew.F_CrtYd, "B.Courtyard": _pcbnew.B_CrtYd}
_LAYER_NAMES = {number: name for name, number in _GRAPHIC_LAYERS.items()}


def graphics(board, ref, on=None):
    """A part's own drawings (silkscreen, fab, courtyard; text excluded), as a
    fixed list: index, layer, shape and points in board mm. Lists from pcbnew
    are live; change items through these helpers, by index, never while
    looping over pcbnew's own list."""
    items = [item for item in part(board, ref).GraphicalItems() if item.GetClass() == "PCB_SHAPE"]
    found = []
    for index, item in enumerate(items):
        name = _LAYER_NAMES.get(item.GetLayer(), str(item.GetLayer()))
        if on is not None and name != on:
            continue
        found.append({"index": index, "layer": name, "shape": item.ShowShape().lower(),
                      "start": xy(item.GetStart()), "end": xy(item.GetEnd()),
                      "width": round(_pcbnew.ToMM(item.GetWidth()), 4)})
    return found


def _graphic(board, ref, index):
    items = [item for item in part(board, ref).GraphicalItems() if item.GetClass() == "PCB_SHAPE"]
    if not 0 <= index < len(items):
        raise KeyError("%s has no drawing %s" % (ref, index))
    return items[index]


def set_graphic(board, ref, index, start=None, end=None):
    """Move a part's drawing (a line's or rectangle's ends) in board mm."""
    item = _graphic(board, ref, index)
    if start is not None:
        item.SetStart(point(*start))
    if end is not None:
        item.SetEnd(point(*end))


def remove_graphic(board, ref, index):
    """Delete a part's drawing by its index from graphics()."""
    part(board, ref).Delete(_graphic(board, ref, index))


def add_line(board, ref, on, start, end, width=0.12):
    """Add a line to a part's drawings on 'F.Silk', 'F.Fab', 'F.Courtyard' ..."""
    if on not in _GRAPHIC_LAYERS:
        raise KeyError("drawing layer is one of %s" % ", ".join(sorted(_GRAPHIC_LAYERS)))
    holder = part(board, ref)
    line = _pcbnew.PCB_SHAPE(holder)
    line.SetShape(_pcbnew.SHAPE_T_SEGMENT)
    line.SetLayer(_GRAPHIC_LAYERS[on])
    line.SetStart(point(*start))
    line.SetEnd(point(*end))
    line.SetWidth(_pcbnew.FromMM(width))
    holder.Add(line)
    return line


def zones(board, net=None, keepout=None):
    """Zones (not teardrops), optionally of one net, or only keep-outs (True) / pours (False)."""
    return [item for item in board.Zones()
            if not item.IsTeardropArea()
            and (net is None or item.GetNetname() == net)
            and (keepout is None or bool(item.GetIsRuleArea()) == keepout)]


def zone_corners(item):
    outline = item.Outline().Outline(0)
    return [xy(outline.CPoint(index)) for index in range(outline.PointCount())]


def set_zone_corners(item, corners):
    """Replace a zone's outline with these (x, y) corners in mm."""
    outline = item.Outline()
    outline.RemoveAllContours()
    outline.NewOutline()
    for x, y in corners:
        outline.Append(_pcbnew.FromMM(x), _pcbnew.FromMM(y))


def refill(board):
    """Repour every zone; do this after any change."""
    _pcbnew.ZONE_FILLER(board).Fill(board.Zones())
# ---- end of helpers; your script follows ----
'''

NAMES = ("load", "save", "mm", "point", "xy", "layer", "part", "move_part", "rotate_part", "pad",
         "set_pad", "describe", "tracks", "vias", "track_ends", "add_track", "add_via", "remove",
         "graphics", "set_graphic", "remove_graphic", "add_line",
         "zones", "zone_corners", "set_zone_corners", "refill")

__all__ = ["HELPERS", "NAMES"]
