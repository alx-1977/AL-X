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
    """KiCad internal units to millimetres."""
    return _pcbnew.ToMM(value)


def point(x, y):
    """A KiCad point from millimetres."""
    return _pcbnew.VECTOR2I(_pcbnew.FromMM(x), _pcbnew.FromMM(y))


def xy(vector):
    """A KiCad point as (x, y) in millimetres."""
    return (round(_pcbnew.ToMM(vector.x), 4), round(_pcbnew.ToMM(vector.y), 4))


def layer(name):
    """A copper layer by its short name: 'F', 'B', 'In1' ..."""
    if name not in _LAYERS:
        raise KeyError("copper layer is one of %s" % ", ".join(sorted(_LAYERS)))
    return _LAYERS[name]


def part(board, ref):
    """The footprint with this reference; KeyError when there is none."""
    found = board.FindFootprintByReference(ref)
    if found is None:
        raise KeyError("no part %s on this board" % ref)
    return found


def move_part(board, ref, x, y):
    """Move a part's origin to (x, y) in board mm."""
    part(board, ref).SetPosition(point(x, y))


def rotate_part(board, ref, degrees):
    """Set the part's rotation (absolute, degrees)."""
    part(board, ref).SetOrientationDegrees(degrees)


def pad(board, ref, number):
    """A part's pad by its number."""
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


def fields(board, ref):
    """A part's fields (Value, MPN, LCSC...) by name."""
    return {item.GetName(): item.GetText() for item in part(board, ref).GetFields()}


def set_field(board, ref, name, value, visible=False):
    """Set one field of a part, adding it (hidden, on the fab layer) when it is new."""
    found = part(board, ref)
    if found.HasField(name):
        found.GetField(name).SetText(value)
        return
    item = _pcbnew.PCB_FIELD(found, _pcbnew.FIELD_T_USER, name)
    item.SetText(value)
    item.SetVisible(visible)
    item.SetLayer(_pcbnew.F_Fab if not found.IsFlipped() else _pcbnew.B_Fab)
    item.SetPosition(found.GetPosition())
    found.Add(item)


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
    """Vias, optionally of one net."""
    return [item for item in board.GetTracks()
            if item.GetClass() == "PCB_VIA" and (net is None or item.GetNetname() == net)]


def track_ends(item):
    """A track's two ends as ((x, y), (x, y)) in mm."""
    return (xy(item.GetStart()), xy(item.GetEnd()))


def _bends(points, radius, fit=False):
    """Straight runs and tangent arcs through the corners of a polyline, radius in mm.

    fit: a corner too tight for the radius gets the largest arc that fits (half of each
    neighbouring run) instead of being refused, as on a routed path."""
    import math as _math
    points = [tuple(map(float, p)) for p in points]
    runs, arcs, start = [], [], points[0]
    for before, corner, after in zip(points, points[1:], points[2:]):
        ux, uy = corner[0] - before[0], corner[1] - before[1]
        vx, vy = after[0] - corner[0], after[1] - corner[1]
        lu, lv = _math.hypot(ux, uy), _math.hypot(vx, vy)
        if lu < 1e-9 or lv < 1e-9:
            raise ValueError("two points at the same place at %s" % (corner,))
        ux, uy, vx, vy = ux / lu, uy / lu, vx / lv, vy / lv
        turn = _math.acos(max(-1.0, min(1.0, ux * vx + uy * vy)))
        if turn < 1e-6:
            continue
        cut = radius * _math.tan(turn / 2)
        if fit:
            room = min(lu if before == points[0] else lu / 2, lv if after == points[-1] else lv / 2)
            if cut > room:
                cut = room
            if cut < 0.05:
                # No room for a curve here: the corner stays a plain corner (17 Nov: skipping it
                # cut the run straight across at an odd angle).
                runs.append((start, corner))
                start = corner
                continue
        bend = cut / _math.tan(turn / 2)
        if cut > min(lu, lv) + 1e-6:
            raise ValueError("a %.2f mm bend does not fit at %s: shorten the radius or move the corner"
                             % (radius, corner))
        t1 = (corner[0] - ux * cut, corner[1] - uy * cut)
        t2 = (corner[0] + vx * cut, corner[1] + vy * cut)
        nx, ny = -uy, ux
        if nx * vx + ny * vy < 0:
            nx, ny = -nx, -ny
        cx, cy = t1[0] + nx * bend, t1[1] + ny * bend
        hx, hy = (t1[0] + t2[0]) / 2 - cx, (t1[1] + t2[1]) / 2 - cy
        length = _math.hypot(hx, hy)
        runs.append((start, t1))
        arcs.append((t1, (cx + hx / length * bend, cy + hy / length * bend), t2))
        start = t2
    runs.append((start, points[-1]))
    return [r for r in runs if _math.hypot(r[1][0] - r[0][0], r[1][1] - r[0][1]) > 1e-6], arcs


def add_track(board, net, points, width, on="F", radius=None, fit=False):
    """A track through points [(x, y), ...] on copper layer `on`, of `width` mm.

    radius (mm) makes every bend a tangent arc of that radius, as Friedl wants
    RF traces (14 Nov); a bend too tight for the radius is refused.
    """
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    # A point given twice in a row (a track starting at its pad) is the same point.
    points = [tuple(map(float, p)) for p in points]
    points = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
    if radius:
        runs, arcs = _bends(points, radius, fit)
    else:
        runs, arcs = list(zip(points, points[1:])), []
    made = []
    for start, end in runs:
        segment = _pcbnew.PCB_TRACK(board)
        segment.SetStart(point(*start))
        segment.SetEnd(point(*end))
        segment.SetWidth(_pcbnew.FromMM(width))
        segment.SetLayer(layer(on))
        segment.SetNet(code)
        made.append(segment)
    for start, middle, end in arcs:
        arc = _pcbnew.PCB_ARC(board)
        arc.SetStart(point(*start))
        arc.SetMid(point(*middle))
        arc.SetEnd(point(*end))
        arc.SetWidth(_pcbnew.FromMM(width))
        arc.SetLayer(layer(on))
        arc.SetNet(code)
        made.append(arc)
    # Nothing of another net may be touched, checked before anything is added (17 Nov: a V2
    # track drawn over an I2C SCL via, found by DRC afterwards, undone and drawn again, four times).
    _refuse_touching(board, net, code, made, layer(on))
    for item in made:
        board.Add(item)
    return made


def _refuse_touching(board, net, code, made, on_layer):
    try:
        clearance = board.GetDesignSettings().m_NetSettings.GetDefaultNetclass().GetClearance()
    except Exception:  # noqa: BLE001 - an older settings API: touching alone is refused
        clearance = 0
    mine = code.GetNetCode()
    others = []
    for item in board.GetTracks():
        if item.GetNetCode() != mine and (item.GetClass() == "PCB_VIA" or item.GetLayer() == on_layer):
            others.append(item)
    for footprint in board.GetFootprints():
        for item in footprint.Pads():
            if item.GetNetCode() != mine and item.IsOnLayer(on_layer):
                others.append(item)
    for new in made:
        shape = new.GetEffectiveShape(on_layer)
        box = shape.BBox()
        box.Inflate(clearance + 1)
        for other in others:
            if not box.Intersects(other.GetBoundingBox()):
                continue
            if other.GetEffectiveShape(on_layer).Collide(shape, clearance):
                if other.GetClass() == "PAD":
                    what = "pad %s.%s" % (other.GetParentFootprint().GetReference(), other.GetNumber())
                    where = other.GetPosition()
                elif other.GetClass() == "PCB_VIA":
                    what, where = "a via", other.GetPosition()
                else:
                    what, where = "a track", other.GetStart()
                raise ValueError("the %s track would touch %s of net %s at (%g, %g) within %g mm; route "
                                 "around it" % (net, what, other.GetNetname(), round(_pcbnew.ToMM(where.x), 3),
                                                round(_pcbnew.ToMM(where.y), 3), round(_pcbnew.ToMM(clearance), 3)))


def style(board):
    """Friedl's routing style as this board shows it (17 Nov: his taste lives in the tools, measured
    from his own boards): the layers he routes on (each with at least a tenth of the board's track
    pieces; never a plane layer) and his usual track width and via size. A board without tracks yet gets his rules: top and layer 3, 0.15 mm, 0.45/0.2 vias."""
    from collections import Counter as _Counter
    counts, widths, vias = _Counter(), _Counter(), _Counter()
    names = {value: key for key, value in _LAYERS.items()}
    for item in board.GetTracks():
        if item.GetClass() == "PCB_VIA":
            vias[(round(_pcbnew.ToMM(item.GetWidth(_pcbnew.F_Cu)), 3), round(_pcbnew.ToMM(item.GetDrillValue()), 3))] += 1
            continue
        counts[names.get(item.GetLayer(), "?")] += 1
        widths[round(_pcbnew.ToMM(item.GetWidth()), 3)] += 1
    total = sum(counts.values())
    order = [name for name in _LAYERS if board.IsLayerEnabled(_LAYERS[name])]
    order.sort(key=lambda name: (name != "F", name == "B", name))
    layers = [name for name in order if total and counts[name] >= 0.1 * total]
    if not layers:
        inner = [name for name in order if name.startswith("In")]
        layers = ["F"] + ([inner[-1]] if len(inner) >= 2 else ["B"])
    return {"layers": layers,
            "width": widths.most_common(1)[0][0] if widths else 0.15,
            "via": list(vias.most_common(1)[0][0]) if vias else [0.45, 0.2]}


def rf_radius(width):
    """The bend radius for an RF track of this width: three times the width, to 0.1 mm below
    (a bend that gentle keeps the line's impedance through the turn; his P0009 has 1.0 mm
    arcs on 0.355 mm lines). A work copy's own arcs are not measured: they may be a model's."""
    import math as _math
    return max(0.5, _math.floor(3 * width * 10 + 1e-6) / 10)


def route(board, net, start, end, width, on="F", step=None, margin=None, bend_cost=2.0, layers=None,
          via=(0.45, 0.2), via_cost=15.0, end_on=None, follow=None):
    """A clean path for one connection, worked out by code (17 Nov, Friedl: the switch control
    lines drawn coordinate by coordinate were "horrible at best"): from start (on layer `on`) to
    end (on `end_on`, else `on`), straight runs and 45-degree bends, as few bends and vias and as
    short as it can, clear of every other net by the board's clearance, inside the board and out
    of keep-outs that forbid tracks. It may change layer through vias among `layers` (default
    the layers this board is routed on, style()). follow: nets already routed that this one runs
    beside where it can, one clearance apart, so lines going the same way travel together
    (his rule: bundled, not wherever there is space). Returns [(layer, [points...]), ...] runs
    and the via points between them."""
    import heapq as _heapq
    import math as _math
    layers = list(layers or style(board)["layers"])
    if on not in layers:
        layers.insert(0, on)
    end_on = end_on or on
    if end_on not in layers:
        layers.append(end_on)
    span = max(abs(end[0] - start[0]), abs(end[1] - start[1]))
    if step is None:
        # A long connection on a coarser grid (17 Nov: a 60 mm control line ran out of time).
        step = 0.1 if span < 15 else 0.2 if span < 40 else 0.25
    if margin is None:
        reason = None
        for wider in (4.0, 8.0, 14.0):
            try:
                return route(board, net, start, end, width, on, step, wider, bend_cost, layers, via, via_cost, end_on,
                             follow)
            except ValueError as error:
                if "no clear path" not in str(error):
                    raise
                reason = error
        raise reason
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    try:
        clearance = _pcbnew.ToMM(board.GetDesignSettings().m_NetSettings.GetDefaultNetclass().GetClearance())
    except Exception:  # noqa: BLE001
        clearance = 0.2
    # Every move is checked as the exact piece of track it draws (17 Nov: point checks on a grid
    # let a path slip past a fine-pitch pin, and drawing it was then refused), so whatever the
    # search finds can be drawn.
    reach = _pcbnew.FromMM(clearance + width / 2.0)
    via_reach = _pcbnew.FromMM(clearance + via[0] / 2.0)
    # The grid runs through the start point, so the first run leaves it exactly straight.
    x0 = start[0] - _math.ceil((start[0] - min(start[0], end[0]) + margin) / step) * step
    y0 = start[1] - _math.ceil((start[1] - min(start[1], end[1]) + margin) / step) * step
    x1, y1 = max(start[0], end[0]) + margin, max(start[1], end[1]) + margin
    cols, rows = int(round((x1 - x0) / step)) + 1, int(round((y1 - y0) / step)) + 1
    if cols * rows * len(layers) > 900000:
        raise ValueError("the two ends are too far apart for one route (%d x %d mm); route it in parts"
                         % (x1 - x0, y1 - y0))
    window = _pcbnew.BOX2I(point(x0, y0), _pcbnew.VECTOR2L(_pcbnew.FromMM(x1 - x0), _pcbnew.FromMM(y1 - y0)))
    window.Inflate(via_reach)
    mine = code.GetNetCode()
    obstacles = {name: [] for name in layers}
    banned = {name: [] for name in layers}
    for name in layers:
        on_layer = layer(name)
        for item in board.GetTracks():
            if item.GetNetCode() != mine and (item.GetClass() == "PCB_VIA" or item.GetLayer() == on_layer) \
                    and window.Intersects(item.GetBoundingBox()):
                obstacles[name].append(item.GetEffectiveShape(on_layer))
        for footprint in board.GetFootprints():
            for item in footprint.Pads():
                if item.GetNetCode() != mine and window.Intersects(item.GetBoundingBox()) and (
                        item.IsOnLayer(on_layer) or item.GetAttribute() == _pcbnew.PAD_ATTRIB_PTH):
                    obstacles[name].append(item.GetEffectiveShape(on_layer))
        for item in board.GetDrawings():
            if item.GetClass() == "PCB_SHAPE" and item.GetLayer() == on_layer and item.GetNetCode() != mine \
                    and item.IsOnCopperLayer() and window.Intersects(item.GetBoundingBox()):
                obstacles[name].append(item.GetEffectiveShape(on_layer))
        for zone in list(board.Zones()) + [z for fp in board.GetFootprints() for z in fp.Zones()]:
            if zone.GetIsRuleArea() and zone.GetDoNotAllowTracks() and zone.IsOnLayer(on_layer):
                banned[name].append(zone.Outline())
    no_vias = [zone.Outline() for zone in list(board.Zones()) + [z for fp in board.GetFootprints() for z in fp.Zones()]
               if zone.GetIsRuleArea() and zone.GetDoNotAllowVias()]
    # Nor on a pad too small for it, its own net's included (a via on a 0.3 mm pin, 17 Nov).
    small_pads = []
    for footprint in board.GetFootprints():
        for item in footprint.Pads():
            size = item.GetSize(_pcbnew.F_Cu) if item.IsOnLayer(_pcbnew.F_Cu) else item.GetSize(_pcbnew.B_Cu)
            if _pcbnew.ToMM(min(size.x, size.y)) < via[0] + 0.1 and window.Intersects(item.GetBoundingBox()):
                small_pads.append(item.GetEffectiveShape(_pcbnew.F_Cu if item.IsOnLayer(_pcbnew.F_Cu) else _pcbnew.B_Cu))
    # Other nets' pours on a layer are not obstacles: they are refilled clear of new copper.
    outline = _pcbnew.SHAPE_POLY_SET()
    has_outline = board.GetBoardPolygonOutlines(outline, False)
    inside = None
    if has_outline and outline.OutlineCount():
        inside = _pcbnew.SHAPE_POLY_SET(outline)
        try:
            inside.Deflate(_pcbnew.FromMM(max(width, via[0]) / 2.0 + 0.3), _pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                           _pcbnew.FromMM(0.01))
        except Exception:  # noqa: BLE001
            inside = outline

    def at(c, r):
        return point(x0 + c * step, y0 + r * step)
    # Each obstacle filed under the 1 mm squares its reach covers, so a cell checks only its
    # neighbours (17 Nov: every cell against every part timed out on a long route).
    bucket = _pcbnew.FromMM(1.0)
    ox, oy = _pcbnew.FromMM(x0), _pcbnew.FromMM(y0)

    def filed(shapes, grow):
        table = {}
        for shape in shapes:
            box = shape.BBox()
            for bx in range(int((box.GetLeft() - grow - ox) // bucket), int((box.GetRight() + grow - ox) // bucket) + 1):
                for by in range(int((box.GetTop() - grow - oy) // bucket), int((box.GetBottom() + grow - oy) // bucket) + 1):
                    table.setdefault((bx, by), []).append(shape)
        return table
    track_table = {name: filed(obstacles[name], reach) for name in layers}
    edge_table = {name: filed(obstacles[name], reach + _pcbnew.FromMM(step * 1.5)) for name in layers}
    via_table = {name: filed(obstacles[name], via_reach) for name in layers}
    small_table = filed(small_pads, _pcbnew.FromMM(via[0] / 2.0 + 0.05))
    # Beside a followed net: within one grid step of the closest a track may come to it.
    hug = reach + _pcbnew.FromMM(step * 1.01)
    beside = {name: [] for name in layers}
    for name in layers:
        for item in board.GetTracks():
            if item.GetNetname() in (follow or ()) and item.GetClass() != "PCB_VIA" \
                    and item.GetLayer() == layer(name) and window.Intersects(item.GetBoundingBox()):
                beside[name].append(item.GetEffectiveShape(layer(name)))
    beside_table = {name: filed(beside[name], hug) for name in layers}
    hugging = {}

    def alongside(c, r, li):
        key = (c, r, li)
        if key not in hugging:
            here = at(c, r)
            hugging[key] = any(shape.Collide(here, hug) for shape in near(beside_table[layers[li]], here))
        return hugging[key]

    def near(table, here):
        return table.get((int((here.x - ox) // bucket), int((here.y - oy) // bucket)), ())
    track_free, via_free = {}, {}

    def free(c, r, li):
        key = (c, r, li)
        if key not in track_free:
            here = at(c, r)
            name = layers[li]
            track_free[key] = (0 <= c < cols and 0 <= r < rows
                               and (inside is None or inside.Contains(here))
                               and not any(area.Contains(here) for area in banned[name])
                               and not any(shape.Collide(here, reach) for shape in near(track_table[name], here)))
        return track_free[key]
    edge_free = {}

    def move_ok(c, r, dc, dr, li):
        key = (c, r, dc, dr, li) if (dc, dr) > (0, 0) else (c + dc, r + dr, -dc, -dr, li)
        if key not in edge_free:
            a, z = at(c, r), at(c + dc, r + dr)
            piece = _pcbnew.SEG(a, z)
            shapes = {id(shape): shape for shape in list(near(edge_table[layers[li]], a))
                      + list(near(edge_table[layers[li]], z))}.values()
            edge_free[key] = not any(shape.Collide(piece, reach) for shape in shapes)
        return edge_free[key]

    def via_ok(c, r):
        if (c, r) not in via_free:
            here = at(c, r)
            via_free[(c, r)] = (not any(area.Contains(here) for area in no_vias)
                                and not any(shape.Collide(here, _pcbnew.FromMM(via[0] / 2.0 + 0.05))
                                            for shape in near(small_table, here))
                                and all(not any(shape.Collide(here, via_reach) for shape in near(via_table[name], here))
                                        for name in layers))
        return via_free[(c, r)]
    def clear(name, a, z):
        # Every 1 mm square along the piece, not only its ends (a long last run passed a J5 pin).
        count = int(_math.hypot(z[0] - a[0], z[1] - a[1]) / 0.4) + 1
        shapes = {}
        for k in range(count + 1):
            here = point(a[0] + (z[0] - a[0]) * k / count, a[1] + (z[1] - a[1]) * k / count)
            shapes.update((id(shape), shape) for shape in near(edge_table[name], here))
        piece = _pcbnew.SEG(point(*a), point(*z))
        return not any(shape.Collide(piece, reach) for shape in shapes.values())

    def lined_up(c, r):
        # A cell from which the end is reached by one straight 0/45/90-degree run.
        dx, dy = abs(end[0] - (x0 + c * step)), abs(end[1] - (y0 + r * step))
        return dx < 1e-3 or dy < 1e-3 or abs(dx - dy) < 1e-3
    finish, fallback = {}, None
    sc, sr = int(round((start[0] - x0) / step)), int(round((start[1] - y0) / step))
    ec, er = int(round((end[0] - x0) / step)), int(round((end[1] - y0) / step))
    sl, el = layers.index(on), layers.index(end_on)
    track_free[(sc, sr, sl)] = track_free[(ec, er, el)] = True
    moves = [(1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)]
    start_state = (sc, sr, sl, -1)
    frontier, best, came, goal = [(0.0, 0.0, start_state)], {start_state: 0.0}, {}, None
    while frontier:
        _, spent, state = _heapq.heappop(frontier)
        c, r, li, d = state
        if li == el and lined_up(c, r) and (c, r) != (sc, sr) or (c, r) == (ec, er) and li == el:
            # The pin sits off the grid, so the route ends from a cell lined up with it, its last
            # run checked exactly (17 Nov: a last run bent to reach the pin grazed J5's next pin).
            if (c, r) not in finish:
                here = (x0 + c * step, y0 + r * step)
                finish[(c, r)] = lined_up(c, r) and clear(layers[li], here, end)
            if finish[(c, r)]:
                goal = state
                break
            if (c, r) == (ec, er) and fallback is None:
                fallback = state  # the old way in, if no lined-up cell reaches the pin
        if spent > best.get(state, 1e18):
            continue
        steps = []
        for index, (dc, dr) in enumerate(moves):
            if free(c + dc, r + dr, li) and move_ok(c, r, dc, dr, li):
                turn = 0.0 if d in (-1, index) else (bend_cost * 2 if (moves[d][0] * dc + moves[d][1] * dr) <= 0
                                                      else bend_cost)
                run = _math.hypot(dc, dr) * step
                if follow and alongside(c, r, li) and alongside(c + dc, r + dr, li):
                    run *= 0.3
                steps.append(((c + dc, r + dr, li, index), run + turn))
        if d != -2 and (c, r) != (sc, sr) and (c, r) != (ec, er) and via_ok(c, r):
            for other in range(len(layers)):
                if other != li and free(c, r, other):
                    steps.append(((c, r, other, -2), via_cost))
        for nxt, cost in steps:
            total = spent + cost
            if total < best.get(nxt, 1e18):
                best[nxt], came[nxt] = total, state
                left = _math.hypot(ec - nxt[0], er - nxt[1]) * step
                _heapq.heappush(frontier, (total + 1.3 * left, total, nxt))
        if len(best) > 4000000:
            break
    goal = goal or fallback
    if goal is None:
        raise ValueError("no clear path for %s between (%g, %g) and (%g, %g); route it in parts"
                         % (net, start[0], start[1], end[0], end[1]))
    states = [goal]
    while states[-1] != start_state:
        states.append(came[states[-1]])
    states.reverse()
    runs, vias, current = [], [], [(start[0], start[1])]
    for prev, here, nxt in zip(states, states[1:], states[2:] + [None]):
        place = (round(x0 + here[0] * step, 4), round(y0 + here[1] * step, 4))
        if here[3] == -2:
            runs.append((layers[prev[2]], current + ([place] if current[-1] != place else [])))
            vias.append(place)
            current = [place]
        elif nxt is not None and nxt[3] != here[3] and nxt[3] != -2:
            current.append(place)
        elif nxt is not None and nxt[3] == -2:
            current.append(place)
    last = (round(x0 + goal[0] * step, 4), round(y0 + goal[1] * step, 4))
    lined = finish.get((goal[0], goal[1]))
    if lined and last != current[-1] and _math.hypot(last[0] - end[0], last[1] - end[1]) > 1e-4:
        dx, dy = moves[goal[3]] if goal[3] >= 0 else (0, 0)
        ux, uy = end[0] - last[0], end[1] - last[1]
        if goal[3] < 0 or abs(ux * dy - uy * dx) > 1e-6 * max(1.0, _math.hypot(ux, uy)):
            current.append(last)
    current.append((end[0], end[1]))
    # The end sits off the grid (the grid runs through the start), so the last run would come in
    # at an odd angle; its corner moves along the run before it until the last run keeps its
    # own 0/45/90-degree direction.
    if not lined and len(current) >= 3 and goal[3] >= 0:
        (ax, ay), (bx, by), (ex, ey) = current[-3], current[-2], current[-1]
        dx, dy = moves[goal[3]]
        ux, uy = bx - ax, by - ay
        cross = ux * dy - uy * dx
        if abs(cross) > 1e-9:
            t = ((ex - ax) * dy - (ey - ay) * dx) / cross
            moved = (round(ax + t * ux, 4), round(ay + t * uy, 4))
            if _math.hypot(moved[0] - bx, moved[1] - by) <= step and 0 < t \
                    and clear(layers[goal[2]], (ax, ay), moved) and clear(layers[goal[2]], moved, (ex, ey)):
                current[-2] = moved
    elif not lined and len(current) == 2 and goal[3] >= 0 and vias and runs and current[0] == vias[-1]:
        # Straight from a via into the pin: the via slides across so the run comes in square;
        # the run before it ends at the via's new place (a long run, so its angle barely moves).
        (vx, vy), (ex, ey) = current
        dx, dy = moves[goal[3]]
        k = ((ex - vx) * dx + (ey - vy) * dy) / float(dx * dx + dy * dy)
        moved = (round(ex - k * dx, 4), round(ey - k * dy, 4))
        name, before = runs[-1]
        if _math.hypot(moved[0] - vx, moved[1] - vy) <= step and k > 0 and clear(layers[goal[2]], moved, (ex, ey)) \
                and clear(name, before[-2], moved) and not any(
                    shape.Collide(point(*moved), via_reach) for other in layers for shape in near(via_table[other], point(*moved))):
            vias[-1] = current[0] = moved
            name, before = runs[-1]
            runs[-1] = (name, before[:-1] + [moved])
    runs.append((layers[goal[2]], current))
    runs = [(name, [p for k, p in enumerate(points) if k == 0 or p != points[k - 1]]) for name, points in runs]
    return [(name, points) for name, points in runs if len(points) > 1], vias


def track_length(board, net):
    """Total length in mm of a net's tracks and arcs (vias not counted)."""
    return round(sum(_pcbnew.ToMM(item.GetLength()) for item in board.GetTracks()
                     if item.GetClass() in ("PCB_TRACK", "PCB_ARC") and item.GetNetname() == net), 4)


def teardrops(board, area=None):
    """Teardrop areas, optionally only those centred inside area (x0, y0, x1, y1) in mm.

    Moving a part leaves its old teardrops behind as stray copper; remove them
    with remove() and let KiCad make new ones.
    """
    found = []
    for zone in board.Zones():
        if not zone.IsTeardropArea():
            continue
        x, y = xy(zone.GetBoundingBox().Centre())
        if area is None or (area[0] <= x <= area[2] and area[1] <= y <= area[3]):
            found.append(zone)
    return found


# Library footprints the edit named (footprints: ["FireFli_ICs/RFSW6024TR13"]),
# copied in by the tool: the script cannot read his library itself.
_LIBRARY_FOOTPRINTS = {}


def _net(board, name):
    """A net by name, made when the board does not have it yet (a new net from the schematic)."""
    code = board.FindNet(name)
    if code is None:
        code = _pcbnew.NETINFO_ITEM(board, name)
        board.Add(code)
    return code


def _library_footprint(footprint):
    import os as _os, tempfile as _tempfile
    if footprint not in _LIBRARY_FOOTPRINTS:
        raise KeyError("name %s in the edit's footprints so the tool copies it in" % footprint)
    library, name = footprint.split("/", 1)
    folder = _tempfile.mkdtemp(prefix=".alx-fp-", dir=_os.path.dirname(BOARD_PATH))
    try:
        with open(_os.path.join(folder, name + ".kicad_mod"), "w") as handle:
            handle.write(_LIBRARY_FOOTPRINTS[footprint])
        made = _pcbnew.FootprintLoad(folder, name)
    finally:
        for item in _os.listdir(folder):
            _os.remove(_os.path.join(folder, item))
        _os.rmdir(folder)
    if made is None:
        raise ValueError("KiCad could not read %s" % footprint)
    made.SetFPID(_pcbnew.LIB_ID(library, name))
    return made


def add_part(board, ref, footprint, at, rotation=0, side="F", value=None, pin_nets=None):
    """Place a new part from a library footprint named in the edit's footprints.

    For a part the approved schematic added, or a new board. pin_nets
    {pad number: net} connects its pads; a net the board lacks is made.
    """
    if board.FindFootprintByReference(ref) is not None:
        raise ValueError("%s is already on the board; use replace_part" % ref)
    new = _library_footprint(footprint)
    board.Add(new)
    if side == "B":
        new.Flip(new.GetPosition(), _pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    new.SetPosition(point(*at))
    new.SetOrientationDegrees(rotation)
    new.SetReference(ref)
    if value is not None:
        new.SetValue(value)
    wanted = {str(k): v for k, v in (pin_nets or {}).items()}
    for item in new.Pads():
        if wanted.get(item.GetNumber()):
            item.SetNet(_net(board, wanted[item.GetNumber()]))
    return dict(describe(board, ref), unconnected=sorted({item.GetNumber() for item in new.Pads()
                                                         if not wanted.get(item.GetNumber())}))


def replace_part(board, ref, footprint, pin_nets=None):
    """Swap a part for a library footprint named in the edit's footprints, in its place.

    The new part keeps the reference, value, fields, position, rotation, side
    and schematic link. pin_nets {pad number: net} sets the new pads' nets;
    without it a pad keeps the net of the old pad with the same number, and a
    pad with no match is left unconnected (listed in the result).
    """
    old = part(board, ref)
    new = _library_footprint(footprint)
    old_nets = {item.GetNumber(): item.GetNetname() for item in old.Pads()}
    keep = {field.GetName(): (field.GetText(), field.IsVisible()) for field in old.GetFields()
            if field.GetName() not in ("Footprint",)}
    board.Add(new)
    if old.IsFlipped():
        new.Flip(new.GetPosition(), _pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
    new.SetPosition(old.GetPosition())
    new.SetOrientationDegrees(old.GetOrientationDegrees())
    new.SetPath(old.GetPath())
    new.SetReference(ref)
    for field_name, (text, visible) in keep.items():
        if field_name in ("Reference",):
            continue
        if new.HasField(field_name):
            new.GetField(field_name).SetText(text)
        else:
            # Directly on the new part: the old one still has this reference.
            item = _pcbnew.PCB_FIELD(new, _pcbnew.FIELD_T_USER, field_name)
            item.SetText(text)
            item.SetVisible(visible)
            item.SetLayer(_pcbnew.F_Fab if not new.IsFlipped() else _pcbnew.B_Fab)
            item.SetPosition(new.GetPosition())
            new.Add(item)
    wanted = {str(k): v for k, v in (pin_nets or {}).items()}
    unconnected = []
    for item in new.Pads():
        net_name = wanted.get(item.GetNumber(), None if wanted else old_nets.get(item.GetNumber()))
        if net_name:
            item.SetNet(_net(board, net_name))
        else:
            unconnected.append(item.GetNumber())
    board.Delete(old)
    return dict(describe(board, ref), unconnected=sorted(set(unconnected)))


def add_via(board, net, at, diameter=0.6, drill=0.3):
    """A through via on a net at (x, y) mm."""
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    via = _pcbnew.PCB_VIA(board)
    via.SetPosition(point(*at))
    via.SetWidth(_pcbnew.FromMM(diameter))
    via.SetDrill(_pcbnew.FromMM(drill))
    via.SetNet(code)
    # A via in a pad fits inside that pad (17 Nov: 0.6 mm vias on the switches' 0.3 mm pins,
    # solder wicking and bridging to the next pin). Nor may it touch another net.
    where = point(*at)
    for footprint in board.GetFootprints():
        for item in footprint.Pads():
            side = _pcbnew.F_Cu if item.IsOnLayer(_pcbnew.F_Cu) else _pcbnew.B_Cu
            if (item.IsOnLayer(_pcbnew.F_Cu) or item.IsOnLayer(_pcbnew.B_Cu)) and \
                    item.GetEffectiveShape(side).Collide(where, _pcbnew.FromMM(diameter / 2.0)):
                size = item.GetSize(side)
                smallest = _pcbnew.ToMM(min(size.x, size.y))
                if diameter > smallest - 0.1:
                    raise ValueError("a %g mm via on pad %s.%s (%g mm across) is bigger than the pad; put "
                                     "vias in a larger pad (an exposed pad) or beside the pad"
                                     % (diameter, footprint.GetReference(), item.GetNumber(), round(smallest, 3)))
    for on_layer in (_pcbnew.F_Cu, _pcbnew.B_Cu):
        _refuse_touching(board, net, code, [via], on_layer)
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
    """A part's own drawings (silkscreen, fab, courtyard; text excluded): id,
    layer, shape and points in board mm. Each id stays valid while others are
    deleted; change drawings through these helpers by id, never while looping
    over pcbnew's own list."""
    found = []
    for item in part(board, ref).GraphicalItems():
        if item.GetClass() != "PCB_SHAPE":
            continue
        name = _LAYER_NAMES.get(item.GetLayer(), str(item.GetLayer()))
        if on is not None and name != on:
            continue
        found.append({"id": item.m_Uuid.AsString(), "layer": name, "shape": item.ShowShape().lower(),
                      "start": xy(item.GetStart()), "end": xy(item.GetEnd()),
                      "width": round(_pcbnew.ToMM(item.GetWidth()), 4)})
    return found


def _graphic(board, ref, identifier):
    for item in part(board, ref).GraphicalItems():
        if item.GetClass() == "PCB_SHAPE" and item.m_Uuid.AsString() == identifier:
            return item
    raise KeyError("%s has no drawing %s" % (ref, identifier))


def set_graphic(board, ref, identifier, start=None, end=None):
    """Move a part's drawing (a line's or rectangle's ends) in board mm."""
    item = _graphic(board, ref, identifier)
    if start is not None:
        item.SetStart(point(*start))
    if end is not None:
        item.SetEnd(point(*end))


def remove_graphic(board, ref, identifier):
    """Delete a part's drawing by its id from graphics()."""
    part(board, ref).Delete(_graphic(board, ref, identifier))


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


def _first_outline(shapes, what):
    """The first outline of a polygon set; KiCad crashes reading one that is not there."""
    if shapes.OutlineCount() < 1:
        raise ValueError("%s has no outline" % what)
    return shapes.Outline(0)


def zone_corners(item):
    """The corners of a zone's first outline (cutouts and further outlines aside)."""
    outline = _first_outline(item.Outline(), "this zone")
    return [xy(outline.CPoint(index)) for index in range(outline.PointCount())]


def set_zone_corners(item, corners):
    """Replace the corners of a zone's first outline, in mm; its cutouts and
    any further outlines are kept as they are."""
    outline = _first_outline(item.Outline(), "this zone")
    outline.Clear()
    for x, y in corners:
        outline.Append(_pcbnew.FromMM(x), _pcbnew.FromMM(y))
    outline.SetClosed(True)


def _copper_shapes(board):
    return [item for item in board.GetDrawings()
            if item.GetClass() == "PCB_SHAPE" and _pcbnew.IsCopperLayer(item.GetLayer())]


def _copper_shape(board, identifier):
    for item in _copper_shapes(board):
        if item.m_Uuid.AsString() == identifier:
            return item
    raise KeyError("no copper shape %s on this board" % identifier)


def copper_shapes(board, net=None):
    """Copper drawn as a shape, not a track (a feed rectangle, a pad extension):
    id, net, layer, shape, whether solid, and its corners in board mm."""
    names = {number: name for name, number in _LAYERS.items()}
    found = []
    for item in _copper_shapes(board):
        if net is not None and item.GetNetname() != net:
            continue
        outline = _pcbnew.SHAPE_POLY_SET()
        item.TransformShapeToPolygon(outline, item.GetLayer(), 0, _pcbnew.FromMM(0.005), _pcbnew.ERROR_INSIDE)
        corners = []
        if outline.OutlineCount():
            chain = outline.Outline(0)
            corners = [xy(chain.CPoint(index)) for index in range(chain.PointCount())]
        found.append({"id": item.m_Uuid.AsString(), "net": item.GetNetname(),
                      "layer": names.get(item.GetLayer(), str(item.GetLayer())),
                      "shape": item.ShowShape().lower(), "solid": item.IsSolidFill(), "corners": corners})
    return found


def set_copper_corners(board, identifier, corners):
    """Make a copper shape a solid polygon with these (x, y) corners in mm."""
    item = _copper_shape(board, identifier)
    item.SetShape(_pcbnew.SHAPE_T_POLY)
    item.SetPolyPoints([point(x, y) for x, y in corners])
    item.SetFilled(True)


def remove_copper(board, identifier):
    """Delete a copper shape by its id from copper_shapes()."""
    board.Delete(_copper_shape(board, identifier))


def add_copper(board, net, on, corners):
    """A solid copper polygon of `net` on copper layer `on`, corners in mm."""
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    item = _pcbnew.PCB_SHAPE(board)
    item.SetShape(_pcbnew.SHAPE_T_POLY)
    item.SetLayer(layer(on))
    item.SetPolyPoints([point(x, y) for x, y in corners])
    item.SetFilled(True)
    item.SetWidth(0)
    item.SetNet(code)
    board.Add(item)
    return item


def add_taper(board, net, on, narrow, narrow_width, wide, wide_width):
    """A straight taper (teardrop) from a narrow end to a wide one: centres
    (x, y) in mm and the width at each end. Friedl's rule: copper wider than
    the pad it leaves or meets joins it through a taper, never a step; where
    the pad is the wider one, no taper is needed."""
    (x0, y0), (x1, y1) = narrow, wide
    length = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    if length <= 0:
        raise ValueError("the two ends of a taper must differ")
    nx, ny = -(y1 - y0) / length, (x1 - x0) / length
    a, b = narrow_width / 2, wide_width / 2
    return add_copper(board, net, on, [(x0 + nx * a, y0 + ny * a), (x1 + nx * b, y1 + ny * b),
                                       (x1 - nx * b, y1 - ny * b), (x0 - nx * a, y0 - ny * a)])


def _pretty(library):
    """The folder of a footprint library in a library copy, by its name (FireFli_Misc)."""
    import os as _os
    root = _os.path.dirname(BOARD_PATH)
    found = [_os.path.join(where, name) for where, names, _ in _os.walk(root)
             for name in names if name == library + ".pretty"]
    if len(found) != 1:
        raise KeyError("no footprint library %s in this copy" % library)
    return found[0]


def footprints(library):
    """The footprint names of a library in a library copy."""
    return sorted(_pcbnew.FootprintEnumerate(_pretty(library)))


def footprint(library, name):
    """A library footprint, to read or change; save it with save_footprint."""
    found = _pcbnew.FootprintLoad(_pretty(library), name)
    if found is None:
        raise KeyError("no footprint %s in %s" % (name, library))
    return found


def save_footprint(library, item):
    """Save a footprint into its library in the copy (a new one is added under its own name)."""
    import os as _os
    folder = _pretty(library)
    if not any(name.endswith(".kicad_mod") for name in _os.listdir(folder)):
        # KiCad tells a library's format from the files already in it.
        raise ValueError("KiCad cannot save into an empty library; write its first footprint "
                         "as a .kicad_mod file, then use save_footprint")
    _pcbnew.FootprintSave(folder, item)


def refill(board):
    """Repour every zone; do this after any change."""
    _pcbnew.ZONE_FILLER(board).Fill(board.Zones())
# ---- end of helpers; your script follows ----
'''

def script_runner(script):
    """Her script, compiled as its own file named "script" with the helpers in reach."""
    return "exec(compile(%r, 'script', 'exec'), dict(globals(), __name__='__main__'))\n" % script


NAMES = ("load", "save", "mm", "point", "xy", "layer", "part", "move_part", "rotate_part", "pad",
         "set_pad", "fields", "set_field", "describe", "tracks", "vias", "track_ends", "add_track", "add_via", "remove",
         "graphics", "set_graphic", "remove_graphic", "add_line",
         "track_length", "teardrops", "replace_part", "add_part",
         "zones", "zone_corners", "set_zone_corners",
         "copper_shapes", "set_copper_corners", "remove_copper", "add_copper", "add_taper",
         "footprints", "footprint", "save_footprint", "refill")

__all__ = ["HELPERS", "NAMES", "script_runner"]

