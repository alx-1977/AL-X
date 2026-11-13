"""Deterministic, crossing-first hierarchical geometry in 2.54 mm units.

Only geometry lives here. Label scope and electrical identity remain owned by
kicad_hierarchy. The bounded search stops at zero, the crossing lower bound.
"""
from __future__ import annotations

import heapq
import itertools
import math

GRID = 2.54
# One allowance for the entire layout, including candidates that are discarded.
# Units count search nodes, neighbors and individual tree-point distance checks.
ROUTING_WORK_LIMIT = 2_000_000


class RoutingBudgetExceeded(ValueError):
    """No partial layout may be accepted when the shared allowance is spent."""


class RoutingBudget:
    def __init__(self, limit=None):
        self.remaining = ROUTING_WORK_LIMIT if limit is None else limit

    def consume(self, units=1):
        if self.remaining < units:
            raise RoutingBudgetExceeded('top-sheet routing work budget exhausted; no layout accepted')
        self.remaining -= units


def crossing_count(routes, budget=None):
    """Proper crossings of maximal straight runs from distinct connections.

    Bends and intentional shared endpoints do not count; wire/bus pairs do.
    A location is counted once per pair of connections.
    """
    count = 0
    for first, second in itertools.combinations(sorted(routes), 2):
        hits = set()
        for a, b in routes[first]:
            for c, d in routes[second]:
                if budget is not None:
                    budget.consume()
                if a[1] == b[1] and c[0] == d[0]:
                    if min(a[0], b[0]) < c[0] < max(a[0], b[0]) and min(c[1], d[1]) < a[1] < max(c[1], d[1]):
                        hits.add((c[0], a[1]))
                elif a[0] == b[0] and c[1] == d[1]:
                    if min(c[0], d[0]) < a[0] < max(c[0], d[0]) and min(a[1], b[1]) < c[1] < max(a[1], b[1]):
                        hits.add((a[0], c[1]))
        count += len(hits)
    return count


def _placements(count, uses):
    weights = [[0] * count for _ in range(count)]
    for members in uses.values():
        for a, b in itertools.combinations(members, 2):
            weights[a][b] += 1
            weights[b][a] += 1
    order, remaining = [], set(range(count))
    while remaining:
        node = min(remaining, key=lambda i: (-sum(weights[i][j] for j in order[-1:]), -sum(weights[i]), i))
        order.append(node)
        remaining.remove(node)
    orders = [tuple(order), tuple(range(count)), tuple(reversed(order))]
    if count <= 5:
        orders.extend(itertools.islice(itertools.permutations(range(count)), 48))
    else:
        orders.extend(tuple(order[i:] + order[:i]) for i in range(min(count, 8)))
    seen = set()
    for cols in dict.fromkeys([math.ceil(math.sqrt(count)), count, 2, 1]):
        for candidate in orders:
            if (cols, candidate) not in seen:
                seen.add((cols, candidate))
                yield cols, candidate


def _pins(rectangles, uses, reverse):
    pins = {}
    for i, (x, y, w, h) in enumerate(rectangles):
        sides = {side: [] for side in ('left', 'right', 'top', 'bottom')}
        for label, members in uses.items():
            if i not in members:
                continue
            partners = [rectangles[j] for j in members if j != i]
            px = sum(r[0] + r[2] / 2 for r in partners) / len(partners)
            py = sum(r[1] + r[3] / 2 for r in partners) / len(partners)
            dx, dy = px - x - w / 2, py - y - h / 2
            side = ('right' if dx >= 0 else 'left') if abs(dx) >= abs(dy) else ('bottom' if dy >= 0 else 'top')
            sides[side].append((py if side in ('left', 'right') else px, label))
        for side, labels in sides.items():
            # Coordinate order agrees at both ends of parallel connections.
            # Reverse label ties together in the second candidate.
            labels.sort(key=lambda item: (item[0], tuple(-ord(c) for c in item[1]) if reverse else tuple(map(ord, item[1]))))
            for slot, (_, label) in enumerate(labels):
                offset = 2 * (slot + 1)
                pins[i, label] = {
                    'left': ((x, y + offset), (-1, 0), 180),
                    'right': ((x + w, y + offset), (1, 0), 0),
                    'top': ((x + offset, y), (0, -1), 90),
                    'bottom': ((x + offset, y + h), (0, 1), 270),
                }[side]
    return pins


def _route(start, targets, blocked, occupied, bounds, initial_axis, budget):
    """Grid A* minimizes (crossings, bends, length), with stable node ties.

    Existing bends/endpoints and parallel overlap are obstacles. A route can
    cross another only by continuing straight through its interior, preventing
    accidental KiCad joins. Same-net tree targets are intentional junctions.
    """
    def distance(p):
        nearest = math.inf
        for q in targets:
            budget.consume()
            nearest = min(nearest, abs(p[0] - q[0]) + abs(p[1] - q[1]))
        return nearest

    state = start, initial_axis
    best = {state: (0, 0, 0)}
    queue = [(0, 0, distance(start), 0, start, initial_axis)]
    previous = {}
    while queue:
        budget.consume()
        cross, bends, _, length, p, axis = heapq.heappop(queue)
        state = p, axis
        if best.get(state) != (cross, bends, length):
            continue
        if p in targets:
            path = [p]
            while state in previous:
                budget.consume()
                state = previous[state]
                path.append(state[0])
            return list(reversed(path))
        for dx, dy, direction in ((-1, 0, 1), (0, -1, 2), (0, 1, 2), (1, 0, 1)):
            budget.consume()
            q = p[0] + dx, p[1] + dy
            if not (4 <= q[0] <= bounds[0] and 8 <= q[1] <= bounds[1]) or q in blocked:
                continue
            at, dest = occupied.get(p, 0), occupied.get(q, 0)
            if (at and (at == 3 or at == direction or axis != direction)) or (dest and (dest == 3 or dest == direction)):
                continue
            cost = cross + bool(dest), bends + (axis != direction), length + 1
            nxt = q, direction
            if nxt not in best or cost < best[nxt]:
                best[nxt] = cost
                previous[nxt] = state
                heapq.heappush(queue, (*cost[:2], cost[2] + distance(q), cost[2], q, direction))
    return None


def _segments(edges, budget=None):
    """Merge unit edges into straight runs, stopping at branch junctions."""
    neighbors = {}
    for a, b in edges:
        if budget is not None:
            budget.consume()
        neighbors.setdefault(a, set()).add(b)
        neighbors.setdefault(b, set()).add(a)
    remaining, result = set(edges), []
    while remaining:
        if budget is not None:
            budget.consume(len(remaining))  # min() scans every remaining edge.
        a, b = min(remaining)
        remaining.remove((a, b))
        ends = [a, b]
        for end in range(2):
            # Use the adjacent original endpoint to establish direction.
            prev, p = (b, a) if end == 0 else (a, b)
            while len(neighbors[p]) == 2:
                if budget is not None:
                    budget.consume()
                q = next(v for v in neighbors[p] if v != prev)
                if (q[0] - p[0], q[1] - p[1]) != (p[0] - prev[0], p[1] - prev[1]):
                    break
                edge = tuple(sorted((p, q)))
                if edge not in remaining:
                    break
                remaining.remove(edge)
                prev, p = p, q
            ends[end] = p
        result.append(tuple(ends))
    return result, sorted(p for p, adjacent in neighbors.items() if len(adjacent) > 2)


def layout(count, uses):
    """Choose placement, facing pins and routes from a bounded stable search.

    Vary connectivity orders, grid/row placements, parallel pin order and route
    order. This minimizes crossings among evaluated candidates, not globally.
    Expand the page for designs that cannot fit the standard A3 usable area.
    """
    budget = RoutingBudget()
    degree = max((sum(i in members for members in uses.values()) for i in range(count)), default=0)
    width, height = max(20, 2 * (degree + 1)), max(12, 2 * (degree + 1))
    best, score = None, None
    for cols, order in _placements(count, uses):
        rows = math.ceil(count / cols)
        extent_x, extent_y = max(150, cols * (width + 12)), max(96, rows * (height + 12))
        rects = [None] * count
        for slot, node in enumerate(order):
            col, row = slot % cols, slot // cols
            x = 10 + (col * (extent_x - width - 20) // (cols - 1) if cols > 1 else (extent_x - width - 20) // 2)
            y = 16 + (row * (extent_y - height - 24) // (rows - 1) if rows > 1 else (extent_y - height - 24) // 2)
            rects[node] = x, y, width, height
        blocked = {(x + dx, y + dy) for x, y, w, h in rects for dx in range(w + 1) for dy in range(h + 1)}
        for reverse in (False, True):
            pins = _pins(rects, uses, reverse)
            labels = sorted(uses)
            for routing_order in (labels, list(reversed(labels))):
                budget.consume()
                occupied, routes, junctions = {}, {}, {}
                for label in routing_order:
                    terminals = []
                    edges, tree = set(), set()
                    for member in sorted(uses[label]):
                        p, normal, _ = pins[member, label]
                        q = p[0] + normal[0], p[1] + normal[1]
                        terminals.append((p, q, 1 if normal[0] else 2))
                    p, q, _ = terminals[0]
                    edges.add(tuple(sorted((p, q))))
                    tree.add(q)
                    failed = q in occupied
                    for p, q, axis in terminals[1:]:
                        path = None if q in occupied else _route(q, tree, blocked, occupied, (extent_x, extent_y), axis, budget)
                        if path is None:
                            failed = True
                            break
                        edges.add(tuple(sorted((p, q))))
                        edges.update(tuple(sorted(pair)) for pair in zip(path, path[1:]))
                        tree.update(path)
                    if failed:
                        break
                    segments, joins = _segments(edges, budget)
                    routes[label], junctions[label] = segments, joins
                    for a, b in segments:
                        axis = 1 if a[1] == b[1] else 2
                        dx, dy = (b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1])
                        p = a
                        while True:
                            budget.consume()
                            occupied[p] = occupied.get(p, 0) | (3 if p in (a, b) else axis)
                            if p == b:
                                break
                            p = p[0] + dx, p[1] + dy
                else:
                    crossings = crossing_count(routes, budget)
                    if score is None or crossings < score:
                        best, score = (rects, pins, routes, junctions, crossings, (extent_x, extent_y)), crossings
                    if score == 0:
                        return best
    if best is None:
        raise ValueError('no block-avoiding top-sheet route within the page')
    return best
