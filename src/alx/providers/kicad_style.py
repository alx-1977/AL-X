"""FireFli schematic rules over KiCad's drawing and exported netlist."""
from collections import defaultdict
from itertools import combinations
import json
from math import cos, radians, sin

from alx.providers.kicad_schematic_helpers import GRID_SUFFIX


def _first(node, key):
    return next(iter(node.named(key)), None)


def _point(node):
    return tuple(round(float(v), 2) for v in node.children[1:3])


def _pins(root):
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
        lib = libs.get(_first(symbol, 'lib_id').children[1])
        if lib is None:
            continue
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
                yield ref, _first(pin, 'number').children[1], pt, _first(symbol, 'uuid')


def _segments(root, kind):
    for wire in root.named(kind):
        pts = [_point(n) for n in _first(wire, 'pts').named('xy')]
        yield from zip(pts, pts[1:])


def violations(path):
    """Identify violations by rule, affected pins and sheet-relative location."""
    from alx.providers.kicad import sexpr
    if not path.exists():
        return set()
    root = sexpr(path.read_text())
    pins = defaultdict(list)
    for ref, pin, point, _ in _pins(root):
        pins[point].append(f'{ref}.{pin}')
    segments = list(_segments(root, 'wire'))
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


def _on(point, a, b):
    x, y = point
    x0, y0 = a
    x1, y1 = b
    return (min(x0, x1) <= x <= max(x0, x1) and min(y0, y1) <= y <= max(y0, y1)
            and abs((x - x0) * (y1 - y0) - (y - y0) * (x1 - x0)) < 1e-6)


def _wire_groups(root, points):
    """Physical wire groups, with crosses connected only at explicit junctions."""
    wires = list(_segments(root, 'wire'))
    incident = defaultdict(set)
    for index, wire in enumerate(wires):
        for point in wire:
            incident[point].add(index)
    for dot in root.named('junction'):
        p = _point(_first(dot, 'at'))
        incident[p].update(i for i, (a, b) in enumerate(wires) if _on(p, a, b))
    joins = defaultdict(set)
    for group in incident.values():
        for member in group:
            joins[member].update(group)
    groups = {}
    for index in range(len(wires)):
        if index in groups:
            continue
        groups[index] = index
        todo = [index]
        while todo:
            for neighbor in joins[todo.pop()]:
                if neighbor not in groups:
                    groups[neighbor] = index
                    todo.append(neighbor)
    # Coordinate tokens also associate a label with a bare pin at that spot.
    return {p: {p} | {groups[i] for i, (a, b) in enumerate(wires) if _on(p, a, b)} for p in points}


def _port_points(root, pins_and_labels):
    buses = list(_segments(root, 'bus'))
    ports = {_point(_first(label, 'at')) for label in root.named('hierarchical_label')}
    ports.update(_point(_first(pin, 'at')) for sheet in root.named('sheet') for pin in sheet.named('pin'))
    for entry in root.named('bus_entry'):
        a, size = _point(_first(entry, 'at')), _point(_first(entry, 'size'))
        ports.update((a, tuple(round(x + d, 2) for x, d in zip(a, size))))
    wire_ends = {p for wire in _segments(root, 'wire') for p in wire}
    ports.update(p for p in wire_ends | pins_and_labels if any(_on(p, a, b) for a, b in buses))
    return ports


def label_violations(path, model):
    """Newness is measured by exported net and joined pins; positions are evidence."""
    from alx.providers.kicad import sexpr
    if not model or not path.is_file():
        return {}
    root = sexpr(path.read_text())
    labels = defaultdict(list)
    for item in root.named('label'):
        labels[item.children[1]].append(_point(_first(item, 'at')))
    if not labels:
        return {}
    pins = defaultdict(set)
    for ref, number, point, uid in _pins(root):
        if uid:
            pins[uid.children[1], number].add(point)
    components = {c['ref']: c for c in model['components']}
    all_points = {p for sites in (*pins.values(), *labels.values()) for p in sites}
    ports = _port_points(root, all_points)
    groups = _wire_groups(root, all_points | ports)
    port_groups = {g for p in ports for g in groups[p]}
    label_groups = {name: {g for p in sites for g in groups[p]} for name, sites in labels.items()}
    findings = {}
    for net in model['nets']:
        name = net['name']
        local = name.rsplit('/', 1)[-1]
        if len(net['nodes']) < 2:
            continue
        instances, members, pin_groups = set(), [], set()
        for node in net['nodes']:
            component = components.get(node['ref'])
            if component is None:
                break
            path_name, _, identifiers = component['path'].rpartition('/')
            candidates = [p for uid in identifiers.split() for p in pins[uid, node['pin']]]
            if not candidates:
                break
            pin_groups.update(g for p in candidates for g in groups[p])
            instances.add(path_name)
            members.append(f'{node["ref"]}.{node["pin"]}')
        else:
            names = {n for n, group in label_groups.items() if group & pin_groups}
            # The exported name can identify a label on an otherwise pinless
            # island. Physical groups also find aliases KiCad did not export.
            names.update(n for n in (name, local) if n in labels)
            connected = pin_groups | {g for n in names for g in label_groups[n]}
            if len(instances) == 1 and names and not connected & port_groups:
                positions = sorted({p for label in names for p in labels[label]})
                members.sort()
                findings[name, tuple(members)] = {'rule': 'label-joined pins', 'net': name, 'pins': members,
                                                    'positions': positions, 'labels': sorted(names)}
    return findings


def placement_warnings(path):
    """Optional drawing guidance. A block grid does not constrain free placement."""
    from alx.providers.kicad import sexpr
    grid_path = path.with_name(path.name + GRID_SUFFIX)
    if not path.is_file() or not grid_path.is_file():
        return set()
    blocks = json.loads(grid_path.read_text())
    root = sexpr(path.read_text())
    parts = [(next(p.children[2] for p in node.named('property') if p.children[1] == 'Reference'),
              _point(_first(node, 'at'))) for node in root.named('symbol')]
    parts = [(ref, p) for ref, p in parts if not ref.startswith('#')]
    tables = defaultdict(list)
    bounds = {}
    warnings = set()
    for name, grid in blocks.items():
        cols, rows = grid['columns'], grid['rows']
        xs, ys = set(cols.values()), set(rows.values())
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        bounds[name] = left, right, top, bottom
        signal = [y for r, y in rows.items() if r not in ('rail', 'ground')]
        by_row = defaultdict(list)
        for ref, (x, y) in parts:
            if left <= x <= right and top <= y <= bottom:
                if x not in xs or y not in ys:
                    warnings.add(f'{name}: {ref} at ({x}, {y}) is off the block slots')
                if signal:
                    nearest = min(signal, key=lambda row: abs(row - y))
                    if abs(nearest - y) < 1.27:
                        by_row[nearest].append((ref, x, y))
        for row in by_row.values():
            for (a, ax, ay), (b, bx, by) in combinations(sorted(row), 2):
                if ax != bx and 0 < abs(round(ay - by, 2)) < 1.27:
                    warnings.add(f'{name}: {a} and {b} are almost aligned at y={ay} and y={by}')
    # Table cells have horizontal ruling at both edges of their row. Exclude
    # those before counting loose Key: value text under the nearest block.
    rules = [(a, b) for a, b in _segments(root, 'polyline') if a[1] == b[1]]
    for item in root.named('text'):
        key, sep, value = item.children[1].partition(':')
        if not (sep and key.strip() and value.strip() and len(key) <= 32 and '\n' not in key):
            continue
        x, y = _point(_first(item, 'at'))
        candidates = [(bottom, name) for name, (left, right, _, bottom) in bounds.items()
                      if left <= x <= right and y > bottom]
        if not candidates:
            continue
        if any(left <= x <= right and top <= y <= bottom for left, right, top, bottom in bounds.values()):
            continue
        above = any(0 < y-a[1] < 5.08 and min(a[0], b[0]) <= x <= max(a[0], b[0]) for a, b in rules)
        below = any(0 < a[1]-y < 5.08 and min(a[0], b[0]) <= x <= max(a[0], b[0]) for a, b in rules)
        if not (above and below):
            tables[max(candidates)[1], x].append(y)
    for (name, x), lines in tables.items():
        if len(lines) > 2:
            warnings.add(f'{name}: use info_table for {len(lines)} loose lines at x={x}')
    return warnings
