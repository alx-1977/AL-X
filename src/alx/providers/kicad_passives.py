"""Capacitors for the FireFli library from JLC's catalogue, through the library's generator (26 Nov 2026).

Changing C94-C96 on P0009 to 0402 took the board agent 33 reads of AL/X's own code and of
the library generator, its README and build script, to work out how to add a 0402 part.
Here that is mechanical:

- jlc_candidates lists the capacitors JLC stocks for a value and size, under Friedl's
  brand rule (Taiyo Yuden for X5R/X7R, YAGEO CC for C0G), from the library's local copy
  of JLC's catalogue. Which one to use is AL/X's choice (Law 2).
- generator_entry and with_entry add the chosen part to generator/capacitors.py and its
  LCSC number to generator/lcsc.csv, where the generator reads them, so the next build
  keeps it.

Part numbers are decoded from the makers' own numbering: Taiyo Yuden's (UMK105B7104KV-F:
50 V, 0402, X7R, 100 nF, ±10 %, 0.5 mm) and YAGEO's CC series (CC0402JRNPO9BN101: 0402,
±5 %, C0G, 50 V, 100 pF).
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Sizes the generator has footprints for (generator/capacitors.py FOOTPRINTS).
TAIYO_SIZE = {'105': '0402', '107': '0603', '212': '0805', '325': '1210', '432': '1812'}
TAIYO_VOLTS = {'A': 4.0, 'J': 6.3, 'L': 10.0, 'E': 16.0, 'T': 25.0, 'G': 35.0, 'U': 50.0, 'H': 100.0}
TAIYO_DIELECTRIC = {'BJ': 'X5R', 'B7': 'X7R'}
TAIYO_TOLERANCE = {'J': '±5%', 'K': '±10%', 'M': '±20%'}
# Thickness letters the library's own Taiyo Yuden parts confirm, by size.
TAIYO_THICKNESS = {('0402', 'V'): '0.5 mm', ('0603', 'A'): '0.8 mm', ('0805', 'G'): '1.25 mm'}
YAGEO_VOLTS = {'5': 6.3, '6': 10.0, '7': 16.0, '8': 25.0, '9': 50.0, '0': 100.0, 'A': 200.0, 'B': 500.0}
YAGEO_TOLERANCE = {'B': '±0.1 pF', 'C': '±0.25 pF', 'D': '±0.5 pF', 'F': '±1%', 'G': '±2%', 'J': '±5%',
                   'K': '±10%'}
YAGEO_THICKNESS = {'0402': '0.5 mm', '0603': '0.8 mm'}
ENOUGH_STOCK = 1000
CACHE = Path('cache') / 'jlc-parts.sqlite3'   # inside the FireFli library, made by generator/lookup_lcsc.py

_TAIYO = re.compile(r'^([AJLETGUH])MK(105|107|212|325|432)([A-Z]?)(BJ|B7)(\d{3}|\d?R\d+)([JKM])([A-Z])')
_YAGEO = re.compile(r'^CC(0402|0603|0805|1210|1812)([BCDFGJK])[RK]NPO([056789AB])B[NB](\d{3}|\d?R\d+)$')


def picofarads(code: str) -> float:
    """A maker's capacitance code in pF: 104 is 10 x 10^4 pF, 4R7 is 4.7 pF, R50 is 0.5 pF."""
    if 'R' in code:
        return float(code.replace('R', '.') if not code.startswith('R') else '0.' + code[1:])
    return int(code[:2]) * 10 ** int(code[2])


def capacitance(pf: float) -> tuple[str, str]:
    """(the catalogue's Capacitance, the schematic's Value): ('100 nF', '100n'), ('1.8 pF', '1.8p')."""
    for scale, unit, short in ((1e6, 'µF', 'u'), (1e3, 'nF', 'n'), (1.0, 'pF', 'p')):
        if pf >= scale or scale == 1.0:
            number = '%g' % round(pf / scale, 4)
            return '%s %s' % (number, unit), number + short
    raise AssertionError('unreachable')


def volts(value: float) -> str:
    return '%g V' % value


def decode(mpn: str) -> dict | None:
    """A Taiyo Yuden X5R/X7R or YAGEO CC C0G part number as the generator's fields, else None."""
    found = _TAIYO.match(mpn)
    if found:
        voltage, size_code, _special, dielectric, code, tolerance, thickness = found.groups()
        size = TAIYO_SIZE[size_code]
        pf = picofarads(code)
        return {'mpn': mpn, 'manufacturer': 'Taiyo Yuden', 'size': size, 'pf': pf,
                'capacitance': capacitance(pf)[0], 'value': capacitance(pf)[1],
                'tolerance': TAIYO_TOLERANCE[tolerance], 'volts': TAIYO_VOLTS[voltage],
                'voltage': volts(TAIYO_VOLTS[voltage]), 'dielectric': TAIYO_DIELECTRIC[dielectric],
                'thickness': TAIYO_THICKNESS.get((size, thickness), '')}
    found = _YAGEO.match(mpn)
    if found:
        size, tolerance, voltage, code = found.groups()
        pf = picofarads(code)
        return {'mpn': mpn, 'manufacturer': 'YAGEO', 'size': size, 'pf': pf,
                'capacitance': capacitance(pf)[0], 'value': capacitance(pf)[1],
                'tolerance': YAGEO_TOLERANCE[tolerance], 'volts': YAGEO_VOLTS[voltage],
                'voltage': volts(YAGEO_VOLTS[voltage]), 'dielectric': 'C0G',
                'thickness': YAGEO_THICKNESS.get(size, '')}
    return None


def _jlc_label(library: str | None) -> str:
    return {'base': 'Basic', 'basic': 'Basic', 'preferred': 'Preferred'}.get(str(library or '').lower(), 'Extended')


def catalogue_date(cache: Path) -> str:
    return datetime.fromtimestamp(cache.stat().st_mtime, tz=timezone.utc).date().isoformat()


def jlc_candidates(cache: Path, pf: float, size: str, limit: int = 12) -> list[dict]:
    """Capacitors of this value and size that JLC stocks, under Friedl's brand rule.

    Taiyo Yuden X5R/X7R and YAGEO CC C0G only (10-30); in stock in the local catalogue
    copy, lowest voltage first within each dielectric. Each says its ratings, LCSC
    number and JLC library type."""
    if not cache.is_file():
        raise FileNotFoundError(str(cache))
    connection = sqlite3.connect('file:%s?mode=ro' % cache, uri=True)
    try:
        prefixes = ['%sMK%s' % (letter, code) for letter in TAIYO_VOLTS
                    for code, name in TAIYO_SIZE.items() if name == size]
        prefixes.append('CC%s' % size)
        found = {}
        for prefix in prefixes:
            # A range on the indexed part number: LIKE would read all seven million rows.
            for lcsc, mpn, maker, library, stock in connection.execute(
                    'SELECT lcsc, mpn, manufacturer, library, stock FROM parts WHERE mpn >= ? AND mpn < ? '
                    'AND stock > 0', (prefix, prefix + '\x7f')):
                part = decode(str(mpn))
                if part is None or abs(part['pf'] - pf) > pf * 1e-6 or part['size'] != size:
                    continue
                if part['manufacturer'].upper() not in str(maker or '').upper():
                    continue
                best = found.get(part['mpn'])
                if best is None or stock > best['stock']:
                    found[part['mpn']] = dict(part, lcsc=lcsc, jlc_library=_jlc_label(library), stock=int(stock))
    finally:
        connection.close()
    order = {'C0G': 0, 'X7R': 1, 'X5R': 2}
    # A listing with a handful left cannot build a batch; shown only when nothing else is stocked.
    plenty = [p for p in found.values() if p['stock'] >= ENOUGH_STOCK]
    ranked = sorted(plenty or found.values(), key=lambda p: (order[p['dielectric']], p['volts'], -p['stock']))
    return [{k: v for k, v in p.items() if k != 'pf'} for p in ranked[:limit]]


def jlc_listing(cache: Path, mpn: str, manufacturer: str) -> dict | None:
    """The one JLC listing of an exact part number, as lookup_lcsc.py chooses it, else None."""
    connection = sqlite3.connect('file:%s?mode=ro' % cache, uri=True)
    try:
        rows = [row for row in connection.execute(
            'SELECT lcsc, manufacturer, library, stock FROM parts WHERE mpn = ?', (mpn,))
            if manufacturer.upper() in str(row[1] or '').upper()]
    finally:
        connection.close()
    stocked = [row for row in rows if row[3]]
    if len(rows) > 1 and len(stocked) == 1:
        rows = stocked
    if len(rows) != 1:
        return None
    return {'lcsc': rows[0][0], 'jlc_library': _jlc_label(rows[0][2]), 'stock': int(rows[0][3] or 0)}


def generator_entry(part: dict, lcsc: str) -> str:
    """The line generator/capacitors.py lists the part with, in its own style."""
    if part['manufacturer'] == 'Taiyo Yuden':
        return '    taiyo(%r, %r, %r, %r, %r, %r, %r, %r),\n' % (
            part['mpn'], part['size'], part['value'], part['capacitance'], part['tolerance'],
            part['voltage'], part['dielectric'], part['thickness'])
    if part['voltage'] == '50 V':
        return '    yageo(%r, %r, %r, %r, %r, %r, %r),\n' % (
            part['mpn'], lcsc, part['size'], part['value'], part['capacitance'], part['tolerance'],
            part['thickness'])
    return ('    Part(%r, "YAGEO", %r, %r, %r, %r, %r, "C0G", %r,\n'
            '         "https://www.lcsc.com/datasheet/%s.pdf"),\n') % (
        part['mpn'], part['size'], part['value'], part['capacitance'], part['tolerance'],
        part['voltage'], part['thickness'], lcsc)


# Where each maker's parts end in the generator's PARTS list.
_TAIYO_END = '    # YAGEO C0G\n'
_PARTS_END = ')\n\nLCSC = lcsc()\n'


def with_entry(source: str, part: dict, lcsc: str) -> str:
    """generator/capacitors.py with the part added at the end of its maker's group."""
    if re.search(r'["\']%s["\']' % re.escape(part['mpn']), source):
        raise ValueError('%s is already in generator/capacitors.py' % part['mpn'])
    anchor = _TAIYO_END if part['manufacturer'] == 'Taiyo Yuden' else _PARTS_END
    if source.count(anchor) != 1:
        raise ValueError('generator/capacitors.py has changed shape: no single %r to add before' % anchor.strip())
    return source.replace(anchor, generator_entry(part, lcsc) + anchor, 1)


def with_lcsc_row(table: str, mpn: str, lcsc: str, library: str) -> str:
    """generator/lcsc.csv with the part's LCSC number and JLC library type."""
    if any(row.get('MPN') == mpn for row in csv.DictReader(io.StringIO(table))):
        raise ValueError('%s is already in generator/lcsc.csv' % mpn)
    out = io.StringIO()
    csv.writer(out, lineterminator='\n').writerow([mpn, lcsc, library, ''])
    return (table if table.endswith('\n') else table + '\n') + out.getvalue()


__all__ = ['catalogue_date', 'decode', 'jlc_candidates', 'jlc_listing', 'with_entry', 'with_lcsc_row']
