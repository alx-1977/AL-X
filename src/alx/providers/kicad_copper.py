"""Copper geometry and the FastHenry format, private to KiCadProvider.

Coordinates and stack-up are read from the saved board, not guessed from a
nominal FR4 stack. Board units are mm; all solver decks use metres.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

from alx.providers.kicad_circuit import AnalysisError, positive


class GeometryJudgement(ValueError):
    pass


def values(node, name):
    return node.one(name).children[1:]


def xy(node, name):
    return tuple(float(v) for v in values(node, name)[:2])


def stackup(tree, require_permittivity=True):
    """Layer thicknesses and heights; permittivity too when capacitance is solved.

    Inductance and resistance need only copper heights and thicknesses, so a
    caller that does not solve capacitance may accept a dielectric with no
    recorded permittivity; it is then reported as None.
    """
    if len(tree.named('setup')) != 1 or len(tree.one('setup').named('stackup')) != 1:
        raise GeometryJudgement('board_stackup_missing')
    entries = tree.one('setup').one('stackup').named('layer')
    result, z = [], 0.0
    for node in entries:
        thickness = node.named('thickness')
        epsilon = node.named('epsilon_r')
        name = node.children[1]
        kind = values(node, 'type')[0]
        # Silkscreen/paste have no physical dielectric role. A mask/coverlay
        # with absent thickness or permittivity cannot be silently ignored.
        if kind in ('Top Silk Screen', 'Bottom Silk Screen', 'Top Solder Paste', 'Bottom Solder Paste'):
            continue
        if not thickness:
            raise GeometryJudgement('stackup_thickness_missing:' + name)
        t = positive(float(thickness[0].children[1])) * .001
        copper = name.endswith('.Cu')
        if not copper and not epsilon and require_permittivity:
            raise GeometryJudgement('stackup_permittivity_missing:' + name)
        er = (1.0 if copper else
              positive(float(epsilon[0].children[1])) if epsilon else None)
        result.append({'name': name, 'kind': kind, 'thickness_m': t, 'epsilon_r': er,
                       'z_m': z+t/2, 'copper': copper})
        z += t
    if not any(e['copper'] for e in result):
        raise GeometryJudgement('stackup_copper_missing')
    return result


def arc_points(node, density, min_chord_mm=0.0):
    a, b, c = xy(node, 'start'), xy(node, 'mid'), xy(node, 'end')
    determinant = 2*(a[0]*(b[1]-c[1])+b[0]*(c[1]-a[1])+c[0]*(a[1]-b[1]))
    if abs(determinant) < 1e-15:
        raise GeometryJudgement('degenerate_arc')
    squares = [x*x+y*y for x, y in (a, b, c)]
    ux = (squares[0]*(b[1]-c[1])+squares[1]*(c[1]-a[1])+squares[2]*(a[1]-b[1]))/determinant
    uy = (squares[0]*(c[0]-b[0])+squares[1]*(a[0]-c[0])+squares[2]*(b[0]-a[0]))/determinant
    angles = [math.atan2(p[1]-uy, p[0]-ux) for p in (a, b, c)]
    sweep = (angles[2]-angles[0]) % (2*math.pi)
    if (angles[1]-angles[0]) % (2*math.pi) > sweep:
        sweep -= 2*math.pi
    radius = math.dist(a, (ux, uy))
    count = max(2, math.ceil(abs(sweep)*density*4))
    if min_chord_mm > 0:
        # Chords shorter than the track is wide overlap each other at a tight
        # bend, and FastHenry's overlapping filaments return NaN. The most
        # chords whose true length (2 r sin(step/2)) is at least the width;
        # one chord when even two would be too short. Independent of density:
        # the finer mesh refines the cross-section instead.
        if min_chord_mm >= 2*radius:
            longest = 1
        else:
            longest = max(1, math.floor(abs(sweep)/(2*math.asin(min_chord_mm/(2*radius)))))
        count = max(1, min(count, longest))
    return [a] + [(ux+radius*math.cos(angles[0]+sweep*i/count),
                   uy+radius*math.sin(angles[0]+sweep*i/count)) for i in range(1, count)] + [c]


def key(point):
    return tuple(round(v, 9) for v in point)


def distance_to_segment(point, a, b):
    dx, dy = b[0]-a[0], b[1]-a[1]
    fraction = max(0., min(1., ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/(dx*dx+dy*dy)))
    return math.dist(point[:2], (a[0]+fraction*dx, a[1]+fraction*dy))


def segment_distance(a, b, c, d):
    def cross(p, q, r):
        return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    # Include proper crossings, not collinear disjoint line extensions.
    if cross(a, b, c)*cross(a, b, d) < 0 and cross(c, d, a)*cross(c, d, b) < 0:
        return 0.
    return min(distance_to_segment(p, u, v) for p, u, v in
               ((a, c, d), (b, c, d), (c, a, b), (d, a, b)))


def copper_zones(tree, copper):
    """Zones on a stack-up copper layer. A zone on no copper layer carries no current.

    Altium imports leave zones on an undefined layer, such as stiffener outlines;
    they are not copper and do not change a coil's inductance.
    """
    found = []
    for zone in tree.named('zone'):
        names = [child.children[1:] for child in zone.named('layers')] + \
                [child.children[1:] for child in zone.named('layer')]
        if any(name in copper for group in names for name in group):
            found.append(zone)
    return found


# Track ends join where their copper overlaps: closer than half the narrower
# width, as two round track ends of those widths touch. Imported boards
# (Altium) leave joined ends 10-200 um apart. This floor joins ends of
# zero-width links such as a port point.
JOIN_TOLERANCE_MM = 0.005


def extract(tree, ports, density, layers):
    """Trace each port's coil as one current path for FastHenry.

    The board's copper on stack-up copper layers becomes a graph: tracks and
    arc chords, vias as vertical links on their drill, and net ties as links
    between their pads. Each port's path is the shortest route between its two
    ends; dead-end stubs carry no current and are left out. Copper of another
    net touching the path, a second route between the ends, or turns of the
    path touching each other are judgements, not facts. Pad spreading
    impedance is outside this basis.
    """
    copper = {entry['name']: entry for entry in layers if entry['copper']}
    edges, graph = [], defaultdict(list)
    anchors: dict[str, list] = defaultdict(list)

    def node_at(point, layer, width=0.0):
        """The existing node whose copper overlaps this end, or a new one."""
        x, y = point
        best = None
        for candidate, reach in anchors[layer]:
            limit = max(min(width, reach)/2, JOIN_TOLERANCE_MM)
            gap = math.hypot(candidate[0]-x, candidate[1]-y)
            if gap <= limit and (best is None or gap < best[0]):
                best = (gap, candidate)
        if best is not None:
            return best[1]
        created = (*key(point), layer)
        anchors[layer].append((created, width))
        return created

    def insert(a, b, width, layer, kind, net, layer_b=None, extra=None,
               join_a=True, join_b=True):
        """One conductor piece. Only real ends join others; an arc's inner chord
        points are its own, or a small arc's chords would merge into each other."""
        if layer not in copper or (layer_b is not None and layer_b not in copper) or width <= 0:
            return
        start = node_at(a, layer, width) if join_a else (*key(a), layer)
        finish = node_at(b, layer_b or layer, width) if join_b else (*key(b), layer_b or layer)
        if start == finish:
            return
        index = len(edges)
        edges.append({'a': start, 'b': finish, 'width_mm': width, 'layer': layer, 'kind': kind,
                      'net': net, **(extra or {})})
        graph[start].append(index)
        graph[finish].append(index)

    if copper_zones(tree, copper):
        raise GeometryJudgement('zone_geometry_requires_judgement')
    for node in tree.named('segment') + tree.named('arc'):
        layer = values(node, 'layer')[0]
        width = float(values(node, 'width')[0])
        points = ([xy(node, 'start'), xy(node, 'end')] if node.name == 'segment'
                  else arc_points(node, density, min_chord_mm=width))
        last = len(points) - 2
        for index, (a, b) in enumerate(zip(points, points[1:])):
            insert(a, b, width, layer, node.name, net_name(node),
                   join_a=index == 0, join_b=index == last)
    for via in tree.named('via'):
        # KiCad lists a via's two end layers; it also joins every copper
        # layer of the stack-up between them.
        ends = [copper[name]['z_m'] for name in values(via, 'layers') if name in copper]
        drill = float(values(via, 'drill')[0])
        ordered = sorted((name for name, entry in copper.items()
                          if ends and min(ends) <= entry['z_m'] <= max(ends)),
                         key=lambda name: copper[name]['z_m'])
        for top, bottom in zip(ordered, ordered[1:]):
            insert(xy(via, 'at'), xy(via, 'at'), drill, top, 'via', net_name(via),
                   layer_b=bottom, extra={'drill_mm': drill})
    pads = []
    for footprint in tree.named('footprint'):
        refs = [p.children[2] for p in footprint.named('property') if p.children[1] == 'Reference']
        refs += [p.children[2] for p in footprint.named('fp_text') if p.children[1] == 'reference']
        reference = refs[0] if len(set(refs)) == 1 else None
        at = values(footprint, 'at')
        origin = tuple(float(v) for v in at[:2])
        angle = -math.radians(float(at[2]) if len(at) > 2 else 0)
        own = []
        for pad in footprint.named('pad'):
            x, y = xy(pad, 'at')
            position = (origin[0]+x*math.cos(angle)-y*math.sin(angle),
                        origin[1]+x*math.sin(angle)+y*math.cos(angle))
            own.append({'reference': reference, 'pad': pad.children[1],
                        'position_mm': position, 'size_mm': xy(pad, 'size'),
                        'layers': values(pad, 'layers'), 'type': pad.children[2],
                        'shape': pad.children[3], 'net': net_name(pad)})
        pads += own
        # A net tie joins its pads' nets through its own copper.
        library = str(footprint.children[1]) if len(footprint.children) > 1 else ''
        if ('nettie' in library.replace('_', '').replace('-', '').lower()
                or footprint.named('net_tie_pad_groups')) and len(own) == 2:
            layer = next((name for name in own[0]['layers'] if name in copper), None)
            if layer is not None:
                width = min(*own[0]['size_mm'], *own[1]['size_mm'])
                insert(own[0]['position_mm'], own[1]['position_mm'], width, layer, 'net_tie',
                       own[0]['net'], extra={'tie_nets': (own[0]['net'], own[1]['net']),
                                             'reference': reference})

    def resolve(endpoint):
        if set(endpoint) == {'reference', 'pad'}:
            matches = [p for p in pads if p['reference'] == endpoint['reference'] and p['pad'] == endpoint['pad']]
            if len(matches) != 1:
                raise GeometryJudgement('port_pad_ambiguous')
            pad = matches[0]
            # A track end anywhere on the pad's copper attaches to it: within
            # half the pad's smaller side of its centre, so the circle stays
            # inside the pad whatever its rotation.
            reach = min(pad['size_mm'])/2
            x, y = pad['position_mm']
            candidates = [candidate for layer in copper
                          if layer in pad['layers'] or '*.Cu' in pad['layers']
                          for candidate, _ in anchors.get(layer, ())
                          if candidate in graph
                          and math.hypot(candidate[0]-x, candidate[1]-y) <= max(reach, JOIN_TOLERANCE_MM)]
            if len(candidates) != 1:
                raise GeometryJudgement('port_pad_attachment_ambiguous')
            return candidates[0]
        if set(endpoint) != {'x_mm', 'y_mm', 'layer'}:
            raise AnalysisError('arguments_unusable')
        # The track end whose copper covers the point; two or none is ambiguous.
        x, y = endpoint['x_mm'], endpoint['y_mm']
        covering = [candidate for candidate, reach in anchors.get(endpoint['layer'], ())
                    if candidate in graph
                    and math.hypot(candidate[0]-x, candidate[1]-y) <= max(reach/2, JOIN_TOLERANCE_MM)]
        if len(covering) != 1:
            raise GeometryJudgement('port_point_attachment_ambiguous')
        return covering[0]

    def route(start, end, banned=frozenset()):
        """Edge indices of the shortest route by length, or None."""
        import heapq
        best, previous, queue = {start: 0.0}, {}, [(0.0, start)]
        while queue:
            distance, current = heapq.heappop(queue)
            if current == end:
                break
            if distance > best.get(current, math.inf):
                continue
            for index in graph[current]:
                if index in banned:
                    continue
                edge = edges[index]
                other = edge['b'] if current == edge['a'] else edge['a']
                step = distance + max(math.dist(edge['a'][:2], edge['b'][:2]), 1e-9)
                if step < best.get(other, math.inf):
                    best[other], previous[other] = step, (current, index)
                    heapq.heappush(queue, (step, other))
        if end not in previous:
            return None
        indices, current = [], end
        while current != start:
            current, index = previous[current]
            indices.append(index)
        return indices[::-1]

    paths = []
    for port in ports:
        start, end = resolve(port['start']), resolve(port['end'])
        if start == end:
            raise GeometryJudgement('port_ends_identical')
        indices = route(start, end)
        if indices is None:
            raise GeometryJudgement('port_copper_branch_or_disconnection')
        # A second route that avoids any one edge would carry part of the
        # current; a single path then misstates the coil.
        if any(route(start, end, frozenset({index})) is not None for index in indices):
            raise GeometryJudgement('port_copper_parallel_route')
        nodes, current = [start], start
        for index in indices:
            edge = edges[index]
            current = edge['b'] if current == edge['a'] else edge['a']
            nodes.append(current)
        nets = {edges[i]['net'] for i in indices}
        selected = set(indices)
        # Each change of net along the path must happen across a net tie that
        # joins exactly those two nets; anything else is a short.
        current = None
        for position, index in enumerate(indices):
            edge = edges[index]
            if edge['kind'] == 'net_tie':
                # The tie was inserted from its first pad to its second: the
                # node the route enters by says which net it leaves on.
                entering = nodes[position] == edge['a']
                enter, leave = edge['tie_nets'] if entering else edge['tie_nets'][::-1]
                if current is not None and current != enter:
                    raise GeometryJudgement(
                        f"port_crosses_nets:{port['id']}:{edge.get('reference')} joins "
                        f"{'/'.join(edge['tie_nets'])}, reached from {current}")
                current = leave
                continue
            if current is not None and edge['net'] != current:
                raise GeometryJudgement(
                    f"port_crosses_nets:{port['id']}:{current} to {edge['net']} at "
                    f"{edge['a'][0]:.3f},{edge['a'][1]:.3f} {edge['layer']} without a net tie")
            current = edge['net']
        # Copper not on the path touching it anywhere along its length: of
        # another net it is a short; of the same net, touching away from the
        # path's nodes, it is a second current route the graph does not see.
        for index in indices:
            edge = edges[index]
            if edge['kind'] in ('via', 'net_tie'):
                continue
            ends = {edge['a'], edge['b']}
            reach = edge['width_mm']/2
            low = (min(edge['a'][0], edge['b'][0])-reach, min(edge['a'][1], edge['b'][1])-reach)
            high = (max(edge['a'][0], edge['b'][0])+reach, max(edge['a'][1], edge['b'][1])+reach)
            for other_index, other in enumerate(edges):
                if (other_index in selected or other['layer'] != edge['layer']
                        or other['kind'] in ('via', 'net_tie')):
                    continue
                margin = other['width_mm']/2
                if (max(other['a'][0], other['b'][0]) < low[0]-margin
                        or min(other['a'][0], other['b'][0]) > high[0]+margin
                        or max(other['a'][1], other['b'][1]) < low[1]-margin
                        or min(other['a'][1], other['b'][1]) > high[1]+margin):
                    continue
                if other['net'] == edge['net'] and ends & {other['a'], other['b']}:
                    continue    # attached at a node: the graph already holds it
                if (segment_distance(edge['a'], edge['b'], other['a'], other['b']) <
                        (edge['width_mm']+other['width_mm'])/2 - JOIN_TOLERANCE_MM):
                    raise GeometryJudgement('off_path_copper_contact')
        path = [dict(edges[i], a=nodes[j], b=nodes[j+1]) for j, i in enumerate(indices)]
        # Turns touching each other. Pieces close along the path, such as the
        # chords of one arc, meet by construction and are not compared.
        along = [0.0]
        for edge in path:
            along.append(along[-1] + math.dist(edge['a'][:2], edge['b'][:2]))
        for i, edge in enumerate(path):
            if edge['kind'] in ('via', 'net_tie'):
                continue
            for j in range(i+1, len(path)):
                other = path[j]
                if other['kind'] in ('via', 'net_tie') or edge['layer'] != other['layer']:
                    continue
                if along[j] - along[i+1] <= 2*max(edge['width_mm'], other['width_mm']):
                    continue
                if (segment_distance(edge['a'], edge['b'], other['a'], other['b']) <
                        (edge['width_mm']+other['width_mm'])/2 - JOIN_TOLERANCE_MM):
                    raise GeometryJudgement('copper_self_contact_union_unresolved')
        paths.append({'id': port['id'], 'net': path[0]['net'], 'nets': sorted(nets), 'edges': path})
    used = set()
    for path in paths:
        identities = {(e['a'], e['b']) for e in path['edges']}
        if used & identities or used & {(b, a) for a, b in identities}:
            raise GeometryJudgement('shared_conductor_ports_require_independent_basis')
        used |= identities
    return paths


def geometry(path, layers):
    entries = {e['name']: e for e in layers}
    edges = path['edges']
    points = [e[k] for e in edges for k in ('a', 'b')]
    half = max(e['width_mm'] for e in edges)/2
    return {'id': path['id'], 'segment_count': len(edges),
            'arc_segment_count': sum(e['kind'] == 'arc' for e in edges),
            'layers': sorted({e['layer'] for e in edges}),
            'total_length_m': sum(math.dist(e['a'][:2], e['b'][:2]) for e in edges)*.001,
            'bounding_box_m': [ (min(p[i] for p in points)-half)*.001 for i in (0, 1)] +
                              [ (max(p[i] for p in points)+half)*.001 for i in (0, 1)],
            'segments': [{'start_mm': list(e['a'][:2]), 'end_mm': list(e['b'][:2]),
                          'width_mm': e['width_mm'], 'layer': e['layer'],
                          'thickness_m': entries[e['layer']]['thickness_m'], 'kind': e['kind']}
                         for e in edges]}


# FastHenry's mutual-inductance formula is unstable for filaments that are
# nearly but not exactly parallel. Imported tracks meant to be straight are a
# few micrometres off (one RF3 track climbs 2.3 um over 19 mm, 0.4 mm from an
# RF1 track) and returned NaN. A 10 um grid straightens that jitter; it is five
# times finer than flex fabrication tolerance (+-50 um), so the change to any
# inductance is far below 0.1%.
SOLVER_GRID_M = 1e-5


def fasthenry_input(paths, layers, frequency, density):
    lookup = {e['name']: e for e in layers}
    lines = ['* ALX copper; SI; bulk copper conductivity 5.8e7 S/m', '.units m',
             f'.default sigma=5.8e7 nhinc={density} nwinc={density} rh=2 rw=2']

    def snapped(point):
        return (round(point[0]*.001/SOLVER_GRID_M)*SOLVER_GRID_M,
                round(point[1]*.001/SOLVER_GRID_M)*SOLVER_GRID_M, lookup[point[2]]['z_m'])

    for p, path in enumerate(paths):
        nodes = [snapped(path['edges'][0]['a'])]
        lines.append(f'N{p}_0 x={nodes[0][0]:.15g} y={nodes[0][1]:.15g} z={nodes[0][2]:.15g}')
        for edge in path['edges']:
            point = snapped(edge['b'])
            if point == nodes[-1]:
                # Shorter than the grid: merged into its neighbour.
                continue
            here, index = len(nodes) - 1, len(nodes)
            nodes.append(point)
            lines.append(f'N{p}_{index} x={point[0]:.15g} y={point[1]:.15g} z={point[2]:.15g}')
            if edge['kind'] == 'via':
                # A vertical bar on the drill: barrel plating is not recorded
                # by KiCad, so the bore stands in for the conductor. A vertical
                # bar has no default width direction; name one.
                size = edge['drill_mm']*.001
                lines.append(f'E{p}_{index} N{p}_{here} N{p}_{index} w={size:.15g} h={size:.15g} '
                             'wx=1 wy=0 wz=0')
                continue
            lines.append(f'E{p}_{index} N{p}_{here} N{p}_{index} w={edge["width_mm"]*.001:.15g} '
                         f'h={lookup[edge["layer"]]["thickness_m"]:.15g}')
        lines.append(f'.external N{p}_0 N{p}_{len(nodes) - 1}')
    return '\n'.join(lines + [f'.freq fmin={frequency:.15g} fmax={frequency:.15g} ndec=1', '.end', ''])


NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eEdD][+-]?\d+)?'


def parse_impedance(source, size, frequency):
    header = re.search(r'Impedance matrix for frequency\s*=\s*('+NUMBER+r')\s+(\d+)\s*x\s*(\d+)', source, re.I)
    if not header or tuple(map(int, header.group(2, 3))) != (size, size) or not math.isclose(float(header[1]), frequency, rel_tol=1e-5):
        raise AnalysisError('invalid_report', {'reason': 'fasthenry_matrix_header'})
    rows = []
    pattern = re.compile('('+NUMBER+r')\s*([+-])\s*('+NUMBER+r')\s*j', re.I)
    for line in source[header.end():].splitlines():
        matches = pattern.findall(line)
        if matches:
            if len(matches) != size:
                raise AnalysisError('invalid_report')
            rows.append([complex(float(a), float(b)*(-1 if sign == '-' else 1)) for a, sign, b in matches])
        if len(rows) == size:
            break
    if len(rows) != size or not all(math.isfinite(z.real) and math.isfinite(z.imag) for row in rows for z in row):
        raise AnalysisError('invalid_report')
    return rows


def net_name(node):
    entries = node.named('net')
    return str(entries[0].children[1]) if entries else '0'


def solver_nonconverged(source):
    return any(marker in source.lower() for marker in (
        'not converg', 'failed to converg', 'no convergence', 'iteration limit',
        'without converg', 'nonconverg'))


def wheeler(path):
    """Mohan 1999 modified Wheeler, square spirals (K1=2.34,K2=2.75).

    Validate regular orthogonal inward/outward winding before using the formula.
    Terminal leads, mixed widths, multilayer coils and arbitrary meanders do not
    supply a defensible turn count and return judgement instead.
    """
    edges = path['edges']
    if len(edges) < 8 or any(e['kind'] != 'segment' for e in edges) or len({e['layer'] for e in edges}) != 1:
        raise GeometryJudgement('modified_wheeler_spiral_geometry_unresolved')
    widths = [e['width_mm'] for e in edges]
    if max(widths)-min(widths) > 1e-6:
        raise GeometryJudgement('modified_wheeler_nonuniform_width')
    directions, lengths = [], []
    for edge in edges:
        dx, dy = edge['b'][0]-edge['a'][0], edge['b'][1]-edge['a'][1]
        if min(abs(dx), abs(dy)) > 1e-7:
            raise GeometryJudgement('modified_wheeler_non_square')
        lengths.append(math.hypot(dx, dy))
        directions.append((round(dx/lengths[-1]), round(dy/lengths[-1])))
    turns = [a[0]*b[1]-a[1]*b[0] for a, b in zip(directions, directions[1:])]
    if len(set(turns)) != 1 or turns[0] not in (-1, 1) or len(edges) % 4:
        raise GeometryJudgement('modified_wheeler_turn_count_unresolved')
    # Regular square-spiral sides change by a constant pitch every half turn.
    changes = [lengths[i+2]-lengths[i] for i in range(len(lengths)-2)]
    pitch2 = sum(changes)/len(changes)
    if abs(pitch2) < 1e-6 or max(abs(c-pitch2) for c in changes) > abs(pitch2)*.02:
        raise GeometryJudgement('modified_wheeler_pitch_unresolved')
    n = len(edges)/4
    outer = max(lengths)+widths[0]
    inner = min(lengths)-abs(pitch2)-widths[0]
    if inner <= 0:
        raise GeometryJudgement('modified_wheeler_inner_diameter_unresolved')
    average = (outer+inner)*.0005
    rho = (outer-inner)/(outer+inner)
    inductance = 4*math.pi*1e-7*2.34*n*n*average/(1+2.75*rho)
    return {'id': path['id'], 'method': 'Mohan_modified_Wheeler_square', 'turns': n,
            'outer_diameter_m': outer*.001, 'inner_diameter_m': inner*.001,
            'inductance_h': inductance, 'k1': 2.34, 'k2': 2.75}
