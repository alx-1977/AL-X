"""A new FireFli library part from a symbol and footprint AL/X chose (19 Nov 2026).

P0005's LED circuit needed a 74LVC1G123 that is not in the FireFli library; with no
way to add it, the board agent guessed names, read library files by hand and placed
KiCad's own symbols, and the job stopped. Here the part is added to a library work
copy as the library holds every part: symbol and footprint named after the part
number, FireFli's fields (maker, MPN, LCSC, datasheet, description) and a catalogue
row. Which symbol and footprint to start from, and the part's data, are AL/X's
choice (Law 1); the copying is mechanical (Law 2). The footprint is then measured
against the datasheet with check_kicad_layout, and written back with his approval.
"""

from __future__ import annotations

import csv
import io
import json
import re

FIELDS = ("Reference", "Value", "Footprint", "Datasheet", "Description", "Manufacturer", "MPN", "LCSC")


def part_name(mpn: str) -> str:
    """The library name of a part number, as the FireFli library names it."""
    return re.sub(r"[/,\\ ]", "_", mpn.strip())


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def renamed_symbol(block: str, old_name: str, new_name: str, fields: dict[str, str]) -> str:
    """A flat library symbol under its new name with FireFli's fields; missing fields hidden."""
    source = block.replace("(symbol %s" % _quoted(old_name), "(symbol %s" % _quoted(new_name), 1)
    source = re.sub(r'\(symbol "%s_(\d+_\d+)"' % re.escape(old_name),
                  lambda m: '(symbol "%s_%s"' % (new_name, m.group(1)), source)
    for name in FIELDS:
        value = fields.get(name, "")
        pattern = re.compile(r'(\(property %s )"(?:[^"\\]|\\.)*"' % re.escape(_quoted(name)))
        if pattern.search(source):
            source = pattern.sub(lambda m: m.group(1) + _quoted(value), source, count=1)
        elif value:
            hidden = ('\n\t\t(property %s %s\n\t\t\t(at 0 0 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)'
                      '\n\t\t\t(hide yes)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)'
                      '\n\t\t\t)\n\t\t)' % (_quoted(name), _quoted(value)))
            last = list(re.finditer(r"\n\t\t\(property ", source))
            if last:
                end = _block_end(source, last[-1].start() + 1)
                source = source[:end] + hidden + source[end:]
            else:
                head = source.index("\n")
                source = source[:head] + hidden + source[head:]
    return source


def _block_end(source: str, start: int) -> int:
    """The index just after the parenthesised block that opens at start."""
    depth, quoted, escape = 0, False, False
    for index in range(start, len(source)):
        char = source[index]
        if quoted:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unbalanced block")


def renamed_footprint(source: str, new_name: str) -> str:
    """A footprint under its new name, its Value the same; drawing, pads and 3D model kept."""
    source = re.sub(r'^\(footprint "(?:[^"\\]|\\.)*"', "(footprint %s" % _quoted(new_name), source, count=1)
    return re.sub(r'(\(property "Value" )"(?:[^"\\]|\\.)*"', lambda m: m.group(1) + _quoted(new_name), source,
                  count=1)


def with_symbol(library_text: str, block: str) -> str:
    """The symbol library with the new symbol added before its closing parenthesis."""
    end = library_text.rstrip().rindex(")")
    return library_text[:end].rstrip("\n") + "\n\t" + block.strip() + "\n" + library_text[end:]


def with_catalogue_row(csv_text: str, values: dict[str, str]) -> str:
    """The catalogue with one row added, in its own columns (others left empty)."""
    reader = csv.DictReader(io.StringIO(csv_text))
    columns = list(reader.fieldnames or ())
    rows = list(reader)
    rows.append({column: values.get(column, "") for column in columns})
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


__all__ = ["FIELDS", "part_name", "renamed_footprint", "renamed_symbol", "with_catalogue_row", "with_symbol"]
