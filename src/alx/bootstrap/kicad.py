"""Compose D-061 capabilities through the existing broker and safety gate."""
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from alx.config.settings import KiCadSettings
from alx.providers.coding_process import run_kicad_cli, run_kicad_python, run_kicad_engine
from alx.providers.kicad import KiCadProvider
from alx.safety import AuthorityPolicy
from alx.tools.kicad import DEFINITIONS, build_kicad_executors


@dataclass(frozen=True)
class KiCadRuntime:
    definitions: tuple
    policies: dict
    executors: dict
    permissions: frozenset
    provider: KiCadProvider | None = None


def build_kicad_runtime(call_id_source, environment, storage_root, runner=None):
    settings = KiCadSettings.from_environment(environment, storage_root)
    provider = KiCadProvider(settings, partial(run_kicad_cli, runner=runner,
                                             timeout_seconds=settings.cli_timeout_seconds),
                             partial(run_kicad_python, runner=runner,
                                     timeout_seconds=settings.script_timeout_seconds))
    provider.engine_paths = {
        name: Path(environment.get(key, default)).expanduser().absolute()
        for name, key, default in (
            ('fasthenry', 'ALX_KICAD_FASTHENRY', '~/.local/share/field-solvers/fasthenry-3.0wr/bin/fasthenry'),
            ('ngspice', 'ALX_KICAD_NGSPICE', '/opt/homebrew/bin/ngspice'),
        )
    }
    provider.execute_engine = partial(run_kicad_engine, runner=runner,
                                      timeout_seconds=settings.cli_timeout_seconds)
    permission = 'design.kicad'
    return KiCadRuntime(DEFINITIONS, {
        d.capability_id: AuthorityPolicy(frozenset({permission}),
            approval_required=d.capability_id == 'write_back_kicad_board')
        for d in DEFINITIONS
    }, build_kicad_executors(provider, call_id_source), frozenset({permission}), provider)
