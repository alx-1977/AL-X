"""Find parts in the FireFli library by what they are (19 Nov 2026).

One P0005 job spent over a hundred calls hunting for parts: KiCad's settings read 35
times, the library list 11, a raw symbol file 21, folders browsed, and names guessed
("FireFli_Capacitors:1u_0603", "FireFli_ICs:74LVC1G123DP") that do not exist. The
library's catalogue already holds every part: this reads it and returns exact names.

Matching is mechanical (Law 2): a value is compared as a number ("1u", "1 µF", "1uF"
are one value; "4k7" is 4700), a package as text in the footprint, package or
description, and words in the query must all appear. Which part to use is AL/X's
choice; each result shows its package, so the choice is visible.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3,
           "M": 1e6, "R": 1.0}


def value_of(text: str) -> float | None:
    """A component value as a number: 100n, 1u, 1 µF, 4k7, 10k, 0R, 2.2uH, 0.47 Ω."""
    cleaned = str(text).strip().replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = re.sub(r"(?i)(f|h)$", "", cleaned)
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([pnuµmkKMR]?)(\d*)", cleaned)
    if not match:
        return None
    number, prefix, tail = match.groups()
    if tail:
        if not prefix:
            return None
        number = "%s.%s" % (number, tail)   # 4k7 -> 4.7k, 2R2 -> 2.2
    return float(number) * _PREFIX[prefix]


def _package(row: dict[str, str]) -> str:
    if row.get("Package"):
        return row["Package"]
    name = row.get("Footprint", "").split(":", 1)[-1]
    found = re.search(r"\b((?:U?DFN|QFN|TDFN|WSON|SON|SOT|SOIC|SSOP|TSSOP|MSOP|SC-?70|DO-|SMA|SMB|LGA|BGA)"
                      r"[-\w. x]*|\d{4} \(\d{4} metric\)|\d{4})", row.get("Description", ""))
    return found.group(1).strip() if found else name


def find_parts(library_root: Path, terms: str = "", value: str = "", package: str = "",
               kind: str = "", limit: int = 20) -> dict[str, Any]:
    """Library parts matching every filter given, with their exact symbol and footprint names.

    terms: words that must all appear in the part number, value, description, maker or
    footprint ("comparator", "74LVC1G123", "TVS 3.3 V"). value: a component value compared
    as a number ("1u", "10k"). package: text in the footprint, package or description
    ("0603", "DFN"). kind: the library, by its name or the first letter of its reference
    ("Capacitors", "R", "IC")."""
    catalogue = library_root / "catalogue"
    if not catalogue.is_dir():
        raise FileNotFoundError(str(catalogue))
    wanted = value_of(value) if value else None
    if value and wanted is None:
        raise ValueError("value %r is not a component value (100n, 1u, 4k7, 10k)" % value)
    words = [w.casefold() for w in re.split(r"\s+", terms.strip()) if w]
    matches = []
    for table in sorted(catalogue.glob("FireFli_*.csv")):
        library = table.stem
        if kind and kind.casefold() not in library.casefold():
            with table.open(newline="", encoding="utf-8") as handle:
                first = next(csv.DictReader(handle), {})
            if not first or not first.get("Reference", "").casefold().startswith(kind.casefold()):
                continue
        with table.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                haystack = " ".join(row.get(k, "") for k in ("MPN", "Value", "Description", "Manufacturer",
                                                          "Footprint", "Package")).casefold()
                if words and not all(w in haystack for w in words):
                    continue
                if wanted is not None:
                    have = value_of(row.get("Value", ""))
                    if have is None or abs(have - wanted) > wanted * 1e-6 + 1e-15:
                        continue
                pkg = _package(row)
                if package and package.casefold() not in " ".join(
                        (row.get("Footprint", ""), pkg, row.get("Description", ""))).casefold():
                    continue
                mpn = row.get("MPN", "")
                matches.append({
                    "symbol": "%s:%s" % (library, re.sub(r"[/,\\ ]", "_", mpn)),
                    "footprint": row.get("Footprint", ""),
                    "value": row.get("Value", ""), "mpn": mpn, "manufacturer": row.get("Manufacturer", ""),
                    "package": pkg, "lcsc": row.get("LCSC", ""), "description": row.get("Description", ""),
                    **({"jlc_library": row["JLC library"]} if row.get("JLC library") else {}),
                    **({"note": row["Note"]} if row.get("Note") else {}),
                })
    exact = [m for m in matches if words and m["mpn"].casefold() == terms.strip().casefold()]
    ordered = exact + [m for m in matches if m not in exact]
    return {"count": len(ordered), "parts": ordered[:max(1, min(limit, 100))],
            "truncated": len(ordered) > limit, "library": str(library_root)}


__all__ = ["find_parts", "value_of"]
