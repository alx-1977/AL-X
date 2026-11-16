"""Schematic edits for her KiCad scripts (D-082): KiCad has no schematic Python API.

Text edits of .kicad_sch files in the work copy, kept as KiCad wrote them
except where changed. Ported from the P0009 switch swap of 14 Nov, where each
of these was done by hand. Runs inside the confined KiCad Python (3.9) with the
board helpers; it reads only files in the copy.
"""

SCHEMATIC_HELPERS = r'''
# ---- schematic helpers (text edits of .kicad_sch; KiCad has no schematic API) ----
import math as _math
import os as _os
import re as _re
import uuid as _uuid

_TOKEN = _re.compile(r'\s*(\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+)')
# Library symbols the edit named (symbols: ["FireFli_ICs:RFSW6024TR13"]), copied
# in by the tool: the script cannot read his library itself.
_LIBRARY_SYMBOLS = {}


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


def _lib_outline(symbol):
    """Every drawn point of a library symbol (its body), its own coordinates (y up)."""
    points = []

    def walk(node):
        for item in node:
            if not isinstance(item, list):
                continue
            head = _head(item)
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


def _on(point, a, b):
    """The point lies on the wire a-b, ends included."""
    (x, y), (x1, y1), (x2, y2) = point, a, b
    if abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) > 1e-6:
        return False
    return (min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6
            and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6)


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
        unit = _child(symbol, "unit")
        unit = unit[1] if unit else "1"
        pins = {}
        for part in _children(lib, "symbol"):
            # Unit 0 is common to every unit; others only to their own.
            owner = _unq(part[1]).rsplit("_", 2)
            if len(owner) == 3 and owner[1] not in ("0", unit):
                continue
            for number, (x, y) in _lib_pins(part).items():
                pins[number] = _placed(at, mirror[1] if mirror else None, x, y)
        return pins

    def _ref(self, symbol):
        for item in _children(symbol, "property"):
            if _unq(item[1]) == "Reference":
                return _unq(item[2])
        return ""

    def symbols(self):
        """Every symbol: ref, lib_id, value, at (x, y), rotation, mirror and its pins {number: (x, y)}."""
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

    _PAPER = {"A5": (210, 148), "A4": (297, 210), "A3": (420, 297), "A2": (594, 420), "A1": (841, 594),
              "A0": (1189, 841), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8)}

    def _title_block(self, root):
        """The title block's area on this sheet, [x0, y0, x1, y1], from his page template
        (16 Nov, Friedl: the drawing ran into the title block). None when unknown."""
        paper = _child(root, "paper")
        if not paper:
            return None
        if _unq(paper[1]) == "User" and len(paper) >= 4:
            width, height = float(paper[2]), float(paper[3])
        elif _unq(paper[1]) in self._PAPER:
            width, height = self._PAPER[_unq(paper[1])]
        else:
            return None
        if len(paper) > 2 and _unq(paper[-1]) == "portrait":
            width, height = height, width
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
        found = [s for s in _children(root, "symbol") if self._ref(s) == ref]
        if not found:
            raise self._missing(ref)
        symbol = found[0]
        return symbol, libs.get(_unq(_child(symbol, "lib_id")[1])), spans

    def pin_direction(self, ref, pin):
        """The way a pin points out of its part on the sheet: (1, 0) right, (0, 1) down, ..."""
        symbol, lib, _ = self._placed_symbol(ref)
        angles = {}
        for part in _children(lib, "symbol"):
            angles.update(_lib_pin_angles(part))
        if str(pin) not in angles:
            raise KeyError("%s has no pin %s" % (ref, pin))
        turn = _math.radians(angles[str(pin)] + 180.0)
        at, mirror = _child(symbol, "at"), _child(symbol, "mirror")
        end = _placed(at, mirror[1] if mirror else None, _math.cos(turn), _math.sin(turn))
        origin = _placed(at, mirror[1] if mirror else None, 0.0, 0.0)
        return (round(end[0] - origin[0]), round(end[1] - origin[1]))

    def _stub_end(self, ref, pin, length=2.54):
        start = self.symbol(ref)["pins"][str(pin)]
        dx, dy = self.pin_direction(ref, pin)
        return start, _r((start[0] + dx * length, start[1] + dy * length))

    def _wired_to(self, point):
        """The wires joined to point, through ends and wires they end on: (their points, their ends)."""
        wires = [tuple(pts) for pts in self.wires()]
        reached, ends, frontier = set(), {_r(point)}, [_r(point)]
        while frontier:
            here = frontier.pop()
            for index, pts in enumerate(wires):
                if index in reached:
                    continue
                if here in (pts[0], pts[-1]) or any(_on(here, a, b) for a, b in zip(pts, pts[1:])) \
                        or any(_on(e, here, here) for e in (pts[0], pts[-1])):
                    reached.add(index)
                    for e in (pts[0], pts[-1]):
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

    def _check_segments(self, segments, allowed, same=()):
        """segments: (a, b, the pin it is for); refused at the first that touches what is there."""
        scene = self._scene()
        root, libs = scene["root"], scene["libs"]
        # Leftover wiring joined to nothing (no pin, label or port anywhere on it) is no hazard:
        # touching it joins nothing. remove_dangling clears it.
        if scene["orphans"] is None:
            scene["orphans"] = self._orphan_wires(root, libs)
        skip = set(same) | scene["orphans"]
        wires = [pts for pts in scene["wires"] if tuple(pts) not in skip]
        things = [("a wire end", p) for pts in wires for p in (pts[0], pts[-1])]
        things += [("label %s" % item["name"], item["at"]) for item in scene["labels"]]
        things += [("a no-connect", point) for point in scene["no_connects"]]
        things += [("pin %s" % name, point) for name, point, _, _ in scene["pins"] if point not in allowed]
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
            for pts in scene["wires"]:
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
            self.check_clear(ref, pin, [start] + points, allow=(target,), joined_to=target)
            self.add_wire(*_straight([start] + points))
            return [start] + points
        # Straight out of the pin to the target's line, then one turn into it: the wire leaves
        # the part's side at once (16 Nov: turning right after the stub ran the RFC wire up the
        # switch's side across where pins 1 and 2 come out). A target behind the pin turns
        # right after the stub, never back over the part.
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
            try:
                self.check_clear(ref, pin, [start] + points, allow=(target,), joined_to=target)
            except ValueError as error:
                reason = reason or error
                continue
            self.add_wire(*_straight([start] + points))
            return [start] + points
        raise reason

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
        for part in _children(lib, "symbol"):
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
                drawn = [_placed(at, mirror[1] if mirror else None, x, y) for x, y in _lib_outline(lib)]
                if drawn:
                    xs, ys = [p[0] for p in drawn], [p[1] for p in drawn]
                    parts.append((name, (min(xs), min(ys), max(xs), max(ys))))
            if power:
                ox, oy = _r(_child(symbol, "at")[1:3])
                ports.append((ox, oy))
                bodies.append(((ox, oy), (ox - 1.5, oy + 0.3, ox + 1.5, oy + 5.6) if "GND" in value.upper()
                               else (ox - 1.5, oy - 5.6, ox + 1.5, oy - 0.3)))
        scene = {"root": root, "libs": libs, "wires": self.wires(), "labels": self.labels(),
                 "no_connects": [_r(_child(n, "at")[1:3]) for n in _children(root, "no_connect")],
                 "pins": pins, "bodies": bodies, "ports": ports, "orphans": None, "parts": parts,
                 "values": values, "title_block": self._title_block(root)}
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
        counted = {}
        for a, b, _ in segments:
            self.add_wire(a, b)
            for point in (a, b):
                counted[point] = counted.get(point, 0) + 1
        for point, count in counted.items():
            if count >= 3:
                self.add_junction(point)
        for port in ports:
            # Turned as his own symbol on the sheet is turned (16 Nov: forcing the angle
            # put imported 3V3 symbols upside down).
            self.copy_symbol(template, port, None)
        return ports

    def ground_pin(self, ref, pin, template):
        """A GND port, pointing down, on a short wire from the pin. template: the reference (#PWR..) of a GND symbol already on this sheet, which is copied."""
        return self.port_pins([(ref, pin)], template, down=True)[0]

    def power_pin(self, ref, pin, template):
        """A power port, pointing up, on a short wire from the pin. template: that port's reference."""
        return self.port_pins([(ref, pin)], template, down=False)[0]

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
            self.text = self.text[:line if line >= 0 else start] + self.text[end:]

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
        self._cut([spans[id(n)] for n in cut])
        self.tidy(points)
        return counts

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
        for name, text in (("Value", value), ("Footprint", footprint)):
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
            return
        block = _LIBRARY_SYMBOLS[lib_id]
        if "(extends" in block:
            raise ValueError("%s is derived from another symbol; use the symbol it extends" % lib_id)
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

    def save(self):
        """Write the sheet back, after checking it still parses."""
        _sx(self.text)
        with open(self.path, "w") as handle:
            handle.write(self.text)


def sheet(name):
    """A schematic sheet of the copy by file name (BHL_RoomReaderV2_RFID.kicad_sch)."""
    return Sheet(name)


def sheets():
    """The schematic files of the copy."""
    root = _os.path.dirname(BOARD_PATH)
    return sorted(name for name in _os.listdir(root) if name.endswith(".kicad_sch"))
# ---- end of schematic helpers ----
'''

NAMES = ("sheet", "sheets")

__all__ = ["NAMES", "SCHEMATIC_HELPERS"]
