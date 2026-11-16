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
# Removing labels and leftovers may split nets (that is what they are for); it never joins any.
SPLITTING = ('remove_labels', 'remove_items', 'remove_dangling', 'unwire')
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
        if op.get('area') is not None and not (isinstance(op['area'], (list, tuple)) and len(op['area']) == 4
                                               and all(isinstance(v, (int, float)) for v in op['area'])):
            raise ChangeError('%s: area is [x1, y1, x2, y2] in mm' % where)
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
          board_nets: Mapping[str, str], touched: Sequence[str],
          before_names: Mapping[str, str] | None = None) -> list[str]:
    """What the result does not do that the change declared, or does that it did not.

    before/after: each schematic pin ("U6.9") with the set of pins on its net;
    after_names/before_names: each pin's net name; board_nets: each board pad's net name.
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
    problems += _unasked(change, before, after, before_names or {}, after_names)
    return problems


def _pins_text(pins, names) -> str:
    """Pins named in a refusal; a whole net is named by its name (16 Nov: a pin on GND was
    reported with its 120 partners, and the agent could not see it was GND)."""
    pins = sorted(pins)
    if len(pins) <= 6:
        return ', '.join(pins)
    nets = sorted({_short(names.get(pin, '')) for pin in pins} - {''})
    return 'net %s (%d pins, e.g. %s)' % ('/'.join(nets) or '?', len(pins), ', '.join(pins[:3]))


def _unasked(change, before, after, before_names, after_names=None):
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
    splitting = any(op['op'] in SPLITTING for op in change)
    groups: dict[str, set] = {}
    # A part the change adds has pins only afterwards; what the change joins them to counts
    # (16 Nov: the added C93 was reported as joined unasked to L6, C44 and GND).
    for pin in set(before) | set(after):
        if pin.split('.')[0] not in free and (pin in before or pin in parent):
            groups.setdefault(find(pin), set()).add(pin)
    problems = []
    for pin, partners in sorted(after.items()):
        if pin.split('.')[0] in free or pin not in before:
            continue
        now = {p for p in partners if p.split('.')[0] not in free}
        gained = now - groups.get(find(pin), set())
        lost = {p for p in before[pin] if p.split('.')[0] not in free} - now
        if gained:
            problems.append('%s changed connection: gained %s, which the change did not join'
                            % (pin, _pins_text(gained, after_names or {})))
        elif lost and not splitting:
            problems.append('%s changed connection: lost %s' % (pin, _pins_text(lost, before_names)))
    return problems[:30]


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
                if op.get("from"):
                    ref, number = _pin(op["from"])
                    start = pad(b, ref, number)
                    net = net or start.GetNetname()
                    points = [xy(start.GetPosition())] + points
                if op.get("to"):
                    ref, number = _pin(op["to"])
                    end = pad(b, ref, number)
                    # A track that starts at a point takes its net from the pad it ends on (16 Nov).
                    net = net or end.GetNetname()
                    points = points + [xy(end.GetPosition())]
                if not net:
                    raise ValueError("track %d: name its net, or start or end it on a pad" % (index + 1))
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

__all__ = ['BOARD_OPS', 'SPLITTING', 'CHANGE_HELPERS', 'ChangeError', 'OPS', 'SCHEMATIC_OPS', 'plain', 'unmet', 'validate_change']
