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


def placed_symbols(folder: Path) -> dict[str, str]:
    """Every placed symbol's library ID by reference, across the project's sheets, including
    each instance reference of a sheet used more than once."""
    found = {}
    for sheet in sorted(Path(folder).rglob('*.kicad_sch')):
        if any(piece.startswith('.') or piece == 'backups' or piece.endswith('-backups')
               for piece in sheet.relative_to(folder).parts[:-1]):
            continue
        source = sheet.read_text(errors='replace')
        for match in re.finditer(r'\n\t\(symbol\n\t\t\(lib_id "((?:[^"\\]|\\.)*)"\)(.*?)\n\t\)', source, re.S):
            body = match.group(2)
            names = re.findall(r'\(property "Reference" "((?:[^"\\]|\\.)*)"', body)[:1]
            names += re.findall(r'\(reference "((?:[^"\\]|\\.)*)"\)', body)
            for name in names:
                if not name.startswith('#'):
                    found[_unescape(name)] = _unescape(match.group(1))
    return found


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


def board_script(refs: list[str], footprint: str, fields: dict[str, str]) -> str:
    """Each named footprint becomes `footprint` in its place, every pad keeping its net, with the
    new part's fields; the zones are poured again."""
    return '''import json
board, unconnected = load(), {}
for ref in %r:
    got = replace_part(board, ref, %r)
    if got["unconnected"]:
        unconnected[ref] = got["unconnected"]
    for name, value in %r.items():
        set_field(board, ref, name, value)
refill(board)
save(board)
print(json.dumps({"unconnected": unconnected}))
''' % (sorted(refs), footprint.replace(':', '/', 1), fields)


def board_fields(fields: dict[str, str]) -> dict[str, str]:
    return {name: value for name, value in fields.items()
            if name not in NOT_ON_BOARD and not name.startswith(('ki_', 'Sim.'))}


__all__ = ['board_fields', 'board_script', 'expand_refs', 'library_fields', 'placed_symbols', 'schematic_script']
