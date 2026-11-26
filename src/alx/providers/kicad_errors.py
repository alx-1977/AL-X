"""The DRC errors of a work copy, each with a close-up ready for show_on_screen.

KiCad marks clearance errors only after its own checker has run, unlike Altium,
so Friedl is told of them and shown each one (26 Nov: three were reported to him,
never shown, and the copy opened for him in KiCad showed nothing).
"""
from __future__ import annotations

import re

SHOWN = 6
MARGIN_MM = 2.5
# KiCad names an item's net in square brackets: "Track [/Charger.Charger_INT] on F.Cu".
_NET = re.compile(r'\[([^\]]+)\]')


def errors_to_show(work_copy_id: str, signatures) -> dict:
    """Error-severity findings with a position, each with a board view of the spot, nets in red."""
    errors = [s for s in signatures if s.get('severity') == 'error']
    shown = []
    for finding in errors:
        at = finding.get('at')
        if not (isinstance(at, (list, tuple)) and len(at) == 2):
            continue
        x, y = float(at[0]), float(at[1])
        nets = sorted({name for item in finding.get('items', ()) for name in _NET.findall(str(item))
                       if not name.startswith('<')})
        shown.append({
            'type': finding.get('type'), 'description': finding.get('description'),
            'items': list(finding.get('items', ())), 'at': [x, y],
            'view': {'label': str(finding.get('type') or 'error')[:40], 'work_copy_id': work_copy_id,
                     'window': [round(x - MARGIN_MM, 3), round(y - MARGIN_MM, 3),
                                round(x + MARGIN_MM, 3), round(y + MARGIN_MM, 3)],
                     'nets': nets, 'marks': {'net:' + name: 'bad' for name in nets}},
        })
        if len(shown) == SHOWN:
            break
    return {'count': len(errors), 'shown': shown}
