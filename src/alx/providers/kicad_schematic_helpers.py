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


def _on(point, a, b):
    """The point lies on the wire a-b, ends included."""
    (x, y), (x1, y1), (x2, y2) = point, a, b
    if abs((x2 - x1) * (y - y1) - (y2 - y1) * (x - x1)) > 1e-6:
        return False
    return (min(x1, x2) - 1e-6 <= x <= max(x1, x2) + 1e-6
            and min(y1, y2) - 1e-6 <= y <= max(y1, y2) + 1e-6)


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

    def _placed_symbol(self, ref):
        root, spans = self._tree()
        libs = self._libs(root)
        found = [s for s in _children(root, "symbol") if self._ref(s) == ref]
        if not found:
            raise KeyError("no symbol %s on %s" % (ref, _os.path.basename(self.path)))
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

    def pin_stub(self, ref, pin, length=2.54):
        """A wire straight out of the pin, in its own direction, one grid step by default; its end."""
        start = self.symbol(ref)["pins"][str(pin)]
        dx, dy = self.pin_direction(ref, pin)
        end = _r((start[0] + dx * length, start[1] + dy * length))
        self.add_wire(start, end)
        return end

    def wire_pin_to(self, ref, pin, target, length=2.54):
        """From a pin, straight out one step, then on to target with one turn. Returns the points."""
        end = self.pin_stub(ref, pin, length)
        dx, _ = self.pin_direction(ref, pin)
        target = _r(target)
        # The turn after the stub goes across the pin's direction, never back over the part.
        corner = (end[0], target[1]) if dx else (target[0], end[1])
        points = [end] + [p for p in (corner, target) if p != end]
        if len(points) > 1:
            self.add_wire(*points)
        return [self.symbol(ref)["pins"][str(pin)]] + points

    def label_pin(self, ref, pin, name, kind="label", length=2.54):
        """A net label at the end of a short wire straight out of the pin."""
        end = self.pin_stub(ref, pin, length)
        dx, dy = self.pin_direction(ref, pin)
        angle = {(1, 0): 0, (-1, 0): 180, (0, -1): 90, (0, 1): 270}[(dx, dy)]
        self.add_label(name, end, angle, kind)
        return end

    def _port_pin(self, ref, pin, template, down):
        end = self.pin_stub(ref, pin)
        dx, dy = self.pin_direction(ref, pin)
        want = 1 if down else -1
        if (dx, dy) != (0, want):
            # Out of the pin first, then turn toward where the port points.
            turned = _r((end[0], end[1] + want * 2.54))
            self.add_wire(end, turned)
            end = turned
        self.copy_symbol(template, end, 0)
        return end

    def ground_pin(self, ref, pin, template):
        """A GND port, pointing down, on a short wire from the pin. template: the reference (#PWR..) of a GND symbol already on this sheet, which is copied."""
        return self._port_pin(ref, pin, template, down=True)

    def power_pin(self, ref, pin, template):
        """A power port, pointing up, on a short wire from the pin. template: that port's reference."""
        return self._port_pin(ref, pin, template, down=False)

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
        if bottom - top >= right - left:
            spots = {"Reference": (right + 1.27, cy - 1.27, "left"), "Value": (right + 1.27, cy + 1.27, "left")}
        else:
            spots = {"Reference": (cx, top - 1.27, ""), "Value": (cx, bottom + 2.54, "")}
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
            prop = _re.sub(r'\(at [-\d.]+ [-\d.]+(?: [-\d.]+)?\)', "(at %g %g 0)" % (round(x, 2), round(y, 2)),
                           prop, count=1)
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
        raise KeyError("no symbol %s on %s" % (ref, _os.path.basename(self.path)))

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
            raise KeyError("no symbol %s on %s" % (ref, _os.path.basename(self.path)))
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
            raise KeyError("no symbol %s on %s" % (ref, _os.path.basename(self.path)))
        symbol = found[0]
        ref = self._ref(symbol)
        if not ref.startswith("#"):
            raise ValueError("only power and ground symbols (#PWR...) are copied; a part needs its own reference")
        start, end = spans[id(symbol)]
        block = self.text[start:end]
        old = _child(symbol, "at")
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
