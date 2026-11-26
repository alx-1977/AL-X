"""RF track helpers loaded into the confined KiCad interpreter (26 Nov 2026).

On the P0009 RF job the board agent worked out 50-ohm widths by hand six times (first
0.20 mm, about 69 ohm, until Friedl asked for proper RF calculation) and built bend arcs
by hand eleven times, with seven failed scripts. These do both in one call each.
"""

RF_HELPERS = r'''
def _elliptic_k(k):
    """The complete elliptic integral of the first kind, by the arithmetic-geometric mean."""
    import math as _math
    a, b = 1.0, _math.sqrt(max(0.0, 1.0 - k * k))
    for _ in range(40):
        a, b = (a + b) / 2.0, _math.sqrt(a * b)
    return _math.pi / (2.0 * a)


def coplanar_impedance(width, gap, height, epsilon_r):
    """Grounded coplanar waveguide (ground pour beside, plane below), in ohm, all sizes in mm."""
    import math as _math
    k = width / (width + 2.0 * gap)
    k1 = _math.tanh(_math.pi * width / (4.0 * height)) / _math.tanh(_math.pi * (width + 2.0 * gap) / (4.0 * height))
    kp, k1p = _math.sqrt(1.0 - k * k), _math.sqrt(1.0 - k1 * k1)
    ratio = _elliptic_k(kp) * _elliptic_k(k1) / (_elliptic_k(k) * _elliptic_k(k1p))
    effective = (1.0 + epsilon_r * ratio) / (1.0 + ratio)
    return 60.0 * _math.pi / _math.sqrt(effective) / (_elliptic_k(k) / _elliptic_k(kp) + _elliptic_k(k1) / _elliptic_k(k1p))


def microstrip_impedance(width, height, epsilon_r):
    """Microstrip over a plane (Hammerstad), in ohm, sizes in mm."""
    import math as _math
    u = width / height
    effective = (epsilon_r + 1.0) / 2.0 + (epsilon_r - 1.0) / 2.0 * (1.0 + 12.0 / u) ** -0.5
    if u < 1.0:
        effective += (epsilon_r - 1.0) / 2.0 * 0.04 * (1.0 - u) ** 2
        return 60.0 / _math.sqrt(effective) * _math.log(8.0 / u + u / 4.0)
    return 120.0 * _math.pi / (_math.sqrt(effective) * (u + 1.393 + 0.667 * _math.log(u + 1.444)))


def board_dielectric(board, on="F"):
    """(height mm, epsilon_r) between copper layer `on` and the next copper layer, from the
    board's own stack-up. KiCad's Python does not expose the stack-up, so it is read from the file."""
    import re as _re
    content = open(board.GetFileName()).read()
    start = content.find("(stackup")
    if start < 0:
        raise ValueError("this board has no stack-up (Board Setup > Physical Stackup): RF widths need one")
    layers = []
    for name, body in _re.findall(r'\(layer\s+"([^"]+)"((?:[^()]|\([^()]*\))*)\)', content[start:content.find("(copper_finish", start)]):
        kind = _re.search(r'\(type\s+"?([^")]+)"?\)', body)
        thickness = _re.search(r'\(thickness\s+([0-9.]+)', body)
        epsilon = _re.search(r'\(epsilon_r\s+([0-9.]+)', body)
        layers.append((name, kind.group(1) if kind else "", float(thickness.group(1)) if thickness else 0.0,
                       float(epsilon.group(1)) if epsilon else None))
    target = {"F": "F.Cu", "B": "B.Cu"}.get(on, on if on.endswith(".Cu") else on + ".Cu")
    names = [item[0] for item in layers]
    if target not in names:
        raise KeyError("no copper layer %s in the stack-up" % target)
    index = names.index(target)
    sides = []
    for step in (1, -1):
        height, epsilons, cursor = 0.0, [], index + step
        while 0 <= cursor < len(layers) and layers[cursor][1] != "copper":
            if layers[cursor][3] is not None:
                height += layers[cursor][2]
                epsilons.append(layers[cursor][3])
            cursor += step
        if 0 <= cursor < len(layers) and epsilons:
            sides.append((height, sum(epsilons) / len(epsilons)))
    if not sides:
        raise ValueError("no dielectric with a known epsilon_r beside %s in the stack-up" % target)
    return min(sides)


def line_impedance(board, width, on="F", gap=None):
    """The impedance in ohm of a track of `width` mm on layer `on`: grounded coplanar with `gap`
    mm to the ground pour beside it, else microstrip."""
    height, epsilon_r = board_dielectric(board, on)
    if gap:
        return coplanar_impedance(width, gap, height, epsilon_r)
    return microstrip_impedance(width, height, epsilon_r)


def rf_width(board, impedance=50.0, on="F", gap=None):
    """The track width in mm (to 0.005 mm) for `impedance` ohm on layer `on` of this board's
    stack-up: grounded coplanar with `gap` mm to the pour beside it, else microstrip. Use it for
    every RF line rather than working it out (26 Nov: 0.20 mm was about 69 ohm)."""
    low, high = 0.02, 10.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if line_impedance(board, middle, on, gap) > impedance:
            low = middle
        else:
            high = middle
    return round(round(((low + high) / 2.0) / 0.005) * 0.005, 3)


def round_bends(board, net, radius=None, on=None):
    """Every corner of the plain tracks of `net` becomes a tangent arc, in one call: radius in mm,
    default rf_radius of the track's width. Only corners where exactly two tracks of the net meet,
    with no via, pad or arc there; a corner with too little room gets the largest arc that fits.
    Nothing else changes, and nothing of another net is touched (the run is left as it was if a
    curve would touch it). on limits it to one layer ('F', 'B', 'In1' ...).
    Returns {"rounded": corners, "runs": runs redrawn, "left": [why a run was left]}.
    26 Nov: eleven arcs were built by hand, seven scripts failed, for four short RF tracks."""
    import math as _math
    names = {value: key for key, value in _LAYERS.items()}
    code = board.FindNet(net)
    if code is None:
        raise KeyError("no net %s on this board" % net)
    mine = code.GetNetCode()
    key = lambda p: (round(p[0], 4), round(p[1], 4))  # noqa: E731
    segments, stops = [], set()
    for item in board.GetTracks():
        if item.GetNetCode() != mine:
            continue
        if item.GetClass() == "PCB_TRACK" and (on is None or item.GetLayer() == layer(on)):
            segments.append(item)
        elif item.GetClass() == "PCB_VIA":
            stops.add(key(xy(item.GetPosition())))
        else:
            stops.add(key(xy(item.GetStart())))
            stops.add(key(xy(item.GetEnd())))
    for footprint in board.GetFootprints():
        for pad_ in footprint.Pads():
            if pad_.GetNetCode() == mine:
                stops.add(key(xy(pad_.GetPosition())))
    ends, meeting = {}, {}
    for item in segments:
        for end in (xy(item.GetStart()), xy(item.GetEnd())):
            ends.setdefault((item.GetLayer(), item.GetWidth(), key(end)), []).append(item)
            # Every track of the net ending here on this layer, whatever its width: a narrower
            # branch makes the point a junction, never a corner to round away.
            meeting[(item.GetLayer(), key(end))] = meeting.get((item.GetLayer(), key(end)), 0) + 1
    used, rounded, runs, left = set(), 0, 0, []
    for item in segments:
        if id(item) in used:
            continue
        group = (item.GetLayer(), item.GetWidth())
        def inner(point):
            return (key(point) not in stops and len(ends.get(group + (key(point),), ())) == 2
                    and meeting.get((group[0], key(point))) == 2)
        chain, points = [item], [xy(item.GetStart()), xy(item.GetEnd())]
        used.add(id(item))
        for forward in (True, False):
            while True:
                tip = points[-1] if forward else points[0]
                if not inner(tip):
                    break
                following = [s for s in ends[group + (key(tip),)] if id(s) not in used]
                if not following:
                    break
                step = following[0]
                used.add(id(step))
                start, end = xy(step.GetStart()), xy(step.GetEnd())
                other = end if key(start) == key(tip) else start
                if forward:
                    points.append(other)
                    chain.append(step)
                else:
                    points.insert(0, other)
                    chain.insert(0, step)
        corners = 0
        for before, corner, after in zip(points, points[1:], points[2:]):
            ux, uy = corner[0] - before[0], corner[1] - before[1]
            vx, vy = after[0] - corner[0], after[1] - corner[1]
            if abs(ux * vy - uy * vx) > 1e-9 * max(1.0, _math.hypot(ux, uy) * _math.hypot(vx, vy)):
                corners += 1
        if not corners:
            continue
        width = _pcbnew.ToMM(item.GetWidth())
        side = names.get(item.GetLayer())
        if side is None:
            left.append("a run of %s on a layer these helpers do not name" % net)
            continue
        wanted = radius if radius is not None else rf_radius(width)
        done, why = False, ""
        for tried in (wanted, wanted / 2.0):
            try:
                # Drawn before the old run goes: the same net, so nothing to collide with, and a
                # refusal leaves the run exactly as it was.
                add_track(board, net, points, width, side, tried, fit=True)
                done = True
                break
            except ValueError as error:
                why = str(error)
        if not done:
            left.append("the run through %s kept its corners: %s" % (points[1], why))
            continue
        for gone in chain:
            remove(board, gone)
        rounded += corners
        runs += 1
    return {"rounded": rounded, "runs": runs, "left": left}
'''
