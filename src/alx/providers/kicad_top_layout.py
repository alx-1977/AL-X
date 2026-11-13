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
NAME_OFFSET = 3     # a net name stands 7.62 mm out from the hub on its own stub


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
                    names[label] = ((stub_x, py - 1), 0)
                else:
                    hub_pin, spoke_pin = (hub_x, py), (x + WIDTH, py)
                    pins[hub, label] = (hub_pin, None, 180)
                    pins[index, label] = (spoke_pin, None, 0)
                    stub_x = hub_x - NAME_OFFSET
                    names[label] = ((stub_x, py - 1), 180)
                # One straight wire between the two pins, and a short stub up
                # from it carrying the net's name, clear of the pins.
                routes[label] = [(hub_pin, spoke_pin), ((stub_x, py), (stub_x, py - 1))]
                junctions[label] = [(stub_x, py)]
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
PAGE_WIDTH, PAGE_FREE_HEIGHT, MARGIN = 165, 85, 10


def _centred(result):
    """The whole drawing moved to the middle of the page above the title block."""
    rectangles, pins, routes, junctions, crossings, extent, names = result
    left = min(x for x, y, w, h in rectangles)
    top = min([y for x, y, w, h in rectangles] + [p[1] for name in names.values() for p in [name[0]]])
    width, height = extent[0] - left, extent[1] - top
    dx = max(MARGIN, (PAGE_WIDTH - width) // 2) - left
    dy = max(MARGIN, (PAGE_FREE_HEIGHT - height) // 2) - top

    def move(point):
        return (point[0] + dx, point[1] + dy)
    return ([(x + dx, y + dy, w, h) for x, y, w, h in rectangles],
            {key: (move(point), side, angle) for key, (point, side, angle) in pins.items()},
            {label: [(move(a), move(b)) for a, b in segments] for label, segments in routes.items()},
            {label: [move(point) for point in points] for label, points in junctions.items()},
            crossings, (extent[0] + dx, extent[1] + dy),
            {label: (move(point), angle) for label, (point, angle) in names.items()})


def layout(count: int, uses: dict[str, list[int]]):
    """Rectangles, pins, routes, junctions, crossings, extent and net-name places, in grid units.

    pins[(sheet, label)] = ((x, y), None, angle); routes[label] = [((x1, y1), (x2, y2)), ...];
    names[label] = ((x, y), angle) for the global label that keeps the net's name.
    """
    if not uses:
        return _centred(_blocks_only(count))
    star = _star(count, uses)
    if star is not None:
        return _centred(_star_layout(count, uses, *star))
    return _centred(_columns_layout(count, uses))


__all__ = ["GRID", "crossing_count", "layout"]
