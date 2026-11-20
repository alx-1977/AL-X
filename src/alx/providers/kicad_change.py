"""Verification of script postconditions for a KiCad edit (D-084a)."""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

PIN = re.compile(r'^[^.\s]+\.[^.\s]+$')

SETTABLE = {
    'field': ('size', 'thickness', 'visible', 'angle', 'at', 'move_by'),
    'text': ('size', 'thickness', 'visible', 'angle', 'at', 'move_by', 'text'),
    'via': ('size', 'drill'),
    'track': ('width',),
    'arc': ('width',),
}


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


def _pins_text(pins, names) -> str:
    """Pins named in a refusal; a whole net is named by its name (16 Nov: a pin on GND was
    reported with its 120 partners, and the agent could not see it was GND)."""
    pins = sorted(pins)
    if len(pins) <= 6:
        return ', '.join(pins)
    nets = sorted({names.get(pin, '') for pin in pins} - {''})
    return 'net %s (%d pins, e.g. %s)' % ('/'.join(nets) or '?', len(pins), ', '.join(pins[:3]))


_TOUCHES = {key: (key,) for fields in SETTABLE.values() for key in fields}
_TOUCHES.update(at=('at', 'offset'), move_by=('at', 'offset'))


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
            for uid in op['ids']:
                allowed.setdefault(uid, set()).update(k for key in op['to'] for k in _TOUCHES[key])
    receipts, problems = [], []
    for op in change:
        if op['op'] != 'set':
            continue
        relative = 'move_by' in op['to']
        holds = (lambda b, a: all(_holds(b, a, key, value) for key, value in op['to'].items()))
        # A relative move is never "already as asked"; an object already at its at is.
        already = [uid for uid in op['ids'] if not relative and holds(None, old.get(uid))]
        wrong = [uid for uid in op['ids'] if not holds(old.get(uid), new.get(uid))]
        receipts.append({'kind': op['kind'], 'matched': len(op['ids']), 'already': len(already),
                         'changed': len(op['ids']) - len(already) - len(wrong), 'not_as_asked': wrong[:20],
                         'ids_changed': [uid for uid in op['ids'] if uid not in already and uid not in wrong][:50]})
        if wrong:
            problems.append('%d %s not as asked, e.g. %s' % (len(wrong), op['kind'], ', '.join(wrong[:5])))
    appeared = sorted(uid for uid in set(new) - set(old))
    gone = sorted(uid for uid in set(old) - set(new))
    others = []
    for uid in sorted(set(old) & set(new)):
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


def _valid_property(kind, key, value):
    def number(n):
        return isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
    if key == 'text':
        return isinstance(value, str)
    if key == 'visible':
        return isinstance(value, bool)
    if key in ('move_by', 'at') or (key == 'size' and kind in ('field', 'text') and isinstance(value, list)):
        return isinstance(value, list) and len(value) == 2 and all(number(n) for n in value)
    return number(value)


def validate_postconditions(value):
    """Validate postconditions, independent of how the script achieves them."""
    fields = {'wire': ('from', 'to'), 'label': ('pin', 'net'), 'disconnect': ('pin',),
              'add': ('ref', 'part'), 'swap': ('ref', 'part'), 'remove': ('ref',),
              'set': ('kind', 'ids', 'to')}
    if not isinstance(value, list):
        raise ChangeError('intent must be a list')
    label_nets = set()
    for i, item in enumerate(value, 1):
        if isinstance(item, dict) and 'label_net' in item:
            if (set(item) != {'label_net', 'reason'}
                    or any(not isinstance(v, str) or not v.strip() for v in item.values())):
                raise ChangeError(f'intent {i}: label_net needs a nonempty name and reason')
            if item['label_net'] in label_nets:
                raise ChangeError(f'intent {i}: duplicate label_net')
            label_nets.add(item['label_net'])
            continue
        if not isinstance(item, dict) or not isinstance(item.get('op'), str) or item['op'] not in fields:
            raise ChangeError(f'intent {i}: expected one of {", ".join(fields)}')
        required = fields[item['op']]
        if set(required) - item.keys() or set(item) - {'op', 'side', *required}:
            raise ChangeError(f'intent {i}: {item["op"]} takes {", ".join(required)} and optional side')
        if item.get('side', 'schematic') not in ('board', 'schematic'):
            raise ChangeError(f'intent {i}: side is schematic or board')
        for key in required:
            v = item[key]
            if key == 'ids':
                valid = isinstance(v, list) and v and all(isinstance(uid, str) and uid for uid in v) and len(set(v)) == len(v)
            elif item['op'] == 'set' and key == 'to':
                valid = (isinstance(v, dict) and v and isinstance(item['kind'], str)
                         and set(v) <= set(SETTABLE.get(item['kind'], ()))
                         and all(_valid_property(item['kind'], key, val) for key, val in v.items()))
            else:
                valid = isinstance(v, str) and v.strip()
                if valid and key in ('pin', 'from', 'to'):
                    valid = PIN.fullmatch(v)
            if not valid:
                raise ChangeError(f'intent {i}: invalid {key}')
        if item['op'] == 'set' and item.get('side') != 'board':
            raise ChangeError(f'intent {i}: set needs side board')
    return value


def design_state(model):
    """Reference to library ID and pin to exact KiCad net name, including singletons."""
    if model is None:
        return {}, {}
    return ({c['ref']: c['lib_id'] for c in model['components']},
            {f"{p['ref']}.{p['pin']}": n['name'] for n in model['nets'] for p in n['nodes']})


def board_state(index):
    from alx.providers.kicad_index import pad_nets
    return ({o['ref']: o['footprint'] for o in index['objects'] if o['kind'] == 'part' and o['ref']},
            pad_nets(index))


def unmet(postconditions, before, after):
    """Verify part transitions and the complete partition of pins into nets."""
    old_parts, old = before
    parts, nets = after
    problems, declared, free = [], set(), set()
    for item in postconditions:
        op = item['op']
        if op not in ('add', 'remove', 'swap'):
            continue
        ref = item['ref']
        declared.add(ref)
        holds = ((ref not in old_parts) if op == 'add' else (ref in old_parts))
        if op == 'remove':
            holds = holds and ref not in parts
        else:
            holds = holds and parts.get(ref) == item['part'] and old_parts.get(ref) != parts.get(ref)
        if not holds:
            problems.append(f'{op} {ref}: expected {item.get("part", "absent")}, '
                            f'was {old_parts.get(ref, "absent")}, now {parts.get(ref, "absent")}')
        elif op in ('remove', 'swap'):
            free.add(ref)
    for ref in sorted((old_parts.keys() | parts.keys()) - declared):
        if old_parts.get(ref) != parts.get(ref):
            problems.append(f'undeclared part {ref}: {old_parts.get(ref, "absent")} -> {parts.get(ref, "absent")}')
    problems.extend(_unasked(postconditions, old, nets, free))
    return problems


def _unasked(postconditions, before, after, free):
    """Rebuild expected pin groups from old nets, declared detachments and joins."""
    parent = {}

    def find(pin):
        parent.setdefault(pin, pin)
        while parent[pin] != pin:
            parent[pin] = parent[parent[pin]]
            pin = parent[pin]
        return pin

    def join(a, b):
        parent[find(a)] = find(b)

    named = {item[key] for item in postconditions for key in ('pin', 'from', 'to')
             if key in item and item['op'] in ('label', 'disconnect', 'wire')}

    def kept(pin):
        return pin.rsplit('.', 1)[0] not in free

    detached = {i['pin'] for i in postconditions if i['op'] == 'disconnect'}
    anchors, problems = {}, []
    for pin, net in before.items():
        if kept(pin) and pin not in detached and net:
            join(pin, anchors.setdefault(net, pin))
    for item in postconditions:
        op = item['op']
        if op == 'wire':
            join(item['from'], item['to'])
            if not after.get(item['from']) or after.get(item['from']) != after.get(item['to']):
                problems.append(f"{item['from']} is not joined to {item['to']}")
        elif op == 'label':
            pin, wanted = item['pin'], item['net']
            join(pin, anchors.setdefault(wanted, pin))
            if after.get(pin) != wanted:
                problems.append(f'{pin} expected {wanted}, found {after.get(pin) or "no net"}')
        elif op == 'disconnect':
            pin = item['pin']
            if pin not in before or pin not in after or (
                    before[pin] == after[pin] and not any(
                        p != pin and net == before[pin] and after.get(p) != net for p, net in before.items())):
                problems.append(f'{pin} did not disconnect from {before.get(pin, "missing pin")}')
            elif (after[pin] and not after[pin].startswith(('Net-', 'unconnected-'))
                  and not any(pin in (i.get('pin'), i.get('from'), i.get('to'))
                              for i in postconditions if i['op'] in ('wire', 'label'))):
                problems.append(f'{pin} is still on named net {after[pin]}')
    expected, actual = {}, {}
    for pin, net in after.items():
        if kept(pin) or pin in named:
            expected.setdefault(find(pin), set()).add(pin)
            actual.setdefault(net or ('pad', pin), set()).add(pin)
    for pin in sorted(before.keys() - after.keys()):
        if kept(pin):
            problems.append(f'{pin} disappeared')
    added_refs = {i['ref'] for i in postconditions if i['op'] == 'add'}
    for pin in sorted(after.keys() - before.keys()):
        if kept(pin) and pin.rsplit('.', 1)[0] not in added_refs:
            problems.append(f'{pin} appeared without a declared part change')
        elif (kept(pin) and pin not in named and after[pin]
              and not after[pin].startswith(('Net-', 'unconnected-'))):
            problems.append(f'{pin} appeared on undeclared net {after[pin]}')
    wired = {find(i['from']) for i in postconditions if i['op'] == 'wire'}
    old_names = {}
    for pin, name in before.items():
        if pin not in detached and kept(pin):
            old_names.setdefault(find(pin), set()).add(name)
    labelled_groups = {find(i['pin']) for i in postconditions if i['op'] == 'label'}
    for pin in sorted(after):
        if not kept(pin) and pin not in named:
            continue
        want = expected[find(pin)]
        got = actual[after[pin] or ('pad', pin)]
        if got != want:
            if got - want:
                problems.append(f'{pin} gained {_pins_text(got - want, after)}')
            if want - got:
                problems.append(f'{pin} lost {_pins_text(want - got, before)}')
        elif (pin in before and before[pin] != after[pin] and find(pin) not in labelled_groups
              and not (find(pin) in wired and after[pin] in old_names.get(find(pin), ()))
              and pin not in detached and not (before[pin].startswith(('Net-', 'unconnected-'))
                                               and after[pin].startswith(('Net-', 'unconnected-')))):
            problems.append(f'{pin} changed net from {before[pin]} to {after[pin]}')
    return problems
