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
    """Resolve nonbranching track paths for the FastHenry current basis.

    Pad spreading impedance is outside that basis. The independent electrostatic
    mesh includes pad unions; missing barrel plating prevents a via current basis.
    """
    copper = {entry['name']: entry for entry in layers if entry['copper']}
    edges, graph = [], defaultdict(list)

    def insert(a, b, width, layer, kind):
        a = (*key(a), layer)
        b = (*key(b), layer)
        if a == b or layer not in copper or width <= 0:
            raise GeometryJudgement('invalid_copper_segment')
        index = len(edges)
        edges.append({'a': a, 'b': b, 'width_mm': width, 'layer': layer, 'kind': kind,
                      'net': net_name(node)})
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
        angle = -math.radians(float(at[2]) if len(at) > 2 else 0)
        for pad in footprint.named('pad'):
            x, y = xy(pad, 'at')
            position = (origin[0]+x*math.cos(angle)-y*math.sin(angle),
                        origin[1]+x*math.sin(angle)+y*math.cos(angle))
            pads.append({'reference': next(iter(ref)), 'pad': pad.children[1],
                         'position_mm': position, 'size_mm': xy(pad, 'size'),
                         'layers': values(pad, 'layers'), 'type': pad.children[2],
                         'shape': pad.children[3], 'net': net_name(pad)})

    def resolve(endpoint):
        if set(endpoint) == {'reference', 'pad'}:
            matches = [p for p in pads if p['reference'] == endpoint['reference'] and p['pad'] == endpoint['pad']]
            if len(matches) != 1:
                raise GeometryJudgement('port_pad_ambiguous')
            pad = matches[0]
            candidates = [p for p in graph if p[:2] == key(pad['position_mm']) and
                          (p[2] in pad['layers'] or '*.Cu' in pad['layers'])]
            if len(candidates) != 1:
                raise GeometryJudgement('port_pad_attachment_ambiguous')
            return candidates[0]
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
        nets = {e['net'] for e in path}
        if len(nets) != 1:
            raise GeometryJudgement('port_crosses_nets')
        paths.append({'id': port['id'], 'net': next(iter(nets)), 'edges': path})
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
    """Bilinear subdivision, resolving long walls independently of height.

    Equal counts in both directions preserve the original aspect ratio (up
    to 2400:1 on the solder-mask walls). Such panels span many solver cells
    and give a poor near-field/preconditioner basis for the thin stack-up.
    """
    a, b, c, d = corners
    width = max(math.dist(a, b), math.dist(c, d))
    height = max(math.dist(a, d), math.dist(b, c))
    nu = density * max(1, math.ceil(width/(8*height)))
    nv = density * max(1, math.ceil(height/(8*width)))
    def point(u, v):
        return tuple((1-u)*(1-v)*a[k]+u*(1-v)*b[k]+u*v*c[k]+(1-u)*v*d[k] for k in range(3))
    for i in range(nu):
        for j in range(nv):
            yield [point(i/nu, j/nv), point((i+1)/nu, j/nv),
                   point((i+1)/nu, (j+1)/nv), point(i/nu, (j+1)/nv)]


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


def net_name(node):
    entries = node.named('net')
    return str(entries[0].children[1]) if entries else '0'


def polygons(shape):
    if shape.is_empty:
        return []
    if shape.geom_type == 'Polygon':
        return [shape]
    if shape.geom_type not in ('MultiPolygon', 'GeometryCollection'):
        return []
    return [p for part in shape.geoms for p in polygons(part)]


def copper_solids(tree, paths, layers, density):
    """Union copper in metre coordinates, including pads and via annuli.

    Electrostatics uses the barrel envelope. FastHenry still refuses vias
    without recorded plating. Unassigned net 0 is allowed only for a single
    synthetic port; it is not treated as an electrical connection in a design.
    """
    from shapely import affinity, set_precision
    from shapely.geometry import Point, LineString, box
    from shapely.ops import unary_union, polygonize
    lookup = {e['name']: e for e in layers if e['copper']}
    solids = []

    def add(net, shape, names):
        shape = set_precision(shape, 1e-12)
        for name in names:
            if name in lookup:
                layer = lookup[name]
                solids.append((net, shape, layer['z_m']-layer['thickness_m']/2,
                               layer['z_m']+layer['thickness_m']/2))

    for node in tree.named('segment') + tree.named('arc'):
        points = [xy(node, 'start'), xy(node, 'end')] if node.name == 'segment' else arc_points(node, density)
        shape = LineString([(x*.001, y*.001) for x, y in points]).buffer(
            float(values(node, 'width')[0])*.0005, quad_segs=2*density)
        add(net_name(node), shape, values(node, 'layer'))
    for footprint in tree.named('footprint'):
        at = values(footprint, 'at')
        ox, oy = (float(v)*.001 for v in at[:2])
        rotation = float(at[2]) if len(at) > 2 else 0.
        for pad in footprint.named('pad'):
            if pad.children[2] == 'np_thru_hole':
                raise GeometryJudgement('nonplated_hole_dielectric_mesh_unresolved')
            sx, sy = (v*.001 for v in xy(pad, 'size'))
            kind = pad.children[3]
            if kind == 'rect':
                shape = box(-sx/2, -sy/2, sx/2, sy/2)
            elif kind in ('circle', 'oval', 'roundrect'):
                radius = min(sx, sy)/2
                if kind == 'circle' and not math.isclose(sx, sy):
                    raise GeometryJudgement('noncircular_circle_pad')
                if kind == 'roundrect':
                    radius = min(sx, sy)*float(values(pad, 'roundrect_rratio')[0])
                if radius <= 0:
                    raise GeometryJudgement('pad_radius_unusable')
                if kind in ('circle', 'oval'):
                    shape = (LineString([(-sx/2+radius, -sy/2+radius),
                                         (sx/2-radius, sy/2-radius)]) if sx != sy else Point(0, 0))
                else:
                    shape = box(-sx/2+radius, -sy/2+radius, sx/2-radius, sy/2-radius)
                shape = shape.buffer(radius, quad_segs=2*density)
            else:
                raise GeometryJudgement('pad_shape_mesh_unresolved:'+kind)
            pat = values(pad, 'at')
            px, py = (float(v)*.001 for v in pat[:2])
            # KiCad saves absolute pad orientation, local pad position.
            angle = float(pat[2]) if len(pat) > 2 else rotation
            shape = affinity.rotate(shape, -angle, origin=(0, 0))
            theta = -math.radians(rotation)
            shape = affinity.translate(shape, ox+px*math.cos(theta)-py*math.sin(theta),
                                       oy+px*math.sin(theta)+py*math.cos(theta))
            names = values(pad, 'layers')
            if '*.Cu' in names:
                names = list(lookup)
            if pad.children[2] == 'thru_hole':
                drill = values(pad, 'drill')
                if not drill or not isinstance(drill[0], str):
                    raise GeometryJudgement('pad_barrel_geometry_unresolved')
                try:
                    radius = positive(float(drill[0]))*.0005
                except (ValueError, TypeError):
                    raise GeometryJudgement('pad_slotted_drill_mesh_unresolved') from None
                offset = pad.one('drill').named('offset')
                if offset:
                    raise GeometryJudgement('pad_offset_drill_mesh_unresolved')
                centre = (ox+px*math.cos(theta)-py*math.sin(theta),
                          oy+px*math.sin(theta)+py*math.cos(theta))
                hole = set_precision(Point(*centre).buffer(radius, quad_segs=2*density), 1e-12)
                if not shape.covers(hole) or not names or any(n not in lookup for n in names):
                    raise GeometryJudgement('pad_drill_or_span_unusable')
                shape = shape.difference(hole)
                span = (min(lookup[n]['z_m']-lookup[n]['thickness_m']/2 for n in names),
                        max(lookup[n]['z_m']+lookup[n]['thickness_m']/2 for n in names))
                solids.append(('@barrel:'+net_name(pad), hole, *span))
            add(net_name(pad), shape, names)
    for via in tree.named('via'):
        names = values(via, 'layers')
        if len(names) != 2 or any(n not in lookup for n in names):
            raise GeometryJudgement('via_span_unresolved')
        top, bottom = sorted((lookup[n] for n in names), key=lambda e: e['z_m'])
        centre = Point(*(v*.001 for v in xy(via, 'at')))
        radius = float(values(via, 'size')[0])*.0005
        drill = float(values(via, 'drill')[0])*.0005
        if not 0 < drill < radius:
            raise GeometryJudgement('via_drill_unusable')
        hole = set_precision(centre.buffer(drill, quad_segs=2*density), 1e-12)
        shape = centre.buffer(radius, quad_segs=2*density).difference(hole)
        span = (top['z_m']-top['thickness_m']/2, bottom['z_m']+bottom['thickness_m']/2)
        names_in_span = [n for n, e in lookup.items() if span[0] < e['z_m'] < span[1]]
        add(net_name(via), shape, names_in_span)
        # Zero-thickness barrel on the nominal bore wall: no invented plating
        # thickness, and no extruded pad-diameter cylinder through the substrate.
        solids.append(('@barrel:'+net_name(via), hole, *span))
    if tree.named('zone'):
        raise GeometryJudgement('zone_geometry_requires_judgement')
    outlines = []
    for node in tree.named('gr_rect'):
        if values(node, 'layer')[0] == 'Edge.Cuts':
            a, b = xy(node, 'start'), xy(node, 'end')
            outlines.append(box(min(a[0], b[0])*.001, min(a[1], b[1])*.001,
                                max(a[0], b[0])*.001, max(a[1], b[1])*.001))
    lines = []
    for node in tree.named('gr_line') + tree.named('gr_arc'):
        if values(node, 'layer')[0] == 'Edge.Cuts':
            pts = [xy(node, 'start'), xy(node, 'end')] if node.name == 'gr_line' else arc_points(node, density)
            lines.append(LineString([(x*.001, y*.001) for x, y in pts]))
    if lines:
        outlines.extend(polygonize(unary_union(lines)))
    if len(outlines) != 1 or not outlines[0].is_valid:
        raise GeometryJudgement('board_outline_unresolved')
    outline = set_precision(outlines[0], 1e-12)
    if any(not outline.covers(s) for _, s, _, _ in solids):
        raise GeometryJudgement('copper_outside_board_outline')
    nets = list(dict.fromkeys(net for net, _, _, _ in solids if not net.startswith('@barrel:')))
    if '0' in nets and (len(paths) != 1 or paths[0]['net'] != '0'):
        raise GeometryJudgement('unassigned_copper_net_ambiguous')
    return solids, outline, nets


def planar_panels(shape, z, density):
    """Triangulate the exact polygon union, including holes; refine triangles."""
    from shapely import constrained_delaunay_triangles
    for polygon in polygons(shape):
        for triangle in constrained_delaunay_triangles(polygon).geoms:
            corners = [(x, y, z) for x, y in list(triangle.exterior.coords)[:3]]
            # Uniform barycentric refinement alone leaves millimetre-long
            # slivers along narrow tracks and rounded ends. Slice from the
            # shortest edge towards its opposite vertex instead. This adds
            # panels linearly with aspect ratio, without moving the boundary.
            lengths = [math.dist(corners[i], corners[(i+1)%3]) for i in range(3)]
            i = lengths.index(min(lengths))
            a, b, c = corners[i], corners[(i+1)%3], corners[(i+2)%3]
            count = density*max(1, math.ceil(max(lengths)/(8*min(lengths))))

            def point(u, v):
                return tuple((1-v)*((1-u)*a[k]+u*b[k])+v*c[k] for k in range(3))

            for j in range(count):
                for i in range(density):
                    panel = [point(i/density, j/count), point((i+1)/density, j/count)]
                    if j+1 < count:
                        panel.extend([point((i+1)/density, (j+1)/count), point(i/density, (j+1)/count)])
                    else:
                        panel.append(c)
                    yield panel


def surface_source(panels):
    return '0 ALX metre units\n' + ''.join(
        ('T' if len(panel) == 3 else 'Q')+' 1 '+' '.join(format(v, '.15g') for p in panel for v in p)+'\n'
        for panel in panels)


def electrostatic_files(solids, outline, nets, layers, density, surface_patches=False):
    """Conformal extruded interfaces with one conductor per identity.

    Slabs are partitioned before meshing. Internal copper faces disappear;
    copper/dielectric faces occur as C panels, never duplicated as D panels.
    Copper-layer gaps are air (the stack-up specifies no filler there).
    """
    from shapely.geometry import Polygon, LineString
    from shapely import set_precision
    from shapely.ops import unary_union
    empty = Polygon()
    boundaries = sorted(set([e['z_m']+sign*e['thickness_m']/2 for e in layers for sign in (-1, 1)] +
                            [z for _, _, lo, hi in solids for z in (lo, hi)]))
    zs = []
    for z in boundaries:
        if not zs or z-zs[-1] > 1e-12:
            zs.append(z)
    slabs = []
    for lo, hi in zip(zs, zs[1:]):
        mid = (lo+hi)/2
        material = next((e for e in layers if abs(mid-e['z_m']) < e['thickness_m']/2), None)
        er = material['epsilon_r'] if material else 1.
        holes = unary_union([s for n, s, a, b in solids if n.startswith('@barrel:') and a < mid < b])
        # Drilling removes track copper too, including tracks ending at the
        # centre of a via/pad. Subtract after the per-net union, not before it.
        copper = {net: unary_union([s for n, s, a, b in solids if n == net and a < mid < b]).difference(holes)
                  for net in nets}
        for i, net in enumerate(nets):
            if any(copper[net].intersection(copper[n]).area > 1e-20 for n in nets[:i]):
                raise GeometryJudgement('different_nets_copper_overlap')
        dielectric = outline.difference(unary_union(list(copper.values())))
        materials = {er: dielectric.difference(holes)}
        materials[1.] = materials.get(1., empty).union(holes)
        slabs.append((lo, hi, er, copper, materials, holes))
    files, listing = {}, []
    conductors = {n: defaultdict(list) for n in nets}
    barrels = {n: [] for n in nets}

    def interface(shape, z, outside, inside):
        if shape.is_empty or outside == inside:
            return
        name = f'dielectric{len(files)}.qui'
        files[name] = surface_source(planar_panels(shape, z, density))
        listing.append(f'D {name} {outside:.15g} {inside:.15g} 0 0 0 0 0 {z+1:.15g}')

    for index, (lo, hi, er, copper, materials, holes) in enumerate(slabs):
        exterior = unary_union(list(copper.values())).boundary
        for net, shape in copper.items():
            for poly in polygons(shape):
                for ring in [poly.exterior, *poly.interiors]:
                    for a, b in zip(ring.coords, list(ring.coords)[1:]):
                        line = set_precision(LineString([a, b]), 1e-12)
                        if surface_patches:
                            line = line.intersection(exterior)
                        parts = [line] if line.geom_type == 'LineString' else list(line.geoms)
                        for part in parts:
                            for u, v in zip(part.coords, list(part.coords)[1:]):
                                face = [(u[0], u[1], lo), (v[0], v[1], lo),
                                        (v[0], v[1], hi), (u[0], u[1], hi)]
                                adjacent_er = 1. if not holes.is_empty and holes.boundary.covers(part) else er
                                conductors[net][adjacent_er].extend(quad_panels(face, density))
            for side, z in ((-1, lo), (1, hi)):
                neighbour = index+side
                other = slabs[neighbour] if 0 <= neighbour < len(slabs) else None
                other_copper = unary_union(list(other[3].values())) if other else empty
                exposed = shape.difference(other_copper)
                if other:
                    for adjacent_er, material_shape in other[4].items():
                        contact = exposed.intersection(material_shape)
                        conductors[net][adjacent_er].extend(planar_panels(contact, z, density))
                        exposed = exposed.difference(material_shape)
                conductors[net][1.].extend(planar_panels(exposed, z, density))
        above = slabs[index+1] if index+1 < len(slabs) else None
        for inside, region in materials.items():
            if above:
                for outside, other_region in above[4].items():
                    interface(region.intersection(other_region), hi, outside, inside)
            else:
                interface(region, hi, 1., inside)
        if index == 0:
            for outside, region in materials.items():
                interface(region, lo, outside, 1.)
        for marker, hole, a, b in solids:
            if not marker.startswith('@barrel:') or not a < (lo+hi)/2 < b:
                continue
            net = marker.removeprefix('@barrel:')
            for poly in polygons(hole):
                for u, v in zip(poly.exterior.coords, list(poly.exterior.coords)[1:]):
                    if not copper[net].is_empty and copper[net].boundary.covers(LineString([u, v])):
                        continue  # already an inner C face of the annular pad
                    face = [(u[0], u[1], lo), (v[0], v[1], lo), (v[0], v[1], hi), (u[0], u[1], hi)]
                    barrels[net].append((er, list(quad_panels(face, density)), hole.centroid.coords[0], (lo+hi)/2))
        if er != 1.:
            for poly in polygons(outline):
                for a, b in zip(poly.exterior.coords, list(poly.exterior.coords)[1:]):
                    dx, dy = b[0]-a[0], b[1]-a[1]
                    middle = ((a[0]+b[0])/2, (a[1]+b[1])/2)
                    sign = 1 if poly.exterior.is_ccw else -1
                    ref = (middle[0]+sign*dy, middle[1]-sign*dx)
                    name = f'dielectric{len(files)}.qui'
                    face = [(a[0], a[1], lo), (b[0], b[1], lo), (b[0], b[1], hi), (a[0], a[1], hi)]
                    files[name] = surface_source(quad_panels(face, density))
                    listing.append(f'D {name} 1 {er:.15g} 0 0 0 {ref[0]:.15g} {ref[1]:.15g} {(lo+hi)/2:.15g}')
    for index, net in enumerate(nets):
        groups = [(er, panels) for er, panels in conductors[net].items() if panels]
        for j, (er, panels) in enumerate(groups):
            name = f'conductor{index}_{j}.qui'
            files[name] = surface_source(panels)
            listing.append(f'C {name} {er:.15g} 0 0 0'+(' +' if j+1 < len(groups) or barrels[net] else ''))
        for j, (er, panels, ref, z) in enumerate(barrels[net]):
            name = f'barrel{index}_{j}.qui'
            files[name] = surface_source(panels)
            if er == 1.:
                listing.append(f'C {name} 1 0 0 0'+(' +' if j+1 < len(barrels[net]) else ''))
            else:
                listing.append(f'B {name} {er:.15g} 1 0 0 0 {ref[0]:.15g} {ref[1]:.15g} {z:.15g} '+
                               ('-+' if j+1 < len(barrels[net]) else '-'))
    files['copper.lst'] = '\n'.join(listing)+'\n'
    return files


def fastcap_files(paths, layers, density, tree):
    solids, outline, nets = copper_solids(tree, paths, layers, density)
    return electrostatic_files(solids, outline, nets, layers, density), nets


def winding_model(path, solids, density):
    """Piecewise-constant surface approximation to V(s)=s/length.

    This voltage basis represents an isolated winding with a floating common
    mode. No artificial slit or internal end face is added at patch boundaries.
    Both the surface mesh and voltage basis are refined in the second solve.
    Multiple nets need electrical boundary conditions supplied by Core, so are
    deliberately unavailable rather than silently grounded.
    """
    from shapely.geometry import Polygon
    from shapely import set_precision
    from shapely.ops import unary_union
    if any(n.startswith('@barrel:') for n, _, _, _ in solids):
        raise GeometryJudgement('winding_multilayer_voltage_basis_unresolved')
    if {n for n, _, _, _ in solids} != {path['net']}:
        raise GeometryJudgement('winding_other_net_boundary_conditions_unresolved')
    if len({(lo, hi) for _, _, lo, hi in solids}) != 1:
        raise GeometryJudgement('winding_multilayer_voltage_basis_unresolved')
    copper = unary_union([s for _, s, _, _ in solids])
    remaining = copper
    edges = path['edges']
    faces = ribbon_faces(path, [{'name': edges[0]['layer'], 'z_m': 0., 'thickness_m': 1.}])
    total = sum(math.dist(e['a'][:2], e['b'][:2]) for e in edges)
    shapes, voltages, traversed = [], [], 0.
    for i, edge in enumerate(edges):
        a, b, c, d = [p[:2] for p in faces[2+4*i]]
        length = math.dist(edge['a'][:2], edge['b'][:2])
        for j in range(density):
            def lerp(p, q, t):
                return tuple(x+(y-x)*t for x, y in zip(p, q))
            shape = set_precision(Polygon([lerp(a, d, j/density), lerp(b, c, j/density),
                                           lerp(b, c, (j+1)/density), lerp(a, d, (j+1)/density)]), 1e-12)
            shape = shape.intersection(remaining)
            if shape.is_empty:
                raise GeometryJudgement('winding_voltage_patch_empty')
            shapes.append(shape)
            remaining = remaining.difference(shape)
            voltages.append((traversed+length*(j+.5)/density)/total)
        traversed += length
    for extra in polygons(remaining):
        # Only terminal-pad overhangs have an unambiguous terminal voltage.
        candidates = [i for i in (0, len(shapes)-1) if extra.distance(shapes[i]) < 1e-12]
        if len(candidates) != 1:
            raise GeometryJudgement('winding_pad_voltage_basis_unresolved')
        i = candidates[0]
        shapes[i] = shapes[i].union(extra)
    _, _, lo, hi = solids[0]
    identities = [f'w{i}' for i in range(len(shapes))]
    return [(n, s, lo, hi) for n, s in zip(identities, shapes)], identities, voltages


def winding_capacitance(matrix, voltages):
    """2U/Vterminal² with zero total charge on the isolated winding."""
    if len(matrix) != len(voltages) or any(len(r) != len(voltages) for r in matrix):
        raise AnalysisError('invalid_report')
    sums = [sum(row) for row in matrix]
    total = sum(sums)
    if total <= 0:
        raise AnalysisError('invalid_report')
    common = -sum(c*v for c, v in zip(sums, voltages))/total
    v = [x+common for x in voltages]
    c = sum(v[i]*matrix[i][j]*v[j] for i in range(len(v)) for j in range(len(v)))
    if not math.isfinite(c) or c <= 0:
        raise GeometryJudgement('winding_capacitance_nonpositive')
    return c


def solver_nonconverged(source):
    return any(marker in source.lower() for marker in (
        'not converg', 'failed to converg', 'no convergence', 'iteration limit',
        'without converg', 'nonconverg'))


def parse_capacitance(source, size):
    if solver_nonconverged(source):
        raise AnalysisError('invalid_report', {'reason': 'fastcap_nonconvergence'})
    sections = re.split(r'CAPACITANCE MATRIX', source, flags=re.I)
    if len(sections) < 2:
        raise AnalysisError('invalid_report', {'reason': 'fastcap_matrix_missing'})
    section = sections[-1]
    header = re.match(r',\s*([^\r\n]*)', section)
    if not header or not header[1].strip():
        raise AnalysisError('invalid_report', {'reason': 'fastcap_units_missing'})
    unit = re.sub(r'[\s-]', '', header[1]).lower()
    factors = {'farads': 1., 'millifarads': 1e-3, 'microfarads': 1e-6,
               'nanofarads': 1e-9, 'picofarads': 1e-12,
               'femtofarads': 1e-15, 'attofarads': 1e-18}
    if unit not in factors:
        raise AnalysisError('invalid_report', {'reason': 'fastcap_units_unknown', 'unit': unit})
    rows = []
    for line in section.splitlines():
        tokens = line.split()
        # FastCap prints the conductor name before the row number, e.g.
        # "1%GROUP19 1 1.688". Names may themselves contain spaces/numbers.
        if len(tokens) >= size+1 and tokens[-size-1] == str(len(rows)+1):
            try:
                row = [float(v)*factors[unit] for v in tokens[-size:]]
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
