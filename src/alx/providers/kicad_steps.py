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


# A pad's teardrop setting is routing, not placement: KiCad writes it into the
# pad when teardrops are added (24 Nov: AL/X's teardrop pass read as moved parts).
_ROUTING_IN_PAD = frozenset({'teardrops'})


def _canonical(node, skip=frozenset()) -> str:
    parts = []
    for child in node.children:
        if hasattr(child, 'children'):
            if child.name not in _VOLATILE and child.name not in skip:
                parts.append(_canonical(child, skip))
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
    pads = sorted(_canonical(pad, _ROUTING_IN_PAD) for pad in footprint.named('pad'))
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


def routing_added(before: Path, after: Path) -> bool:
    """Whether a board gained copper (tracks, arcs, vias, zones, copper shapes) between two files."""
    if not after.is_file():
        return False
    old = board_steps(before.read_text())[1] if before.is_file() else Counter()
    return bool(board_steps(after.read_text())[1] - old)


def fingerprints(project: Path, board_name: str) -> dict:
    """What each step looks like now: schematic files, placement and copper."""
    from alx.providers.kicad import icloud_copies
    schematics = {}
    for path in sorted(project.rglob('*.kicad_sch')):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(project).as_posix()
            if not any(part.startswith('.') for part in relative.split('/')):
                schematics[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    # iCloud's conflict copies ("Charger 2.kicad_sch") are not his schematic: on 26 Nov they made
    # an unchanged schematic read as changed, and a routing-only job asked for his approval again.
    # The digest with them is kept too: approvals given before then were recorded with it.
    with_copies = _digest(schematics)
    for copy in icloud_copies(schematics, folder=project):
        schematics.pop(copy)
    board = project / board_name
    placement, copper = board_steps(board.read_text()) if board.is_file() else (_digest([]), Counter())
    return {'schematic': _digest(schematics), 'placement': placement,
            'routing': _digest(sorted(copper.items())), 'copper': dict(copper),
            'schematic_with_copies': with_copies}


def original_of(steps: dict) -> dict:
    """The fingerprints a work copy keeps as its original: the steps alone."""
    return {name: steps[name] for name in STEPS}


def step_status(original: dict, approvals: dict, current: dict) -> dict:
    """Each step: unchanged, approved, or needs_approval; a step needs every one before it."""
    def same(name, stored):
        # A schematic recorded before 26 Nov may include iCloud's copies (see fingerprints).
        return stored is not None and (stored == current[name] or (
            name == 'schematic' and stored == current.get('schematic_with_copies')))

    status, earlier_ok = {}, True
    for index, step in enumerate(STEPS):
        record = approvals.get(step) or {}
        held = all(same(name, record.get(name)) for name in STEPS[:index + 1])
        later = approvals.get('routing') or {}
        if same(step, original.get(step)):
            state = 'unchanged'
        elif held and record:
            state = 'approved'
        elif (step == 'placement' and later and all(same(name, later.get(name))
                                                      for name in ('schematic', 'placement'))):
            # His routing approval saw the placement as it now is.
            state = 'approved'
        elif step == 'placement' and record and same('schematic', record.get('schematic')):
            # Parts nudged after he approved the placement, while routing (17 Nov: C1 moved 0.2 mm
            # for Würth's gap, and he was asked three times, blind). Shown with the routed board.
            state = 'adjusted'
        else:
            state = 'needs_approval'
        covered = all(same(name, later.get(name)) for name in ('schematic', 'placement'))
        placed = approvals.get('placement') or {}
        if step == 'routing' and state == 'unchanged' and not covered and (
                status.get('placement') == 'adjusted' or approvals.get('routing_due')
                # Moved since his placement approval, even back to where it started (19 Nov recheck).
                or (placed and placed.get('placement') != current['placement'])):
            # D-082b: a part nudged after his placement approval is covered by his routing sign-off,
            # so the routing is shown and approved again even if no copper changed (18 Nov review:
            # an adjusted placement reached write-back with no sign-off of it at all).
            state = 'needs_approval'
        if not earlier_ok and state != 'unchanged':
            # A changed step cannot stand on an earlier one he has not seen.
            state = 'waiting'
        status[step] = state
        earlier_ok = earlier_ok and state in ('unchanged', 'approved', 'adjusted')
    return status


def his_own_steps(approvals: dict, before: dict, after: dict) -> dict:
    """Approvals with each step Friedl changed himself counted as approved as he saved it.

    26 Nov: he moved four parts in KiCad and saved, and was then asked to approve his own
    placement before a clearance fix could go in. Only the steps his edit changed: her work in
    other steps still needs his approval.
    """
    approvals = dict(approvals)
    for index, step in enumerate(STEPS):
        if before[step] != after[step]:
            approvals[step] = {name: after[name] for name in STEPS[:index + 1]}
            if step == 'placement':
                approvals[step]['routing_seen'] = after['routing']
            if step == 'routing':
                approvals.pop('routing_due', None)
    return approvals


def ready(status: dict, step: str) -> bool:
    """Every step up to and including this one is unchanged or approved."""
    return all(status[name] in ('unchanged', 'approved', 'adjusted') for name in STEPS[:STEPS.index(step) + 1])


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


def board_pad_nets(board_text: str) -> dict:
    """Each board pad as REF.PAD with its net's name, from the one board index (18 Nov)."""
    from alx.providers.kicad_index import board_index, pad_nets
    return pad_nets(board_index(board_text))


def board_groups(board_text: str, with_parts: bool = False):
    """Every net of a board as its set of (reference, pad), for nets joining two or more pads,
    from the one board index (18 Nov). with_parts also gives every part with a connected pad
    (not a logo or a hole)."""
    from alx.providers.kicad_index import board_index, net_groups
    return net_groups(board_index(board_text), with_parts)


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

    def names(nodes):
        # Every pin, in full: each line is the identity of its difference (review of #247: cut to
        # twelve pins, a new break on a large net read as the old one). Shortened only to show.
        return ', '.join('%s.%s' % node for node in sorted(nodes))
    # A net that differs by a few pins is named by what differs, not listed whole
    # (16 Nov: three new GND pins listed every GND pin on the board, twice).
    lines, matched = [], set()
    for group in sorted(left - right, key=sorted):
        best = max(right, key=lambda other: len(group & other), default=frozenset())
        common = group & best
        if not common:
            lines.append('schematic only: ' + names(group))
            continue
        matched.add(best)
        anchor = '%s.%s' % min(common)
        if group - best:
            lines.append('schematic joins to the net of %s, the board does not: %s' % (anchor, names(group - best)))
        if best - group:
            lines.append('board joins to the net of %s, the schematic does not: %s' % (anchor, names(best - group)))
    lines += ['board only: ' + names(group) for group in sorted(right - left - matched, key=sorted)
              if not any(group & other for other in left - right)]
    lines += ['part on the board only: ' + ref for ref in sorted(board_refs - schematic_refs)]
    lines += ['part in the schematic only: ' + ref for ref in sorted(schematic_refs - board_refs)]
    return sorted(lines)


def shown_mismatch(line: str, limit: int = 12) -> str:
    """A parity line as it is shown: a long pin list cut to the first few."""
    head, sep, pins = line.rpartition(': ')
    items = pins.split(', ')
    if not sep or len(items) <= limit:
        return line
    return '%s: %s and %d more' % (head, ', '.join(items[:limit]), len(items) - limit)


__all__ = ['STEPS', 'board_groups', 'board_pad_nets', 'board_steps', 'edit_refusal', 'fingerprints', 'parity_mismatches',
           'ready', 'shown_mismatch', 'step_status']
