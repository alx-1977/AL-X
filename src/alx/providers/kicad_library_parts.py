"""Writing a new FireFli part into a library copy: its name, symbol entry and catalogue row (19 Nov 2026).

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


__all__ = ["FIELDS", "part_name", "with_catalogue_row", "with_symbol"]
