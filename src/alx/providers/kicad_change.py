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
    'wire': ('sheet', 'from', 'to'),
    'label': ('sheet', 'pin', 'net'),
    'power': ('sheet', 'pin', 'port'),
    'no_connect': ('sheet', 'pins'),
    'tidy_fields': ('sheet', 'refs'),
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
}
OPS = {**SCHEMATIC_OPS, **BOARD_OPS}


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
    symbols, footprints, touched = set(), set(), set()
    for index, op in enumerate(change):
        where = 'operation %d' % (index + 1)
        if not isinstance(op, Mapping) or op.get('op') not in OPS:
            raise ChangeError('%s: op is one of %s' % (where, ', '.join(sorted(OPS))))
        missing = [field for field in OPS[op['op']] if op.get(field) in (None, '', [])]
        if missing:
            raise ChangeError('%s (%s) needs %s' % (where, op['op'], ', '.join(missing)))
        for field in ('from', 'to', 'pin'):
            if op.get(field) is not None and not (isinstance(op[field], str) and PIN.match(op[field])):
                raise ChangeError('%s: %s is REF.PIN, like U6.9' % (where, field))
        for field in ('pins',):
            if op.get(field) is not None and not all(isinstance(p, str) and PIN.match(p) for p in op[field]):
                raise ChangeError('%s: pins are REF.PIN, like U6.4' % where)
        if op['op'] == 'swap_symbol':
            symbols.add(op['lib_id'])
        if op['op'] in ('replace_part', 'add_part'):
            footprints.add(op['footprint'])
        if op['op'] == 'track' and not (op.get('points') or (op.get('from') and op.get('to'))):
            raise ChangeError('%s (track) needs points, or from and to pads' % where)
        for field in ('ref',):
            if isinstance(op.get(field), str):
                touched.add(op[field])
        for field in ('refs',):
            touched.update(r for r in op.get(field) or () if isinstance(r, str))
        for field in ('from', 'to', 'pin'):
            if isinstance(op.get(field), str) and PIN.match(op[field]):
                touched.add(op[field].split('.')[0])
        touched.update(p.split('.')[0] for p in op.get('pins') or () if isinstance(p, str))
    return {'symbols': sorted(symbols), 'footprints': sorted(footprints), 'touched': sorted(touched),
            'schematic': any(op['op'] in SCHEMATIC_OPS for op in change),
            'board': any(op['op'] in BOARD_OPS for op in change)}


def _short(net: str) -> str:
    return net.rsplit('/', 1)[-1]


def unmet(change: Sequence[Mapping[str, Any]], before: Mapping[str, frozenset],
          after: Mapping[str, frozenset], after_names: Mapping[str, str],
          board_nets: Mapping[str, str], touched: Sequence[str]) -> list[str]:
    """What the result does not do that the change declared, or does that it did not.

    before/after: each schematic pin ("U6.9") with the set of pins on its net;
    after_names: each pin's net name; board_nets: each board pad's net name.
    """
    problems = []
    for op in change:
        kind = op['op']
        if kind == 'wire' and after.get(op['from']) is not None and op['to'] not in after.get(op['from'], ()):
            problems.append('%s is not joined to %s' % (op['from'], op['to']))
        elif kind == 'wire' and op['from'] not in after:
            problems.append('%s is not in the schematic' % op['from'])
        elif kind == 'label' and _short(after_names.get(op['pin'], '')) != _short(op['net']):
            problems.append('%s is on %s, not %s' % (op['pin'], after_names.get(op['pin'], 'no net'), op['net']))
        elif kind == 'power' and _short(after_names.get(op['pin'], '')) != _short(op['port'].split(':', 1)[-1]):
            problems.append('%s is on %s, not %s' % (op['pin'], after_names.get(op['pin'], 'no net'), op['port']))
        elif kind == 'no_connect':
            for pin in op['pins']:
                if len(after.get(pin, frozenset({pin}))) > 1:
                    problems.append('%s is joined to %s, not left unconnected'
                                    % (pin, ', '.join(sorted(after[pin] - {pin}))))
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
    # Nothing else may be joined or split: an untouched part's pins keep the
    # same partners among untouched parts.
    touched = set(touched)
    for pin, partners in before.items():
        if pin.split('.')[0] in touched or pin not in after:
            continue
        old = {p for p in partners if p.split('.')[0] not in touched}
        new = {p for p in after[pin] if p.split('.')[0] not in touched}
        if old != new:
            problems.append('%s changed connection: %s' % (pin, ', '.join(sorted(old ^ new))[:200]))
    return problems


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


def apply_change(change):
    """Carry out a structured change in order; every operation's outcome, in order."""
    sheets, board, report = {}, None, []

    def sheet_of(name):
        if name not in sheets:
            sheets[name] = sheet(name)
        return sheets[name]
    for index, op in enumerate(change):
        kind, done = op["op"], {}
        if kind in ("remove_symbols", "swap_symbol", "wire", "label", "power", "no_connect", "tidy_fields"):
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
            elif kind == "wire":
                ref, number = _pin(op["from"])
                to_ref, to_number = _pin(op["to"])
                done["points"] = s.wire_pin_to(ref, number, s.symbol(to_ref)["pins"][to_number])
            elif kind == "label":
                ref, number = _pin(op["pin"])
                s.label_pin(ref, number, op["net"], op.get("kind", "label"))
            elif kind == "power":
                ref, number = _pin(op["pin"])
                template = op.get("template") or _power_template(s, op["port"])
                if "GND" in op["port"].split(":", 1)[-1].upper():
                    s.ground_pin(ref, number, template)
                else:
                    s.power_pin(ref, number, template)
            elif kind == "no_connect":
                for text in op["pins"]:
                    ref, number = _pin(text)
                    s.add_no_connect(s.symbol(ref)["pins"][number])
            elif kind == "tidy_fields":
                for ref in op["refs"]:
                    s.tidy_fields(ref)
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
                if op.get("from"):
                    ref, number = _pin(op["from"])
                    start = pad(b, ref, number)
                    net = net or start.GetNetname()
                    points = [xy(start.GetPosition())] + points
                if op.get("to"):
                    ref, number = _pin(op["to"])
                    points = points + [xy(pad(b, ref, number).GetPosition())]
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
            elif kind == "remove_teardrops":
                gone = teardrops(b, op["area"])
                for item in gone:
                    remove(b, item)
                done["removed"] = len(gone)
        report.append(dict(op=kind, n=index + 1, **{k: v for k, v in done.items() if k != "pads"}))
    for s in sheets.values():
        s.save()
    if board is not None:
        refill(board)
        save(board)
    return report
# ---- end of structured changes ----
'''

__all__ = ['BOARD_OPS', 'CHANGE_HELPERS', 'ChangeError', 'OPS', 'SCHEMATIC_OPS', 'plain', 'unmet', 'validate_change']
