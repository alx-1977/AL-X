"""FireFli schematic rules over KiCad's drawing and exported netlist."""
from collections import defaultdict
from itertools import combinations
import json

from alx.providers.kicad_schematic_helpers import (
    GRID_SUFFIX, orientation_findings, paper_size, placed_point, point_on_segment as _on, segment_groups,
)


def _first(node, key):
    return next(iter(node.named(key)), None)


def _point(node):
    return tuple(round(float(v), 2) for v in node.children[1:3])


def _pins(root):
    libs = {s.children[1]: s for h in root.named('lib_symbols') for s in h.named('symbol')}
    for symbol in root.named('symbol'):
        ref = next(p.children[2] for p in symbol.named('property') if p.children[1] == 'Reference')
        at = _first(symbol, 'at').children
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
                a, b = map(float, _first(pin, 'at').children[1:3])
                pt = placed_point(at, axis, a, b)
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


def _wire_groups(root, points):
    """Physical wire groups, with crosses connected only at explicit junctions."""
    wires = list(_segments(root, 'wire'))
    groups = segment_groups(wires, (_point(_first(dot, 'at')) for dot in root.named('junction')))
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


def _grid_bounds(path):
    sidecar = path.with_name(path.name + GRID_SUFFIX)
    if not sidecar.is_file():
        return {}
    return {name: (min(grid['columns'].values()), max(grid['columns'].values()),
                   min(grid['rows'].values()), max(grid['rows'].values()))
            for name, grid in json.loads(sidecar.read_text()).items()}


def _block_lookup(path, root):
    """Grid rectangles take precedence. Title rows divide the remaining page into bands."""
    paper = _first(root, 'paper')
    size = paper_size(paper.children[1:]) if paper else None
    grids = {name: bounds for name, bounds in _grid_bounds(path).items()
             if bounds[0] < bounds[1] and bounds[2] < bounds[3]}
    underlines = set()
    for line in root.named('polyline'):
        points = sorted(_point(p) for p in _first(line, 'pts').named('xy'))
        stroke = _first(line, 'stroke')
        kind = _first(stroke, 'type') if stroke else None
        if len(points) == 2 and points[0][1] == points[1][1] and kind and kind.children[1] == 'solid':
            underlines.add(points[0])
    rows = []
    for item in root.named('text'):
        effects = _first(item, 'effects')
        font = _first(effects, 'font') if effects else None
        bold = _first(font, 'bold') if font else None
        if bold is None or 'no' in bold.children[1:]:
            continue
        x, y = _point(_first(item, 'at'))
        for left, line_y in underlines:
            if abs(left - x) <= 0.05 and 0 < line_y - y <= 2.54:
                rows.append((line_y, left, item.children[1]))
    bands = []
    for y, x, name in sorted(set(rows)):
        if not bands or y - bands[-1][0] > 2.54:
            bands.append((y, []))
        bands[-1][1].append((x, y, name))

    def locate(point):
        x, y = point
        if size and not (0 <= x <= size[0] and 0 <= y <= size[1]):
            return None
        explicit = [name for name, (left, right, top, bottom) in grids.items()
                    if left <= x <= right and top <= y <= bottom]
        if explicit:
            return ('grid', explicit[0]) if len(explicit) == 1 else None
        row = next((titles for top, titles in reversed(bands) if top <= y), ())
        title = max((title for title in row if title[0] <= x and title[1] <= y), default=None)
        return ('title', title) if title is not None else None

    return (locate, bool(grids or bands))


def orientation_violations(path):
    """Key by instance UUID and rule so moving a pre-existing issue does not reintroduce it."""
    if not path.is_file():
        return {}
    return {(uid, rule): {'ref': ref, 'rule': rule, 'at': point}
            for uid, ref, rule, point in orientation_findings(path.read_text())}


def label_violations(path, model):
    """Find within-block local joins, or advisory joins on a sheet without detectable blocks."""
    from alx.providers.kicad import sexpr
    if not model or not path.is_file():
        return {}
    root = sexpr(path.read_text())
    labels = defaultdict(list)
    for item in root.named('label'):
        labels[item.children[1]].append(_point(_first(item, 'at')))
    if not labels:
        return {}
    block_at, has_blocks = _block_lookup(path, root)
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
        instances, members, physical, pin_blocks = set(), [], [], set()
        for node in net['nodes']:
            component = components.get(node['ref'])
            if component is None:
                break
            path_name, _, identifiers = component['path'].rpartition('/')
            candidates = [p for uid in identifiers.split() for p in pins[uid, node['pin']]]
            if not candidates:
                break
            physical.append({g for p in candidates for g in groups[p]})
            pin_blocks.update(block_at(p) for p in candidates)
            instances.add(path_name)
            members.append(f'{node["ref"]}.{node["pin"]}')
        else:
            if not physical or set.intersection(*physical):
                continue
            pin_groups = set.union(*physical)
            names = {n for n, group in label_groups.items() if group & pin_groups}
            # The exported name can identify a label on an otherwise pinless
            # island. Physical groups also find aliases KiCad did not export.
            names.update(n for n in (name, local) if n in labels)
            connected = pin_groups | {g for n in names for g in label_groups[n]}
            inside_one = len(pin_blocks) == 1 and None not in pin_blocks
            if (len(instances) == 1 and names and not connected & port_groups
                    and (inside_one or not has_blocks)):
                positions = sorted({p for label in names for p in labels[label]})
                members.sort()
                issue = {'rule': 'label-joined pins', 'net': name, 'pins': members,
                         'positions': positions, 'labels': sorted(names), 'warning': not has_blocks}
                if inside_one:
                    kind, block = next(iter(pin_blocks))
                    issue['block'] = block if kind == 'grid' else block[2]
                findings[name, tuple(members)] = issue
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
    bounds = _grid_bounds(path)
    warnings = set()
    for name, grid in blocks.items():
        cols, rows = grid['columns'], grid['rows']
        xs, ys = set(cols.values()), set(rows.values())
        left, right, top, bottom = bounds[name]
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
