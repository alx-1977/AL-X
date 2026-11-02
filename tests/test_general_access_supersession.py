"""D-063 / Law 0: general access replaces four narrow read capabilities."""

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from alx import tools
from alx.bootstrap.kicad import build_kicad_runtime
from alx.bootstrap.particle_local import build_particle_local_runtime
from alx.contracts import CapabilityDefinition
from alx.core.model_reasoner import _catalogue_payload


REMOVED = {
    'read_own_source', 'list_particle_usb_devices',
    'read_particle_serial_log', 'find_kicad_library_footprint',
}


def assert_absent(identifiers):
    assert REMOVED.isdisjoint(identifiers)


def test_no_removed_id_in_complete_catalogue_or_executor_maps(tmp_path):
    # Inspect every tool module, including definitions not currently enabled
    # by runtime configuration, rather than only the default catalogue.
    definitions = {}
    for entry in pkgutil.iter_modules(tools.__path__):
        module = importlib.import_module(f'{tools.__name__}.{entry.name}')
        for value in vars(module).values():
            for candidate in value if isinstance(value, (tuple, list)) else (value,):
                if isinstance(candidate, CapabilityDefinition):
                    definitions[candidate.capability_id] = candidate
    assert definitions
    assert_absent(definitions)
    catalogue = _catalogue_payload(tuple(definitions.values()))
    for capability_id in REMOVED:
        assert capability_id not in catalogue

    for runtime in (
        build_kicad_runtime(lambda: 'call', {}, tmp_path),
        build_particle_local_runtime(lambda: 'call', {}, tmp_path),
    ):
        assert_absent(runtime.executors)
        assert_absent(runtime.policies)

    # Covers every bootstrap, executor map, policy and prompt, including maps
    # that cannot be composed here without external services. No production
    # reference may retain a removed path.
    for path in (ROOT / 'src/alx').rglob('*'):
        if path.is_file() and path.suffix in {'.py', '.html', '.js', '.json'}:
            source = path.read_text()
            for capability_id in REMOVED:
                assert capability_id not in source, path


@pytest.mark.parametrize('capability_id', sorted(REMOVED))
def test_restored_registration_or_executor_is_rejected(capability_id):
    with pytest.raises(AssertionError):
        assert_absent({'run_program', capability_id})
