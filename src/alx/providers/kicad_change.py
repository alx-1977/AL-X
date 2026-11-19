"""Board edits as structured changes, carried out and checked by code (D-084).

The model writes a change: a list of operations with exact references, pins,
nets and places. `validate_change` checks its shape and says which library
parts it needs; `CHANGE_HELPERS` carries it out inside the confined KiCad
script run, with the tested board and schematic helpers; `unmet` checks the
result against what the change declared, from KiCad's own netlist. Nothing
here decides what a change should be.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

PIN = re.compile(r'^[^.\s]+\.[^.\s]+$')

SCHEMATIC_OPS = {
    'remove_symbols': ('sheet', 'refs'),
    'swap_symbol': ('sheet', 'ref', 'lib_id'),
    'add_symbol': ('sheet', 'ref', 'like', 'at'),
    'wire': ('sheet', 'from', 'to'),
    'label': ('sheet', 'pin', 'net'),
    'power': ('sheet', 'port'),
    'no_connect': ('sheet', 'pins'),
    'tidy_fields': ('sheet', 'refs'),
    'remove_labels': ('nets',),
    'remove_items': ('sheet', 'area'),
    'remove_dangling': ('sheet',),
    'unwire': ('sheet', 'pins'),
}
BOARD_OPS = {
    'remove_parts': ('refs',),
    'replace_part': ('ref', 'footprint'),
    'add_part': ('ref', 'footprint', 'at'),
    'place': ('ref',),
    'track': ('width',),
    'via': ('net', 'at'),
    'remove_tracks': ('nets',),
    'remove_teardrops': ('area',),
    'remove_dangling_tracks': ('area',),
    'sync_from_schematic': (),
    'keepout': ('outline',),
    'remove_keepouts': ('area',),
    'copper_shape': ('net', 'outline'),
    'route': ('from', 'to'),
    'to_plane': ('pins',),
    'remove_copper_shapes': ('nets', 'area'),
    'add_teardrops': (),
    'set': ('kind', 'where', 'to'),
    'title_block': (),
}
OPS = {**SCHEMATIC_OPS, **BOARD_OPS}
# What set may change, per kind of board object (Altium's Inspector), and how it picks them.
SETTABLE = {
    'field': ('size', 'thickness', 'visible', 'angle', 'at', 'move_by'),
    'text': ('size', 'thickness', 'visible', 'angle', 'at', 'move_by', 'text'),
    'via': ('size', 'drill'),
    'track': ('width',),
    'arc': ('width',),
}
FILTERS = ('refs', 'nets', 'layer', 'name', 'value', 'footprint', 'area', 'reads', 'ids', 'all', 'named')


class ChangeError(ValueError):
    """A change that names something wrongly or leaves a field out; the detail says which."""


def plain(value: Any) -> Any:
    """A change as plain lists and dicts: the broker hands it over read-only (15 Nov: every
    structured edit of the first live run failed converting it)."""
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


def validate_change(change: Any) -> dict[str, Any]:
    """Check a change's shape; return the library parts it needs and the parts it touches."""
    if not isinstance(change, (list, tuple)) or not change or len(change) > 200:
        raise ChangeError('change is a list of 1 to 200 operations')
    symbols, footprints = set(), set()
    for index, op in enumerate(change):
        where = 'operation %d' % (index + 1)
        if not isinstance(op, Mapping) or op.get('op') not in OPS:
            raise ChangeError('%s: op is one of %s' % (where, ', '.join(sorted(OPS))))
        missing = [field for field in OPS[op['op']] if op.get(field) in (None, '', [])]
        if missing:
            raise ChangeError('%s (%s) needs %s' % (where, op['op'], ', '.join(missing)))
        for field in ('from', 'to', 'pin'):
            if op['op'] == 'set':
                break  # set's to is the properties it sets
            value = op.get(field)
            if op['op'] in ('route', 'track') and isinstance(value, (list, tuple)) and len(value) == 2 \
                    and all(isinstance(v, (int, float)) for v in value):
                continue  # a point [x, y] in mm, where a route or track may start or end
            if value is not None and not (isinstance(value, str) and PIN.match(value)):
                raise ChangeError('%s: %s is REF.PIN, like U6.9%s' % (
                    where, field, ', or a point [x, y] in mm' if op['op'] in ('route', 'track') else ''))
        for field in ('pins',):
            if op.get(field) is not None and not all(isinstance(p, str) and PIN.match(p) for p in op[field]):
                raise ChangeError('%s: pins are REF.PIN, like U6.4' % where)
        if op.get('area') is not None and not (isinstance(op['area'], (list, tuple)) and len(op['area']) == 4
                                               and all(isinstance(v, (int, float)) for v in op['area'])):
            raise ChangeError('%s: area is [x1, y1, x2, y2] in mm' % where)
        if op['op'] == 'set':
            if op['kind'] not in SETTABLE:
                raise ChangeError('%s (set): kind is one of %s' % (where, ', '.join(SETTABLE)))
            filters, values = op['where'], op['to']
            if not isinstance(filters, Mapping) or not set(filters) <= set(FILTERS):
                raise ChangeError('%s (set): where takes %s' % (where, ', '.join(FILTERS)))
            # Every filter given must filter (18 Nov review: name: null and all: "false" picked everything).
            for key, value in filters.items():
                if key in ('refs', 'nets', 'ids'):
                    ok = isinstance(value, (list, tuple)) and value and all(isinstance(v, str) and v for v in value)
                elif key == 'area':
                    ok = (isinstance(value, (list, tuple)) and len(value) == 4
                          and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))
                elif key in ('all', 'named'):
                    ok = value is True
                else:
                    ok = isinstance(value, str) and bool(value)
                if not ok:
                    raise ChangeError('%s (set): where %s is %s' % (where, key, {
                        'refs': 'a list of designators', 'nets': 'a list of net names', 'ids': 'a list of ids',
                        'area': '[x0, y0, x1, y1] in mm', 'all': 'true', 'named': 'true'}.get(key, 'a value')))
            if filters.get('all') and len(filters) > 1:
                raise ChangeError('%s (set): all: true goes alone; it picks every %s' % (where, op['kind']))
            if not isinstance(values, Mapping) or not values or not set(values) <= set(SETTABLE[op['kind']]):
                raise ChangeError('%s (set): to sets %s of a %s' % (where, ', '.join(SETTABLE[op['kind']]), op['kind']))
            for field in ('at', 'move_by'):
                if field in values and not (isinstance(values[field], (list, tuple)) and len(values[field]) == 2
                                            and all(isinstance(v, (int, float)) for v in values[field])):
                    raise ChangeError('%s (set): %s is [x, y] in mm' % (where, field))
        if op['op'] == 'power' and not (op.get('pin') or op.get('pins')):
            raise ChangeError('%s (power) needs pin, or pins for a row of neighbouring pins' % where)
        if op['op'] == 'swap_symbol' or (op['op'] == 'add_symbol' and op.get('lib_id')):
            symbols.add(op['lib_id'])
        if op['op'] in ('replace_part', 'add_part'):
            footprints.add(op['footprint'])
        if op['op'] == 'track' and not (op.get('points') or (op.get('from') and op.get('to'))):
            raise ChangeError('%s (track) needs points, or from and to pads' % where)
        if op['op'] == 'track' and not (op.get('net') or op.get('from') or op.get('to')):
            raise ChangeError('%s (track) needs net, or a from or to pad to take it from' % where)
    return {'symbols': sorted(symbols), 'footprints': sorted(footprints),
            'schematic': any(op['op'] in SCHEMATIC_OPS for op in change),
            'board': any(op['op'] in BOARD_OPS for op in change)}


def _power_pins(op):
    return list(op.get('pins') or ()) + ([op['pin']] if op.get('pin') else [])


def _label_holds(op, got: str, before_names) -> bool:
    """A label's pin is on the net it names. Where a net of exactly that name already
    exists (a global one, 16 Nov: V2 and V3), only that net will do: a local label of the
    same name makes a separate net that KiCad names after the sheet."""
    if got == op['net']:
        return True
    if op['net'] in set(before_names.values()):
        return False
    return _short(got) == _short(op['net'])


def _short(net: str) -> str:
    """A net's own name: without its sheet path, and without its bus (/RFID_Control.V3 is V3),
    16 Nov: after the conversion a V3 label landed rightly on /RFID_Control.V3."""
    return net.rsplit('/', 1)[-1].rsplit('.', 1)[-1]


def unmet(change: Sequence[Mapping[str, Any]], before: Mapping[str, frozenset],
          after: Mapping[str, frozenset], after_names: Mapping[str, str],
          board_nets: Mapping[str, str], change_result: Sequence[Mapping[str, Any]] = (),
          before_names: Mapping[str, str] | None = None) -> list[str]:
    """What the result does not do that the change declared, or does that it did not.

    before/after: each schematic pin ("U6.9") with the set of pins on its net;
    after_names/before_names: each pin's net name; board_nets: each board pad's net name;
    change_result: what each operation reported, in order (a removal's scope).
    """
    problems = []
    swapped = {op['ref'] for op in change if op['op'] == 'swap_symbol'}
    for op in change:
        kind = op['op']
        if kind == 'wire' and after.get(op['from']) is not None and op['to'] not in after.get(op['from'], ()):
            problems.append('%s is not joined to %s' % (op['from'], op['to']))
        elif kind == 'wire' and op['from'] not in after:
            problems.append('%s is not in the schematic' % op['from'])
        elif kind == 'label' and not _label_holds(op, after_names.get(op['pin'], ''), before_names or {}):
            got = after_names.get(op['pin'], 'no net')
            hint = ('; %s is a global net on this board: use kind global_label' % op['net']
                    if op['net'] in set((before_names or {}).values()) and got != op['net'] else '')
            if not hint and op['pin'].split('.')[0] in swapped and got != 'no net':
                hint = '; the old part\'s wiring may still sit on this pin: unwire it first'
            problems.append('%s is on %s, not %s%s' % (op['pin'], got, op['net'], hint))
        elif kind == 'power':
            for pin in _power_pins(op):
                if _short(after_names.get(pin, '')) != _short(op['port'].split(':', 1)[-1]):
                    problems.append('%s is on %s, not %s' % (pin, after_names.get(pin, 'no net'), op['port']))
        elif kind == 'no_connect':
            for pin in op['pins']:
                if len(after.get(pin, frozenset({pin}))) > 1:
                    problems.append('%s is joined to %s, not left unconnected'
                                    % (pin, _pins_text(after[pin] - {pin}, after_names)))
        elif kind == 'add_symbol':
            if not any(pin.split('.')[0] == op['ref'] for pin in after):
                problems.append('%s is not in the schematic' % op['ref'])
        elif kind == 'remove_symbols':
            problems += ['%s is still in the schematic' % ref for ref in op['refs']
                         if any(pin.split('.')[0] == ref for pin in after)]
        elif kind in ('replace_part', 'add_part'):
            for pad, net in (op.get('pin_nets') or {}).items():
                key = '%s.%s' % (op['ref'], pad)
                if _short(board_nets.get(key, '')) != _short(net):
                    problems.append('board pad %s is on %s, not %s' % (key, board_nets.get(key, 'no net'), net))
        elif kind == 'remove_parts':
            problems += ['%s is still on the board' % ref for ref in op['refs']
                         if any(pad.split('.')[0] == ref for pad in board_nets)]
    problems += _unasked(change, before, after, before_names or {}, after_names, change_result)
    return problems


def _pins_text(pins, names) -> str:
    """Pins named in a refusal; a whole net is named by its name (16 Nov: a pin on GND was
    reported with its 120 partners, and the agent could not see it was GND)."""
    pins = sorted(pins)
    if len(pins) <= 6:
        return ', '.join(pins)
    nets = sorted({_short(names.get(pin, '')) for pin in pins} - {''})
    return 'net %s (%d pins, e.g. %s)' % ('/'.join(nets) or '?', len(pins), ', '.join(pins[:3]))


def _unasked(change, before, after, before_names, after_names=None, change_result=()):
    """Pins joined or split that the change did not ask for.

    The nets the change should give are worked out from the nets before and
    what each operation joins: a wire its two pins, a label or power port its
    pin and that net. Every pin's net afterwards must lie within that; only
    parts swapped or removed are left out, since their pins change by design.
    15 Nov: a GND port landed on an old wire and grounded RF1; the earlier
    check left out every part the change named, tidied text included, and
    missed it.
    """
    free = {op['ref'] for op in change if op['op'] == 'swap_symbol'}
    free |= {ref for op in change if op['op'] == 'remove_symbols' for ref in op['refs']}
    parent: dict[str, str] = {}

    def find(pin):
        parent.setdefault(pin, pin)
        while parent[pin] != pin:
            parent[pin] = parent[parent[pin]]
            pin = parent[pin]
        return pin

    def join(a, b):
        parent[find(a)] = find(b)
    # A swapped part's old pins say nothing about its new ones: only the
    # operations join them.
    kept = (lambda pin: pin.split('.')[0] not in free)
    for pin, group in before.items():
        if kept(pin):
            find(pin)
            for other in group:
                if kept(other):
                    join(pin, other)
    named: dict[str, str] = {}
    for pin, name in before_names.items():
        if kept(pin) and pin in before:
            named.setdefault(_short(name), pin)
    for op in change:
        if op['op'] == 'wire':
            join(op['from'], op['to'])
        elif op['op'] in ('label', 'power'):
            net = _short(op['net'] if op['op'] == 'label' else op['port'].split(':', 1)[-1])
            for pin in (_power_pins(op) if op['op'] == 'power' else [op['pin']]):
                if net in named:
                    join(pin, named[net])
                named.setdefault(net, pin)
    # What a removal may split: only the pins and nets it targets (18 Nov review: one removal
    # anywhere in a batch let an unrelated broken connection through). remove_dangling takes
    # only what touches nothing, so it may split nothing.
    scope: set = set()
    # Each operation's own result, by its number: swaps run first, so results are in run order
    # (review of #247: a later swap's result was read as a removal's).
    numbered = {item['n']: item for item in change_result if isinstance(item, Mapping) and 'n' in item}

    def result_of(index):
        if numbered:
            return numbered.get(index + 1) or {}
        return (change_result[index] if index < len(change_result) else {}) or {}
    for index, op in enumerate(change):
        if op['op'] == 'unwire':
            scope.update(op['pins'])
        elif op['op'] == 'remove_labels':
            # Only if it removed a label at all (19 Nov recheck: removing nothing excused a split),
            # and a net named in full, or by a short name only one net has.
            done = result_of(index)
            removed = done.get('removed')
            if not (sum(removed.values()) if isinstance(removed, dict) else removed):
                continue
            for wanted in op['nets']:
                full = {name for name in set(before_names.values()) if name == wanted}
                if not full:
                    full = {name for name in set(before_names.values()) if _short(name) == _short(wanted)}
                    if len(full) > 1:
                        continue
                scope.update(pin for pin, name in before_names.items() if name in full)
        elif op['op'] == 'remove_items':
            # Only the pins wired to what went (19 Nov recheck: their whole net hid another split).
            scope.update(result_of(index).get('scope') or ())
    groups: dict[str, set] = {}
    # A part the change adds has pins only afterwards; what the change joins them to counts
    # (16 Nov: the added C93 was reported as joined unasked to L6, C44 and GND).
    for pin in set(before) | set(after):
        if pin.split('.')[0] not in free and (pin in before or pin in parent):
            groups.setdefault(find(pin), set()).add(pin)
    problems = []
    # A pin that is gone without its part being removed or swapped (18 Nov review: only pins
    # still there were looked at, so a whole net could vanish unseen).
    for pin in sorted(set(before) - set(after)):
        if pin.split('.')[0] not in free:
            problems.append('%s is gone from the schematic, but its part was not removed' % pin)
    for pin, partners in sorted(after.items()):
        if pin.split('.')[0] in free or pin not in before:
            continue
        now = {p for p in partners if p.split('.')[0] not in free}
        gained = now - groups.get(find(pin), set())
        lost = {p for p in before[pin] if p.split('.')[0] not in free} - now
        if gained:
            problems.append('%s changed connection: gained %s, which the change did not join'
                            % (pin, _pins_text(gained, after_names or {})))
        elif lost and not (pin in scope or lost <= scope):
            problems.append('%s changed connection: lost %s' % (pin, _pins_text(lost, before_names)))
    return problems[:30]


# The kinds of board object each other board operation may change; any not listed may change any.
ALL_KINDS = ('part', 'field', 'pad', 'track', 'arc', 'via', 'zone', 'keepout', 'teardrop', 'text', 'graphic')
AFFECTS = {
    'title_block': (),
    'add_teardrops': ('teardrop', 'pad', 'via'),
    'remove_teardrops': ('teardrop', 'pad', 'via'),
    'keepout': ('keepout', 'zone'),
    'remove_keepouts': ('keepout', 'zone'),
    'track': ('track', 'arc', 'teardrop', 'zone'),
    'route': ('track', 'arc', 'via', 'teardrop', 'zone'),
    'via': ('via', 'teardrop', 'zone'),
    'to_plane': ('track', 'arc', 'via', 'teardrop', 'zone'),
    'remove_tracks': ('track', 'arc', 'via', 'teardrop', 'zone'),
    'remove_dangling_tracks': ('track', 'arc', 'via', 'teardrop', 'zone'),
    'copper_shape': ('zone', 'graphic', 'teardrop'),
    'remove_copper_shapes': ('zone', 'graphic', 'teardrop'),
}
# Which indexed properties each settable property may change (positions are absolute).
_TOUCHES = {'size': ('size',), 'thickness': ('thickness',), 'visible': ('visible',), 'angle': ('angle',),
            'at': ('at', 'offset'), 'move_by': ('at', 'offset'), 'text': ('text',), 'drill': ('drill',),
            'width': ('width',)}


def _close(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-4
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    return a == b


def _holds(before, after, key, value):
    """Whether one object, after, has what set asked; before for a relative move."""
    if after is None:
        return False
    if key == 'size' and after['kind'] in ('field', 'text'):
        wanted = [value, value] if isinstance(value, (int, float)) else list(value)
        return after.get('size') is not None and _close(list(after['size'][:2]), wanted)
    if key == 'at':
        return after.get('at') is not None and _close(list(after['at'][:2]), list(value))
    if key == 'move_by':
        return (before is not None and before.get('at') is not None and after.get('at') is not None
                and _close([after['at'][0] - before['at'][0], after['at'][1] - before['at'][1]], list(value)))
    if key in ('size', 'thickness', 'angle', 'drill', 'width'):
        return after.get(key) is not None and _close(after[key], value)
    return after.get(key) == value


def set_check(change, before, after):
    """Each set operation's receipt (matched, already as asked, changed), and what does not hold.

    An object picked must end as asked (a move by where it is now). When the edit only sets,
    the whole board is compared by address: each object may differ only in the properties the
    operations that picked it name; nothing else may change, appear or go (18 Nov review: the
    first version passed the 206 thinned designator strokes, checking only what was named)."""
    old = {o['id']: o for o in before['objects']}
    new = {o['id']: o for o in after['objects']}
    allowed: dict[str, set] = {}
    for op in change:
        if op['op'] == 'set':
            for uid in op['_ids']:
                allowed.setdefault(uid, set()).update(k for key in op['to'] for k in _TOUCHES[key])
    receipts, problems = [], []
    for op in change:
        if op['op'] != 'set':
            continue
        relative = 'move_by' in op['to']
        holds = (lambda b, a: all(_holds(b, a, key, value) for key, value in op['to'].items()))
        # A relative move is never "already as asked"; an object already at its at is.
        already = [uid for uid in op['_ids'] if not relative and holds(None, old.get(uid))]
        wrong = [uid for uid in op['_ids'] if not holds(old.get(uid), new.get(uid))]
        receipts.append({'kind': op['kind'], 'matched': len(op['_ids']), 'already': len(already),
                         'changed': len(op['_ids']) - len(already) - len(wrong), 'not_as_asked': wrong[:20],
                         'ids_changed': [uid for uid in op['_ids'] if uid not in already and uid not in wrong][:50]})
        if wrong:
            problems.append('%d %s not as asked, e.g. %s' % (len(wrong), op['kind'], ', '.join(wrong[:5])))
    # Kinds of board object the other operations in the batch may change; every other object on
    # the board must stay exactly as it was (19 Nov recheck: a title-block edit switched the whole
    # comparison off, and an Edge.Cuts move passed).
    affected = set()
    for op in change:
        if op['op'] != 'set' and op['op'] not in SCHEMATIC_OPS:
            affected |= set(AFFECTS.get(op['op'], ALL_KINDS))
    appeared = sorted(uid for uid in set(new) - set(old) if new[uid]['kind'] not in affected)
    gone = sorted(uid for uid in set(old) - set(new) if old[uid]['kind'] not in affected)
    others = []
    for uid in sorted(set(old) & set(new)):
        if old[uid]['kind'] in affected:
            continue
        differing = {key for key in set(old[uid]) | set(new[uid])
                     if not _close(old[uid].get(key), new[uid].get(key))}
        differing -= allowed.get(uid, set())
        if differing:
            others.append('%s (%s: %s)' % (uid, old[uid]['kind'], ', '.join(sorted(differing))))
    if appeared or gone:
        problems.append('objects appeared (%d) or went (%d), e.g. %s' % (
            len(appeared), len(gone), ', '.join((appeared + gone)[:5])))
    if others:
        problems.append('%d objects changed that no set asked for, e.g. %s' % (len(others), '; '.join(others[:5])))
    for receipt in receipts:
        receipt['others_changed'] = len(others) + len(appeared) + len(gone)
    return receipts, problems


CHANGE_HELPERS = r'''
# ---- structured changes (D-084): the change is carried out here, not written by hand ----
def _pin(text):
    ref, number = text.split(".", 1)
    return ref, number


def _power_template(s, port):
    port = port.split(":", 1)[-1]
    for item in s.symbols():
        if item["ref"].startswith("#") and item["value"] == port:
            # By uuid: imported power symbols often share #PWR? (15 Nov).
            return item["uuid"] or item["ref"]
    ports = sorted({item["value"] for item in s.symbols() if item["ref"].startswith("#")})
    raise KeyError("no %s port on %s to copy; this sheet has %s" % (port, s.path.rsplit("/", 1)[-1],
                                                                       ", ".join(ports) or "none"))


def sync_from_schematic(b, data, place):
    """The board takes the schematic as it is: each part's footprint (swapped in place where it
    differs), value, fields and pad nets. A part new to the board goes where place says
    ({ref: [x, y, rotation?]}); parts only on the board are named, never removed."""
    out = {"replaced": [], "added": [], "fields_set": 0, "pads_renetted": 0, "needs_place": [],
           "not_in_schematic": data.get("not_in_schematic", [])}
    for ref, info in sorted(data["parts"].items()):
        if not info["footprint"]:
            continue
        found = b.FindFootprintByReference(ref)
        library_name = info["footprint"].replace(":", "/", 1)
        if found is None:
            if ref not in place:
                out["needs_place"].append(ref)
                continue
            where = place[ref]
            add_part(b, ref, library_name, (where[0], where[1]), where[2] if len(where) > 2 else 0,
                     value=info["value"])
            found = b.FindFootprintByReference(ref)
            found.SetPath(_pcbnew.KIID_PATH(info["path"]))
            out["added"].append(ref)
        elif str(found.GetFPID().GetUniStringLibId()) != info["footprint"]:
            replace_part(b, ref, library_name)
            found = b.FindFootprintByReference(ref)
            out["replaced"].append(ref)
        if info["value"] and found.GetValue() != info["value"]:
            found.SetValue(info["value"])
        for name, text in info["fields"].items():
            if name in ("Reference", "Value", "Footprint"):
                continue
            if found.HasField(name):
                if found.GetField(name).GetText() != text:
                    found.GetField(name).SetText(text)
                    out["fields_set"] += 1
            else:
                field = _pcbnew.PCB_FIELD(found, _pcbnew.FIELD_T_USER, name)
                field.SetText(text)
                field.SetVisible(False)
                field.SetLayer(_pcbnew.F_Fab if not found.IsFlipped() else _pcbnew.B_Fab)
                field.SetPosition(found.GetPosition())
                found.Add(field)
                out["fields_set"] += 1
        nets = data["nets"].get(ref, {})
        for item in found.Pads():
            name = nets.get(item.GetNumber())
            if name and item.GetNetname() != name:
                item.SetNet(_net(b, name))
                out["pads_renetted"] += 1
    return out


def apply_change(change):
    """Carry out a structured change in order; every operation's outcome, in order."""
    sheets, board, report = {}, None, []

    def sheet_of(name):
        if name not in sheets:
            sheets[name] = sheet(name)
        return sheets[name]
    # Every part swapped before anything is drawn round it, where nothing before a swap
    # names that part (16 Nov, R17 and R18: one switch's GND port was placed where the
    # other switch's new pins then came out). Reports keep each operation's own number.
    order = list(range(len(change)))
    first = next((i for i, op in enumerate(change) if op["op"] == "swap_symbol"), None)
    if first is not None:
        def names(op):
            found = {op.get("ref")} | {str(p).split(".")[0] for p in op.get("pins") or ()}
            found |= {str(op.get(k) or "").split(".")[0] for k in ("from", "to", "pin")}
            return found | set(op.get("refs") or ())
        swaps = [i for i in range(first + 1, len(change)) if change[i]["op"] == "swap_symbol"
                 and not any(change[i]["ref"] in names(change[k]) for k in range(first + 1, i))]
        order = [i for i in order if i not in swaps]
        order[first + 1:first + 1] = swaps
    for index in order:
        op = change[index]
        kind, done = op["op"], {}
        if kind == "remove_labels" and not op.get("sheet"):
            # Without a sheet: every sheet that has them (16 Nov: three refusals for a missing sheet).
            done["removed"] = {}
            folder = _os.path.dirname(BOARD_PATH)
            for name in sorted(n for n in _os.listdir(folder) if n.endswith(".kicad_sch")):
                held = sheets.get(name) or sheet(name)
                count = held.remove_labels(op["nets"], op.get("area"))
                if count:
                    sheets[name] = held
                    done["removed"][name] = count
        elif kind in ("remove_symbols", "swap_symbol", "add_symbol", "wire", "label", "power", "no_connect", "tidy_fields",
                      "remove_labels", "remove_items", "remove_dangling", "unwire"):
            s = sheet_of(op["sheet"])
            if kind == "remove_symbols":
                for ref in op["refs"]:
                    s.remove_symbol(ref)
            elif kind == "swap_symbol":
                loose = s.swap_symbol(op["ref"], op["lib_id"], value=op.get("value"),
                                      footprint=op.get("footprint"), at=op.get("at"), rotation=op.get("rotation"))
                if loose:
                    s.tidy(loose)
                done["old_pins_tidied"] = len(loose)
                # The new part's own notes (its Notes field), so they are read when it is used.
                library_part = s._libs(s._tree()[0]).get(op["lib_id"])
                for prop in _children(library_part or [], "property"):
                    if _unq(prop[1]) == "Notes" and _unq(prop[2]).strip():
                        done["notes"] = _unq(prop[2]).strip()
                # New pins where the old part's wiring still ends: that wiring is now on them
                # (15 Nov: the old VCTL wire sat on the new switch's NC pin 11).
                ends = {p for pts in s.wires() for p in (pts[0], pts[-1])} | {l["at"] for l in s.labels()}
                done["pins_still_wired"] = sorted((n for n, point in s.symbol(op["ref"])["pins"].items()
                                                   if point in ends), key=lambda n: (len(n), n))
            elif kind == "add_symbol":
                pins = s.copy_part(op["like"], op["ref"], tuple(op["at"]), op.get("rotation"))
                if op.get("lib_id"):
                    s.swap_symbol(op["ref"], op["lib_id"], value=op.get("value"))
                done["pins"] = {n: list(p) for n, p in s.symbol(op["ref"])["pins"].items()}
            elif kind == "wire":
                ref, number = _pin(op["from"])
                to_ref, to_number = _pin(op["to"])
                done["points"] = s.wire_pin_to(ref, number, s.symbol(to_ref)["pins"][to_number],
                                               via=[tuple(p) for p in op.get("via") or ()] or None)
                if not done["points"]:
                    done["already_joined"] = "%s and %s were already wired together; nothing drawn" % (
                        op["from"], op["to"])
            elif kind == "label":
                ref, number = _pin(op["pin"])
                s.label_pin(ref, number, op["net"], op.get("kind", "label"))
            elif kind == "power":
                pins = [_pin(text) for text in (op.get("pins") or [])] + ([_pin(op["pin"])] if op.get("pin") else [])
                template = op.get("template") or _power_template(s, op["port"])
                done["ports"] = len(s.port_pins(pins, template,
                                                down="GND" in op["port"].split(":", 1)[-1].upper()))
            elif kind == "no_connect":
                for text in op["pins"]:
                    ref, number = _pin(text)
                    s.add_no_connect(s.symbol(ref)["pins"][number])
            elif kind == "tidy_fields":
                for ref in op["refs"]:
                    s.tidy_fields(ref)
            elif kind == "remove_labels":
                done["removed"] = s.remove_labels(op["nets"], op.get("area"))
            elif kind == "remove_items":
                done["removed"] = s.remove_items(op["area"], tuple(op.get("kinds") or
                                                              ("wire", "label", "no_connect", "junction", "power")))
                # Its scope: the pins wired to what it removed (their nets are added by the check).
                done["scope"] = s.removed_scope
            elif kind == "remove_dangling":
                done["removed"] = s.remove_dangling(op.get("area"))
            elif kind == "unwire":
                done["removed"] = {}
                for text in op["pins"]:
                    for what, count in s.unwire(*_pin(text)).items():
                        done["removed"][what] = done["removed"].get(what, 0) + count
        else:
            if board is None:
                board = load()
            b = board
            if kind == "remove_parts":
                nets = {item.GetNetname() for ref in op["refs"] for item in part(b, ref).Pads()} - {""}
                for ref in op["refs"]:
                    b.Delete(part(b, ref))
                if op.get("with_tracks", True):
                    still = {item.GetNetname() for fp in b.GetFootprints() for item in fp.Pads()}
                    gone = [t for t in list(b.GetTracks()) if t.GetNetname() in nets - still]
                    for item in gone:
                        remove(b, item)
                    done["copper_removed"] = len(gone)
            elif kind == "replace_part":
                done.update(replace_part(b, op["ref"], op["footprint"], op.get("pin_nets")))
            elif kind == "add_part":
                done.update(add_part(b, op["ref"], op["footprint"], tuple(op["at"]), op.get("rotation", 0),
                                     op.get("side", "F"), op.get("value"), op.get("pin_nets")))
            elif kind == "place":
                found = part(b, op["ref"])
                if op.get("near"):
                    anchor = part(b, op["near"]["ref"]).GetPosition()
                    dx, dy = op["near"].get("offset", (0, 0))
                    move_part(b, op["ref"], mm(anchor.x) + dx, mm(anchor.y) + dy)
                elif op.get("at"):
                    move_part(b, op["ref"], *op["at"])
                if op.get("side") in ("F", "B") and (op["side"] == "B") != found.IsFlipped():
                    found.Flip(found.GetPosition(), _pcbnew.FLIP_DIRECTION_TOP_BOTTOM)
                if op.get("rotation") is not None:
                    rotate_part(b, op["ref"], op["rotation"])
                done["at"] = describe(b, op["ref"])["at"]
            elif kind == "track":
                points = [tuple(p) for p in op.get("points") or ()]
                net = op.get("net")
                if isinstance(op.get("from"), (list, tuple)):
                    points = [tuple(op["from"])] + points
                elif op.get("from"):
                    ref, number = _pin(op["from"])
                    start = pad(b, ref, number)
                    net = net or start.GetNetname()
                    points = [xy(start.GetPosition())] + points
                if isinstance(op.get("to"), (list, tuple)):
                    points = points + [tuple(op["to"])]
                elif op.get("to"):
                    ref, number = _pin(op["to"])
                    end = pad(b, ref, number)
                    # A track that starts at a point takes its net from the pad it ends on (16 Nov).
                    net = net or end.GetNetname()
                    points = points + [xy(end.GetPosition())]
                if not net:
                    raise ValueError("track %d: name its net, or start or end it on a pad" % (index + 1))
                # Ordinary connections go through route, which draws them in his style (17 Nov: the
                # agent drew control lines point by point again and none of his style applied).
                # track is for RF and feed copper (0.3 mm and wider) and short stubs.
                import math as _m
                drawn = sum(_m.hypot(q[0] - p[0], q[1] - p[1]) for p, q in zip(points, points[1:]))
                if float(op["width"]) < 0.3 and drawn > 2.0:
                    raise ValueError("track %d: a %.1f mm %s line is drawn with route, not track (route "
                                     "{from, to, follow?}; split it at points [x, y] if it cannot find a "
                                     "path in one go). track is for RF or feed copper 0.3 mm and wider, "
                                     "and stubs up to 2 mm" % (index + 1, drawn, net))
                add_track(b, net, points, op["width"], op.get("layer", "F"), op.get("radius"))
                done["length_mm"] = track_length(b, net)
            elif kind == "via":
                add_via(b, op["net"], tuple(op["at"]), op.get("diameter", 0.6), op.get("drill", 0.3))
            elif kind == "remove_tracks":
                area = op.get("area")

                def inside(item):
                    if area is None:
                        return True
                    ends = [item.GetPosition()] if item.GetClass() == "PCB_VIA" else [item.GetStart(), item.GetEnd()]
                    return all(area[0] <= mm(p.x) <= area[2] and area[1] <= mm(p.y) <= area[3] for p in ends)
                gone = [t for t in list(b.GetTracks()) if t.GetNetname() in op["nets"] and inside(t)]
                for item in gone:
                    remove(b, item)
                done["removed"] = len(gone)
            elif kind == "remove_dangling_tracks":
                # KiCad's own connectivity says which ends lead nowhere; repeated, since
                # taking one stub off can leave the next hanging (16 Nov: 17 stubs and 2 vias
                # were left where moved parts had been).
                area, total = op["area"], 0

                def within(item):
                    ends = [item.GetPosition()] if item.GetClass() == "PCB_VIA" else [item.GetStart(), item.GetEnd()]
                    return all(min(area[0], area[2]) <= mm(p.x) <= max(area[0], area[2])
                               and min(area[1], area[3]) <= mm(p.y) <= max(area[1], area[3]) for p in ends)
                for _ in range(20):
                    # Zones filled as the board now is, as the DRC will see them.
                    refill(b)
                    b.BuildConnectivity()
                    links = b.GetConnectivity()
                    copper = [_pcbnew.F_Cu, _pcbnew.B_Cu] + [_pcbnew.In1_Cu + 2 * i for i in range(30)
                                                            if b.IsLayerEnabled(_pcbnew.In1_Cu + 2 * i)]

                    def dangling(item):
                        # A via joins nothing unless it is connected on two layers, as KiCad's DRC counts it.
                        if item.GetClass() == "PCB_VIA":
                            return sum(1 for layer in copper if links.IsConnectedOnLayer(item, layer)) < 2
                        return links.TestTrackEndpointDangling(item, False)
                    gone = [t for t in list(b.GetTracks()) if within(t) and dangling(t)]
                    if not gone:
                        break
                    for item in gone:
                        remove(b, item)
                    total += len(gone)
                done["removed"] = total
            elif kind == "keepout":
                # A keep-out (rule) area: no zone fill, and by default no tracks or vias, on the
                # copper layers named (all by default). The pours pull back from it when refilled
                # (17 Nov: a chip antenna's clearance from its datasheet layout).
                area = _pcbnew.ZONE(b)
                area.SetIsRuleArea(True)
                area.SetDoNotAllowZoneFills(True)
                area.SetDoNotAllowTracks(bool(op.get("no_tracks", True)))
                area.SetDoNotAllowVias(bool(op.get("no_vias", True)))
                area.SetDoNotAllowPads(False)
                area.SetDoNotAllowFootprints(False)
                layers = _pcbnew.LSET()
                names = op.get("layers") or ["F", "B"] + ["In%d" % i for i in range(1, 31)]
                for name in names:
                    copper = {"F": _pcbnew.F_Cu, "B": _pcbnew.B_Cu}.get(name)
                    if copper is None and name.startswith("In") and name[2:].isdigit():
                        copper = _pcbnew.In1_Cu + 2 * (int(name[2:]) - 1)
                    if copper is not None and b.IsLayerEnabled(copper):
                        layers.AddLayer(copper)
                area.SetLayerSet(layers)
                if op.get("name"):
                    area.SetZoneName(op["name"])
                outline = area.Outline()
                outline.NewOutline()
                for x, y in op["outline"]:
                    outline.Append(_pcbnew.FromMM(x), _pcbnew.FromMM(y))
                b.Add(area)
                done["keepout"] = {"corners": len(op["outline"]), "layers": layers.CuStack().size()}
            elif kind == "route":
                # One connection, path found by code: 45-degree bends, as few bends and vias as it
                # can, clear of everything (17 Nov). from/to: REF.PAD or [x, y]; the net comes from
                # the pads; it may cross to another layer through vias.
                ends, sides, net = [], [], op.get("net")
                for end in (op["from"], op["to"]):
                    if isinstance(end, str):
                        ref, number = _pin(end)
                        found = pad(b, ref, number)
                        net = net or found.GetNetname()
                        ends.append(xy(found.GetPosition()))
                        sides.append(op.get("layer") if op.get("layer") and found.IsOnLayer(layer(op["layer"]))
                                     else ("F" if found.IsOnLayer(_pcbnew.F_Cu) else "B"))
                    else:
                        # A point end is on the layer named, else the first of the layers given
                        # (17 Nov: a route between points with layers [In2] started on top).
                        ends.append((float(end[0]), float(end[1])))
                        sides.append(op.get("layer") or (op.get("layers") or ["F"])[0])
                if not net:
                    raise ValueError("route %d: name its net, or start or end it on a pad" % (index + 1))
                # His style (17 Nov), from his own board: its usual width and via, its routing layers;
                # an RF-wide line (0.3 mm and up) stays on its own layer with arcs at his RF radius.
                profile = style(b)
                width = float(op.get("width") or profile["width"])
                rf = width >= 0.3
                route_layers = op.get("layers") or ([sides[0]] if rf and sides[0] == sides[1] else None)
                radius = op.get("radius", rf_radius(width) if rf else None)
                via_size = tuple(profile["via"])
                runs, vias = route(b, net, ends[0], ends[1], width, sides[0], end_on=sides[1],
                                   layers=route_layers, via=via_size, follow=op.get("follow"))
                for name, corners in runs:
                    # A curve sits inside its corner, nearer what is there; where the full RF radius
                    # would touch something the curve tightens, and at worst stays a 45-degree corner.
                    for tried in ([radius, radius / 2.0, None] if radius else [None]):
                        try:
                            add_track(b, net, corners, width, name, tried, fit=True)
                            break
                        except ValueError:
                            if tried is None:
                                raise
                for spot in vias:
                    add_via(b, net, spot, *via_size)
                # A point end where this net's copper on another layer already ends gets the via
                # that joins them (17 Nov: pieces routed layer by layer met at points, unjoined).
                for spot, side, given in zip(ends, sides, (op["from"], op["to"])):
                    if not isinstance(given, str):
                        here = point(*spot)
                        has_via = any(t.GetClass() == "PCB_VIA" and t.GetNetname() == net and t.GetPosition() == here
                                      for t in b.GetTracks())
                        other = any(t.GetClass() != "PCB_VIA" and t.GetNetname() == net and t.GetLayer() != layer(side)
                                    and here in (t.GetStart(), t.GetEnd()) for t in b.GetTracks())
                        if other and not has_via:
                            add_via(b, net, spot, *via_size)
                            vias.append(spot)
                import math as _m
                length = sum(_m.hypot(q[0] - p[0], q[1] - p[1]) for _, pts in runs for p, q in zip(pts, pts[1:]))
                # The neat score: what in this route goes against his style, for her to see.
                odd = sum(1 for _, pts in runs for p, q in zip(pts, pts[1:])
                          if _m.hypot(q[0] - p[0], q[1] - p[1]) > 0.2
                          and abs(((_m.degrees(_m.atan2(q[1] - p[1], q[0] - p[0])) + 22.5) % 45) - 22.5) > 1)
                neat = []
                square = 0
                for _, pts in runs:
                    for p, q, r in zip(pts, pts[1:], pts[2:]):
                        a = _m.atan2(q[1] - p[1], q[0] - p[0])
                        c = _m.atan2(r[1] - q[1], r[0] - q[0])
                        if abs((_m.degrees(c - a) + 180) % 360 - 180) > 60:
                            square += 1
                if square:
                    neat.append("%d corner(s) of 90 degrees or sharper (his rule: none); route from pad to "
                                "pad, or put your in-between points where the line turns by 45" % square)
                if odd:
                    neat.append("%d run(s) not at 0/45/90 degrees" % odd)
                strays = sorted({n for n, _ in runs} - set(route_layers or profile["layers"]) - set(sides))
                if strays:
                    neat.append("on %s, not one of this board's routing layers" % ", ".join(strays))
                if rf and vias:
                    neat.append("an RF line through %d via(s): keep it on one layer" % len(vias))
                if vias and len(vias) > 2:
                    neat.append("%d vias" % len(vias))
                bends = sum(len(pts) - 2 for _, pts in runs)
                if bends > 4 + 2 * len(vias):
                    neat.append("%d bends" % bends)
                straight = _m.hypot(ends[1][0] - ends[0][0], ends[1][1] - ends[0][1])
                if straight > 2 and length > 1.6 * straight:
                    neat.append("%.0f%% longer than straight" % (100 * (length / straight - 1)))
                done["route"] = {"net": net, "width": width, "radius": radius,
                                 "runs": [{"layer": n, "points": pts} for n, pts in runs],
                                 "vias": vias, "bends": bends, "length_mm": round(length, 3),
                                 "straight_mm": round(straight, 3), "neat": neat or "clean"}
            elif kind == "to_plane":
                # His rule (17 Nov): a power or ground pin goes into its plane as quickly as it can, by a
                # short stub and one via right at the pin, not a track wandering off to a via somewhere.
                import math as _m
                profile = style(b)
                via_size = tuple(op.get("via") or profile["via"])
                rules = [z for z in list(b.Zones()) + [z for fp in b.GetFootprints() for z in fp.Zones()]
                         if z.GetIsRuleArea() and z.GetDoNotAllowVias()]
                outline = _pcbnew.SHAPE_POLY_SET()
                inside = outline if b.GetBoardPolygonOutlines(outline, False) and outline.OutlineCount() else None
                if inside is not None:
                    inside.Deflate(_pcbnew.FromMM(via_size[0] / 2 + 0.3), _pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,
                                   _pcbnew.FromMM(0.01))
                made = []
                for pin in op["pins"]:
                    ref, number = _pin(pin)
                    found = pad(b, ref, number)
                    net = found.GetNetname()
                    side = _pcbnew.F_Cu if found.IsOnLayer(_pcbnew.F_Cu) else _pcbnew.B_Cu
                    planes = sorted({b.GetLayerName(on) for z in b.Zones() if not z.GetIsRuleArea()
                                     and z.GetNetname() == net for on in z.GetLayerSet().CuStack()
                                     if on != side})
                    if not planes:
                        raise ValueError("%s is on %s, which has no plane on another layer to go down to"
                                         % (pin, net or "no net"))
                    centre = xy(found.GetPosition())
                    # Already in its plane only if copper really joins it: a via in the pad, or a
                    # track of its net from the pad to a via (review of #247: any same-net via within
                    # 1.2 mm, another pin's, counted, and nothing was drawn).
                    vias = [t for t in b.GetTracks() if t.GetClass() == "PCB_VIA" and t.GetNetname() == net]

                    def at_via(spot):
                        return any(_m.hypot(*(a - c for a, c in zip(xy(spot), xy(v.GetPosition()))))
                                   <= _pcbnew.ToMM(v.GetWidth(_pcbnew.F_Cu)) / 2 + 1e-3 for v in vias)
                    # Followed through any number of its net's tracks and arcs, not just one.
                    pieces = [t for t in b.GetTracks() if t.GetClass() in ("PCB_TRACK", "PCB_ARC")
                              and t.GetNetname() == net]
                    reached, ends, grown = set(), [], True
                    while grown:
                        grown = False
                        for t in pieces:
                            if id(t) in reached:
                                continue
                            tips = (t.GetStart(), t.GetEnd())
                            if any(found.HitTest(tip) or any(_m.hypot(*(a - c for a, c in zip(xy(tip), e))) < 1e-3
                                                             for e in ends) for tip in tips):
                                reached.add(id(t))
                                ends.extend(xy(tip) for tip in tips)
                                grown = True
                    joined = any(found.HitTest(v.GetPosition()) for v in vias) or any(at_via(_pcbnew.VECTOR2I(
                        _pcbnew.FromMM(e[0]), _pcbnew.FromMM(e[1]))) for e in ends)
                    if found.GetAttribute() == _pcbnew.PAD_ATTRIB_PTH or joined:
                        made.append({"pin": pin, "already": "a hole or via of %s joined to the pin" % net})
                        continue
                    body = xy(found.GetParentFootprint().GetPosition())
                    out = (centre[0] - body[0], centre[1] - body[1])
                    size = found.GetSize(side)
                    half = _pcbnew.ToMM(max(size.x, size.y)) / 2
                    stub = max(0.15, min(0.4, _pcbnew.ToMM(min(size.x, size.y))))
                    directions = [(_m.cos(a * _m.pi / 4), _m.sin(a * _m.pi / 4)) for a in range(8)]
                    directions.sort(key=lambda d: -(d[0] * out[0] + d[1] * out[1]))
                    placed = None
                    for distance in [half + via_size[0] / 2 + 0.1 + 0.05 * k for k in range(25)]:
                        for dx, dy in directions:
                            spot = (round(centre[0] + dx * distance, 3), round(centre[1] + dy * distance, 3))
                            here = point(*spot)
                            if inside is not None and not inside.Contains(here):
                                continue
                            if any(z.Outline().Contains(here) for z in rules):
                                continue
                            try:
                                via_item = add_via(b, net, spot, *via_size)
                            except ValueError:
                                continue
                            try:
                                add_track(b, net, [centre, spot], stub, "F" if side == _pcbnew.F_Cu else "B")
                            except ValueError:
                                b.Remove(via_item)
                                continue
                            placed = spot
                            break
                        if placed:
                            break
                    if not placed:
                        raise ValueError("no room for a via within %.1f mm of %s clear of everything; move "
                                         "what crowds it, or route it with route" % (half + 1.6, pin))
                    made.append({"pin": pin, "net": net, "via": placed, "plane": planes,
                                 "stub_mm": round(_m.hypot(placed[0] - centre[0], placed[1] - centre[1]), 3)})
                done["to_plane"] = made
            elif kind == "copper_shape":
                # A solid copper polygon on one net: a taper from a wide feed to a small pad, or feed
                # copper (17 Nov, his rule: copper wider than its pad joins through a taper). Solid
                # copper, not a pour, so a no-pour keep-out (an antenna's clearance) keeps it; like a
                # track it may touch nothing of another net.
                piece = _pcbnew.PCB_SHAPE(b)
                piece.SetShape(_pcbnew.SHAPE_T_POLY)
                piece.SetPolyPoints([_pcbnew.VECTOR2I(_pcbnew.FromMM(x), _pcbnew.FromMM(y)) for x, y in op["outline"]])
                piece.SetFilled(True)
                piece.SetWidth(0)
                on = layer(op.get("layer", "F"))
                piece.SetLayer(on)
                code = _net(b, op["net"])
                piece.SetNet(code)
                _refuse_touching(b, op["net"], code, [piece], on)
                b.Add(piece)
                done["copper_shape"] = {"net": op["net"], "corners": len(op["outline"])}
            elif kind == "remove_copper_shapes":
                # Copper shapes and pours of these nets lying wholly inside area: to redraw a taper
                # or feed copper (17 Nov: pour-type shapes from an earlier version, empty in a keep-out).
                x0, y0, x1, y1 = (min(op["area"][0], op["area"][2]), min(op["area"][1], op["area"][3]),
                                  max(op["area"][0], op["area"][2]), max(op["area"][1], op["area"][3]))

                def inside(box):
                    return (x0 <= _pcbnew.ToMM(box.GetLeft()) and _pcbnew.ToMM(box.GetRight()) <= x1
                            and y0 <= _pcbnew.ToMM(box.GetTop()) and _pcbnew.ToMM(box.GetBottom()) <= y1)
                gone = 0
                for zone in list(b.Zones()):
                    if (not zone.GetIsRuleArea() and not zone.IsTeardropArea()
                            and zone.GetNetname() in op["nets"] and inside(zone.GetBoundingBox())):
                        b.Delete(zone)
                        gone += 1
                for item in list(b.GetDrawings()):
                    if (item.GetClass() == "PCB_SHAPE" and item.IsOnCopperLayer()
                            and item.GetNetname() in op["nets"] and inside(item.GetBoundingBox())):
                        b.Delete(item)
                        gone += 1
                done["removed"] = gone
            elif kind == "remove_keepouts":
                # Keep-out areas lying wholly inside area [x1, y1, x2, y2] (or only the one named),
                # to resize or replace them (17 Nov: AE1's old clearance was the wrong size).
                x0, y0, x1, y1 = (min(op["area"][0], op["area"][2]), min(op["area"][1], op["area"][3]),
                                  max(op["area"][0], op["area"][2]), max(op["area"][1], op["area"][3]))
                gone = []
                for zone in list(b.Zones()):
                    if not zone.GetIsRuleArea():
                        continue
                    if op.get("name") and zone.GetZoneName() != op["name"]:
                        continue
                    box = zone.GetBoundingBox()
                    if (x0 <= _pcbnew.ToMM(box.GetLeft()) and _pcbnew.ToMM(box.GetRight()) <= x1
                            and y0 <= _pcbnew.ToMM(box.GetTop()) and _pcbnew.ToMM(box.GetBottom()) <= y1):
                        gone.append(zone.GetZoneName() or "unnamed")
                        b.Delete(zone)
                done["removed"] = gone
            elif kind == "set":
                # The objects picked from the board index before the script (provider), by address.
                done["applied_to"] = set_properties(b, op["_ids"], op["to"])
                if op["kind"] in ("via", "track", "arc"):
                    refill(b)
            elif kind == "add_teardrops":
                # Teardrops where tracks meet pads and vias and have none (18 Nov: KiCad makes them
                # only in its own window). RF nets (curved tracks) and skip_nets get none.
                done["teardrops_added"], done["teardrops_skipped"] = add_teardrops(
                    b, op.get("skip_nets") or (), op.get("area"))
                refill(b)
            elif kind == "title_block":
                block = b.GetTitleBlock()
                if op.get("title") is not None:
                    block.SetTitle(op["title"])
                if op.get("date") is not None:
                    block.SetDate(op["date"])
                if op.get("revision") is not None:
                    block.SetRevision(op["revision"])
                if op.get("company") is not None:
                    block.SetCompany(op["company"])
                b.SetTitleBlock(block)
                done["title_block"] = {"title": block.GetTitle(), "date": block.GetDate(),
                                       "revision": block.GetRevision()}
            elif kind == "sync_from_schematic":
                done.update(sync_from_schematic(b, op["_sync"], op.get("place") or {}))
            elif kind == "remove_teardrops":
                gone = teardrops(b, op["area"])
                for item in gone:
                    remove(b, item)
                done["removed"] = len(gone)
        report.append(dict(op=kind, n=index + 1, **{k: v for k, v in done.items() if k != "pads"}))
    for s in sheets.values():
        # Every part on a sheet the change touched gets its reference and value placed his
        # way, asked or not (16 Nov, Friedl: designators and values were still a mess).
        tidied = 0
        for item in s.symbols():
            if item["ref"] and not item["ref"].startswith("#"):
                try:
                    s.tidy_fields(item["ref"])
                    tidied += 1
                except (KeyError, ValueError):
                    pass
        labels = s.tidy_labels()
        report.append(dict(op="tidy_sheet", sheet=s.path.rsplit("/", 1)[-1], parts=tidied, labels=labels))
        s.save()
    if board is not None:
        refill(board)
        save(board)
    return report
# ---- end of structured changes ----
'''

__all__ = ['BOARD_OPS', 'FILTERS', 'SETTABLE', 'set_check', 'CHANGE_HELPERS', 'ChangeError', 'OPS', 'SCHEMATIC_OPS', 'plain', 'unmet', 'validate_change']
