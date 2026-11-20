"""Schematic edits for her KiCad scripts (D-082): KiCad has no schematic Python API.

Text edits of .kicad_sch files in the work copy, kept as KiCad wrote them
except where changed. Ported from the P0009 switch swap of 14 Nov, where each
of these was done by hand. Runs inside the confined KiCad Python (3.9) with the
board helpers; it reads only files in the copy.
"""

GRID_SUFFIX = ".alx-grid.json"

_GEOMETRY = r'''
# ---- schematic helpers (text edits of .kicad_sch; KiCad has no schematic API) ----
import math as _math
import json as _json
import os as _os
import re as _re
import uuid as _uuid

_TOKEN = _re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+)')
# Library symbols the edit named (symbols: ["FireFli_ICs:RFSW6024TR13"]), copied
# in by the tool: the script cannot read his library itself.
_LIBRARY_SYMBOLS = {}
_DESIGN_BLOCKS = {}


def _sx(text):
    """Nested lists of tokens (strings keep their quotes) and each list's span."""
    stack, root, spans = [], [], {}
    current = root
    for match in _TOKEN.finditer(text):
        token = match.group(1)
        if token == "(":
            node = []
            spans[id(node)] = [match.start(1), None]
            current.append(node)
            stack.append(current)
            current = node
        elif token == ")":
            spans[id(current)][1] = match.end(1)
            current = stack.pop()
        else:
            current.append(token)
    return root[0], spans


def _head(node):
    return node[0] if isinstance(node, list) and node else None


def _child(node, name):
    for item in node:
        if _head(item) == name:
            return item
    return None


def _children(node, name):
    return [item for item in node if _head(item) == name]


def _unq(value):
    return value[1:-1].replace('\\"', '"') if value.startswith('"') else value


def _q(value):
    return '"%s"' % str(value).replace('\\', '\\\\').replace('"', '\\"')


def _r(point):
    return (round(float(point[0]), 2), round(float(point[1]), 2))


_PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
          "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8)}


def _paper_size(paper):
    if not paper:
        return None
    if paper[0] == "User" and len(paper) >= 3:
        size = float(paper[1]), float(paper[2])
    else:
        size = _PAPER.get(paper[0])
    return size[::-1] if size and paper[-1] == "portrait" else size


def _lib_pins(symbol):
    """{number: (x, y)} connection points of a library symbol, its own coordinates (y up)."""
    pins = {}

    def walk(node):
        for item in node:
            if _head(item) == "pin":
                at, number = _child(item, "at"), _child(item, "number")
                if at is not None and number is not None:
                    pins[_unq(number[1])] = (float(at[1]), float(at[2]))
            elif isinstance(item, list):
                walk(item)
    walk(symbol)
    return pins


def _lib_pin_angles(symbol):
    """{number: angle} of a library symbol's pins: the way each pin runs from its end into the body."""
    angles = {}

    def walk(node):
        for item in node:
            if _head(item) == "pin":
                at, number = _child(item, "at"), _child(item, "number")
                if at is not None and number is not None:
                    angles[_unq(number[1])] = float(at[3]) if len(at) > 3 else 0.0
            elif isinstance(item, list):
                walk(item)
    walk(symbol)
    return angles


def _lib_drawings(symbol, lib):
    """Symbol drawings for the placed unit and body style, including common drawings."""
    unit = _child(symbol, "unit")
    style = _child(symbol, "body_style") or _child(symbol, "convert")
    for part in _children(lib, "symbol"):
        owner = _unq(part[1]).rsplit("_", 2)
        if (len(owner) != 3 or (owner[1] in ("0", unit[1] if unit else "1")
                                and owner[2] in ("0", style[1] if style else "1"))):
            yield part


def _lib_outline(symbol):
    """Every drawn point of a library symbol (its body), its own coordinates (y up)."""
    points = []

    def walk(node):
        for item in node:
            if not isinstance(item, list):
                continue
            head = _head(item)
            if head == "circle":
                x, y = map(float, _child(item, "center")[1:3])
                radius = float(_child(item, "radius")[1])
                points.extend(((x - radius, y - radius), (x + radius, y + radius)))
            arc_points = [_child(item, key) for key in ("start", "mid", "end")] if head == "arc" else []
            if arc_points and all(arc_points):
                a, b, c = (tuple(map(float, point[1:3])) for point in arc_points)
                ux, uy = b[0] - a[0], b[1] - a[1]
                vx, vy = c[0] - a[0], c[1] - a[1]
                determinant = 2 * (ux * vy - uy * vx)
                if abs(determinant) > 1e-9:
                    u2, v2 = ux * ux + uy * uy, vx * vx + vy * vy
                    x = a[0] + (u2 * vy - v2 * uy) / determinant
                    y = a[1] + (ux * v2 - vx * u2) / determinant
                    radius = _math.hypot(a[0] - x, a[1] - y)
                    # The whole circle conservatively bounds the arc.
                    points.extend(((x - radius, y - radius), (x + radius, y + radius)))
            if head in ("start", "end", "mid", "xy", "center") and len(item) >= 3:
                try:
                    points.append((float(item[1]), float(item[2])))
                except ValueError:
                    pass
            if head not in ("property", "pin"):
                walk(item)
    walk(symbol)
    return points


def _placed(at, mirror, x, y):
    """A library point (y up) placed on the sheet (y down): mirrored, then turned."""
    sx, sy = x, -y
    if mirror == "x":
        sy = -sy
    elif mirror == "y":
        sx = -sx
    turn = _math.radians(-float(at[3]) if len(at) > 3 else 0.0)
    rx = sx * _math.cos(turn) - sy * _math.sin(turn)
    ry = sx * _math.sin(turn) + sy * _math.cos(turn)
    return _r((float(at[1]) + rx, float(at[2]) + ry))


def _on(point, a, b):
    """The point lies on the wire a-b, ends included."""
    (x, y), (x1, y1), (x2, y2) = point, a, b
    if abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) > 1e-6:
        return False
    return (min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6
            and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6)


def _segment_groups(segments, junctions):
    """Group physically connected segments, joining crosses only at explicit junctions."""
    from collections import defaultdict
    incident = defaultdict(set)
    for index, (a, b) in enumerate(segments):
        incident[a].add(index)
        incident[b].add(index)
    for point in junctions:
        incident[point].update(i for i, (a, b) in enumerate(segments) if _on(point, a, b))
    neighbors = defaultdict(set)
    for indices in incident.values():
        for index in indices:
            neighbors[index].update(indices)
    groups = {}
    for first in range(len(segments)):
        if first in groups:
            continue
        groups[first] = first
        pending = [first]
        while pending:
            for index in neighbors[pending.pop()]:
                if index not in groups:
                    groups[index] = first
                    pending.append(index)
    return groups


def _power_side(drawings, rotation, axis):
    """Sheet y of the graphic's bounding centre relative to the connection pin."""
    pins = {number: point for part in drawings for number, point in _lib_pins(part).items()}
    if len(pins) != 1:
        return None
    at = ["at", 0, 0, rotation]
    ys = [_placed(at, axis, x, y)[1] for part in drawings for x, y in _lib_outline(part)]
    if ys:
        return (min(ys) + max(ys)) / 2 - _placed(at, axis, *next(iter(pins.values())))[1]


def _power_turn(drawings, net, axis=None):
    want = 1 if "GND" in net.upper() else -1
    turns = [(abs(side), turn) for turn in (0, 90, 180, 270)
             if (side := _power_side(drawings, turn, axis)) is not None and side * want > 0.01]
    if not turns:
        raise ValueError("cannot determine the graphic orientation for " + net)
    return max(turns, key=lambda item: (item[0], -item[1]))[1]


def _orientation(symbol, lib, ref, net):
    at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
    axis = mirror[1] if mirror else None
    rotation = float(at[3])
    drawings = list(_lib_drawings(symbol, lib))
    if _child(lib, "power"):
        side = _power_side(drawings, rotation, axis)
        down = "GND" in net.upper()
        if side is not None and side * (1 if down else -1) <= 0.01:
            return "ground graphic must be below its pin" if down else "supply graphic must be above its pin"
    else:
        pins = {pin for part in drawings for pin in _lib_pins(part)}
        # Three-pin transistors are exempt. U and IC references apply
        # even where a particular unit has only a few drawn pins.
        if (len(pins) > 3 or ref.startswith(("U", "IC"))) and _math.cos(
                _math.radians(rotation)) * (-1 if axis == "x" else 1) < -0.01:
            return "library top faces down"


def _orientation_findings(text):
    root, _ = _sx(text)
    libs = {_unq(n[1]): n for n in _children(_child(root, "lib_symbols") or [], "symbol")}
    for symbol in _children(root, "symbol"):
        lib = libs.get(_unq(_child(symbol, "lib_id")[1]))
        if lib is None:
            continue
        props = {_unq(p[1]): _unq(p[2]) for p in _children(symbol, "property")}
        ref = props.get("Reference", "")
        problem = _orientation(symbol, lib, ref, props.get("Value", ""))
        if problem:
            uid = _child(symbol, "uuid")
            yield (_unq(uid[1]) if uid else ref, ref, problem, _r(_child(symbol, "at")[1:3]))
'''

_geometry = {}
exec(_GEOMETRY, _geometry)
placed_point = _geometry["_placed"]
point_on_segment = _geometry["_on"]
segment_groups = _geometry["_segment_groups"]
orientation_findings = _geometry["_orientation_findings"]
paper_size = _geometry["_paper_size"]

SCHEMATIC_HELPERS = "_GRID_SUFFIX = %r\n" % GRID_SUFFIX + _GEOMETRY + r'''
def _straight(points):
    """A path without points where it runs straight on: no wire break there for another
    wire's end to join (16 Nov: a GND lead crossing an RF wire at its stub end)."""
    out = [points[0]]
    for here, nxt in zip(points[1:], points[2:]):
        prev = out[-1]
        if (prev[0] == here[0] == nxt[0]) or (prev[1] == here[1] == nxt[1]):
            continue
        out.append(here)
    out.append(points[-1])
    return [p for i, p in enumerate(out) if i == 0 or p != out[i - 1]]


def _backtracks(points):
    """The path turns straight back over itself somewhere (19 Nov 2026: a fallback route ran
    out of a pin's stub and back along it; the checks only see wiring already drawn)."""
    for a, b, c in zip(points, points[1:], points[2:]):
        u, v = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
        if abs(u[0] * v[1] - u[1] * v[0]) < 1e-9 and u[0] * v[0] + u[1] * v[1] < 0:
            return True
    return False


def _top_properties(block):
    """(name, start, end) of each property directly in a symbol's text block."""
    found, depth, index, quoted = [], 0, 0, False
    while index < len(block):
        char = block[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
            if depth == 2 and block.startswith('(property "', index):
                name_end = block.index('"', index + 11)
                level, j, inside = 0, index, False
                while j < len(block):
                    c = block[j]
                    if inside:
                        if c == "\\":
                            j += 1
                        elif c == '"':
                            inside = False
                    elif c == '"':
                        inside = True
                    elif c == "(":
                        level += 1
                    elif c == ")":
                        level -= 1
                        if level == 0:
                            break
                    j += 1
                found.append((block[index + 11:name_end], index, j + 1))
                index = j
                depth -= 1
        elif char == ")":
            depth -= 1
        index += 1
    return found


def _overlap(a, b, c, d):
    """Two wires on one line sharing a stretch (more than a point)."""
    cross = lambda p, q, r: (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])  # noqa: E731
    if a == b or c == d or abs(cross(a, b, c)) > 1e-6 or abs(cross(a, b, d)) > 1e-6:
        return False
    axis = 0 if abs(b[0] - a[0]) >= abs(b[1] - a[1]) else 1
    low = max(min(a[axis], b[axis]), min(c[axis], d[axis]))
    high = min(max(a[axis], b[axis]), max(c[axis], d[axis]))
    return high - low > 1e-6


class Sheet:
    """One schematic sheet of the copy, edited as text and saved with save()."""

    def __init__(self, name):
        self.path = _os.path.join(_os.path.dirname(BOARD_PATH), name)
        if not self.path.endswith(".kicad_sch") or not _os.path.isfile(self.path):
            raise KeyError("no schematic sheet %s in this copy" % name)
        with open(self.path) as handle:
            self.text = handle.read()

    # -- reading --
    def _tree(self):
        return _sx(self.text)

    def _libs(self, root):
        cache = _child(root, "lib_symbols") or []
        return {_unq(item[1]): item for item in _children(cache, "symbol")}

    def _symbol_pins(self, symbol, libs):
        lib = libs.get(_unq(_child(symbol, "lib_id")[1]))
        if lib is None:
            return {}
        at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
        pins = {}
        for part in _lib_drawings(symbol, lib):
            for number, (x, y) in _lib_pins(part).items():
                pins[number] = _placed(at, mirror[1] if mirror else None, x, y)
        return pins

    def _ref(self, symbol):
        for item in _children(symbol, "property"):
            if _unq(item[1]) == "Reference":
                return _unq(item[2])
        return ""

    def symbols(self):
        """List of dicts with ref, lib_id, value, at (x, y), rotation, mirror, uuid and pins {number: (x, y)}."""
        root, _ = self._tree()
        libs = self._libs(root)
        found = []
        for symbol in _children(root, "symbol"):
            at = _child(symbol, "at")
            props = {_unq(p[1]): _unq(p[2]) for p in _children(symbol, "property")}
            mirror = _child(symbol, "mirror")
            found.append({"ref": props.get("Reference", ""), "lib_id": _unq(_child(symbol, "lib_id")[1]),
                          "value": props.get("Value", ""), "at": _r((at[1], at[2])),
                          "rotation": float(at[3]) if len(at) > 3 else 0.0,
                          "mirror": mirror[1] if mirror else None,
                          "uuid": _unq(_child(symbol, "uuid")[1]) if _child(symbol, "uuid") else "",
                          "pins": self._symbol_pins(symbol, libs)})
        return found

    def _page_size(self, root):
        paper = _child(root, "paper")
        return _paper_size([_unq(token) for token in paper[1:]]) if paper else None

    def _title_block(self, root):
        """The title block's area on this sheet, [x0, y0, x1, y1], from his page template
        (16 Nov, Friedl: the drawing ran into the title block). None when unknown."""
        page = self._page_size(root)
        if page is None:
            return None
        width, height = page
        folder = _os.path.dirname(self.path)
        margins, reach_x, reach_y = (10.0, 10.0), 110.0, 32.0   # KiCad's own title block
        try:
            import json as _json
            project = [n for n in _os.listdir(folder) if n.endswith(".kicad_pro")]
            layout = _json.load(open(_os.path.join(folder, project[0]))).get("schematic", {}).get(
                "page_layout_descr_file") if project else None
            if layout and _os.path.isfile(_os.path.join(folder, layout)):
                text = open(_os.path.join(folder, layout)).read()
                found = _re.search(r"\(right_margin ([\d.]+)\).*?\(bottom_margin ([\d.]+)\)", text)
                if found:
                    margins = (float(found.group(1)), float(found.group(2)))
                reach_x, reach_y = 0.0, 0.0
                for item in _re.finditer(r"\((tbtext|bitmap|line|rect)\b[^\n]*?\(pos ([\d.-]+) ([\d.-]+) rbcorner\)"
                                         r"(?:[^\n]*?\(scale ([\d.]+)\))?", text):
                    x, y = float(item.group(2)), float(item.group(3))
                    if item.group(1) == "bitmap":
                        # A picture is centred on its point; its size from the PNG header (300 ppi).
                        header = _re.search(r'\(data "89 50 4E 47 0D 0A 1A 0A 00 00 00 0D 49 48 44 52 '
                                            r'((?:[0-9A-F]{2} ){8})', text[item.start():])
                        if header:
                            raw = bytes.fromhex(header.group(1))
                            scale = float(item.group(4) or 1)
                            w = int.from_bytes(raw[:4], "big") / 300 * 25.4 * scale
                            h = int.from_bytes(raw[4:8], "big") / 300 * 25.4 * scale
                            x, y = x + w / 2, y + h / 2
                    else:
                        y += 3.0   # the text's own height, below its point
                    reach_x, reach_y = max(reach_x, x), max(reach_y, y)
        except (OSError, ValueError, IndexError):
            pass
        if not reach_x or not reach_y:
            return None
        right, bottom = width - margins[0], height - margins[1]
        return (right - reach_x, bottom - reach_y, right, bottom)

    def _missing(self, ref):
        """No such symbol here; where it is instead (16 Nov: the agent named the wrong sheet)."""
        folder = _os.path.dirname(self.path)
        elsewhere = []
        for name in sorted(n for n in _os.listdir(folder) if n.endswith(".kicad_sch")):
            if _os.path.join(folder, name) == self.path:
                continue
            with open(_os.path.join(folder, name)) as handle:
                if '(property "Reference" "%s"' % ref in handle.read():
                    elsewhere.append(name)
        return KeyError("no symbol %s on %s%s" % (ref, _os.path.basename(self.path),
                                                   "; it is on %s" % ", ".join(elsewhere) if elsewhere else ""))

    def _placed_symbol(self, ref):
        root, spans = self._tree()
        libs = self._libs(root)
        found = [s for s in _children(root, "symbol") if self._ref(s) == ref
                 or (_child(s, "uuid") and _unq(_child(s, "uuid")[1]) == ref)]
        if not found:
            raise self._missing(ref)
        symbol = found[0]
        return symbol, libs.get(_unq(_child(symbol, "lib_id")[1])), spans

    def pin_direction(self, ref, pin):
        """The way a pin points out of its part on the sheet: (1, 0) right, (0, 1) down, ..."""
        symbol, lib, _ = self._placed_symbol(ref)
        direction = self._direction_of(symbol, lib, pin) if lib is not None else None
        if direction is None:
            raise KeyError("%s has no pin %s" % (ref, pin))
        return direction

    def _stub_end(self, ref, pin, length=2.54):
        start = self.symbol(ref)["pins"][str(pin)]
        dx, dy = self.pin_direction(ref, pin)
        return start, _r((start[0] + dx * length, start[1] + dy * length))

    def _wired_to(self, point):
        """The wires joined to point, through ends and wires they end on: (their points, their ends)."""
        wires = [tuple(pts) for pts in self.wires()]
        root, _ = self._tree()
        dots = {_r(_child(dot, "at")[1:3]) for dot in _children(root, "junction")}
        reached, ends, frontier = set(), {_r(point)}, [_r(point)]
        while frontier:
            here = frontier.pop()
            for index, pts in enumerate(wires):
                if index in reached:
                    continue
                if any(_on(here, a, b) for a, b in zip(pts, pts[1:])):
                    reached.add(index)
                    attached = {dot for dot in dots if any(_on(dot, a, b) for a, b in zip(pts, pts[1:]))}
                    for e in {pts[0], pts[-1], *attached}:
                        if e not in ends:
                            ends.add(e)
                            frontier.append(e)
        return [wires[i] for i in reached], ends

    def _orphan_wires(self, root, libs):
        """Wires in groups that reach no pin, label or port."""
        wires = [tuple(pts) for pts in self.wires()]
        anchors = {p for s in _children(root, "symbol") for p in self._symbol_pins(s, libs).values()}
        anchors |= {_r(_child(n, "at")[1:3]) for n in root
                    if _head(n) in ("label", "global_label", "hierarchical_label")}
        parent = list(range(len(wires)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i
        for i, a in enumerate(wires):
            for j in range(i + 1, len(wires)):
                b = wires[j]
                if any(_on(e, c, d) for e in (a[0], a[-1]) for c, d in zip(b, b[1:])) or \
                        any(_on(e, c, d) for e in (b[0], b[-1]) for c, d in zip(a, a[1:])):
                    parent[find(i)] = find(j)
        anchored = {find(i) for i, pts in enumerate(wires)
                    if any(_on(p, c, d) for p in anchors for c, d in zip(pts, pts[1:]))}
        return {pts for i, pts in enumerate(wires) if find(i) not in anchored}

    def check_clear(self, ref, pin, points, allow=(), joined_to=None):
        """New wiring from a pin through points must touch nothing already on the sheet.

        15 Nov: a GND port's wire landed on an old wire's corner and joined a
        signal to ground. Anything the path would touch (a wire, a wire end, a
        label, a no-connect, another pin) is refused, naming what and where.
        """
        own = self.symbol(ref)["pins"][str(pin)]
        # Wiring already on the target's net may be touched: it joins what is to be joined.
        same, ends = self._wired_to(joined_to) if joined_to is not None else ([], set())
        self._check_segments([(a, b, "%s.%s" % (ref, pin)) for a, b in zip(points, points[1:])],
                             {own} | {_r(p) for p in allow} | ends, same)

    def _check_segments(self, segments, allowed, same=(), strict=False, scene=None):
        """segments: (a, b, the pin it is for); refused at the first that touches what is there."""
        scene = self._scene() if scene is None else scene
        root, libs = scene["root"], scene["libs"]
        # Leftover wiring joined to nothing (no pin, label or port anywhere on it) is no hazard:
        # touching it joins nothing. remove_dangling clears it.
        if scene["orphans"] is None:
            scene["orphans"] = self._orphan_wires(root, libs)
        skip = set(same) | (set() if strict else scene["orphans"])
        wires = [pts for pts in scene["wires"] if tuple(pts) not in skip]
        things = [("a wire end", p) for pts in wires for p in (pts[0], pts[-1])]
        things += [("label %s" % item["name"], item["at"]) for item in scene["labels"]]
        things += [("a no-connect", point) for point in scene["no_connects"]]
        things += [("pin %s" % name, point) for name, point, _, _ in scene["pins"] if point not in allowed]
        if strict:
            things += [("a port or junction", point) for point in scene["ports_and_dots"]]
        pin_names = {}
        for name, point, _, _ in scene["pins"]:
            pin_names.setdefault(point, name)
        label_names = {item["at"]: item["name"] for item in scene["labels"]}

        def joined(point):
            # What that wiring already reaches, so the refusal says whose net it is.
            group, _ = self._wired_to(point)
            reached = sorted({name for q, name in list(pin_names.items()) + list(label_names.items())
                              if any(_on(q, c, d) for pts in group for c, d in zip(pts, pts[1:]))})
            return (" (wiring joined to %s)" % ", ".join(reached[:6])) if reached else ""
        bodies = scene["bodies"]
        # Where a pin not yet wired will come out, one grid step: nothing new may cross it
        # (16 Nov: a GND lead ran across pin 5's spot and the V2 label then had no way out).
        ends_now = {p for pts in scene["wires"] for p in (pts[0], pts[-1])} | {i["at"] for i in scene["labels"]}
        stubs = [(name, point, _r((point[0] + direction[0] * 2.54, point[1] + direction[1] * 2.54)))
                 for name, point, power, direction in scene["pins"]
                 if not power and direction is not None and point not in allowed and point not in ends_now]
        for a, b, who in segments:
            for name, p, q in stubs:
                # Running along that spot, or through where its wire will end, is in the way;
                # crossing it is not (16 Nov: leads refused for crossing a free pin's spot).
                along = (_on(a, p, q) and _on(b, p, q)) or (_on(p, a, b) and _on(q, a, b)) or \
                    ((_on(a, p, q) or _on(b, p, q)) and (a[0] == b[0] == p[0] == q[0] or a[1] == b[1] == p[1] == q[1]))
                if along or _on(q, a, b):
                    raise ValueError("%s: the new wire would run across where pin %s comes out (%g, %g); "
                                     "wire that pin first or route another way" % (who, name, p[0], p[1]))
            block = scene["title_block"]
            if block and max(a[0], b[0]) > block[0] and max(a[1], b[1]) > block[1]:
                raise ValueError("%s: the new wire would run into the title block (x > %g, y > %g); route "
                                 "another way" % (who, block[0], block[1]))
            for part, (x0, y0, x1, y1) in scene["parts"]:
                if (max(a[0], b[0]) > x0 + 0.2 and min(a[0], b[0]) < x1 - 0.2
                        and max(a[1], b[1]) > y0 + 0.2 and min(a[1], b[1]) < y1 - 0.2):
                    raise ValueError("%s: the new wire would run across %s; route around it (via)" % (who, part))
            for (ox, oy), (x0, y0, x1, y1) in bodies:
                if (ox, oy) in (a, b):
                    continue
                if max(a[0], b[0]) >= x0 and min(a[0], b[0]) <= x1 and max(a[1], b[1]) >= y0 and min(a[1], b[1]) <= y1:
                    raise ValueError("%s: the new wire would run over the port symbol at (%g, %g)%s; route another "
                                     "way (a port placed before a part was swapped can sit where the new part's "
                                     "pins come out: swap parts first)" % (who, ox, oy, joined((ox, oy))))
            for what, point in things:
                if point not in allowed and _on(point, a, b):
                    raise ValueError("%s: the new wire would touch %s at (%g, %g)%s; clear it (remove_labels, "
                                     "remove_items, remove_dangling), route to that wiring if it is the net "
                                     "you want, or join the pins another way"
                                     % (who, what, point[0], point[1], joined(point) if what == "a wire end" else ""))
            # Never along a wire already there, whatever its net: two wires on one line
            # (16 Nov, R18: the new R38 wire ran over the old one).
            for pts in scene["wires"] + scene.get("other_wiring", []):
                for c, d in zip(pts, pts[1:]):
                    if _overlap(a, b, c, d):
                        raise ValueError("%s: the new wire would run along an existing wire from (%g, %g) to "
                                         "(%g, %g)%s; those pins may already be joined" % (
                                             who, c[0], c[1], d[0], d[1], joined(c)))
            for end in (a, b):
                if end in allowed:
                    continue
                for pts in wires:
                    if any(_on(end, c, d) for c, d in zip(pts, pts[1:])):
                        raise ValueError("%s: the new wire would land on an existing wire at (%g, %g)%s; clear "
                                         "it (remove_items, remove_dangling) or join the pins another way"
                                         % (who, end[0], end[1], joined(end)))

    def pin_stub(self, ref, pin, length=2.54):
        """A wire straight out of the pin, in its own direction, one grid step by default; its end."""
        start, end = self._stub_end(ref, pin, length)
        self.check_clear(ref, pin, [start, end])
        self.add_wire(start, end)
        return end

    def wire_pin_to(self, ref, pin, target, length=2.54, via=None):
        """From a pin, straight out one step, then on to target with one turn. Returns the points.

        via: points to pass through on the way, to route around what is in the way; every
        piece then runs straight across or straight up and down.
        """
        start, end = self._stub_end(ref, pin, length)
        dx, dy = self.pin_direction(ref, pin)
        target = _r(target)
        # Already wired to each other: nothing is drawn (16 Nov, R18: R38 to J5.2 drawn a
        # second time over the wire that already joined them).
        group, _ = self._wired_to(target)
        if any(_on(start, a, b) for pts in group for a, b in zip(pts, pts[1:])):
            return []
        if via:
            points = [end] + [_r(p) for p in via] + [target]
            points = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
            for a, b in zip(points, points[1:]):
                if a[0] != b[0] and a[1] != b[1]:
                    raise ValueError("%s.%s: via (%g, %g) to (%g, %g) is neither straight across nor straight up "
                                     "and down" % (ref, pin, a[0], a[1], b[0], b[1]))
            try:
                self.check_clear(ref, pin, [start] + points, allow=(target,), joined_to=target)
            except ValueError:
                # A guessed via that is not clear: find the way round instead (19 Nov 2026).
                via = None
            else:
                self.add_wire(*_straight([start] + points))
                return [start] + points
        # Straight out of the pin to the target's line, then one turn into it: the wire leaves
        # the part's side at once (16 Nov: turning right after the stub ran the RFC wire up the
        # switch's side across where pins 1 and 2 come out). A target behind the pin turns
        # right after the stub, never back over the part.
        # A target already wired: straight out of the pin onto that wiring, a T with a
        # junction, is his usual way (16 Nov: the second 220 pF on the TX1 line).
        if not via and group:
            hits = []
            for pts in group:
                for a, b in zip(pts, pts[1:]):
                    if dx and a[0] == b[0] and min(a[1], b[1]) < end[1] < max(a[1], b[1]) \
                            and (a[0] - end[0]) * dx >= 0:
                        hits.append((abs(a[0] - end[0]), (a[0], end[1])))
                    if dy and a[1] == b[1] and min(a[0], b[0]) < end[0] < max(a[0], b[0]) \
                            and (a[1] - end[1]) * dy >= 0:
                        hits.append((abs(a[1] - end[1]), (end[0], a[1])))
            for _, tee in sorted(hits):
                tee = _r(tee)
                try:
                    self.check_clear(ref, pin, [start, end, tee] if tee != end else [start, end],
                                     allow=(tee,), joined_to=target)
                except ValueError:
                    continue
                self.add_wire(*_straight([start, end, tee] if tee != end else [start, end]))
                self.add_junction(tee)
                return [start, end, tee]
        # Into a part's pin the wire arrives the way that pin points, out of it first, never
        # turning right at the pin (16 Nov, Friedl: C37's net turned down at the capacitor pin).
        into = next((d for _, point, power, d in self._scene()["pins"]
                     if point == target and not power and d is not None), None)
        entry = _r((target[0] + into[0] * length, target[1] + into[1] * length)) if into else target
        ahead = (entry[0] - end[0]) * dx > 0 if dx else (entry[1] - end[1]) * dy > 0
        corners = [(entry[0], end[1]), (end[0], entry[1])]
        if not ((ahead and dx) or (not ahead and dy)):
            corners.reverse()
        tries = []
        for corner in corners:
            points = [end] + [p for p in (corner, entry, target) if p != end]
            points = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
            if points not in tries:
                tries.append(points)
        if into is not None and entry != target:
            # Where the target pin's own spot is taken, straight to it as before.
            tries += [[end] + [p for p in (c, target) if p != end] for c in
                      ((target[0], end[1]), (end[0], target[1]))]
        reason = None
        for points in tries:
            if _backtracks([start] + points):
                continue
            try:
                self.check_clear(ref, pin, [start] + points, allow=(target,), joined_to=target)
            except ValueError as error:
                reason = reason or error
                continue
            self.add_wire(*_straight([start] + points))
            return [start] + points
        # Neither turn is clear: find the way round (19 Nov 2026: twenty refused edits in one
        # job, each a guessed via point; a clear path has one right answer, so code finds it).
        # A target already wired is reached anywhere on its wiring, a T with a junction, as he
        # draws a second part onto a net; its own pin lead is taken by that wiring.
        tees = set()
        for pts in group:
            for a, b in zip(pts, pts[1:]):
                steps = int(round(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / 1.27))
                for k in range(1, steps):
                    tees.add(_r((a[0] + (b[0] - a[0]) * k / steps, a[1] + (b[1] - a[1]) * k / steps)))
        routed = self._route(ref, pin, end, [entry, target] if entry != target else [target],
                             target, heading=(dx, dy), tees=tees)
        if routed is not None:
            last = routed[-1]
            tee = last if last in tees else None
            points = [end] + routed + ([target] if tee is None and last != target else [])
            self.check_clear(ref, pin, [start] + points, allow=(target,) + ((tee,) if tee else ()),
                             joined_to=target)
            self.add_wire(*_straight([start] + points))
            if tee:
                self.add_junction(tee)
            return [start] + points
        raise ValueError("%s; and no clear way round within 30 mm either: move a part or supply "
                         "clear via points" % reason)

    def _route(self, ref, pin, begin, goals, target, margin=30.48, heading=None, tees=(),
               arrival_heading=None, channels=(), strict=False, enter_pin=True, leads=(),
               scene=None, area=None):
        """A clear grid path from begin to the first of goals that it reaches, or None.

        On the 1.27 mm grid, straight runs and as few turns as possible, kept within
        margin of the two ends. Every step obeys the same rules as _check_segments
        (no part or port body, no wire end, label, no-connect or other pin, never along a
        wire, never across where an unwired pin comes out, never into the title block, and
        no turn on top of another wire), and the caller checks the whole path again.
        """
        grid = 1.27

        def on_grid(p):
            return all(abs(round(v / grid) * grid - v) < 0.006 for v in p)
        goal = next((g for g in goals if on_grid(g)), None)
        if goal is None or not on_grid(begin):
            return None
        arrive = ({_r(goal)} if enter_pin else set()) | {t for t in tees if on_grid(t)}
        if not arrive:
            return None
        scene = self._scene() if scene is None else scene
        if scene["orphans"] is None:
            scene["orphans"] = self._orphan_wires(scene["root"], scene["libs"])
        own = self.symbol(ref)["pins"][str(pin)] if ref is not None else begin
        same, ends = ((), set()) if strict else self._wired_to(target)
        allowed = {own, begin, _r(target)} | arrive | ends
        skip = set() if strict else set(same) | scene["orphans"]
        bounds = [begin, *arrive]
        x0, x1 = min(p[0] for p in bounds) - margin, max(p[0] for p in bounds) + margin
        y0, y1 = min(p[1] for p in bounds) - margin, max(p[1] for p in bounds) + margin
        if area is not None:
            x0, y0, x1, y1 = max(x0, area[0]), max(y0, area[1]), min(x1, area[2]), min(y1, area[3])

        def near(*pts):
            return any(x0 - 3 <= p[0] <= x1 + 3 and y0 - 3 <= p[1] <= y1 + 3 for p in pts)
        segments = [(c, d) for pts in scene["wires"] + scene.get("other_wiring", []) for c, d in zip(pts, pts[1:])
                    if _hits_box(c, d, (x0, y0, x1, y1))]
        live = [(c, d) for pts in scene["wires"] if tuple(pts) not in skip
                for c, d in zip(pts, pts[1:]) if _hits_box(c, d, (x0, y0, x1, y1))]
        wired_ends = {p for pts in scene["wires"] if tuple(pts) not in skip for p in (pts[0], pts[-1])}
        if strict:
            wired_ends.update(scene["ports_and_dots"])
        points = {p for p in wired_ends | {i["at"] for i in scene["labels"]} | set(scene["no_connects"])
                  | {point for _, point, _, _ in scene["pins"]} if p not in allowed and near(p)}
        all_ends = {p for pts in scene["wires"] for p in (pts[0], pts[-1])} | {i["at"] for i in scene["labels"]}
        stubs = [(point, _r((point[0] + d[0] * 2.54, point[1] + d[1] * 2.54)))
                 for _, point, power, d in scene["pins"]
                 if not power and d is not None and point not in allowed and point not in all_ends and near(point)]
        boxes = [b for _, b in scene["parts"] if b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0]
        bodies = scene["bodies"]
        block = scene["title_block"]
        cache = {}

        def step_ok(a, b):
            key = (a, b) if a <= b else (b, a)
            if key in cache:
                return cache[key]
            ok = True
            if block and max(a[0], b[0]) > block[0] and max(a[1], b[1]) > block[1]:
                ok = False
            for bx0, by0, bx1, by1 in boxes if ok else ():
                if (max(a[0], b[0]) > bx0 + 0.2 and min(a[0], b[0]) < bx1 - 0.2
                        and max(a[1], b[1]) > by0 + 0.2 and min(a[1], b[1]) < by1 - 0.2):
                    ok = False
                    break
            for origin, (bx0, by0, bx1, by1) in bodies if ok else ():
                if origin not in (a, b) and max(a[0], b[0]) >= bx0 and min(a[0], b[0]) <= bx1 \
                        and max(a[1], b[1]) >= by0 and min(a[1], b[1]) <= by1:
                    ok = False
                    break
            if ok and any(_on(p, a, b) for p in points):
                ok = False
            for p, q in stubs if ok else ():
                along = (_on(a, p, q) and _on(b, p, q)) or (_on(p, a, b) and _on(q, a, b)) or \
                    ((_on(a, p, q) or _on(b, p, q)) and (a[0] == b[0] == p[0] == q[0] or a[1] == b[1] == p[1] == q[1]))
                if along or _on(q, a, b):
                    ok = False
                    break
            if ok and any(_overlap(a, b, c, d) for c, d in segments):
                ok = False
            if ok and any(_overlap(a, b, c, d) or _on(c, a, b) for c, d in leads):
                ok = False
            cache[key] = ok
            return ok

        def on_live_wire(p):
            return (strict or p not in allowed) and any(_on(p, c, d) for c, d in live)
        import heapq
        start = _r(begin)
        moves = ((grid, 0.0), (-grid, 0.0), (0.0, grid), (0.0, -grid))
        # Leaving the pin's stub the way the pin points: never back over the stub itself.
        first = (heading[0] * grid, heading[1] * grid) if heading else None
        frontier = [(0.0, 0, start, first)]
        best = {(start, first): 0.0}
        came = {}
        counter = 0
        while frontier:
            _, _, here, heading = heapq.heappop(frontier)
            if here in arrive and (here != goal or arrival_heading is None
                                   or heading != arrival_heading):
                path, state = [here], (here, heading)
                while state in came:
                    state = came[state]
                    path.append(state[0])
                return _straight(path[::-1])[1:]
            cost = best[(here, heading)]
            for move in moves:
                if heading is not None and move == (-heading[0], -heading[1]):
                    continue
                nxt = _r((here[0] + move[0], here[1] + move[1]))
                if not (x0 <= nxt[0] <= x1 and y0 <= nxt[1] <= y1) or not step_ok(here, nxt):
                    continue
                turning = heading is not None and move != heading
                if turning and on_live_wire(here):
                    continue
                # Prefer the open channels between declared slot lines. Turns
                # and longer routes still cost more, and free space stays usable.
                channel_cost = 0.0
                if channels:
                    axis = 0 if move[1] else 1
                    channel_cost = 0.35 if here[axis] in channels[axis] else 0.0
                crossing_cost = 0.15 if strict and nxt not in arrive and on_live_wire(nxt) else 0.0
                total = cost + 1.0 + channel_cost + crossing_cost + (6.0 if turning else 0.0)
                if total < best.get((nxt, move), float("inf")):
                    best[(nxt, move)] = total
                    came[(nxt, move)] = (here, heading)
                    counter += 1
                    guess = min(abs(nxt[0] - p[0]) + abs(nxt[1] - p[1]) for p in arrive) / grid
                    heapq.heappush(frontier, (total + guess, counter, nxt, move))
        return None

    def label_pin(self, ref, pin, name, kind="label", length=2.54):
        """A net label at the end of a short wire straight out of the pin; on a pin already
        wired, on the pin itself (16 Nov: the stub landed on the pin's own wire)."""
        dx, dy = self.pin_direction(ref, pin)
        point = self.symbol(ref)["pins"][str(pin)]
        if any(point in (pts[0], pts[-1]) for pts in self.wires()):
            angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
            self.add_label(name, point, angle, kind)
            return point
        end = self.pin_stub(ref, pin, length)
        angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
        self.add_label(name, end, angle, kind)
        return end

    def _direction_of(self, symbol, lib, number):
        """A pin's outward direction from its parsed symbol (pin_direction without re-reading)."""
        angles = {}
        for part in _lib_drawings(symbol, lib):
            angles.update(_lib_pin_angles(part))
        if str(number) not in angles:
            return None
        turn = _math.radians(angles[str(number)] + 180.0)
        at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
        end = _placed(at, mirror[1] if mirror else None, _math.cos(turn), _math.sin(turn))
        origin = _placed(at, mirror[1] if mirror else None, 0.0, 0.0)
        return (round(end[0] - origin[0]), round(end[1] - origin[1]))

    def _scene(self):
        """What is on the sheet, read once for each state of its text (16 Nov: the checks
        re-read the whole sheet for every pin and an edit ran out of time)."""
        cached = getattr(self, "_scene_cache", None)
        if cached is not None and cached[0] is self.text:
            return cached[1]
        root, _ = self._tree()
        libs = self._libs(root)
        pins, bodies, ports, parts, values = [], [], [], [], {}
        for symbol in _children(root, "symbol"):
            name = self._ref(symbol)
            lib = libs.get(_unq(_child(symbol, "lib_id")[1]))
            power = name.startswith("#")
            value = next((_unq(p[2]) for p in _children(symbol, "property") if _unq(p[1]) == "Value"), "")
            for number, point in self._symbol_pins(symbol, libs).items():
                direction = None if power or lib is None else self._direction_of(symbol, lib, number)
                pins.append(("%s.%s" % (name, number), point, power, direction))
                if power:
                    values[point] = value
            if not power and lib is not None:
                # The part's drawn body (16 Nov: the VCTL wire was drawn straight over C56).
                at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
                drawn = [_placed(at, mirror[1] if mirror else None, x, y)
                         for part in _lib_drawings(symbol, lib) for x, y in _lib_outline(part)]
                if drawn:
                    xs, ys = [p[0] for p in drawn], [p[1] for p in drawn]
                    parts.append((name, (min(xs), min(ys), max(xs), max(ys))))
            if power:
                ox, oy = _r(_child(symbol, "at")[1:3])
                ports.append((ox, oy))
                bodies.append(((ox, oy), (ox - 1.5, oy + 0.3, ox + 1.5, oy + 5.6) if "GND" in value.upper()
                               else (ox - 1.5, oy - 5.6, ox + 1.5, oy - 0.3)))
        ports_and_dots = {_r(_child(pin, "at")[1:3]) for item in _children(root, "sheet")
                          for pin in _children(item, "pin")}
        ports_and_dots.update(_r(_child(dot, "at")[1:3]) for dot in _children(root, "junction"))
        scene = {"root": root, "libs": libs, "wires": self.wires(), "labels": self.labels(),
                 "no_connects": [_r(_child(n, "at")[1:3]) for n in _children(root, "no_connect")],
                 "pins": pins, "bodies": bodies, "ports": ports, "orphans": None, "parts": parts,
                 "values": values, "title_block": self._title_block(root), "ports_and_dots": ports_and_dots}
        self._scene_cache = (self.text, scene)
        return scene

    def _body_clear(self, port, down, reach=5.6):
        """Where a port's symbol is drawn (below a GND point, above a power one), nothing of
        the sheet may be: 16 Nov, Friedl: a GND symbol drawn on a net is not acceptable.
        reach: how far its drawing and name go from its point (from his own symbol)."""
        # At least as far as the later checks count a port symbol (16 Nov: one switch's GND was
        # placed where the next switch's GND pins come out, then refused their leads).
        reach = max(reach, 5.6)
        x0, x1 = port[0] - 1.6, port[0] + 1.6
        y0, y1 = (port[1] + 0.3, port[1] + reach) if down else (port[1] - reach, port[1] - 0.3)
        scene = self._scene()
        # Nor where another part's pins will be wired: one grid step out of each pin.
        for _, (px, py), power, direction in scene["pins"]:
            if power or direction is None:
                continue
            qx, qy = px + direction[0] * 2.54, py + direction[1] * 2.54
            if max(px, qx) >= x0 and min(px, qx) <= x1 and max(py, qy) >= y0 and min(py, qy) <= y1:
                return False
        for pts in scene["wires"]:
            for (ax, ay), (bx, by) in zip(pts, pts[1:]):
                if max(ax, bx) >= x0 and min(ax, bx) <= x1 and max(ay, by) >= y0 and min(ay, by) <= y1:
                    return False
        if any(max(x0, a0) < min(x1, a1) and max(y0, b0) < min(y1, b1)
               for _, (a0, b0, a1, b1) in scene["parts"]):
            return False
        block = scene["title_block"]
        if block and x1 > block[0] and y1 > block[1]:
            return False
        points = [item["at"] for item in scene["labels"]] + [point for _, point, _, _ in scene["pins"]]
        points += scene["ports"]
        return not any(x0 <= px <= x1 and y0 <= py <= y1 for px, py in points)

    def port_pins(self, pins, template, down):
        """One port for each row of neighbouring pins: each pin straight out one step, the
        row's ends joined by one wire, and a single port (GND down, power up) on it.

        15 Nov: a port per pin put the ports of neighbouring NC pins on top of one another.
        16 Nov, Friedl's drawing: where the row is boxed in (NC pins between two RF pins),
        the wire goes on down, crossing the RF wire once, to a port clear of everything.
        pins: [(ref, number), ...]. Returns the port points.
        """
        want = 1 if down else -1
        # How far his symbol's name sits from its point (an imported GND's is 6.35 mm below).
        reach = 3.0
        # Wiring already on this port's net may be touched: it is the net the leads join
        # (16 Nov: GND and 3V3 leads refused for touching GND and 3V3 wiring).
        value = next((item["value"] for item in self.symbols()
                      if item["uuid"] == template or item["ref"] == template), None)
        same, same_ends = [], set()
        for point, port_value in self._scene()["values"].items():
            if value is not None and port_value == value:
                group, ends = self._wired_to(point)
                same += list(group)
                same_ends |= set(ends) | {point}
        for item in self.symbols():
            if item["uuid"] == template or item["ref"] == template:
                tree, _ = self._tree()
                for node in _children(tree, "symbol"):
                    if _child(node, "uuid") and _unq(_child(node, "uuid")[1]) == item["uuid"]:
                        value = [p for p in _children(node, "property") if _unq(p[1]) == "Value"]
                        if value and _child(value[0], "at"):
                            reach = max(reach, abs(float(_child(value[0], "at")[2]) - item["at"][1]) + 0.9)
                break
        rows = {}
        for ref, number in pins:
            start, end = self._stub_end(ref, number)
            direction = self.pin_direction(ref, number)
            key = (ref, direction, end[0] if direction[0] else end[1])
            rows.setdefault(key, []).append((start, end, "%s.%s" % (ref, number)))
        # A row is pins one grid step apart: a gap (another pin between) starts a new row.
        split = []
        for key, row in sorted(rows.items()):
            along = 1 if key[1][0] else 0
            row.sort(key=lambda item: item[1][along])
            current = [row[0]]
            for item in row[1:]:
                if item[1][along] - current[-1][1][along] > 2.54 + 1e-6:
                    split.append((key, current))
                    current = []
                current.append(item)
            split.append((key, current))
        ports, segments, allowed = [], [], set()
        for (ref, (dx, dy), _), row in split:
            along = 1 if dx else 0
            row.sort(key=lambda item: item[1][along])
            ends = [item[1] for item in row]
            own = [(item[0], item[1], item[2]) for item in row]
            own += [(a[1], b[1], "%s to %s" % (a[2], b[2])) for a, b in zip(row, row[1:])]
            # Only the pins themselves may already be wired; every new point is checked.
            row_allowed = {item[0] for item in row}
            if (dx, dy) == (0, -want):
                # Pins pointing away from where the port goes (GND pins on top): the row
                # runs out past the nearer side of the part, clear of its side pins, and on
                # into the port the way it points (16 Nov: never into its pin sideways).
                xs = [point[0] for point in self.symbol(ref)["pins"].values()]
                start, who = (ends[0], row[0][2]) if ends[0][0] - min(xs) <= max(xs) - ends[-1][0] \
                    else (ends[-1], row[-1][2])
                step, first = (0, want * 2.54), None
            elif dx:
                # Pins on a side: the row runs up and down; the port goes below (or above) it.
                start = ends[-1] if down else ends[0]
                who = (row[-1] if down else row[0])[2]
                step, first = (0, want * 2.54), _r((start[0], start[1] + want * 2.54))
            elif len(ends) == 1:
                start, who, step, first = ends[0], row[0][2], (0, want * 2.54), ends[0]
            else:
                start = ends[len(ends) // 2]
                who = row[len(ends) // 2][2]
                step, first = (0, want * 2.54), _r((start[0], start[1] + want * 2.54))
            # The first place, going on in the same way, where the lead touches nothing and
            # the symbol sits on nothing; crossing a wire on the way is allowed. Failing that,
            # the row runs out past the side of the part and the port hangs there (16 Nov: a
            # GND under one switch sat on the next switch's wiring).
            xs = [point[0] for point in self.symbol(ref)["pins"].values()]
            # Each try: the lead's points from the row to the port.
            tries = [] if first is None else [
                ([start, _r((first[0] + step[0] * k, first[1] + step[1] * k))], who) for k in range(8)]
            for lead_start, lead_who, edge in ((ends[-1], row[-1][2], max(xs) + 5.08),
                                               (ends[0], row[0][2], min(xs) - 5.08)):
                if not dx:
                    sign = 1 if edge > lead_start[0] else -1
                    for k in range(4):
                        # At least one step down (or up) into the port, the way its pin points.
                        for drop in range(1, 5):
                            # Out past the side of the part, then on down, crossing a wire
                            # once if it must (16 Nov: boxed in by the agent's RF wire).
                            corner = _r((edge + sign * 2.54 * k, lead_start[1]))
                            end = _r((corner[0], corner[1] + want * 2.54 * drop))
                            tries.append(([lead_start, corner] + ([end] if drop else []), lead_who))
            port, reason = None, None
            for points, lead_who in tries:
                candidate, lead_start = points[-1], points[0]
                lead = [(a, b, "%s to its port" % lead_who) for a, b in zip(points, points[1:]) if a != b]
                try:
                    self._check_segments(own + lead, row_allowed | same_ends, same)
                except ValueError as error:
                    reason = error
                    continue
                if self._body_clear(candidate, down, reach):
                    port = candidate
                    segments += own + lead
                    allowed |= row_allowed
                    break
                reason = ValueError("%s: no clear place for its %s port nearby"
                                    % (who, "GND" if down else "power"))
            if port is None:
                raise reason
            ports.append(port)
        source, library, _ = self._placed_symbol(template)
        old = self.text
        try:
            counted = {}
            for a, b, _ in segments:
                self.add_wire(a, b)
                for point in (a, b):
                    counted[point] = counted.get(point, 0) + 1
            for point, count in counted.items():
                if count >= 3:
                    self.add_junction(point)
            for port in ports:
                if library and _child(library, "power"):
                    self.place_power(_unq(_child(source, "lib_id")[1]), port)
                else:
                    self.copy_symbol(template, port, None)
        except Exception:
            self.text = old
            raise
        return ports

    def ground_pin(self, ref, pin, template):
        """A GND port, pointing down, on a short wire from the pin. template: the reference (#PWR..) of a GND symbol already on this sheet, which is copied."""
        return self.port_pins([(ref, pin)], template, down=True)[0]

    def power_pin(self, ref, pin, template):
        """A power port, pointing up, on a short wire from the pin. template: that port's reference."""
        return self.port_pins([(ref, pin)], template, down=False)[0]

    def place_power(self, net, at):
        """Place a power symbol with its connection pin at (x, y). Return its new reference.

        Pass the net name or a specific LIBRARY:SYMBOL. A definition must be cached
        in this sheet or supplied through edit's symbols argument.
        """
        return _place_power(self, net, at)

    def upright(self, ref):
        """Correct an upside-down symbol. If pins need manual rewiring, return
        their old and proposed positions and leave the drawing intact.
        """
        return _upright(self, ref)

    def tidy_fields(self, ref):
        """Reference and value beside the part, the same way for every part, clear of its body.

        Upright parts get them to the right, reference above value; parts lying
        down get them above and below, centred.
        """
        symbol, lib, spans = self._placed_symbol(ref)
        at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
        outline = []
        for part in _children(lib, "symbol"):
            outline += _lib_outline(part) + list(_lib_pins(part).values())
        placed = [_placed(at, mirror[1] if mirror else None, x, y) for x, y in outline] or [_r(at[1:3])]
        left, right = min(p[0] for p in placed), max(p[0] for p in placed)
        top, bottom = min(p[1] for p in placed), max(p[1] for p in placed)
        cx, cy = (left + right) / 2, (top + bottom) / 2
        props = {_unq(p[1]): _unq(p[2]) for p in _children(symbol, "property")}
        width = lambda name: 0.9 * 1.27 * len(props.get(name, "")) + 0.5  # noqa: E731
        # Centred text at a worked-out spot: KiCad flips left/right justification on parts
        # turned 180 degrees, which put text over the part (16 Nov).
        side = lambda name, x, sign: x + sign * (1.27 + width(name) / 2)  # noqa: E731
        right_side = {"Reference": (side("Reference", right, 1), cy - 1.27, ""),
                      "Value": (side("Value", right, 1), cy + 1.27, "")}
        above_below = {"Reference": (cx, top - 1.27, ""), "Value": (cx, bottom + 2.54, "")}
        left_side = {"Reference": (side("Reference", left, -1), cy - 1.27, ""),
                     "Value": (side("Value", left, -1), cy + 1.27, "")}
        upright = bottom - top >= right - left
        corners = {"Reference": (side("Reference", right, 1), top - 1.27, ""),
                   "Value": (side("Value", right, 1), bottom + 2.54, "")}
        stacks = [{"Reference": (sx, y - 2.54, ""), "Value": (sx, y, "")}
                  for y in (top - 1.27,) for sx in (side("Value", right, 1), side("Value", left, -1))]
        stacks += [{"Reference": (sx, y, ""), "Value": (sx, y + 2.54, "")}
                   for y in (bottom + 2.54,) for sx in (side("Value", right, 1), side("Value", left, -1))]
        # A part lying down next to another (C44 over C45, 16 Nov): both lines above it, or
        # both below, centred, before any side.
        stacked_above = {"Reference": (cx, top - 3.81, ""), "Value": (cx, top - 1.27, "")}
        stacked_below = {"Reference": (cx, bottom + 2.54, ""), "Value": (cx, bottom + 5.08, "")}
        options = ([right_side, corners, above_below, left_side] if upright
                   else [above_below, stacked_above, stacked_below, right_side, corners, left_side]) + stacks
        # And a ring of nearby spots, reference over value, for crowded places.
        wide = max(width("Reference"), width("Value"))
        for i in range(4):
            for sx in (right + 1.27 + wide / 2 + 2.54 * i, left - 1.27 - wide / 2 - 2.54 * i):
                for k in range(-3, int((bottom - top) / 1.27) + 4):
                    y = top + 1.27 * k
                    options.append({"Reference": (sx, y - 1.27, ""), "Value": (sx, y + 1.27, "")})
        for k in range(1, 4):
            options.append({"Reference": (cx, top - 1.27 - 2.54 * k, ""), "Value": (cx, top - 1.27 * k, "")})
            options.append({"Reference": (cx, bottom + 2.54 * k, ""), "Value": (cx, bottom + 2.54 * k + 2.54, "")})
        # The place where the text lands on the least: wiring, ports, labels, other parts'
        # pins (16 Nov: sideways GND ports sat on a switch's reference and value).
        root_now, _ = self._tree()
        libs_now = self._libs(root_now)
        own = set(self._symbol_pins(symbol, libs_now).values())
        segments = [(a, b) for pts in self.wires() for a, b in zip(pts, pts[1:])]
        boxes = []
        for item in self.labels():
            (lx, ly), w = item["at"], 1.27 * len(item["name"]) + 2.5
            angle = 0
            for node in root_now:
                if _head(node) in ("label", "global_label", "hierarchical_label") and \
                        _r(_child(node, "at")[1:3]) == item["at"]:
                    at_node = _child(node, "at")
                    angle = float(at_node[3]) if len(at_node) > 3 else 0
            if angle == 180:
                boxes.append((lx - w, ly - 1.2, lx, ly + 1.2))
            elif angle == 90:
                boxes.append((lx - 1.2, ly - w, lx + 1.2, ly))
            elif angle == 270:
                boxes.append((lx - 1.2, ly, lx + 1.2, ly + w))
            else:
                boxes.append((lx, ly - 1.2, lx + w, ly + 1.2))
        # Other parts' bodies (16 Nov: C45's reference sat on C44, 5 mm above it).
        boxes += [body for name, body in self._scene()["parts"] if name != ref]
        mine = _unq(_child(symbol, "uuid")[1]) if _child(symbol, "uuid") else None
        for other in _children(root_now, "symbol"):
            # Its own text and pins are not in its way (a fresh parse: compared by uuid).
            if (mine and _child(other, "uuid") and _unq(_child(other, "uuid")[1]) == mine) or \
                    (not mine and self._ref(other) == ref):
                continue
            for px, py in self._symbol_pins(other, libs_now).values():
                if (px, py) not in own:
                    boxes.append((px - 0.3, py - 0.3, px + 0.3, py + 0.3))
            if self._ref(other).startswith("#"):
                ox, oy = _r(_child(other, "at")[1:3])
                boxes.append((ox - 2.2, oy - 2.2, ox + 2.2, oy + 2.2))
                # And its name where it is drawn (an imported GND's is 6.35 mm below it).
                for prop in _children(other, "property"):
                    pat = _child(prop, "at")
                    if _unq(prop[1]) == "Value" and pat is not None and "hide yes" not in str(prop):
                        vx, vy = float(pat[1]), float(pat[2])
                        half = (0.9 * 1.27 * len(_unq(prop[2])) + 0.5) / 2
                        boxes.append((vx - half, vy - 1.0, vx + half, vy + 1.0))
                continue
            # Other parts' visible reference and value text (16 Nov: C44 and C45 overlapped).
            turned_other = (float(_child(other, "at")[3]) if len(_child(other, "at")) > 3 else 0.0) % 180
            for prop in _children(other, "property"):
                if _unq(prop[1]) not in ("Reference", "Value") or "hide yes" in str(prop):
                    continue
                pat = _child(prop, "at")
                if pat is None:
                    continue
                px, py = float(pat[1]), float(pat[2])
                pw = 0.9 * 1.27 * len(_unq(prop[2])) + 0.5
                angle = ((float(pat[3]) if len(pat) > 3 else 0.0) + turned_other) % 180
                text = str(prop)
                if angle == 90:
                    boxes.append((px - 1.0, py - pw / 2, px + 1.0, py + pw / 2))
                elif "justify left" in text:
                    boxes.append((px, py - 1.0, px + pw, py + 1.0))
                elif "justify right" in text:
                    boxes.append((px - pw, py - 1.0, px, py + 1.0))
                else:
                    boxes.append((px - pw / 2, py - 1.0, px + pw / 2, py + 1.0))

        def box(name, x, y, justify):
            w = width(name)
            x0 = x if justify == "left" else x - w if justify == "right" else x - w / 2
            return (x0 - 0.3, y - 1.0, x0 + w + 0.3, y + 1.0)

        def hits(spots):
            count = 0
            for name, (x, y, justify) in spots.items():
                if name not in props:
                    continue
                x0, y0, x1, y1 = box(name, x, y, justify)
                count += sum(1 for bx0, by0, bx1, by1 in boxes if bx0 <= x1 and bx1 >= x0 and by0 <= y1 and by1 >= y0)
                # A wire needs air beside the text, not only no overlap (16 Nov: C44's text
                # sat on either side of its own wire, a quarter of a millimetre off it).
                count += sum(1 for (ax, ay), (bx, by) in segments
                             if max(ax, bx) >= x0 - 0.5 and min(ax, bx) <= x1 + 0.5
                             and max(ay, by) >= y0 - 0.5 and min(ay, by) <= y1 + 0.5)
            return count
        # Fewest things touched; among equals, the order above, then the nearest.
        scored = [(hits(option), index if index < 9 else 9 + abs(option["Value"][0] - cx) + abs(option["Value"][1] - cy),
                   index) for index, option in enumerate(options)]
        spots = options[min(scored)[2]]
        symbol, lib, spans = self._placed_symbol(ref)
        start, end = spans[id(symbol)]
        block = self.text[start:end]
        for name, (x, y, justify) in spots.items():
            match = _re.search(r'\(property "%s" ' % name, block)
            if not match:
                continue
            depth, index = 0, match.start()
            for index in range(match.start(), len(block)):
                depth += {"(": 1, ")": -1}.get(block[index], 0)
                if depth == 0:
                    break
            prop = block[match.start():index + 1]
            # KiCad turns a field with its part: on a part turned 90 or 270 the field's own
            # angle is 90 for the text to read across (16 Nov: it ran down the wires).
            turned = (float(at[3]) if len(at) > 3 else 0.0) % 180
            prop = _re.sub(r'\(at [-\d.]+ [-\d.]+(?: [-\d.]+)?\)',
                           "(at %g %g %g)" % (round(x, 2), round(y, 2), turned), prop, count=1)
            prop = _re.sub(r'\s*\(justify [^)]*\)', "", prop)
            if justify and "(effects" in prop:
                prop = prop.replace("(effects", "(effects (justify %s)" % justify, 1)
            block = block[:match.start()] + prop + block[index + 1:]
        self.text = self.text[:start] + block + self.text[end:]

    def symbol(self, ref):
        """One symbol by reference, as symbols() gives it."""
        for item in self.symbols():
            if item["ref"] == ref:
                return item
        raise self._missing(ref)

    def wires(self):
        """Every wire as its list of points (x, y)."""
        root, _ = self._tree()
        return [[_r((p[1], p[2])) for p in _children(_child(w, "pts"), "xy")] for w in _children(root, "wire")]

    def labels(self):
        """Net labels of every kind: name, kind, at."""
        root, _ = self._tree()
        return [{"name": _unq(item[1]), "kind": _head(item), "at": _r(_child(item, "at")[1:3])}
                for item in root if _head(item) in ("label", "global_label", "hierarchical_label")]

    # -- changing --
    def _cut(self, spans_to_cut):
        for start, end in sorted(spans_to_cut, reverse=True):
            line = self.text.rfind("\n", 0, start)
            self.text = self.text[:line if line >= 0 and not self.text[line:start].strip() else start] + self.text[end:]

    def _append(self, block):
        end = self.text.rfind("\n\t(sheet_instances")
        if end < 0:
            end = self.text.rstrip().rfind(")")
        self.text = self.text[:end] + "\n" + block + self.text[end:]

    def remove_symbol(self, ref):
        """Take a symbol off the sheet, with its no-connect marks and the wiring only it used.

        Returns the pin points it left behind.
        """
        root, spans = self._tree()
        libs = self._libs(root)
        found = [s for s in _children(root, "symbol") if self._ref(s) == ref]
        if not found:
            raise self._missing(ref)
        points = set()
        for symbol in found:
            points |= set(self._symbol_pins(symbol, libs).values())
        marks = [n for n in _children(root, "no_connect") if _r(_child(n, "at")[1:3]) in points]
        self._cut([spans[id(item)] for item in found + marks])
        self.tidy(points)
        return sorted(points)

    def tidy(self, points):
        """Remove wires, labels and junctions left hanging from these points, and nothing else."""
        points = {_r(p) for p in points}
        while True:
            root, spans = self._tree()
            libs = self._libs(root)
            pins = {p for s in _children(root, "symbol") for p in self._symbol_pins(s, libs).values()}
            wires = [(w, [_r((p[1], p[2])) for p in _children(_child(w, "pts"), "xy")])
                     for w in _children(root, "wire")]
            held = {_r(_child(n, "at")[1:3]) for n in _children(root, "no_connect")}
            held |= {_r(_child(n, "at")[1:3]) for n in root
                     if _head(n) in ("label", "global_label", "hierarchical_label")}
            for entry in _children(root, "bus_entry"):
                at, size = _child(entry, "at"), _child(entry, "size")
                held |= {_r(at[1:3]), _r((float(at[1]) + float(size[1]), float(at[2]) + float(size[2])))}

            def joined(point, me):
                if point in pins or point in held:
                    return True
                return any(other is not me and any(_on(point, a, b) for a, b in zip(pts, pts[1:]))
                           for other, pts in wires)
            loose = [(w, pts) for w, pts in wires
                     if any(end in points and not joined(end, w) for end in (pts[0], pts[-1]))]
            if not loose:
                break
            for _, pts in loose:
                points |= set(pts)
            self._cut([spans[id(w)] for w, _ in loose])
        root, spans = self._tree()
        libs = self._libs(root)
        pins = {p for s in _children(root, "symbol") for p in self._symbol_pins(s, libs).values()}
        wires = [[_r((p[1], p[2])) for p in _children(_child(w, "pts"), "xy")] for w in _children(root, "wire")]
        on_wire = lambda p: any(_on(p, a, b) for pts in wires for a, b in zip(pts, pts[1:]))  # noqa: E731
        dead = [n for n in root if _head(n) in ("label", "global_label", "hierarchical_label")
                and _r(_child(n, "at")[1:3]) in points and not on_wire(_r(_child(n, "at")[1:3]))
                and _r(_child(n, "at")[1:3]) not in pins]
        for junction in _children(root, "junction"):
            p = _r(_child(junction, "at")[1:3])
            if p not in points:
                continue
            ends = sum(pts.count(p) for pts in wires)
            middles = sum(1 for pts in wires for a, b in zip(pts, pts[1:]) if _on(p, a, b) and p not in (a, b))
            if ends + 2 * middles + (1 if p in pins else 0) < 3:
                dead.append(junction)
        self._cut([spans[id(item)] for item in dead])

    @staticmethod
    def _inside(point, area):
        return area is None or (min(area[0], area[2]) - 1e-6 <= point[0] <= max(area[0], area[2]) + 1e-6
                                and min(area[1], area[3]) - 1e-6 <= point[1] <= max(area[1], area[3]) + 1e-6)

    def unwire(self, ref, pin):
        """Take off whatever is wired to one pin (wires, labels, no-connects, ports) and the
        wiring that then hangs loose; wiring that goes on to other pins stays.
        16 Nov: a swapped part's new pins sat on the old part's wiring. Returns counts."""
        point = self.symbol(ref)["pins"][str(pin)]
        root, spans = self._tree()
        libs = self._libs(root)
        cut, others, counts = [], set(), {}
        for node in _children(root, "wire"):
            pts = [_r((p[1], p[2])) for p in _children(_child(node, "pts"), "xy")]
            if point in (pts[0], pts[-1]):
                cut.append(node)
                others |= set(pts) - {point}
                counts["wire"] = counts.get("wire", 0) + 1
            elif any(_on(point, a, b) for a, b in zip(pts, pts[1:])):
                raise ValueError("%s.%s: a wire runs through the pin at (%g, %g); remove_items there first"
                                 % (ref, pin, point[0], point[1]))
        for node in root:
            head = _head(node)
            if head in ("label", "global_label", "hierarchical_label", "no_connect") and \
                    _r(_child(node, "at")[1:3]) == point:
                cut.append(node)
                counts[head] = counts.get(head, 0) + 1
            elif head == "symbol" and self._ref(node).startswith("#") and \
                    point in self._symbol_pins(node, libs).values():
                cut.append(node)
                counts["power"] = counts.get("power", 0) + 1
        self._cut([spans[id(n)] for n in cut])
        self.tidy(others)
        return counts

    def tidy_labels(self):
        """Every plain net label sits on its wire, not centred across it: an import's
        "(justify left)" gets "bottom" (16 Nov, Friedl: middle-aligned labels on the net).
        Global and sheet labels keep their own shape. Returns how many changed."""
        changed = 0

        def fix(match):
            nonlocal changed
            block = match.group(0)
            justify = _re.search(r'\(justify ([a-z ]*)\)', block)
            if justify is None:
                at = _re.search(r'\(at [-\d.]+ [-\d.]+ ([-\d.]+)\)', block)
                side = "right" if at and float(at.group(1)) in (180.0, 270.0) else "left"
                block = block.replace("(effects", "(effects (justify %s bottom)" % side, 1)
                changed += 1
            elif "bottom" not in justify.group(1).split() and "top" not in justify.group(1).split():
                block = block[:justify.start()] + "(justify %s bottom)" % justify.group(1).strip() + block[justify.end():]
                changed += 1
            return block
        self.text = _re.sub(r'\n\t\(label "(?:[^"\\]|\\.)*".*?\n\t\)', fix, self.text, flags=_re.S)
        return changed

    def remove_labels(self, names, area=None):
        """Remove net labels with these names (inside area [x1, y1, x2, y2] when given), and
        the wire stubs that only held them. Returns how many labels went."""
        root, spans = self._tree()
        names = set(names)
        found = [n for n in root if _head(n) in ("label", "global_label", "hierarchical_label")
                 and _unq(n[1]) in names and self._inside(_r(_child(n, "at")[1:3]), area)]
        points = {_r(_child(n, "at")[1:3]) for n in found}
        self._cut([spans[id(n)] for n in found])
        self.tidy(points)
        return len(found)

    def remove_items(self, area, kinds=("wire", "label", "no_connect", "junction", "power")):
        """Remove what lies wholly inside area [x1, y1, x2, y2]: wires, labels, no-connects,
        junctions and power symbols (#PWR, found by place, so unannotated ones too).
        Parts are never removed this way (remove_symbols). Returns counts by kind."""
        root, spans = self._tree()
        libs = self._libs(root)
        cut, points, counts = [], set(), {}
        for node in root:
            head = _head(node)
            kind = {"global_label": "label", "hierarchical_label": "label"}.get(head, head)
            if kind == "wire" and "wire" in kinds:
                pts = [_r((p[1], p[2])) for p in _children(_child(node, "pts"), "xy")]
                if all(self._inside(p, area) for p in pts):
                    cut.append(node)
                    points |= set(pts)
                    counts["wire"] = counts.get("wire", 0) + 1
            elif kind in ("label", "no_connect", "junction") and kind in kinds:
                at = _r(_child(node, "at")[1:3])
                if self._inside(at, area):
                    cut.append(node)
                    points.add(at)
                    counts[kind] = counts.get(kind, 0) + 1
            elif head == "symbol" and "power" in kinds and self._ref(node).startswith("#"):
                at = _r(_child(node, "at")[1:3])
                if self._inside(at, area):
                    cut.append(node)
                    points |= set(self._symbol_pins(node, libs).values())
                    counts["power"] = counts.get("power", 0) + 1
        # What the removal can part: the pins wired to what goes, found before it goes (18 Nov
        # review: every pin on the sheet was excused for a small rectangle).
        self.removed_scope = self.pins_touching(points)
        self._cut([spans[id(n)] for n in cut])
        self.tidy(points)
        return counts

    def pins_touching(self, points):
        """Symbol pins (REF.NUMBER, not power symbols) wired on this sheet to any of these points."""
        root, _ = self._tree()
        libs = self._libs(root)

        def on(p, a, b):
            cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
            return (abs(cross) < 1e-6 and min(a[0], b[0]) - 1e-6 <= p[0] <= max(a[0], b[0]) + 1e-6
                    and min(a[1], b[1]) - 1e-6 <= p[1] <= max(a[1], b[1]) + 1e-6)
        segments = [seg for wire in self.wires() for seg in zip(wire, wire[1:])]
        reach, used, grown = set(points), set(), True
        while grown:
            grown = False
            for index, (a, b) in enumerate(segments):
                if index not in used and any(on(p, a, b) for p in reach):
                    used.add(index)
                    reach |= {a, b}
                    grown = True
        found = set()
        for symbol in _children(root, "symbol"):
            ref = self._ref(symbol)
            if not ref or ref.startswith("#"):
                continue
            for number, point in self._symbol_pins(symbol, libs).items():
                if point in reach or any(on(point, *segments[i]) for i in used):
                    found.add("%s.%s" % (ref, number))
        return sorted(found)

    def remove_dangling(self, area=None):
        """Remove what connects to nothing (inside area when given): no-connects off any pin,
        labels on no wire or pin, power symbols touching nothing, wires with a free end.
        Returns counts by kind."""
        counts = {}
        root, spans = self._tree()
        libs = self._libs(root)
        # A label is held by a wire, a bus, a pin or a bus entry's end (an import puts labels
        # straight on bus-entry ends, 16 Nov: four such were taken as loose).
        wires = self.wires()
        wires += [[_r((q[1], q[2])) for q in _children(_child(b, "pts"), "xy")] for b in _children(root, "bus")]
        on_wire = lambda p: any(_on(p, a, b) for pts in wires for a, b in zip(pts, pts[1:]))  # noqa: E731
        parts = {p for s in _children(root, "symbol") if not self._ref(s).startswith("#")
                 for p in self._symbol_pins(s, libs).values()}
        pins_only = set(parts)
        for entry in _children(root, "bus_entry"):
            at, size = _child(entry, "at"), _child(entry, "size")
            parts |= {_r(at[1:3]), _r((float(at[1]) + float(size[1]), float(at[2]) + float(size[2])))}
        ports = [s for s in _children(root, "symbol") if self._ref(s).startswith("#")]
        port_pins = {id(s): set(self._symbol_pins(s, libs).values()) for s in ports}
        cut = []
        for node in _children(root, "no_connect"):
            at = _r(_child(node, "at")[1:3])
            if at not in parts and self._inside(at, area):
                cut.append(node)
                counts["no_connect"] = counts.get("no_connect", 0) + 1
        for node in root:
            if _head(node) in ("label", "global_label", "hierarchical_label"):
                at = _r(_child(node, "at")[1:3])
                if at not in parts and not on_wire(at) and self._inside(at, area):
                    cut.append(node)
                    counts["label"] = counts.get("label", 0) + 1
        for node in ports:
            others = parts | {p for s in ports if s is not node for p in port_pins[id(s)]}
            mine = port_pins[id(node)]
            if mine and all(p not in others and not on_wire(p) for p in mine) \
                    and self._inside(_r(_child(node, "at")[1:3]), area):
                cut.append(node)
                counts["power"] = counts.get("power", 0) + 1
        self._cut([spans[id(n)] for n in cut])
        before = len(self.wires())
        self.tidy({p for pts in self.wires() for p in (pts[0], pts[-1]) if self._inside(p, area)})
        if before - len(self.wires()):
            counts["wire"] = before - len(self.wires())
        # A bus entry whose wire side reaches nothing (16 Nov: entries left where V1 and LS
        # were taken off the RFID Control bus).
        root, spans = self._tree()
        wires = self.wires()
        buses = [[_r((q[1], q[2])) for q in _children(_child(b, "pts"), "xy")] for b in _children(root, "bus")]
        labels = {item["at"] for item in self.labels()}
        on = lambda p, lines: any(_on(p, c, d) for pts in lines for c, d in zip(pts, pts[1:]))  # noqa: E731
        loose = []
        for entry in _children(root, "bus_entry"):
            at, size = _child(entry, "at"), _child(entry, "size")
            a = _r(at[1:3])
            b = _r((float(at[1]) + float(size[1]), float(at[2]) + float(size[2])))
            # Its wire side is the end that is not on a bus; loose when that reaches nothing.
            wire_side = [p for p in (a, b) if not on(p, buses)]
            if wire_side and all(not on(p, wires) and p not in labels and p not in pins_only for p in wire_side) \
                    and self._inside(a, area):
                loose.append(entry)
        if loose:
            self._cut([spans[id(n)] for n in loose])
            counts["bus_entry"] = len(loose)
        return counts

    def swap_symbol(self, ref, lib_id, value=None, footprint=None, at=None, rotation=None):
        """Change a symbol to another library symbol (named in the edit's symbols), in its place.

        Keeps its reference, fields and instances. Returns the old pin points
        that no new pin now lands on, to wire or tidy.
        """
        if footprint and "/" in footprint and ":" not in footprint:
            footprint = footprint.replace("/", ":", 1)
        if lib_id not in _LIBRARY_SYMBOLS and lib_id not in self._libs(self._tree()[0]):
            raise KeyError("name %s in the edit's symbols so the tool copies it in" % lib_id)
        before = self.symbol(ref)["pins"]
        self._ensure_lib(lib_id)
        root, spans = self._tree()
        symbol = [s for s in _children(root, "symbol") if self._ref(s) == ref][0]
        start, end = spans[id(symbol)]
        block = self.text[start:end]
        block = block.replace('(lib_id %s)' % _child(symbol, "lib_id")[1], '(lib_id %s)' % _q(lib_id), 1)
        old_at = _child(symbol, "at")
        if at is not None or rotation is not None:
            x, y = at if at is not None else (float(old_at[1]), float(old_at[2]))
            turn = rotation if rotation is not None else (float(old_at[3]) if len(old_at) > 3 else 0)
            dx, dy = x - float(old_at[1]), y - float(old_at[2])
            first = block.index("(at ")
            close = block.index(")", first)
            head_end = block.index("(property") if "(property" in block else len(block)

            def shift(m):
                return "(at %g %g %s)" % (float(m.group(1)) + dx, float(m.group(2)) + dy, m.group(3))
            block = (block[:first] + "(at %g %g %g)" % (x, y, turn) + block[close + 1:head_end]
                     + _re.sub(r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', shift, block[head_end:]))
        # The new part's own fields, from its library part: an assembler builds from these
        # (16 Nov: swapped switches still named the old PE42462 MPN, LCSC number and
        # datasheet). The old part's identity fields go; its reference stays.
        library_fields = {_unq(p[1]): _unq(p[2]) for p in _children(self._libs(self._tree()[0])[lib_id], "property")}
        kept = ("Reference", "Value", "Footprint")
        for name, start_at, end_at in reversed(_top_properties(block)):
            if name not in kept and name not in library_fields:
                cut_from = block.rfind("\n", 0, start_at)
                block = block[:cut_from if cut_from >= 0 else start_at] + block[end_at:]
        fields = [(n, t) for n, t in library_fields.items() if n not in kept]
        fields += [("Value", value if value is not None else library_fields.get("Value")),
                   ("Footprint", footprint if footprint is not None else library_fields.get("Footprint"))]
        for name, text in fields:
            if text is None:
                continue
            pattern = r'\(property "%s" "(?:[^"\\]|\\.)*"' % name
            if _re.search(pattern, block):
                block = _re.sub(pattern, lambda m: '(property "%s" %s' % (name, _q(text)), block, count=1)
            else:
                # A symbol without the field gets it, hidden, at the symbol.
                at_now = _re.search(r'\(at ([-\d.]+) ([-\d.]+)', block)
                where = block.find("\n\t\t(pin ")
                where = where if where >= 0 else block.rfind(")")
                block = (block[:where] + '\n\t\t(property "%s" %s\n\t\t\t(at %s %s 0)\n\t\t\t(hide yes)\n'
                         '\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n\t\t\t)\n\t\t)'
                         % (name, _q(text), at_now.group(1), at_now.group(2)) + block[where:])
        lib = self._libs(self._tree()[0])[lib_id]
        numbers = sorted({n for part in _children(lib, "symbol") for n in _lib_pins(part)},
                         key=lambda n: (len(n), n))
        block = _re.sub(r'\n\s*\(pin "[^"]*"\s*\(uuid "[^"]*"\)\s*\)', "", block)
        pins = "".join('\n\t\t(pin %s\n\t\t\t(uuid "%s")\n\t\t)' % (_q(n), _uuid.uuid4()) for n in numbers)
        where = block.find("\n\t\t(instances")
        where = where if where >= 0 else block.rfind(")")
        block = block[:where] + pins + block[where:]
        self.text = self.text[:start] + block + self.text[end:]
        after = set(self.symbol(ref)["pins"].values())
        return sorted(set(before.values()) - after)

    def _ensure_lib(self, lib_id):
        root, spans = self._tree()
        if lib_id in self._libs(root):
            if lib_id not in _LIBRARY_SYMBOLS:
                return
            # The sheet's stored copy is replaced by his library's own (16 Nov: an imported
            # copy of the 100 pF part had no MPN or LCSC, and parts swapped to it lost theirs).
            cache = _child(root, "lib_symbols")
            stored = [n for n in _children(cache, "symbol") if _unq(n[1]) == lib_id]
            if stored:
                first, last = spans[id(stored[0])]
                self.text = self.text[:first] + self.text[last:]
                root, spans = self._tree()
        block = _LIBRARY_SYMBOLS[lib_id]
        if "(extends" in block:
            raise ValueError("%s came without the symbol it extends; name it as LIBRARY:NAME from his "
                             "library so the tool can read both" % lib_id)
        name = lib_id.split(":", 1)[1]
        block = block.replace("(symbol %s" % _q(name), "(symbol %s" % _q(lib_id), 1)
        cache = _child(root, "lib_symbols")
        if cache is None:
            raise ValueError("this sheet has no lib_symbols section")
        end = spans[id(cache)][1] - 1
        self.text = self.text[:end] + "\t" + block.replace("\n", "\n\t") + "\n\t" + self.text[end:]

    def add_wire(self, *points):
        """A wire through points (x, y) on the sheet."""
        for a, b in zip(points, points[1:]):
            self._append('\t(wire\n\t\t(pts\n\t\t\t(xy %g %g) (xy %g %g)\n\t\t)\n\t\t(stroke\n\t\t\t(width 0)\n'
                         '\t\t\t(type default)\n\t\t)\n\t\t(uuid "%s")\n\t)'
                         % (a[0], a[1], b[0], b[1], _uuid.uuid4()))

    def add_label(self, name, at, angle=0, kind="label"):
        """A net label (label, global_label or hierarchical_label) at a wire end or pin."""
        justify = "right" if angle in (180, 270) else "left"
        shape = "" if kind == "label" else "\t\t(shape input)\n"
        self._append('\t(%s %s\n%s\t\t(at %g %g %g)\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.27 1.27)\n'
                     '\t\t\t)\n\t\t\t(justify %s%s)\n\t\t)\n\t\t(uuid "%s")\n\t)'
                     % (kind, _q(name), shape, at[0], at[1], angle, justify,
                        " bottom" if kind == "label" else "", _uuid.uuid4()))

    def add_no_connect(self, at):
        """A no-connect mark on a pin end at (x, y)."""
        self._append('\t(no_connect\n\t\t(at %g %g)\n\t\t(uuid "%s")\n\t)' % (at[0], at[1], _uuid.uuid4()))

    def add_junction(self, at):
        """A junction dot where three or more wires meet at (x, y)."""
        self._append('\t(junction\n\t\t(at %g %g)\n\t\t(diameter 0)\n\t\t(color 0 0 0 0)\n\t\t(uuid "%s")\n\t)'
                     % (at[0], at[1], _uuid.uuid4()))

    def copy_symbol(self, ref, at, rotation=0):
        """A copy of a power or ground symbol on this sheet at another place; ref may be its uuid.

        Imported power symbols often share the reference #PWR? (15 Nov): a uuid
        names exactly one.
        """
        root, spans = self._tree()
        found = [s for s in _children(root, "symbol")
                 if (_child(s, "uuid") and _unq(_child(s, "uuid")[1]) == ref)] or \
                [s for s in _children(root, "symbol") if self._ref(s) == ref]
        if not found:
            raise self._missing(ref)
        symbol = found[0]
        ref = self._ref(symbol)
        if not ref.startswith("#"):
            raise ValueError("only power and ground symbols (#PWR...) are copied; a part needs its own reference")
        start, end = spans[id(symbol)]
        block = self.text[start:end]
        old = _child(symbol, "at")
        if rotation is None:
            rotation = float(old[3]) if len(old) > 3 else 0.0
        dx, dy = at[0] - float(old[1]), at[1] - float(old[2])
        first = block.index("(at ")
        close = block.index(")", first)
        head_end = block.index("(property")

        def shift(m):
            return "(at %g %g %s)" % (float(m.group(1)) + dx, float(m.group(2)) + dy, m.group(3))
        block = (block[:first] + "(at %g %g %g)" % (at[0], at[1], rotation) + block[close + 1:head_end]
                 + _re.sub(r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', shift, block[head_end:]))
        block = _re.sub(r'\(uuid "[^"]+"\)', lambda m: '(uuid "%s")' % _uuid.uuid4(), block)
        # Power symbols are numbered by KiCad; a free number keeps the sheet
        # valid. One left unannotated (#PWR?) stays so, like its original.
        if "?" not in ref:
            taken = {self._ref(s) for s in _children(root, "symbol")}
            number = 1
            while "#PWR%04d" % number in taken or "#PWR0%d" % number in taken:
                number += 1
            block = block.replace('"%s"' % ref, '"#PWR%04d"' % number)
        self._append("\t" + block)

    def copy_part(self, like, ref, at, rotation=None):
        """A new part, a copy of part `like` on this sheet (same library part, fields and
        footprint), under the free reference `ref` at `at`. 16 Nov: the second 220 pF on TX1.
        Its pins may touch nothing already there; it is wired with wire, label and power."""
        folder = _os.path.dirname(self.path)
        for name in (n for n in _os.listdir(folder) if n.endswith(".kicad_sch")):
            with open(_os.path.join(folder, name)) as handle:
                text = handle.read()
            if '(property "Reference" "%s"' % ref in text or '(reference "%s")' % ref in text:
                raise ValueError("%s is already used on %s; take a free reference" % (ref, name))
        root, spans = self._tree()
        found = [n for n in _children(root, "symbol") if self._ref(n) == like]
        if not found:
            raise self._missing(like)
        if like.startswith("#"):
            raise ValueError("%s is a power symbol; use power to place ports" % like)
        symbol = found[0]
        start, end = spans[id(symbol)]
        block = self.text[start:end]
        old = _child(symbol, "at")
        turn = rotation if rotation is not None else (float(old[3]) if len(old) > 3 else 0.0)
        dx, dy = at[0] - float(old[1]), at[1] - float(old[2])
        first = block.index("(at ")
        close = block.index(")", first)
        head_end = block.index("(property")

        def shift(m):
            return "(at %g %g %s)" % (float(m.group(1)) + dx, float(m.group(2)) + dy, m.group(3))
        block = (block[:first] + "(at %g %g %g)" % (at[0], at[1], turn) + block[close + 1:head_end]
                 + _re.sub(r'\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)', shift, block[head_end:]))
        block = _re.sub(r'\(uuid "[^"]+"\)', lambda m: '(uuid "%s")' % _uuid.uuid4(), block)
        block = block.replace('(property "Reference" "%s"' % like, '(property "Reference" "%s"' % ref, 1)
        block = block.replace('(reference "%s")' % like, '(reference "%s")' % ref)
        scene = self._scene()
        self._append("\t" + block)
        mine = self.symbol(ref)["pins"]
        for number, point in mine.items():
            if any(_on(point, a, b) for pts in scene["wires"] for a, b in zip(pts, pts[1:])) or \
                    point in {q for _, q, _, _ in scene["pins"]} or point in {l["at"] for l in scene["labels"]}:
                raise ValueError("%s pin %s at (%g, %g) would land on what is already there; place it "
                                 "elsewhere" % (ref, number, point[0], point[1]))
        body = [b for n, b in self._scene()["parts"] if n == ref]
        for name, (x0, y0, x1, y1) in scene["parts"]:
            if body and max(x0, body[0][0]) < min(x1, body[0][2]) and max(y0, body[0][1]) < min(y1, body[0][3]):
                raise ValueError("%s would sit on %s; place it elsewhere" % (ref, name))
        return mine

    def save(self):
        """Write the sheet back, after checking it still parses."""
        _sx(self.text)
        with open(self.path, "w") as handle:
            handle.write(self.text)
        if getattr(self, "_wire_hops", False):
            path = _os.path.splitext(BOARD_PATH)[0] + ".kicad_pro"
            if _os.path.isfile(path):
                with open(path) as handle:
                    settings = _json.load(handle)
                drawing = settings.setdefault("schematic", {}).setdefault("drawing", {})
                if not drawing.get("hop_over_size_choice"):
                    drawing["hop_over_size_choice"] = 3  # FireFli P0005 drawing setting
                    with open(path, "w") as handle:
                        handle.write(_json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def sheet(name):
    """A schematic sheet of the copy by file name (BHL_RoomReaderV2_RFID.kicad_sch)."""
    return Sheet(name)


def sheets():
    """List of schematic filenames at the top of the work copy."""
    root = _os.path.dirname(BOARD_PATH)
    return sorted(name for name in _os.listdir(root) if name.endswith(".kicad_sch"))

# Geometry helpers operate on parsed nodes. Unrelated top-level items retain their bytes.
def _sexp(node, depth=1):
    # Keep modified KiCad items readable and compatible with the existing sheet helpers.
    indent = "\t" * depth
    pieces = ["(" + node[0]]
    for n in node[1:]:
        pieces.append("\n" + indent + "\t" + _sexp(n, depth + 1) if isinstance(n, list) else " " + n)
    return "".join(pieces) + ("\n" + indent if any(isinstance(n, list) for n in node) else "") + ")"


def _walk(node):
    yield node
    for item in node:
        if isinstance(item, list):
            yield from _walk(item)


def _translate_layout(node, dx, dy, renew_ids=False):
    for item in _walk(node):
        if _head(item) in ("at", "xy", "start", "mid", "end", "center"):
            item[1:3] = ["%.6f" % (float(item[1]) + dx), "%.6f" % (float(item[2]) + dy)]
        if renew_ids and _head(item) == "uuid":
            item[1] = _q(str(_uuid.uuid4()))


def _apply_symbol_layout(twin, layout):
    twin[:] = [n for n in twin if _head(n) not in ("at", "mirror")]
    twin[3:3] = [n for n in layout if _head(n) in ("at", "mirror")]
    props = {n[1]: n for n in _children(layout, "property")}
    for prop in _children(twin, "property"):
        if prop[1] in props:
            for key in ("at", "effects"):
                value = _child(props[prop[1]], key)
                if value:
                    prop[:] = [n for n in prop if _head(n) != key] + [value]


def _block_grids(s):
    try:
        with open(s.path + _GRID_SUFFIX) as handle:
            return _json.load(handle)
    except FileNotFoundError:
        return {}


def _snap(value):
    return round(round(float(value) / 1.27) * 1.27, 2)


def block_grid(sheet, name, origin, columns, rows, col_pitch=12.7, row_pitch=7.62):
    """Define a named placement grid in the work copy. Names are ordered left to right and top to bottom.

    Origin and pitches must be on the 1.27 mm grid. Supply row names such as
    ["rail", 1, 2, "ground"]. Returns the named column and row coordinates.
    """
    columns, rows = list(map(str, columns)), list(map(str, rows))
    if (not isinstance(name, str) or not name.strip()
            or any(not names or not all(names) or len(set(names)) != len(names) for names in (columns, rows))):
        raise ValueError("give the block, columns and rows distinct nonempty names")
    x, y = map(float, origin)
    steps = (x, y, float(col_pitch), float(row_pitch))
    if any(not _math.isfinite(n) or abs(n - _snap(n)) > 1e-6 for n in steps) or min(steps[2:]) <= 0:
        raise ValueError("origin and positive pitches must be multiples of 1.27 mm")
    grids = _block_grids(sheet)
    definition = {"columns": {c: _snap(x + i * steps[2]) for i, c in enumerate(columns)},
                  "rows": {r: _snap(y + i * steps[3]) for i, r in enumerate(rows)}}
    grids[name] = definition
    with open(sheet.path + _GRID_SUFFIX, "w") as handle:
        _json.dump(grids, handle, indent=2)
    return definition


def _relocate_symbol(s, ref, point, rotation, mirror):
    symbol, _, spans = s._placed_symbol(ref)
    first, last = spans[id(symbol)]
    at = _child(symbol, "at")
    dx, dy = point[0] - float(at[1]), point[1] - float(at[2])
    at[1:] = ["%g" % point[0], "%g" % point[1], "%g" % rotation]
    symbol[:] = [n for n in symbol if _head(n) != "mirror"]
    if mirror:
        symbol.insert(symbol.index(at) + 1, ["mirror", mirror])
    for field in _children(symbol, "property"):
        pos = _child(field, "at")
        if pos:
            pos[1:3] = ["%g" % (float(pos[1]) + dx), "%g" % (float(pos[2]) + dy)]
    old = s.text
    s.text = old[:first] + _sexp(symbol) + old[last:]
    try:
        s.tidy_fields(ref)
    except Exception:
        s.text = old
        raise


def _at_node(point, rotation):
    return ["at", "%g" % point[0], "%g" % point[1], "%g" % rotation]


def _orient_power(symbol, lib, net, point, axis=None):
    drawings = list(_lib_drawings(symbol, lib))
    rotation = _power_turn(drawings, net, axis)
    pins = {number: xy for part in drawings for number, xy in _lib_pins(part).items()}
    number, pin = next(iter(pins.items()))
    offset = _placed(_at_node((0, 0), rotation), axis, *pin)
    anchor = _r(tuple(p - d for p, d in zip(point, offset)))
    _child(symbol, "at")[1:] = _at_node(anchor, rotation)[1:]
    down = "GND" in net.upper()
    ys = [_placed(_at_node(anchor, rotation), axis, *xy)[1]
          for part in drawings for xy in _lib_outline(part)]
    for prop in _children(symbol, "property"):
        position = _child(prop, "at")
        if _unq(prop[1]) == "Value" and position:
            y = max(ys) + 1.27 if down else min(ys) - 1.27
            position[1:] = _at_node((point[0], y), rotation % 180)[1:]
            effects = _child(prop, "effects")
            if effects:
                effects[:] = [item for item in effects if _head(item) != "justify"]
    return number


def _place_power(s, net, at):
    point = tuple(map(float, at))
    if len(point) != 2 or not all(map(_math.isfinite, point)):
        raise ValueError("give the power pin position in sheet millimetres")
    point = _r(point)
    root, _ = s._tree()
    cached = s._libs(root)
    definitions = dict(cached)
    definitions.update({name: _sx(text)[0] for name, text in _LIBRARY_SYMBOLS.items() if name not in cached})
    matches = [(name, lib) for name, lib in definitions.items() if _child(lib, "power")
               and (net == name or (":" not in net and any(
                   _unq(p[1]) == "Value" and _unq(p[2]) == net for p in _children(lib, "property"))))]
    matches = [(name, lib) for name, lib in matches if name.rsplit(":", 1)[-1] == net] or matches
    if len(matches) != 1:
        raise ValueError("power symbol %s: %s; name one LIBRARY:SYMBOL in edit's symbols"
                         % (net, ", ".join(name for name, _ in matches) or "no matching definition"))
    name, lib = matches[0]
    if name not in cached and _child(lib, "extends"):
        raise ValueError("power symbol %s needs its flattened library definition" % name)
    value = next(_unq(p[2]) for p in _children(lib, "property") if _unq(p[1]) == "Value")
    paths, _ = _hierarchy()
    instances = paths.get(_os.path.realpath(s.path), [])
    if not instances:
        raise ValueError("power placement needs a sheet in the project hierarchy")
    taken = set()
    for file in paths:
        tree = root if file == _os.path.realpath(s.path) else Sheet(file)._tree()[0]
        taken.update(s._ref(n) for n in _children(tree, "symbol"))
        taken.update(_unq(n[1]) for n in _walk(tree) if _head(n) == "reference")
    number = 1
    references = []
    for _ in instances:
        while {"#PWR%04d" % number, "#PWR0%d" % number, "#PWR%d" % number} & taken:
            number += 1
        references.append("#PWR%04d" % number)
        number += 1
    ref = references[0]
    project = _os.path.splitext(_os.path.basename(BOARD_PATH))[0]
    symbol = ["symbol", ["lib_id", _q(name)], _at_node(point, 0), ["unit", "1"],
              ["in_bom", "no"], ["on_board", "no"], ["dnp", "no"],
              ["uuid", _q(_uuid.uuid4())],
              ["property", _q("Reference"), _q(ref), _at_node(point, 0),
               ["effects", ["font", ["size", "1.27", "1.27"]], ["hide", "yes"]]],
              ["property", _q("Value"), _q(value), _at_node(point, 0),
               ["effects", ["font", ["size", "1.27", "1.27"]]]],
              ["instances", ["project", _q(project), *[
                  ["path", _q(path), ["reference", _q(reference)], ["unit", "1"]]
                  for path, reference in zip(instances, references)]]]]
    pin = _orient_power(symbol, lib, value, point)
    symbol.append(["pin", _q(pin), ["uuid", _q(_uuid.uuid4())]])
    if name not in cached:
        s._ensure_lib(name)
    s._append(_sexp(symbol))
    return ref


def _upright(s, name):
    symbol, lib, spans = s._placed_symbol(name)
    ref = s._ref(symbol)
    net = next((_unq(p[2]) for p in _children(symbol, "property") if _unq(p[1]) == "Value"), "")
    if lib is None:
        raise ValueError("no library definition for " + ref)
    if not _orientation(symbol, lib, ref, net):
        return {"changed": False, "rewire": {}}
    lib_id = _unq(_child(symbol, "lib_id")[1])
    before = s._symbol_pins(symbol, {lib_id: lib})
    at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
    axis = mirror[1] if mirror else None
    start, end = spans[id(symbol)]
    if _child(lib, "power"):
        _orient_power(symbol, lib, net, next(iter(before.values())), axis)
        s.text = s.text[:start] + _sexp(symbol) + s.text[end:]
        return {"changed": True, "rewire": {}}
    # Reflect the current placement across a horizontal line through its
    # anchor. This preserves which side of the symbol each pin is on.
    rotation = (-float(at[3])) % 360
    new_axis = {"x": None, None: "x", "y": None}[axis]
    if axis == "y":
        rotation = (rotation + 180) % 360
    at[3] = "%g" % rotation
    if mirror:
        symbol.remove(mirror)
    if new_axis:
        symbol.insert(symbol.index(at) + 1, ["mirror", new_axis])
    after = s._symbol_pins(symbol, {lib_id: lib})
    scene = s._scene()
    terminals = ({p for pin, p, _, _ in scene["pins"] if pin.rsplit(".", 1)[0] != ref}
                 | {item["at"] for item in scene["labels"]} | scene["ports_and_dots"])
    wires = [(a, b) for points in scene["wires"] for a, b in zip(points, points[1:])]
    rewire = {pin: {"from": point, "to": after[pin]} for pin, point in before.items()
              if point != after[pin] and any(
                  spot in terminals or any(_on(spot, a, b) for a, b in wires)
                  for spot in (point, after[pin]))}
    if rewire:
        return {"changed": False, "rewire": rewire}
    root, original_spans = s._tree()
    nc_moves = {before[pin]: point for pin, point in after.items()}
    updated = [(start, end, _sexp(symbol))]
    for mark in _children(root, "no_connect"):
        position = _child(mark, "at")
        old = _r(position[1:3])
        if old in nc_moves and old != nc_moves[old]:
            position[1:] = _at_node(nc_moves[old], 0)[1:3]
            updated.append((*original_spans[id(mark)], _sexp(mark)))
    old_text = s.text
    try:
        for first, last, content in sorted(updated, reverse=True):
            s.text = s.text[:first] + content + s.text[last:]
        s.tidy_fields(ref)
    except Exception:
        s.text = old_text
        raise
    return {"changed": True, "rewire": {}}


def place_in_slot(sheet, ref, block, column, row, rotation=0):
    """Move a symbol anchor to a named column and row and tidy its fields. Returns (x, y)."""
    grid = _block_grids(sheet)[block]
    point = grid["columns"][str(column)], grid["rows"][str(row)]
    _relocate_symbol(sheet, ref, point, rotation, sheet.symbol(ref)["mirror"])
    return point


def mirror_block(sheet, source_block, target_block, ref_map, axis="y"):
    """Copy slot assignments and orientations to existing twins. y flips columns, x flips rows,
    and None gives an identical layout. Wires and symbol identities are untouched.
    Returns destination anchors by reference.
    """
    grids = _block_grids(sheet)
    source, target = grids[source_block], grids[target_block]
    if (axis not in ("x", "y", None) or source_block == target_block
            or len(set(ref_map.values())) != len(ref_map) or set(ref_map) & set(ref_map.values())):
        raise ValueError("use distinct source and target blocks, a one-to-one ref_map and axis x, y or None")
    read = {name: list(source[name].values()) for name in ("columns", "rows")}
    write = {name: list(target[name].values()) for name in read}
    if any(len(read[name]) != len(write[name]) for name in read):
        raise ValueError("twin block grids need the same numbers of columns and rows")
    if axis:
        write["columns" if axis == "y" else "rows"].reverse()
    original, result = sheet.text, {}
    try:
        for ref, twin in ref_map.items():
            part = sheet.symbol(ref)
            x, y = part["at"]
            if x not in read["columns"] or y not in read["rows"]:
                raise ValueError("%s is outside the source block's slots" % ref)
            point = write["columns"][read["columns"].index(x)], write["rows"][read["rows"].index(y)]
            turn, flip = part["rotation"], part["mirror"]
            if axis:
                turn = -turn
                if flip and flip != axis:
                    turn += 180
                flip = None if flip else axis
            _relocate_symbol(sheet, twin, point, turn % 360, flip)
            result[twin] = point
    except Exception:
        sheet.text = original
        raise
    return result


def _route_schematic(sheet, pin_a, pin_b):
    """route(sheet, pin_a, pin_b) -> wire points, or [] if already wired. Pins are REF.PIN.

    Runs straight out of each pin and routes on the 1.27 mm grid. Can tee
    into the destination's existing wiring. Fails with the obstructed pins
    and a reason if there is no clear path in the search area.
    """
    ref, pin = pin_a.rsplit(".", 1)
    other, other_pin = pin_b.rsplit(".", 1)
    start, begin = sheet._stub_end(ref, pin, 1.27)
    target, entry = sheet._stub_end(other, other_pin, 1.27)
    if start == target:
        raise ValueError("%s and %s share a pin position; move a part before routing" % (pin_a, pin_b))
    group, _ = sheet._wired_to(target)
    if any(_on(start, a, b) for pts in group for a, b in zip(pts, pts[1:])):
        return []
    if not group and sheet._wired_to(start)[0]:
        return list(reversed(_route_schematic(sheet, pin_b, pin_a)))
    out, into = sheet.pin_direction(ref, pin), sheet.pin_direction(other, other_pin)
    if not any(out) or not any(into) or any(_snap(v) != v for v in (*start, *target)):
        raise ValueError("%s to %s needs orthogonal pins on the 1.27 mm grid" % (pin_a, pin_b))
    scene = sheet._scene()
    if {start, target}.intersection(scene["no_connects"]):
        raise ValueError("%s to %s: remove the no-connect mark before routing" % (pin_a, pin_b))
    for point, wanted in ((start, pin_a), (target, pin_b)):
        overlapping = [name for name, position, _, _ in scene["pins"] if position == point and name != wanted]
        if overlapping:
            raise ValueError("%s shares its pin position with %s" % (wanted, ", ".join(overlapping)))
    terminals = {p for _, p, _, _ in scene["pins"]} | scene["ports_and_dots"]
    wires = [(a, b) for wire in sheet.wires() for a, b in zip(wire, wire[1:])]
    tees = set()
    for pts in group:
        for a, b in zip(pts, pts[1:]):
            steps = int(round(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / 1.27))
            for index in range(1, steps):
                point = _r(tuple(u + (v - u) * index / steps for u, v in zip(a, b)))
                # A crossing of two wires is never a tee candidate.
                if (point not in terminals and
                        sum(_on(point, c, d) for c, d in wires) == 1):
                    tees.add(point)
    grids = _block_grids(sheet)
    lines = tuple({value for grid in grids.values() for value in grid[key].values()}
                  for key in ("columns", "rows"))
    # If the stubs face each other and meet or overlap, draw one straight lead.
    if not group and out == tuple(-v for v in into) and (
            _on(begin, start, target) or begin == target) and _on(entry, start, target) and (
            start[0] == target[0] if out[1] else start[1] == target[1]):
        direct = (start, target)
        try:
            sheet._check_segments([(start, target, pin_a)], {start, target}, strict=True)
        except ValueError:
            pass
        else:
            _draw_routed_wire(sheet, direct, wires)
            return list(direct)
    directions = tuple(d * 1.27 for d in into)
    try:
        sheet._check_segments([(start, begin, pin_a)], {start, target} | ({begin} & tees), strict=True)
        # When the destination already has a lead, a tee provides its straight entry.
        try:
            sheet._check_segments([(entry, target, pin_b)], {start, target}, strict=True)
            enter_pin = True
        except ValueError:
            if not tees:
                raise
            enter_pin = False
        leads = [(start, begin)] + ([(target, entry)] if enter_pin else [])
        tail = sheet._route(ref, pin, begin, [entry], target, heading=out, tees=tees,
                            arrival_heading=directions, channels=lines if grids else (),
                            strict=True, enter_pin=enter_pin, leads=leads)
        if tail is None:
            raise ValueError("no clear orthogonal path within 30.48 mm of the endpoints")
        end = tail[-1] if tail else begin
        tee = end in tees
        points = _straight([start, begin, *tail] + ([] if tee else [target]))
        allowed = {start, end} if tee else {start, target}
        sheet._check_segments([(a, b, pin_a) for a, b in zip(points, points[1:])], allowed, strict=True)
        if _backtracks(points):
            raise ValueError("the leads would overlap")
    except ValueError as error:
        raise ValueError("%s to %s: %s" % (pin_a, pin_b, error)) from error
    _draw_routed_wire(sheet, points, wires)
    if tee:
        sheet.add_junction(end)
    return points


def _draw_routed_wire(sheet, points, wires):
    def crosses(a, b, c, d):
        if a[0] == b[0] and c[1] == d[1]:
            crossing = a[0], c[1]
        elif a[1] == b[1] and c[0] == d[0]:
            crossing = c[0], a[1]
        else:
            return False
        return crossing not in (a, b, c, d) and _on(crossing, a, b) and _on(crossing, c, d)

    if any(crosses(a, b, c, d) for a, b in zip(points, points[1:]) for c, d in wires):
        sheet._wire_hops = True
    sheet.add_wire(*points)


def info_table(sheet, origin, columns_mm, rows):
    """Draw a ruled information table with 1.016 mm cells and a bold first row.

    origin is the top-left point. columns_mm lists widths. Each row supplies
    one string per column. Returns [left, top, right, bottom].
    """
    from itertools import accumulate
    x, y = map(float, origin)
    widths = list(map(float, columns_mm))
    rows = [list(row) for row in rows]
    if (not widths or any(not _math.isfinite(w) or w <= 0 for w in widths)
            or not all(_math.isfinite(n) for n in (x, y)) or not rows
            or any(len(row) != len(widths) for row in rows)):
        raise ValueError("give positive column widths and one cell per column in every row")
    xs = list(accumulate([x, *widths]))
    ys = [y + 5.08 * i for i in range(len(rows) + 1)]

    def line(x1, y1, x2, y2, width):
        sheet._append('\t(polyline (pts (xy %g %g) (xy %g %g)) (stroke (width %g) (type solid)) (uuid "%s"))'
                      % (x1, y1, x2, y2, width, _uuid.uuid4()))

    for index, cy in enumerate(ys):
        line(xs[0], cy, xs[-1], cy, 0.3048 if index <= 1 or index == len(rows) else 0.1524)
    for index, cx in enumerate(xs):
        line(cx, ys[0], cx, ys[-1], 0.3048 if index in (0, len(widths)) else 0.1524)
    for index, row in enumerate(rows):
        for cx, cell in zip(xs, row):
            if cell != "":
                sheet._append('\t(text %s (at %g %g 0) (effects (font (size 1.016 1.016)%s) '
                              '(justify left bottom)) (uuid "%s"))'
                              % (_q(str(cell).replace("\n", "\\n")), cx + 1.27, ys[index] + 3.556,
                                 " (thickness 0.2032) (bold yes)" if index == 0 else "", _uuid.uuid4()))
    return [xs[0], ys[0], xs[-1], ys[-1]]


def place_design_block(sheet, library_block, at, ref_map=None):
    """Place a FireFli block's drawing at the specified top-left anchor (x, y).

    ref_map pairs template references with symbols already on the destination
    sheet. Bound symbols keep their UUIDs, values, fields and instances. Free
    references are allocated for unbound symbols. Returns references, items,
    and missing_nets for named interfaces absent from the destination sheet.
    """
    if library_block not in _DESIGN_BLOCKS:
        raise ValueError("unknown FireFli design block %s; available: %s" %
                         (library_block, ", ".join(sorted(_DESIGN_BLOCKS)) or "none"))
    ref_map = dict(ref_map or {})
    source, _ = _sx(_DESIGN_BLOCKS[library_block])
    target, spans = sheet._tree()
    source_libs, target_libs = sheet._libs(source), sheet._libs(target)
    parts = _children(source, "symbol")
    source_refs = [sheet._ref(n) for n in parts]
    template_keys = {}
    for part, ref in zip(parts, source_refs):
        if source_refs.count(ref) > 1:
            if not ref.endswith("?"):
                raise ValueError("ambiguous repeated block reference " + ref)
            ref += "@" + _unq(_child(part, "uuid")[1])
        template_keys[id(part)] = ref
    if (set(ref_map) - set(template_keys.values())
            or len(ref_map) != len(set(ref_map.values())) or _children(source, "sheet")):
        raise ValueError("a block needs unique template references, a one-to-one ref_map and no child sheets")
    target_parts = _children(target, "symbol")
    existing = {sheet._ref(n): n for n in target_parts}
    if any(sum(sheet._ref(n) == ref for n in target_parts) != 1 for ref in ref_map.values()):
        raise ValueError("each bound reference must name exactly one symbol on the destination sheet")
    kinds = ("wire", "bus", "bus_entry", "junction", "no_connect", "label", "global_label",
             "hierarchical_label", "symbol", "text", "text_box", "polyline", "rectangle",
             "circle", "arc", "bezier", "image")
    drawing = [item for item in source if _head(item) in kinds]
    locations = [point for item in drawing for point in _geometry(item, sheet, source_libs)]
    if not locations:
        raise ValueError("the design block is empty")
    anchor = tuple(_snap(min(p[axis] for p in locations)) for axis in (0, 1))
    delta = tuple(float(v) - a for v, a in zip(at, anchor))
    if len(delta) != 2 or any(not _math.isfinite(v) or abs(v - _snap(v)) > 1e-6 for v in delta):
        raise ValueError("place the block at a point on the 1.27 mm grid")
    paths, _ = _hierarchy()
    instances = paths.get(_os.path.realpath(sheet.path), [])
    if len(instances) != 1:
        raise ValueError("place the block on a sheet with one unambiguous project instance")
    project = _os.path.splitext(_os.path.basename(BOARD_PATH))[0]
    taken = set(existing)
    for file in paths:
        other, _ = sheet._tree() if file == _os.path.realpath(sheet.path) else Sheet(file)._tree()
        taken.update(sheet._ref(n) for n in _children(other, "symbol"))
        taken.update(_unq(n[1]) for n in _walk(other) if _head(n) == "reference")
    present_nets = {n["name"] for n in sheet.labels()} | set(sheet._scene()["values"].values())
    present_nets.update(_unq(pin[1]) for child in _children(target, "sheet") for pin in _children(child, "pin"))
    local_labels = [_unq(n[1]) for n in _children(source, "label")]
    expected = {name for name in local_labels if local_labels.count(name) == 1}
    expected.update(_unq(n[1]) for n in source if _head(n) in ("global_label", "hierarchical_label"))
    for part in parts:
        if sheet._ref(part).startswith("#PWR"):
            expected.update(_unq(p[2]) for p in _children(part, "property") if _unq(p[1]) == "Value")
    imports, replacements, blocks, refs = {}, [], [], {}
    for item in drawing:
        _translate_layout(item, *delta, renew_ids=True)
        if _head(item) != "symbol":
            blocks.append(_sexp(item))
            continue
        ref = sheet._ref(item)
        key = template_keys[id(item)]
        lib = _unq(_child(item, "lib_id")[1])
        if key in ref_map:
            twin = existing[ref_map[key]]
            first, last = spans[id(twin)]
            _apply_symbol_layout(twin, item)
            if sheet._symbol_pins(twin, target_libs) != sheet._symbol_pins(item, source_libs):
                raise ValueError("%s and %s have different pin positions" % (key, ref_map[key]))
            replacements.append((first, last, _sexp(twin)))
            refs[key] = ref_map[key]
            continue
        if lib in target_libs and target_libs[lib] != source_libs[lib]:
            raise ValueError("different cached definitions of " + lib)
        if lib not in target_libs:
            imports[lib] = source_libs[lib]
        new_ref = ref
        if new_ref in taken or new_ref.endswith("?"):
            prefix = ref.rstrip("0123456789?")
            number = 1
            while "%s%d" % (prefix, number) in taken:
                number += 1
            new_ref = "%s%d" % (prefix, number)
        taken.add(new_ref)
        refs[key] = new_ref
        next(p for p in _children(item, "property") if _unq(p[1]) == "Reference")[2] = _q(new_ref)
        item[:] = [n for n in item if _head(n) != "instances"]
        unit = _child(item, "unit")
        item.append(["instances", ["project", _q(project), ["path", _q(instances[0]),
                     ["reference", _q(new_ref)], ["unit", unit[1] if unit else "1"]]]])
        blocks.append(_sexp(item))
    if imports:
        end = spans[id(_child(target, "lib_symbols"))][1] - 1
        replacements.append((end, end, "\n" + "\n".join(map(_sexp, imports.values())) + "\n"))
    for first, last, text in sorted(replacements, reverse=True):
        sheet.text = sheet.text[:first] + text + sheet.text[last:]
    for block in blocks:
        sheet._append("\t" + block)
    return {"references": refs, "items": len(drawing), "missing_nets": sorted(expected - present_nets)}


def _header(root, key):
    return _sexp(_child(root, key))


def _root_sheet():
    return sheet(_os.path.splitext(_os.path.basename(BOARD_PATH))[0] + ".kicad_sch")


def _sheet_row_item(item, position, size):
    """Move a sheet's pins and properties with their original edges."""
    at, dimensions = _child(item, "at"), _child(item, "size")
    old, old_size = _r(at[1:3]), _r(dimensions[1:3])
    moves = []
    directions = {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, 1)}
    for child in _children(item, "pin") + _children(item, "property"):
        pos = _child(child, "at")
        if pos is None:
            continue
        point = _r(pos[1:3])
        offsets = [round(p - a, 2) for p, a in zip(point, old)]
        if _head(child) == "pin":
            direction = directions.get(float(pos[3]) % 360)
            if direction is None:
                raise ValueError("sheet pin has a non-orthogonal orientation: " + _unq(child[1]))
            axis = 0 if direction[0] else 1
            if direction[axis] > 0:
                offsets[axis] += size[axis] - old_size[axis]
            cross = 1 - axis
            if not 0 <= offsets[cross] <= size[cross]:
                raise ValueError("sheet size is too small for pin " + _unq(child[1]))
        elif offsets[1] >= old_size[1]:
            offsets[1] += size[1] - old_size[1]
        new = _r(tuple(p + d for p, d in zip(position, offsets)))
        if _head(child) == "pin":
            moves.append((point, new, direction, _unq(child[1])))
        pos[1:3] = _at_node(new, 0)[1:3]
    at[1:3] = _at_node(position, 0)[1:3]
    dimensions[1:3] = _at_node(size, 0)[1:3]
    return moves


def _root_harnesses(top, root, spans, moves):
    """Extract affected physical wire and bus groups, keeping their electrical terminals."""
    from collections import defaultdict

    pins = defaultdict(list)
    for old, new, direction, name in moves:
        pins[old].append((new, name))
    changed = {old for old, entries in pins.items() if any(old != new for new, _ in entries)}
    if not changed:
        return [], []
    labels = [item for kind in ("label", "global_label", "hierarchical_label") for item in _children(root, kind)]
    marks = {_r(_child(item, "at")[1:3]): item for item in _children(root, "no_connect")}
    dots = {_r(_child(item, "at")[1:3]): item for item in _children(root, "junction")}
    terminal_sets = {"wire": set(), "bus": set()}
    libs = top._libs(root)
    terminal_sets["wire"].update(point for item in _children(root, "symbol")
                                 for point in top._symbol_pins(item, libs).values())
    for entry in _children(root, "bus_entry"):
        a, d = (_r(_child(entry, key)[1:3]) for key in ("at", "size"))
        b = _r(tuple(p + q for p, q in zip(a, d)))
        for kind in terminal_sets:
            terminal_sets[kind].update((a, b))

    removed, edits, harnesses, connected = set(), [], [], set()
    for kind, stationary in terminal_sets.items():
        nodes = _children(root, kind)
        segments = [tuple(_r(n[1:3]) for n in _children(_child(item, "pts"), "xy")) for item in nodes]
        groups = defaultdict(list)
        for index, group in _segment_groups(segments, dots if kind == "wire" else ()).items():
            groups[group].append(index)
        for group in groups.values():
            edges = [segments[index] for index in group]

            def touches(point):
                return any(_on(point, a, b) for a, b in edges)

            sheet_pins = {point for point in pins if touches(point)}
            if not sheet_pins & changed:
                continue
            connected.update(sheet_pins)
            removed.update(id(nodes[i]) for i in group)
            if kind == "wire":
                removed.update(id(item) for point, item in dots.items() if touches(point))
            attached = [label for label in labels if touches(_r(_child(label, "at")[1:3]))]
            if any(id(label) in removed for label in attached):
                raise ValueError("root label touches multiple harnesses; separate its connection first")
            removed.update(map(id, attached))
            endpoints = sorted({new for old in sheet_pins for new, _ in pins[old]}
                               | {point for point in stationary if touches(point)})
            harnesses.append((kind, endpoints, attached))
    for old in sorted(changed - connected):
        destinations = [new for new, _ in pins[old]]
        attached = [label for label in labels if _r(_child(label, "at")[1:3]) == old]
        if attached or len(destinations) > 1:
            name = pins[old][0][1]
            kind = "bus" if "{" in name or "[" in name else "wire"
            harnesses.append((kind, sorted(destinations), attached))
            removed.update(map(id, attached))
        if old in marks and len(destinations) == 1:
            mark = marks[old]
            _child(mark, "at")[1:] = _at_node(destinations[0], 0)[1:3]
            edits.append((*spans[id(mark)], _sexp(mark)))
    return edits + [(*spans[uid], "") for uid in removed], harnesses


def _root_segment(top, kind, a, b):
    top._append('(%s (pts (xy %g %g) (xy %g %g)) (stroke (width 0) (type default)) (uuid %s))'
                % (kind, *a, *b, _q(_uuid.uuid4())))


def _root_label_anchor(label, edges, fallback):
    if not edges:
        return fallback

    def length(edge):
        return sum(abs(x - y) for x, y in zip(*edge))

    horizontal = [tuple(sorted(edge)) for edge in edges if edge[0][1] == edge[1][1]]
    if _head(label) == "label" and horizontal:
        left, right = max(horizontal, key=length)
        effects = _child(label, "effects") or []
        justify = _child(effects, "justify") or []
        inset = min(2.54, right[0] - left[0])
        return _r((right[0] - inset if "right" in justify else left[0] + inset, left[1]))
    a, b = max(edges, key=length)
    return _r(tuple(_snap((x + y) / 2) for x, y in zip(a, b)))


def _root_harness(top, kind, terminals, labels, directions, area):
    """Route a trunk and attach further terminals at explicit junctions."""
    points = [terminals[0]]
    edges = []
    for terminal in terminals[1:]:
        if edges:
            # Attach away from the original pins' one-step straight leads.
            candidates = set()
            for a, b in edges:
                steps = round(max(abs(b[0] - a[0]), abs(b[1] - a[1])) / 1.27)
                candidates.update(_r(tuple(x + (y - x) * i / steps for x, y in zip(a, b)))
                                  for i in range(1, steps))
            candidates.update(set(points) - set(terminals))
            candidates = sorted(candidates, key=lambda p: (sum(abs(x - y) for x, y in zip(p, terminal)), p))
        else:
            candidates = [terminals[0]]
        for target in candidates:
            try:
                branch = _route_root_leg(top, kind, terminal, target, directions, area)
            except ValueError as error:
                detail = str(error)
                continue
            if edges:
                if kind == "wire" and not any(_r(_child(n, "at")[1:3]) == target
                                              for n in _children(top._tree()[0], "junction")):
                    top.add_junction(target)
                if kind == "bus":
                    tree, spans = top._tree()
                    for node in _children(tree, "bus"):
                        a, b = (_r(n[1:3]) for n in _children(_child(node, "pts"), "xy"))
                        if target not in (a, b) and _on(target, a, b):
                            top._cut([spans[id(node)]])
                            _root_segment(top, "bus", a, target)
                            _root_segment(top, "bus", target, b)
                            break
            points.extend(branch)
            edges.extend(zip(branch, branch[1:]))
            break
        else:
            raise ValueError("cannot reconnect %s terminal at %s: %s"
                             % (kind, terminal, detail if candidates else "no available junction"))
    for label in labels:
        point = _root_label_anchor(label, edges, terminals[0])
        _child(label, "at")[1:3] = _at_node(point, 0)[1:3]
        top._append(_sexp(label))


def _route_root_leg(top, kind, start, target, directions, area):
    scene = dict(top._scene())
    root = scene["root"]
    wires = {k: [[_r(p[1:3]) for p in _children(_child(n, "pts"), "xy")] for n in _children(root, k)]
             for k in ("wire", "bus")}
    scene["wires"] = wires[kind]
    scene["orphans"] = set()
    scene["parts"] = list(scene["parts"])
    for item in _children(root, "sheet"):
        x, y = _r(_child(item, "at")[1:3])
        w, h = _r(_child(item, "size")[1:3])
        scene["parts"].append(("sheet", (x, y, x + w, y + h)))
    other = wires["bus" if kind == "wire" else "wire"]
    scene["other_wiring"] = other
    scene["ports_and_dots"] = scene["ports_and_dots"] | {p for pts in other for p in pts}
    for item in _children(root, "bus_entry"):
        a = _r(_child(item, "at")[1:3])
        d = _r(_child(item, "size")[1:3])
        scene["ports_and_dots"].update((a, _r(tuple(x + y for x, y in zip(a, d)))))
    if any(_snap(v) != v for v in (*start, *target)):
        raise ValueError("harness endpoints must be on the 1.27 mm grid")
    who = "%s from %s to %s" % (kind, start, target)
    outgoing, incoming = directions.get(start), directions.get(target)
    begin = _r(tuple(p + 1.27 * d for p, d in zip(start, outgoing))) if outgoing else start
    end = _r(tuple(p + 1.27 * d for p, d in zip(target, incoming))) if incoming else target
    leads = [(a, b) for a, b in ((start, begin), (target, end)) if a != b]
    allowed = {start, target}
    if _on(begin, start, target) and _on(end, start, target) and (
            start[0] == target[0] or start[1] == target[1]):
        points = [start, target]
        try:
            top._check_segments([(start, target, who)], allowed, strict=True, scene=scene)
        except ValueError:
            points = None
    else:
        points = None
    if points is None:
        tail = top._route(None, None, begin, [end], target, margin=max(area[2:]),
                          heading=outgoing, strict=True,
                          arrival_heading=tuple(d * 1.27 for d in incoming) if incoming else None,
                          leads=leads, scene=scene, area=area)
        if tail is None:
            raise ValueError("cannot redraw " + who)
        points = _straight([start, begin, *tail, target])
        if _backtracks(points):
            raise ValueError("redrawing %s would overlap its own lead" % who)
        top._check_segments([(a, b, who) for a, b in zip(points, points[1:])],
                            allowed, strict=True, scene=scene)
    if kind == "wire":
        _draw_routed_wire(top, points, [tuple(wire) for wire in wires[kind]])
    else:
        for a, b in zip(points, points[1:]):
            _root_segment(top, kind, a, b)
    return points


def arrange_root(order, size=None, gap=25.4):
    """Save the root sheet symbols in one centred row above the title block.

    Order lists each exact sheet name, filename or UUID once. Size is (width,
    height) in mm and defaults to the largest extents. The existing harness
    terminals are rerouted on the drawing grid. An obstructed route raises
    with its endpoints before the sheet is saved. Use an empty electrical intent.
    """
    top = _root_sheet()
    root, spans = top._tree()
    items = _children(root, "sheet")
    order = list(order)
    aliases = list(_sheet_files(root))
    selected = [[item for item, names in zip(items, aliases) if name in names] for name in order]
    if (not items or len(order) != len(items) or any(len(matches) != 1 for matches in selected)
            or len({id(matches[0]) for matches in selected}) != len(items)):
        raise ValueError("order must name each root sheet exactly once")
    dimensions = [_r(_child(item, "size")[1:3]) for item in items]
    width, height = (tuple(map(float, size)) if size is not None else
                     tuple(round(_math.ceil((max(axis) - 1e-6) / 1.27) * 1.27, 2)
                           for axis in zip(*dimensions)))
    gap = float(gap)
    if any(not _math.isfinite(n) or n <= 0 or abs(_snap(n) - n) > 1e-6 for n in (width, height, gap)):
        raise ValueError("sheet size and gap must be positive multiples of 1.27 mm")
    width, height, gap = map(_snap, (width, height, gap))
    page = top._page_size(root)
    if page is None:
        raise ValueError("unknown root page size")
    title = top._title_block(root)
    bottom = title[1] if title else page[1] - 10
    row_width = len(items) * width + (len(items) - 1) * gap
    if row_width > page[0] - 20 or height > bottom - 20:
        raise ValueError("the row does not fit on the root page above its title block")
    x0, y = _snap((page[0] - row_width) / 2), _snap((10 + bottom - height) / 2)
    edits, moves, positions = [], [], {}
    for index, (name, (item,)) in enumerate(zip(order, selected)):
        position = _r((x0 + index * (width + gap), y))
        moves.extend(_sheet_row_item(item, position, (width, height)))
        edits.append((*spans[id(item)], _sexp(item)))
        positions[name] = position
    directions = {}
    for _, new, direction, name in moves:
        if new in directions:
            raise ValueError("arranging sheets would overlap pin " + name)
        directions[new] = direction
    harness_edits, harnesses = _root_harnesses(top, root, spans, moves)
    for first, last, replacement in sorted(edits + harness_edits, reverse=True):
        top.text = top.text[:first] + replacement + top.text[last:]
    scene = top._scene()
    stationary = {point for _, point, _, _ in scene["pins"]} | {item["at"] for item in scene["labels"]}
    untouched = [[_r(p[1:3]) for p in _children(_child(n, "pts"), "xy")]
                 for kind in ("wire", "bus") for n in _children(scene["root"], kind)]
    for old, new, _, name in moves:
        if old != new and (new in stationary or any(_on(new, a, b) for a, b in untouched)):
            raise ValueError("arranging sheets would put pin %s on existing wiring at %s" % (name, new))
    for kind, terminals, labels in sorted(harnesses, key=lambda group: (-len(group[1]), group[0])):
        _root_harness(top, kind, terminals, labels, directions, (10, 10, page[0] - 10, bottom))
    top.save()
    return {"positions": positions, "size": (width, height)}


def _sheet_files(root):
    for item in _children(root, "sheet"):
        props = {_unq(p[1]).lower(): _unq(p[2]) for p in _children(item, "property")}
        yield props["sheetfile"], props["sheetname"], _unq(_child(item, "uuid")[1])


def _hierarchy():
    """All instance paths by file. Report cycles and ambiguous reused sheets to the caller."""
    top = _root_sheet()
    paths = {}
    pages = set()
    def visit(s, path, parents):
        file = _os.path.realpath(s.path)
        if file in parents:
            raise ValueError("cyclic sheet hierarchy at " + file)
        paths.setdefault(file, []).append(path)
        tree, _ = s._tree()
        pages.update(_unq(p[1]) for p in _walk(tree) if _head(p) == "page")
        for child, _, uid in _sheet_files(tree):
            visit(sheet(_os.path.join(_os.path.dirname(file), child)), path + "/" + uid, parents | {file})
    tree, _ = top._tree()
    visit(top, "/" + _unq(_child(tree, "uuid")[1]), set())
    return paths, pages


def _prepare_sheet(parent, paths, pages, name, file, at=None, template=None):
    if not name or file != _os.path.basename(file) or not file.endswith(".kicad_sch"):
        raise ValueError("give a sheet name and a new .kicad_sch filename")
    root, _ = parent._tree()
    if any(n == name for _, n, _ in _sheet_files(root)):
        raise ValueError("sheet name already in use: " + name)
    page = next(str(i) for i in range(2, len(pages) + 3) if str(i) not in pages)
    x, y = at or (25.4, 25.4 + 20.32 * len(_children(root, "sheet")))
    uid = str(_uuid.uuid4())
    parent_path = paths[_os.path.realpath(parent.path)][0]
    project = _os.path.splitext(_os.path.basename(BOARD_PATH))[0]
    block = ('(sheet (at %g %g) (size 25.4 12.7) (stroke (width 0.1524) (type solid)) '
             '(fill (color 0 0 0 0)) (uuid %s) '
             '(property "Sheetname" %s (at %g %g 0) (effects (font (size 1.27 1.27)) (justify left bottom))) '
             '(property "Sheetfile" %s (at %g %g 0) (effects (font (size 1.27 1.27)) (justify left top))) '
             '(instances (project %s (path %s (page %s)))))' %
             (x, y, _q(uid), _q(name), x, y, _q(file), x, y+12.7, _q(project), _q(parent_path), _q(page)))
    child = Sheet.__new__(Sheet)
    child.path = _os.path.join(_os.path.dirname(parent.path), file)
    if _os.path.lexists(child.path):
        raise ValueError("sheet file already exists: " + file)
    child.text = '(kicad_sch %s (generator "eeschema") (uuid %s) %s (lib_symbols))\n' % (
        _header(root, "version"), _q(str(_uuid.uuid4())), _header(template or root, "paper"))
    return child, block, parent_path + "/" + uid


def _save_new_sheet(parent, child, block):
    _sx(child.text)
    parent._append(block)
    _sx(parent.text)
    with open(child.path, "x") as handle:
        handle.write(child.text)
    parent.save()


def new_sheet(name, file, at=None):
    """Create a child of the project root and return its saved Sheet."""
    top = _root_sheet()
    paths, pages = _hierarchy()
    child, block, _ = _prepare_sheet(top, paths, pages, name, file, at)
    _save_new_sheet(top, child, block)
    return child


def _geometry(item, s, libs):
    at = _child(item, "at")
    if _head(item) == "symbol":
        return [_r(at[1:3]), *s._symbol_pins(item, libs).values()]
    coords = [_r(p[1:3]) for p in _walk(item) if _head(p) in ("xy", "start", "mid", "end", "center")]
    if coords:
        return coords
    if at:
        x, y = _r(at[1:3])
        size = _child(item, "size")
        return [(x, y), (x + float(size[1]), y + float(size[2]))] if size else [(x, y)]
    return []


def _hits_box(a, b, box):
    # Clip the parametric segment against both intervals.
    lo, hi = 0., 1.
    for start, end, low, high in zip(a, b, box[:2], box[2:]):
        if end == start:
            if not low <= start <= high:
                return False
        else:
            p, q = sorted(((low-start)/(end-start), (high-start)/(end-start)))
            lo, hi = max(lo, p), min(hi, q)
    return lo <= hi


def _region(s, box):
    root, spans = s._tree()
    libs = s._libs(root)
    inside, crossing = [], []
    local_labels = {}
    for item in root:
        if not isinstance(item, list) or _head(item) in ("lib_symbols", "title_block", "sheet_instances"):
            continue
        points = _geometry(item, s, libs)
        if not points:
            continue
        selected = all(s._inside(p, box) for p in points)
        if _head(item) == "label":
            local_labels.setdefault(_unq(item[1]), {}).setdefault(selected, []).append(points[0])
        if selected:
            if _head(item) == "sheet":
                raise ValueError("region includes a hierarchical sheet")
            inside.append((item, spans[id(item)]))
        elif any(_hits_box(a, b, box) for a, b in zip(points, points[1:])):
            crossing.append({"kind": _head(item), "points": points})
    for name, locations in local_labels.items():
        if True in locations and False in locations:
            crossing.append({"kind": "shared local label", "name": name,
                             "inside": locations[True], "outside": locations[False]})
    return inside, crossing


def move_region(source, destination, area):
    """Move items wholly in area [x0, y0, x1, y1] mm to an existing unique sheet instance.

    Save both sheets and return references (list), items (count) and
    crossings (wire geometry and shared local labels). Crossing items
    stay on the source.
    """
    src, dst = sheet(source), sheet(destination)
    if _os.path.realpath(src.path) == _os.path.realpath(dst.path):
        raise ValueError("source and destination are the same sheet")
    paths, _ = _hierarchy()
    old, new = (paths.get(_os.path.realpath(s.path), []) for s in (src, dst))
    if len(old) != 1 or len(new) != 1:
        raise ValueError("move needs a single hierarchy instance of each sheet")
    selected, crossing = _region(src, area)
    report = _transfer_region(src, dst, old[0], new[0], selected, crossing)
    dst.save()
    src.save()
    return report


def _transfer_region(src, dst, old_path, new_path, selected, crossing):
    sr, _ = src._tree()
    dr, spans = dst._tree()
    source_libs, dest_libs = src._libs(sr), dst._libs(dr)
    moved, imports, blocks = [], {}, []
    for item, span in selected:
        if _head(item) == "symbol":
            ref = src._ref(item)
            moved.append(ref)
            lib = _unq(_child(item, "lib_id")[1])
            if lib in dest_libs and dest_libs[lib] != source_libs[lib]:
                raise ValueError("different cached definitions of " + lib)
            if lib not in dest_libs:
                imports[lib] = source_libs[lib]
            entries = [n for n in _walk(_child(item, "instances") or []) if _head(n) == "path"]
            if len(entries) != 1 or _unq(entries[0][1]) != old_path:
                raise ValueError("ambiguous instance path for " + ref)
            block = src.text[span[0]:span[1]]
            blocks.append(_re.sub(r'(\(path\s+)' + _re.escape(entries[0][1]) + r'(?=\s|\))',
                                  lambda m: m[1] + _q(new_path), block, count=1))
        else:
            blocks.append(src.text[span[0]:span[1]])
    end = spans[id(_child(dr, "lib_symbols"))][1]-1
    dst.text = dst.text[:end] + "\n".join(map(_sexp, imports.values())) + dst.text[end:]
    for block in blocks:
        dst._append(block)
    src._cut([span for _, span in selected])
    return {"references": moved, "items": len(blocks), "crossings": crossing}


def split_sheet(source_file, region, new_name, new_file, keep="remaining"):
    """Split one unique sheet instance and save both files.

    The region is [x0, y0, x1, y1] in mm. With keep="remaining", the new
    file takes the selected region. With keep="moved", the source file
    and sheet symbol retain the selected region and its instance path;
    the new file takes the other items. A child splits into siblings.
    Return references and item count transferred to the new path, plus
    crossings (wires and shared local labels at the split). Resolve
    crossings in the same script before asking the edit gate to keep it.
    """
    if keep not in ("moved", "remaining"):
        raise ValueError('keep must be "moved" or "remaining"')
    src = sheet(source_file)
    paths, pages = _hierarchy()
    instances = paths.get(_os.path.realpath(src.path), [])
    if len(instances) != 1:
        raise ValueError("split needs a single hierarchy instance of the source sheet")
    old_path = instances[0]
    parent_path = old_path.rsplit("/", 1)[0]
    parent = src
    if parent_path:
        parent_file = next(file for file, entries in paths.items() if parent_path in entries)
        if len(paths[parent_file]) != 1:
            raise ValueError("split needs a single hierarchy instance of the parent sheet")
        parent = sheet(parent_file)
    selected, crossings = _region(src, region)
    if not selected:
        raise ValueError("no items inside the split region")
    root, spans = src._tree()
    if keep == "moved":
        inside = {span[0] for _, span in selected}
        libs = src._libs(root)
        # Drawing objects outside the region belong to the new file. Headers
        # and embedded data are not positioned objects.
        selected = [(item, spans[id(item)]) for item in root if isinstance(item, list)
                    and _head(item) not in ("lib_symbols", "title_block", "sheet_instances")
                    and spans[id(item)][0] not in inside and _geometry(item, src, libs)]
    if any(_head(item) == "sheet" for item, _ in selected):
        raise ValueError("split would move a nested sheet instance")
    tree, _ = parent._tree()
    source_symbol = next((node for node in _children(tree, "sheet")
                          if _unq(_child(node, "uuid")[1]) == old_path.rsplit("/", 1)[1]), None)
    interface = {_unq(pin[1]) for pin in _children(source_symbol, "pin")} if source_symbol else set()
    if any(_head(item) == "hierarchical_label" and _unq(item[1]) in interface for item, _ in selected):
        raise ValueError("split would move a hierarchical label away from its parent sheet pin")
    dst, block, new_path = _prepare_sheet(parent, paths, pages, new_name, new_file, template=root)
    report = _transfer_region(src, dst, old_path, new_path, selected, crossings)
    _save_new_sheet(parent, dst, block)
    if src is not parent:
        src.save()
    return report


def copy_layout(file, ref_map, area, offset, target_area=None):
    """Replace twin geometry in a translated rectangle, retaining target identities and fields.

    ref_map pairs source and target references. area and optional target_area
    are [x0, y0, x1, y1] mm. offset is (dx, dy) mm. Save the sheet and return
    references (the map) and items (count).
    """
    import copy
    s = sheet(file)
    dx, dy = offset
    target = target_area or [area[0] + dx, area[1] + dy, area[2] + dx, area[3] + dy]
    if not (target[2] < area[0] or area[2] < target[0] or target[3] < area[1] or area[3] < target[1]):
        raise ValueError("source and target blocks overlap")
    source, cut1 = _region(s, area)
    dest, cut2 = _region(s, target)
    if cut1 or cut2:
        raise ValueError("items cross the block boundary: %s" % (cut1+cut2))
    # The twins may have different supply notes and tables. Copy electrical layout only.
    kinds = ("wire", "junction", "no_connect", "symbol")
    source = [(item, span) for item, span in source if _head(item) in kinds]
    dest = [(item, span) for item, span in dest if _head(item) in kinds]
    twins = {s._ref(item): item for item, _ in dest if _head(item) == "symbol"}
    refs = [s._ref(item) for item, _ in source if _head(item) == "symbol"]
    dest_refs = [s._ref(item) for item, _ in dest if _head(item) == "symbol"]
    if (set(ref_map) != set(refs) or len(set(ref_map.values())) != len(refs)
            or set(ref_map.values()) != set(dest_refs) or len(twins) != len(dest_refs)):
        raise ValueError("map every symbol in both blocks, including power symbols, one to one")
    blocks = []
    for item, _ in source:
        layout = copy.deepcopy(item)
        _translate_layout(layout, dx, dy, renew_ids=True)
        if _head(layout) == "symbol":
            twin = twins[ref_map[s._ref(layout)]]
            _apply_symbol_layout(twin, layout)
            layout = twin
        blocks.append(_sexp(layout))
    s._cut([span for _, span in dest])
    for block in blocks:
        s._append(block)
    s.save()
    return {"references": dict(ref_map), "items": len(blocks)}

def help(name):
    """Return a documented schematic helper's full docstring by name."""
    helpers = {helper.__name__: helper for helper in
               (new_sheet, move_region, split_sheet, copy_layout, sheet, sheets, help, arrange_root,
                block_grid, place_in_slot, mirror_block, info_table, place_design_block)}
    helpers["route"] = _route_schematic
    helpers.update({"Sheet." + method.__name__: method for method in
                    (Sheet.symbols, Sheet.symbol, Sheet.wires, Sheet.labels, Sheet.place_power, Sheet.upright)})
    if name not in helpers:
        raise ValueError("unknown schematic helper %s; choose from %s" % (name, ", ".join(sorted(helpers))))
    return helpers[name].__doc__


# ---- end of schematic helpers ----
'''

__all__ = ["SCHEMATIC_HELPERS"]
