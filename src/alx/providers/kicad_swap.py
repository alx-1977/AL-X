"""One library part for another at the same pins, schematic then board (26 Nov 2026).

"C94-C96 to 0402" on P0009 meant: find or make the library part, swap each symbol, swap
each footprint, check. The board agent worked each step out by reading code. swap_part
does it in one call per step of Friedl's order (D-082): the schematic first; once he has
approved it, the board, with every pad keeping its net and the footprint taking the new
part's fields (an assembler builds from those: 16 Nov the swapped switches still named the
old MPN). The scripts here run through the ordinary edit, so every check and gate applies.
"""
from __future__ import annotations

import re
from pathlib import Path

# Fields a footprint never takes from its symbol: KiCad's own, and the simulator's.
NOT_ON_BOARD = ('Reference', 'Footprint', 'ki_keywords', 'ki_fp_filters', 'ki_description')
_PROPERTY = re.compile(r'\(property "((?:[^"\\]|\\.)*)" "((?:[^"\\]|\\.)*)"')
_RANGE = re.compile(r'^([A-Za-z_]+)(\d+)\s*[-–]\s*(?:\1)?(\d+)$')


def expand_refs(refs) -> list[str]:
    """References as given, with ranges spelt out: ["C94-C96", "R3"] -> C94, C95, C96, R3."""
    if not isinstance(refs, (list, tuple)):
        raise ValueError('refs is a list of references, e.g. ["C94-C96", "R3"]')
    out = []
    for given in refs:
        given = str(given).strip()
        span = _RANGE.match(given)
        if span:
            first, last = int(span.group(2)), int(span.group(3))
            if last < first or last - first > 200:
                raise ValueError('%s is not a range of references' % given)
            out.extend('%s%d' % (span.group(1), n) for n in range(first, last + 1))
        elif re.fullmatch(r'[A-Za-z_]+\d+[A-Za-z]?', given):
            out.append(given)
        else:
            raise ValueError('%r is not a reference' % given)
    return list(dict.fromkeys(out))


def _unescape(value: str) -> str:
    return re.sub(r'\\(.)', r'\1', value)


def library_fields(symbol_block: str) -> dict[str, str]:
    """A library symbol's own fields, by name (its first property of each name)."""
    fields = {}
    for name, value in _PROPERTY.findall(symbol_block):
        fields.setdefault(_unescape(name), _unescape(value))
    return fields


def _placed(folder: Path):
    """(every reference, library ID, own Reference, fields) of each placed symbol on the project's sheets."""
    from alx.providers.kicad import sexpr
    for sheet in sorted(Path(folder).rglob('*.kicad_sch')):
        if any(piece.startswith('.') or piece == 'backups' or piece.endswith('-backups')
               for piece in sheet.relative_to(folder).parts[:-1]):
            continue
        # Parsed, not matched by layout: sheets AL/X wrote earlier can lack KiCad's own indentation
        # (27 Nov replay: P0009's C94-C96 began "(symbol" at column 0 and were not found).
        root = sexpr(sheet.read_text(errors='replace'))
        if root.name != 'kicad_sch':
            continue
        for symbol in root.named('symbol'):
            lib_id = [str(n.children[1]) for n in symbol.named('lib_id') if len(n.children) > 1][:1]
            fields = {}
            for n in symbol.named('property'):
                if len(n.children) > 2:
                    fields.setdefault(str(n.children[1]), str(n.children[2]))
            own = fields.get('Reference')
            if not lib_id or not own:
                continue
            names = [own] + [str(n.children[1]) for n in _all(symbol, 'reference') if len(n.children) > 1]
            names = tuple(dict.fromkeys(n for n in names if not n.startswith('#')))
            if names:
                yield names, lib_id[0], own, fields


def placed_symbols(folder: Path) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Every placed symbol by reference, across the project's sheets: (library ID, the symbol's own
    Reference, every reference it carries). A sheet used more than once gives one symbol several
    references; changing it changes them all."""
    return {name: (lib_id, own, names) for names, lib_id, own, _fields in _placed(folder) for name in names}


SHOWN_CHANGES = 60


def symbol_changes(before: Path, after: Path) -> list[dict]:
    """What an edit changed on the placed symbols, part by part: its library symbol and each field
    that differs, old and new (27 Nov replay: the board agent read a 25 kB diff of three swapped
    capacitors 27 times, 2000 characters a call, to see what it had changed)."""
    def parts(folder):
        return {own: dict(fields, lib_id=lib_id) for _names, lib_id, own, fields in _placed(folder)}
    old, new = parts(before), parts(after)
    out = []
    for ref in sorted(old.keys() | new.keys()):
        was, now = old.get(ref), new.get(ref)
        if was is None or now is None:
            out.append({'ref': ref, 'change': 'added' if was is None else 'removed',
                        'lib_id': (now or was)['lib_id']})
            continue
        names = [name for name in dict.fromkeys([*was, *now]) if was.get(name) != now.get(name)]
        if names:
            out.append({'ref': ref, 'change': 'changed', 'was': {n: was.get(n) for n in names},
                        'now': {n: now.get(n) for n in names}})
    return out[:SHOWN_CHANGES]


def _all(node, name):
    for child in node.children:
        if hasattr(child, 'children'):
            if child.name == name:
                yield child
            yield from _all(child, name)


def schematic_refs(placed: dict, refs: list[str]) -> list[str]:
    """The symbols to swap for these references, by their own Reference. A shared sheet's symbol
    serves every use of the sheet, so all its references must be asked for together."""
    out = []
    for ref in refs:
        _lib_id, own, names = placed[ref]
        left = [name for name in names if name not in refs]
        if left:
            raise ValueError('%s is on a sheet used more than once: swapping it also changes %s. Name them '
                             'all, or give that use its own sheet first' % (ref, ', '.join(left)))
        out.append(own)
    return list(dict.fromkeys(out))


def schematic_script(refs: list[str], part: str) -> str:
    """Each named symbol becomes `part` where it stands; a pin that moved is joined back by a wire
    from where it was, so every connection stays (a FireFli R or C lands on the same points)."""
    return '''import json
wanted, part, done, joined = set(%r), %r, [], []
for name in sheets():
    s = sheet(name)
    here = [item for item in s.symbols() if item["ref"] in wanted and item["lib_id"] != part]
    for item in here:
        before = item["pins"]
        s.swap_symbol(item["ref"], part)
        after = s.symbol(item["ref"])["pins"]
        for number, old in before.items():
            new = after.get(number)
            if new is not None and tuple(new) != tuple(old):
                s.add_wire(tuple(old), tuple(new))
                joined.append("%%s.%%s" %% (item["ref"], number))
        done.append(item["ref"])
    if here:
        s.save()
print(json.dumps({"swapped": done, "joined": joined}))
''' % (sorted(refs), part)


def board_script(refs: list[str], footprint: str, fields: dict[str, str], keep: tuple = ()) -> str:
    """Each named footprint becomes `footprint` in its place, every pad keeping its net, with the
    new part's fields; refs in keep already have the footprint and take only the fields. The zones
    are poured again."""
    return '''import json
board, unconnected = load(), {}
for ref in %r:
    if ref not in %r:
        got = replace_part(board, ref, %r)
        if got["unconnected"]:
            unconnected[ref] = got["unconnected"]
    for name, value in %r.items():
        set_field(board, ref, name, value)
refill(board)
save(board)
print(json.dumps({"unconnected": unconnected}))
''' % (sorted(refs), sorted(keep), footprint.replace(':', '/', 1), fields)


def board_fields(fields: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in fields.items()
            if name not in NOT_ON_BOARD and not name.startswith(('ki_', 'Sim.'))}


__all__ = ['board_fields', 'board_script', 'expand_refs', 'library_fields', 'placed_symbols', 'schematic_refs',
           'schematic_script', 'symbol_changes']
