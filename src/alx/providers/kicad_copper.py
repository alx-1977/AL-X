"""Copper geometry and field-solver formats, private to KiCadProvider.

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


def stackup(tree):
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
        if not copper and not epsilon:
            raise GeometryJudgement('stackup_permittivity_missing:' + name)
        er = 1.0 if copper else positive(float(epsilon[0].children[1]))
        result.append({'name': name, 'kind': kind, 'thickness_m': t, 'epsilon_r': er,
                       'z_m': z+t/2, 'copper': copper})
        z += t
    if not any(e['copper'] for e in result):
        raise GeometryJudgement('stackup_copper_missing')
    return result


def arc_points(node, density):
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
    count = max(2, math.ceil(abs(sweep)*density*4))
    radius = math.dist(a, (ux, uy))
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


def extract(tree, ports, density, layers):
    """Resolve unique nonbranching paths; retain physical facts for judgement.

    Unmodelled zones, barrel plating and pad unions are explicitly returned to
    Core. They cannot be replaced with a guessed equivalent wire or thickness.
    """
    copper = {entry['name']: entry for entry in layers if entry['copper']}
    edges, graph = [], defaultdict(list)

    def insert(a, b, width, layer, kind):
        a = (*key(a), layer)
        b = (*key(b), layer)
        if a == b or layer not in copper or width <= 0:
            raise GeometryJudgement('invalid_copper_segment')
        index = len(edges)
        edges.append({'a': a, 'b': b, 'width_mm': width, 'layer': layer, 'kind': kind})
        graph[a].append(index)
        graph[b].append(index)

    if tree.named('zone'):
        raise GeometryJudgement('zone_geometry_requires_judgement')
    for node in tree.named('segment') + tree.named('arc'):
        layer = values(node, 'layer')[0]
        width = float(values(node, 'width')[0])
        points = [xy(node, 'start'), xy(node, 'end')] if node.name == 'segment' else arc_points(node, density)
        for a, b in zip(points, points[1:]):
            insert(a, b, width, layer, node.name)
    # KiCad saves diameter/drill but not the barrel's plating thickness.
    # That missing physical input is material to both resistance and skin loss.
    if tree.named('via'):
        raise GeometryJudgement('via_barrel_plating_not_recorded')
    pads = []
    for footprint in tree.named('footprint'):
        refs = [p.children[2] for p in footprint.named('property') if p.children[1] == 'Reference']
        refs += [p.children[2] for p in footprint.named('fp_text') if p.children[1] == 'reference']
        ref = set(refs)
        if len(ref) != 1:
            raise GeometryJudgement('footprint_reference_ambiguous')
        at = values(footprint, 'at')
        origin = tuple(float(v) for v in at[:2])
        angle = math.radians(float(at[2]) if len(at) > 2 else 0)
        for pad in footprint.named('pad'):
            x, y = xy(pad, 'at')
            position = (origin[0]+x*math.cos(angle)-y*math.sin(angle),
                        origin[1]+x*math.sin(angle)+y*math.cos(angle))
            pads.append({'reference': next(iter(ref)), 'pad': pad.children[1],
                         'position_mm': position, 'size_mm': xy(pad, 'size'),
                         'layers': values(pad, 'layers'), 'type': pad.children[2],
                         'shape': pad.children[3]})

    def resolve(endpoint):
        if set(endpoint) == {'reference', 'pad'}:
            matches = [p for p in pads if p['reference'] == endpoint['reference'] and p['pad'] == endpoint['pad']]
            if len(matches) != 1:
                raise GeometryJudgement('port_pad_ambiguous')
            pad = matches[0]
            candidates = [p for p in graph if p[:2] == key(pad['position_mm']) and p[2] in pad['layers']]
            if len(candidates) != 1:
                raise GeometryJudgement('port_pad_attachment_ambiguous')
            # Pad area must enter both field models. The conservative supported
            # case is an inline rectangular pad equal to the track cross-section.
            raise GeometryJudgement('pad_union_field_mesh_not_supported')
        if set(endpoint) != {'x_mm', 'y_mm', 'layer'}:
            raise AnalysisError('arguments_unusable')
        point = (*key((endpoint['x_mm'], endpoint['y_mm'])), endpoint['layer'])
        if point not in graph:
            raise GeometryJudgement('port_point_attachment_ambiguous')
        return point

    paths = []
    for port in ports:
        start, end = resolve(port['start']), resolve(port['end'])
        if start == end:
            raise GeometryJudgement('port_ends_identical')
        current, previous, indices, nodes = start, None, [], [start]
        while current != end:
            available = [i for i in graph[current] if i != previous]
            if len(available) != 1 or (current == start and len(graph[current]) != 1):
                raise GeometryJudgement('port_copper_branch_or_disconnection')
            index = available[0]
            if index in indices:
                raise GeometryJudgement('port_copper_cycle')
            edge = edges[index]
            current = edge['b'] if current == edge['a'] else edge['a']
            previous = index
            indices.append(index)
            nodes.append(current)
        if len(graph[end]) != 1:
            raise GeometryJudgement('port_copper_branch')
        # Endpoint-only graph traversal must never overlook a T-junction along
        # a segment. Copper touching off-path requires a physical union mesh.
        selected = set(indices)
        for index in indices:
            edge = edges[index]
            a, b = edge['a'], edge['b']
            dx, dy = b[0]-a[0], b[1]-a[1]
            length2 = dx*dx+dy*dy
            for other_index, other in enumerate(edges):
                if other_index in selected or other['layer'] != edge['layer']:
                    continue
                for point in (other['a'], other['b']):
                    t = ((point[0]-a[0])*dx+(point[1]-a[1])*dy)/length2
                    if 0 <= t <= 1 and math.dist(point[:2], (a[0]+t*dx, a[1]+t*dy)) <= (edge['width_mm']+other['width_mm'])/2:
                        raise GeometryJudgement('off_path_copper_contact')
        path = [dict(edges[i], a=nodes[j], b=nodes[j+1]) for j, i in enumerate(indices)]
        for i, edge in enumerate(path):
            for other in path[i+2:]:
                if (edge['layer'] == other['layer'] and
                        segment_distance(edge['a'], edge['b'], other['a'], other['b']) <=
                        (edge['width_mm']+other['width_mm'])/2):
                    # Consecutive chord pieces of an arc may overlap naturally
                    # near their common join. Their union needs the same exact
                    # solid mesh as touching turns; don't model distinct wires.
                    raise GeometryJudgement('copper_self_contact_union_unresolved')
            for pad in pads:
                pad_layers = pad['layers']
                if edge['layer'] in pad_layers or '*.Cu' in pad_layers:
                    if distance_to_segment(pad['position_mm'], edge['a'], edge['b']) <= (
                            math.hypot(*pad['size_mm'])+edge['width_mm'])/2:
                        raise GeometryJudgement('connected_pad_union_field_mesh_not_supported')
        paths.append({'id': port['id'], 'edges': path})
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


def fasthenry_input(paths, layers, frequency, density):
    lookup = {e['name']: e for e in layers}
    lines = ['* ALX copper; SI; bulk copper conductivity 5.8e7 S/m', '.units m',
             f'.default sigma=5.8e7 nhinc={density} nwinc={density} rh=2 rw=2']
    for p, path in enumerate(paths):
        edges = path['edges']
        for index, point in enumerate([edges[0]['a']] + [e['b'] for e in edges]):
            lines.append(f'N{p}_{index} x={point[0]*.001:.15g} y={point[1]*.001:.15g} z={lookup[point[2]]["z_m"]:.15g}')
        for index, edge in enumerate(edges):
            lines.append(f'E{p}_{index} N{p}_{index} N{p}_{index+1} w={edge["width_mm"]*.001:.15g} h={lookup[edge["layer"]]["thickness_m"]:.15g}')
        lines.append(f'.external N{p}_0 N{p}_{len(edges)}')
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


def quad_panels(corners, density):
    """Bilinear subdivision of one oriented rectangular/trapezoid face."""
    a, b, c, d = corners
    def point(u, v):
        return tuple((1-u)*(1-v)*a[k]+u*(1-v)*b[k]+u*v*c[k]+(1-u)*v*d[k] for k in range(3))
    for i in range(density):
        for j in range(density):
            yield [point(i/density, j/density), point((i+1)/density, j/density),
                   point((i+1)/density, (j+1)/density), point(i/density, (j+1)/density)]


def panel_source(faces, density):
    lines = ['0 ALX metre units']
    for face in faces:
        for panel in quad_panels(face, density):
            lines.append('Q 1 ' + ' '.join(format(v, '.15g') for point in panel for v in point))
    return '\n'.join(lines) + '\n'


def ribbon_faces(path, layers):
    """Closed mitered ribbon; joins have no overlapping internal end panels."""
    lookup = {e['name']: e for e in layers}
    edges = path['edges']
    normals = []
    for edge in edges:
        a, b = edge['a'], edge['b']
        length = math.dist(a[:2], b[:2])
        normals.append((-(b[1]-a[1])/length, (b[0]-a[0])/length))
    sections = []
    for i, point in enumerate([edges[0]['a']] + [e['b'] for e in edges]):
        edge = edges[min(i, len(edges)-1)]
        width = edge['width_mm']*.0005
        if 0 < i < len(edges):
            if abs(edge['width_mm']-edges[i-1]['width_mm']) > 1e-9:
                raise GeometryJudgement('variable_width_union_mesh_unresolved')
            n0, n1 = normals[i-1], normals[i]
            denominator = 1+n0[0]*n1[0]+n0[1]*n1[1]
            if denominator < .1:
                raise GeometryJudgement('copper_miter_degenerate')
            normal = ((n0[0]+n1[0])/denominator, (n0[1]+n1[1])/denominator)
        else:
            normal = normals[0 if i == 0 else -1]
        layer = lookup[point[2]]
        z0, z1 = layer['z_m']-layer['thickness_m']/2, layer['z_m']+layer['thickness_m']/2
        x, y = point[0]*.001, point[1]*.001
        sections.append([(x+normal[0]*width, y+normal[1]*width, z0),
                         (x-normal[0]*width, y-normal[1]*width, z0),
                         (x-normal[0]*width, y-normal[1]*width, z1),
                         (x+normal[0]*width, y+normal[1]*width, z1)])
    faces = [list(reversed(sections[0])), sections[-1]]
    for a, b in zip(sections, sections[1:]):
        for k in range(4):
            faces.append([a[k], a[(k+1)%4], b[(k+1)%4], b[k]])
    return faces


def fastcap_files(paths, layers, density):
    """Vacuum capacitance of equipotential port conductors.

    FastCap 2's dielectric-interface construction needs a conformal surface
    union with the board outline and pads. Never substitute an averaged er for
    the saved stack-up. Until that union is available the caller must explicitly
    mark this result as diagnostic, not board self-capacitance.
    """
    files, listing = {}, []
    for index, path in enumerate(paths):
        name = f'conductor{index}.qui'
        files[name] = panel_source(ribbon_faces(path, layers), density)
        listing.append(f'C {name} 1.0 0 0 0')
    files['copper.lst'] = '\n'.join(listing) + '\n'
    return files


def parse_capacitance(source, size):
    sections = re.split(r'CAPACITANCE MATRIX', source, flags=re.I)
    if len(sections) < 2:
        raise AnalysisError('invalid_report', {'reason': 'fastcap_matrix_missing'})
    section = sections[-1]
    if not re.search(r'pico\s*-?\s*farads', section, re.I):
        raise AnalysisError('invalid_report', {'reason': 'fastcap_units_missing'})
    rows = []
    for line in section.splitlines():
        tokens = line.split()
        if tokens and tokens[0] == str(len(rows)+1) and len(tokens) >= size+1:
            try:
                row = [float(v)*1e-12 for v in tokens[-size:]]
            except ValueError:
                continue
            if not all(math.isfinite(v) for v in row):
                raise AnalysisError('invalid_report')
            rows.append(row)
        if len(rows) == size:
            break
    if len(rows) != size or any(rows[i][i] <= 0 for i in range(size)):
        raise AnalysisError('invalid_report')
    return rows


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
