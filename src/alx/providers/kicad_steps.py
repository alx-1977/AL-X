"""Board work in Friedl's order (D-082): schematic, placement, routing, then write-back.

Mechanics only. Each step is a fingerprint of what Friedl looks at in it: the
schematic files, where every part sits, and the copper. A step he has not
changed needs no approval; a changed one needs his, and an approval holds only
while that step and every step before it are as he saw them. Nothing here
reads his words or decides what a change means.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

STEPS = ('schematic', 'placement', 'routing')
COPPER = ('segment', 'arc', 'via', 'zone')
# Board drawings are copper when they sit on a copper layer: a taper or a feed
# drawn as a shape (review, 15 Nov: such copper slipped past the step lock).
DRAWINGS = ('gr_poly', 'gr_rect', 'gr_line', 'gr_arc', 'gr_circle', 'gr_curve')
# What KiCad rewrites without anyone changing the design: identifiers, the
# fill it recomputes on every refill, and net names a rename touches (a
# wrong connection is caught by the pin-by-pin comparison, not here).
_VOLATILE = frozenset({'uuid', 'tstamp', 'net', 'net_name', 'filled_polygon', 'fill_segments'})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _canonical(node) -> str:
    parts = []
    for child in node.children:
        if hasattr(child, 'children'):
            if child.name not in _VOLATILE:
                parts.append(_canonical(child))
        else:
            parts.append(json.dumps(child))
    return '(' + ' '.join(parts) + ')'


def _footprint_place(footprint, references) -> list:
    refs = references(footprint)
    at = footprint.named('at')
    layer = footprint.named('layer')
    name = footprint.children[1] if len(footprint.children) > 1 and isinstance(footprint.children[1], str) else ''
    # Its pads too: a pad made larger or moved is a change to the placement
    # Friedl approved, not something to slip past it (review, 15 Nov).
    pads = sorted(_canonical(pad) for pad in footprint.named('pad'))
    return [refs[0] if refs else '', name.split(':', 1)[-1],
            layer[0].children[1] if layer and len(layer[0].children) > 1 else '',
            [str(v) for v in at[0].children[1:]] if at else [], pads]


def board_steps(board_text: str):
    """The placement fingerprint, and the copper as a count of items, of one board."""
    from alx.providers.kicad import references, sexpr
    tree = sexpr(board_text)
    places = sorted(_footprint_place(f, references) for f in tree.named('footprint'))
    def on_copper(node):
        layers = node.named('layer') + node.named('layers')
        return any(isinstance(v, str) and v.endswith('.Cu') for item in layers for v in item.children[1:])
    copper = Counter(_canonical(node) for node in tree.children
                     if hasattr(node, 'children') and (node.name in COPPER
                                                       or (node.name in DRAWINGS and on_copper(node))))
    return _digest(places), copper


def fingerprints(project: Path, board_name: str) -> dict:
    """What each step looks like now: schematic files, placement and copper."""
    schematics = {}
    for path in sorted(project.rglob('*.kicad_sch')):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(project).as_posix()
            if not any(part.startswith('.') for part in relative.split('/')):
                schematics[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    board = project / board_name
    placement, copper = board_steps(board.read_text()) if board.is_file() else (_digest([]), Counter())
    return {'schematic': _digest(schematics), 'placement': placement,
            'routing': _digest(sorted(copper.items())), 'copper': dict(copper)}


def step_status(original: dict, approvals: dict, current: dict) -> dict:
    """Each step: unchanged, approved, or needs_approval; a step needs every one before it."""
    status, earlier_ok = {}, True
    for index, step in enumerate(STEPS):
        record = approvals.get(step) or {}
        held = all(record.get(name) == current[name] for name in STEPS[:index + 1])
        if current[step] == original.get(step):
            state = 'unchanged'
        elif held and record:
            state = 'approved'
        else:
            state = 'needs_approval'
        if not earlier_ok and state != 'unchanged':
            # A changed step cannot stand on an earlier one he has not seen.
            state = 'waiting'
        status[step] = state
        earlier_ok = earlier_ok and state in ('unchanged', 'approved')
    return status


def ready(status: dict, step: str) -> bool:
    """Every step up to and including this one is unchanged or approved."""
    return all(status[name] in ('unchanged', 'approved') for name in STEPS[:STEPS.index(step) + 1])


def edit_refusal(before: dict, after: dict, original: dict, approvals: dict):
    """Why an edit breaks the order, or None.

    Parts may move and copper may be removed only once the schematic is
    approved; copper may be added only once the placement is approved too.
    """
    status = step_status(original, approvals, after)
    added = Counter(after['copper']) - Counter(before['copper'])
    removed = Counter(before['copper']) - Counter(after['copper'])
    if added and not ready(status, 'placement'):
        return {'needs': 'schematic' if not ready(status, 'schematic') else 'placement',
                'detail': 'new copper before Friedl approved the %s' % (
                    'schematic' if not ready(status, 'schematic') else 'placement'),
                'copper_added': sum(added.values()), 'steps': status}
    if (after['placement'] != before['placement'] or removed) and not ready(status, 'schematic'):
        return {'needs': 'schematic', 'detail': 'the board changed before Friedl approved the schematic',
                'steps': status}
    return None


def board_groups(board_text: str, with_parts: bool = False):
    """Every net of a board as its set of (reference, pad), for nets joining two or more pads.

    with_parts also gives every part with a connected pad (not a logo or a hole).
    """
    from alx.providers.kicad import references, sexpr
    tree = sexpr(board_text)
    names = {}
    for net in tree.named('net'):
        if len(net.children) > 2:
            names[str(net.children[1])] = str(net.children[2])
    groups = {}
    for footprint in tree.named('footprint'):
        refs = references(footprint)
        # A board item with no reference (a logo, a graphic) is not a part.
        if not refs or not refs[0] or refs[0].startswith('#'):
            continue
        for pad in footprint.named('pad'):
            nets = pad.named('net')
            if not nets or len(nets[0].children) < 2:
                continue
            # KiCad 9 writes the name beside the number; KiCad 10 the name alone.
            values = [str(v) for v in nets[0].children[1:]]
            name = values[-1] if len(values) > 1 else names.get(values[0], values[0])
            if name:
                groups.setdefault(name, set()).add((refs[0], str(pad.children[1])))
    joined = {frozenset(nodes) for nodes in groups.values() if len(nodes) > 1}
    if with_parts:
        return joined, {ref for nodes in groups.values() for ref, _ in nodes}
    return joined


def parity_mismatches(schematic_nets: dict, board_text: str) -> list:
    """Connections the schematic and the board disagree on, pin by pin, as plain lines.

    Only parts on both sides are compared; a part on one side only is named.
    """
    board, board_refs = board_groups(board_text, with_parts=True)
    schematic = {nodes for nodes in schematic_nets if len(nodes) > 1}
    schematic_refs = {ref for nodes in schematic_nets for ref, _ in nodes}
    shared = board_refs & schematic_refs

    def within(group):
        return frozenset(node for node in group if node[0] in shared)
    left = {g for g in map(within, schematic) if len(g) > 1}
    right = {g for g in map(within, board) if len(g) > 1}
    lines = ['schematic only: ' + ', '.join('%s.%s' % node for node in sorted(group))
             for group in left - right]
    lines += ['board only: ' + ', '.join('%s.%s' % node for node in sorted(group))
              for group in right - left]
    lines += ['part on the board only: ' + ref for ref in sorted(board_refs - schematic_refs)]
    lines += ['part in the schematic only: ' + ref for ref in sorted(schematic_refs - board_refs)]
    return sorted(lines)


__all__ = ['STEPS', 'board_groups', 'board_steps', 'edit_refusal', 'fingerprints', 'parity_mismatches',
           'ready', 'step_status']
