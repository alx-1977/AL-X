"""FireFli pin-to-wire and junction rules over KiCad's s-expression reader."""
from collections import defaultdict
from math import cos, radians, sin


def _first(node, key):
    return next(iter(node.named(key)), None)


def _point(node):
    return tuple(round(float(v), 2) for v in node.children[1:3])


def violations(path):
    """Identify violations by rule, affected pins and sheet-relative location."""
    from alx.providers.kicad import sexpr
    if not path.exists():
        return set()
    root = sexpr(path.read_text())
    pins, segments = defaultdict(list), []
    libs = {s.children[1]: s for h in root.named('lib_symbols') for s in h.named('symbol')}
    for symbol in root.named('symbol'):
        ref = next(p.children[2] for p in symbol.named('property') if p.children[1] == 'Reference')
        at = _first(symbol, 'at').children
        x, y = map(float, at[1:3])
        angle = radians(float(at[3]))
        mirror = _first(symbol, 'mirror')
        axis = mirror.children[1] if mirror else ''
        unit = _first(symbol, 'unit')
        style = _first(symbol, 'body_style') or _first(symbol, 'convert')
        lib = libs[_first(symbol, 'lib_id').children[1]]
        for drawing in lib.named('symbol'):
            _, u, variant = drawing.children[1].rsplit('_', 2)
            if u not in ('0', unit.children[1] if unit else '1') or variant not in (
                    '0', style.children[1] if style else '1'):
                continue
            for pin in drawing.named('pin'):
                a, b = _point(_first(pin, 'at'))
                if axis == 'x':
                    b = -b
                if axis == 'y':
                    a = -a
                pt = round(x + a*cos(angle) - b*sin(angle), 2), round(y - a*sin(angle) - b*cos(angle), 2)
                pins[pt].append(f"{ref}.{_first(pin, 'number').children[1]}")
    for wire in root.named('wire'):
        pts = [_point(n) for n in _first(wire, 'pts').named('xy')]
        segments.extend(zip(pts, pts[1:]))
    ends = {point for segment in segments for point in segment}
    dots = {_point(_first(n, 'at')) for n in root.named('junction')}
    nc = {_point(_first(n, 'at')) for n in root.named('no_connect')}

    def on_middle(point):
        x, y = point
        return any((a[0] == b[0] == x and min(a[1], b[1]) < y < max(a[1], b[1]))
                   or (a[1] == b[1] == y and min(a[0], b[0]) < x < max(a[0], b[0]))
                   for a, b in segments)

    found = set()
    for point, names in pins.items():
        if point in nc:
            continue
        if len(names) > 1:
            kind = 'power symbol on pin' if any(n.startswith('#') for n in names) else 'pin-to-pin'
        elif point not in ends:
            kind = 'pin on middle of a wire' if on_middle(point) else 'pin not wired'
        else:
            continue
        found.add((kind, *point, tuple(sorted(names))))
    found.update(('T-join without junction', *point, ('wire',)) for point in ends - dots if on_middle(point))
    return found
