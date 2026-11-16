"""Where a hierarchical top sheet's blocks, pins, wires and net names go (2026-11-13).

Geometry only, in 2.54 mm grid units; label scope and the netlist proof stay
in kicad_hierarchy. Friedl's rules for the drawing, after a first layout put
the blocks in the page corners with names on top of the pins: blocks close
together, nets that do not cross, names clear of pins and block titles.

The usual import is a star: one sheet (the microcontroller) shares a signal
with each of the others, and no other sheet pair shares one. The hub then
stands in the middle and each other block beside it, its pins level with the
hub's, so every connection is one short straight wire and none can cross.
Any other pattern is drawn in two columns with a lane of vertical tracks
between them, the same way every time.
"""
from __future__ import annotations

GRID = 2.54
WIDTH = 20          # 50.8 mm, room for the longest pin names inside a block
GAP = 20            # 50.8 mm between facing blocks: room for a net name
PITCH = 2           # 5.08 mm between pins
PAD = 3             # 7.62 mm from a block's top to its first pin
MIN_HEIGHT = 10     # 25.4 mm
ROW_GAP = 5         # 12.7 mm between blocks on one side
LEFT, TOP = 20, 20  # 50.8 mm from the page corner
NAME_OFFSET = 3     # a net label sits on its wire 7.62 mm from the left-hand block


def _height(pins: int) -> int:
    return max(MIN_HEIGHT, 2 * PAD + max(0, pins - 1) * PITCH)


def _star(count: int, uses: dict[str, list[int]]):
    """The hub and, per other sheet, the labels it shares with the hub; None if not a star."""
    if not uses:
        return None
    share = [sum(index in members for members in uses.values()) for index in range(count)]
    hub = max(range(count), key=lambda index: (share[index], -index))
    if any(len(members) != 2 or hub not in members for members in uses.values()):
        return None
    spokes = {index: sorted(label for label, members in uses.items() if index in members)
              for index in range(count) if index != hub}
    return hub, spokes


def _star_layout(count, uses, hub, spokes):
    order = sorted(spokes, key=lambda index: (-len(spokes[index]), index))
    sides = {"right": [], "left": []}
    load = {"right": 0, "left": 0}
    for index in order:
        side = "right" if load["right"] <= load["left"] else "left"
        sides[side].append(index)
        load[side] += _height(len(spokes[index])) + ROW_GAP
    hub_x = LEFT + WIDTH + GAP if sides["left"] else LEFT
    columns = {"left": LEFT, "right": hub_x + WIDTH + GAP}
    rectangles = [None] * count
    pins, routes, junctions, names = {}, {}, {}, {}
    bottom = TOP + MIN_HEIGHT
    for side, members in sides.items():
        y = TOP
        for index in members:
            labels = spokes[index]
            height = _height(len(labels))
            x = columns[side]
            rectangles[index] = (x, y, WIDTH, height)
            for slot, label in enumerate(labels):
                py = y + PAD + slot * PITCH
                if side == "right":
                    hub_pin, spoke_pin = (hub_x + WIDTH, py), (x, py)
                    pins[hub, label] = (hub_pin, None, 0)
                    pins[index, label] = (spoke_pin, None, 180)
                    stub_x = hub_x + WIDTH + NAME_OFFSET
                else:
                    hub_pin, spoke_pin = (hub_x, py), (x + WIDTH, py)
                    pins[hub, label] = (hub_pin, None, 180)
                    pins[index, label] = (spoke_pin, None, 0)
                    stub_x = x + WIDTH + NAME_OFFSET
                # One straight wire between the two pins, its net label (plain
                # text, as in Altium and as KiCad intends) on the wire itself.
                routes[label] = [(hub_pin, spoke_pin)]
                junctions[label] = []
                names[label] = ((stub_x, py), 0)
            y += height + ROW_GAP
        bottom = max(bottom, y - ROW_GAP)
    rectangles[hub] = (hub_x, TOP, WIDTH, bottom - TOP)
    extent = (max(x + w for x, y, w, h in rectangles), max(y + h for x, y, w, h in rectangles))
    return rectangles, pins, routes, junctions, 0, extent, names


def _columns_layout(count, uses):
    """Two columns facing each other and a lane of vertical tracks between them."""
    labels = sorted(uses)
    lane = 2 * (len(labels) + 3)
    right_x = LEFT + WIDTH + lane
    tracks = {label: LEFT + WIDTH + 2 * (slot + 2) for slot, label in enumerate(labels)}
    rectangles, pins, routes, junctions, names = [None] * count, {}, {}, {}, {}
    heights: dict[str, list[int]] = {label: [] for label in labels}
    row_y, row_height = TOP + 4, 0
    for index in range(count):
        column = index % 2
        if column == 0 and index:
            row_y += row_height + 2 * ROW_GAP
            row_height = 0
        mine = [label for label in labels if index in uses[label]]
        height = _height(len(mine))
        row_height = max(row_height, height)
        x = LEFT if column == 0 else right_x
        rectangles[index] = (x, row_y, WIDTH, height)
        edge = x + WIDTH if column == 0 else x
        for slot, label in enumerate(mine):
            # Right-hand pins sit half a step lower, so no two stubs share a height.
            py = row_y + PAD + slot * PITCH + column
            heights[label].append(py)
            pins[index, label] = ((edge, py), None, 0 if column == 0 else 180)
            routes.setdefault(label, []).append(((edge, py), (tracks[label], py)))
    for label in labels:
        x = tracks[label]
        levels = sorted(heights[label])
        # Tracks start above every block, where no stub runs.
        routes[label].append(((x, TOP), (x, levels[0])))
        routes[label].extend(((x, a), (x, b)) for a, b in zip(levels, levels[1:]))
        junctions[label] = [(x, level) for level in levels[:-1]]
        names[label] = ((x, TOP), 90)
    extent = (max(x + w for x, y, w, h in rectangles), max(y + h for x, y, w, h in rectangles))
    return rectangles, pins, routes, junctions, crossing_count(routes), extent, names


def _hub_layout(count, uses):
    """The sheet that talks to the others (the microcontroller) in the middle, the rest
    beside it (16 Nov, Friedl: buses and ports, as in his Altium sheets).

    Sheets that share a signal among themselves stand on the same side, together. A
    signal between the hub and one sheet is one straight wire, its pins level. A signal
    several sheets share (a bus such as I2C) runs on a track outside those sheets, their
    pins for it on their outer edge, and reaches the hub through the gap between two of
    them; so nothing has to cross the straight wires.
    """
    share = [sum(index in members for members in uses.values()) for index in range(count)]
    hub = max(range(count), key=lambda index: (share[index], -index))
    others = [index for index in range(count) if index != hub]
    parent = {index: index for index in others}

    def find(index):
        while parent[index] != index:
            index = parent[index]
        return index
    for members in uses.values():
        outer = [index for index in members if index != hub]
        for a, b in zip(outer, outer[1:]):
            parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for index in others:
        groups.setdefault(find(index), []).append(index)
    trunk_labels = {label for label, members in uses.items() if len([i for i in members if i != hub]) > 1}

    def labels_of(index):
        return sorted(label for label, members in uses.items() if index in members)

    def height(index):
        mine = labels_of(index)
        inner = [label for label in mine if label not in trunk_labels]
        outer = [label for label in mine if label in trunk_labels]
        return _height(max(len(inner), len(outer)))
    ordered = sorted(groups.values(), key=lambda group: (-sum(height(i) for i in group), min(group)))
    connected = [group for group in ordered if any(labels_of(i) for i in group)]
    idle = [i for group in ordered if not any(labels_of(j) for j in group) for i in group]
    sides = {"right": [], "left": []}
    load = {"right": 0, "left": 0}
    for group in connected:
        side = "right" if load["right"] <= load["left"] else "left"
        sides[side].extend(group)
        load[side] += sum(height(i) + ROW_GAP for i in group)
    for index in idle:
        side = "right" if load["right"] <= load["left"] else "left"
        sides[side].append(index)
        load[side] += height(index) + ROW_GAP
    above = 4 + 2 * max(1, len(trunk_labels))
    top = TOP + above
    hub_x = LEFT + WIDTH + GAP + 2 * len(trunk_labels) if sides["left"] else LEFT
    columns = {"left": LEFT + 2 * len(trunk_labels), "right": hub_x + WIDTH + GAP}
    rectangles = [None] * count
    pins, routes, junctions, names = {}, {}, {}, {}
    bottom = top + MIN_HEIGHT
    for side, members in sides.items():
        y = top
        blocks = []
        for index in members:
            h = height(index)
            x = columns[side]
            rectangles[index] = (x, y, WIDTH, h)
            blocks.append((index, y, h))
            mine = labels_of(index)
            inner = [label for label in mine if label not in trunk_labels]
            outer = [label for label in mine if label in trunk_labels]
            inner_edge, outer_edge = (x, x + WIDTH) if side == "right" else (x + WIDTH, x)
            in_angle, out_angle = (180, 0) if side == "right" else (0, 180)
            hub_edge, hub_angle = (hub_x + WIDTH, 0) if side == "right" else (hub_x, 180)
            for slot, label in enumerate(inner):
                py = y + PAD + slot * PITCH
                pins[index, label] = ((inner_edge, py), None, in_angle)
                if hub in uses[label]:
                    pins[hub, label] = ((hub_edge, py), None, hub_angle)
                    routes[label] = [((hub_edge, py), (inner_edge, py))]
                    junctions[label] = []
                    near = min(hub_edge, inner_edge) + NAME_OFFSET
                    names[label] = ((near, py), 0)
            for slot, label in enumerate(outer):
                py = y + PAD + slot * PITCH
                pins[index, label] = ((outer_edge, py), None, out_angle)
            y += h + ROW_GAP
        bottom = max(bottom, y - ROW_GAP)
        # Each shared signal on this side: a track outside the sheets, joined to the hub
        # through the gap above the first sheet that uses it.
        side_trunks = sorted({label for index in members for label in labels_of(index) if label in trunk_labels})
        for slot, label in enumerate(side_trunks):
            edge = columns[side] + WIDTH if side == "right" else columns[side]
            track_x = edge + 2 * (slot + 1) if side == "right" else edge - 2 * (slot + 1)
            levels = []
            segments = []
            for index, by, bh in blocks:
                if (index, label) in pins:
                    (px, py), _, _ = pins[index, label]
                    levels.append(py)
                    segments.append(((px, py), (track_x, py)))
            if hub in uses[label]:
                first = min(by for index, by, bh in blocks if (index, label) in pins)
                # Clear of the sheet's name, which sits just above its block.
                gy = first - 3 - 2 * slot
                hub_edge = hub_x + WIDTH if side == "right" else hub_x
                pins[hub, label] = ((hub_edge, gy), None, 0 if side == "right" else 180)
                segments.append(((hub_edge, gy), (track_x, gy)))
                levels.append(gy)
                names[label] = ((min(hub_edge, track_x) + NAME_OFFSET, gy), 0)
            else:
                names[label] = ((track_x, min(levels)), 90)
            levels = sorted(set(levels))
            segments.extend(((track_x, a), (track_x, b)) for a, b in zip(levels, levels[1:]))
            routes[label] = segments
            junctions[label] = [(track_x, level) for level in levels[1:-1]]
    rectangles[hub] = (hub_x, TOP, WIDTH, bottom - TOP)
    extent = (max(x + w for x, y, w, h in rectangles) + 2 * len(trunk_labels),
              max(y + h for x, y, w, h in rectangles))
    return rectangles, pins, routes, junctions, crossing_count(routes), extent, names


def crossing_count(routes) -> int:
    """Places where a straight run of one connection crosses through another's."""
    hits = set()
    labels = sorted(routes)
    for i, first in enumerate(labels):
        for second in labels[i + 1:]:
            for a, b in routes[first]:
                for c, d in routes[second]:
                    for (p, q), (r, t) in (((a, b), (c, d)), ((c, d), (a, b))):
                        if p[1] == q[1] and r[0] == t[0] and min(p[0], q[0]) < r[0] < max(p[0], q[0]) \
                                and min(r[1], t[1]) < p[1] < max(r[1], t[1]):
                            hits.add((first, second, r[0], p[1]))
    return len(hits)


def _blocks_only(count):
    """Blocks with nothing to connect yet: two columns, close together."""
    rectangles = []
    for index in range(count):
        column, row = index % 2, index // 2
        rectangles.append((LEFT + column * (WIDTH + GAP), TOP + row * (MIN_HEIGHT + ROW_GAP), WIDTH, MIN_HEIGHT))
    extent = (max(x + w for x, y, w, h in rectangles), max(y + h for x, y, w, h in rectangles))
    return rectangles, {}, {}, {}, 0, extent, {}


# A3 landscape in grid units, and the part of it above the title block.
A3 = (165, 117)
TITLE_BLOCK, MARGIN = 32, 10


def _centred(result, page):
    """The whole drawing moved to the middle of the page it is on, above the title block."""
    rectangles, pins, routes, junctions, crossings, extent, names = result
    page_width, free_height = page[0], page[1] - TITLE_BLOCK
    left = min(x for x, y, w, h in rectangles)
    top = min([y for x, y, w, h in rectangles] + [p[1] for name in names.values() for p in [name[0]]])
    width, height = extent[0] - left, extent[1] - top
    dx = max(MARGIN, int(page_width - width) // 2) - left
    dy = max(MARGIN, int(free_height - height) // 2) - top

    def move(point):
        return (point[0] + dx, point[1] + dy)
    return ([(x + dx, y + dy, w, h) for x, y, w, h in rectangles],
            {key: (move(point), side, angle) for key, (point, side, angle) in pins.items()},
            {label: [(move(a), move(b)) for a, b in segments] for label, segments in routes.items()},
            {label: [move(point) for point in points] for label, points in junctions.items()},
            crossings, (extent[0] + dx, extent[1] + dy),
            {label: (move(point), angle) for label, (point, angle) in names.items()})


def layout(count: int, uses: dict[str, list[int]], page: tuple[float, float] = A3):
    """Rectangles, pins, routes, junctions, crossings, extent and net-name places, in grid units.

    pins[(sheet, label)] = ((x, y), None, angle); routes[label] = [((x1, y1), (x2, y2)), ...];
    names[label] = ((x, y), angle) for the global label that keeps the net's name.
    """
    if not uses:
        return _centred(_blocks_only(count), page)
    star = _star(count, uses)
    if star is not None:
        return _centred(_star_layout(count, uses, *star), page)
    hub = _centred(_hub_layout(count, uses), page)
    columns = _centred(_columns_layout(count, uses), page)
    # The hub arrangement unless it crosses more than the two columns would.
    return hub if hub[4] <= columns[4] else columns


__all__ = ["A3", "GRID", "MARGIN", "TITLE_BLOCK", "crossing_count", "layout"]
