"""Tests address the one `kicad` capability by action (D-074)."""
from dataclasses import dataclass
from typing import Any

ACTION = {
    'check_kicad_board': 'check', 'edit_kicad_work_copy': 'edit',
    'export_kicad_manufacturing': 'export', 'read_reference_file': 'read_reference',
    'write_back_kicad_board': 'write_back', 'analyse_kicad_copper': 'analyse_copper',
    'simulate_kicad_circuit': 'simulate',
}


@dataclass
class ActionResult:
    """A kicad result with its action's own values, as each test reads them."""
    state: Any
    failure: Any
    values: Any
    call_id: str
    capability_id: str


def kicad_call(executors, action, arguments):
    action = ACTION.get(action, action)
    result = executors['kicad']({'action': action, **arguments})
    values = result.values.get('result', {}) if result.values else result.values
    return ActionResult(result.state, result.failure, values, result.call_id, result.capability_id)
