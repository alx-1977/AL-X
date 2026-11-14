"""Compose D-061 capabilities through the existing broker and safety gate."""
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from alx.config.settings import KiCadSettings
from alx.providers.coding_process import (build_swift_program, run_confined_converter, run_kicad_cli,
                                         run_kicad_engine, run_kicad_python)
from alx.providers.kicad import KiCadProvider
from alx.safety import AuthorityPolicy
from alx.tools.kicad import DEFINITIONS, FREE_ACTIONS, KICAD, build_kicad_executors


@dataclass(frozen=True)
class KiCadRuntime:
    definitions: tuple
    policies: dict
    executors: dict
    permissions: frozenset
    provider: KiCadProvider | None = None


from alx.bootstrap.look import ALX_STORAGE  # one storage folder for both tools


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
            ('pdftoppm', 'ALX_PDFTOPPM', '/opt/homebrew/bin/pdftoppm'),
            ('pdftotext', 'ALX_PDFTOTEXT', '/opt/homebrew/bin/pdftotext'),
        )
    }
    provider.execute_engine = partial(run_kicad_engine, runner=runner,
                                      timeout_seconds=settings.cli_timeout_seconds)
    # D-073: datasheet sections for the viewer, rendered under the same confinement.
    provider.execute_page = partial(run_confined_converter, runner=runner, timeout_seconds=60)
    provider.build_program = partial(build_swift_program, runner=runner)
    # Datasheets in her own storage are readable too, and fetched ones go there.
    storage = Path(environment.get('ALX_STORAGE_ROOT', ALX_STORAGE)).expanduser().resolve()
    provider.reference_roots = tuple(dict.fromkeys((*provider.design_roots, storage)))
    provider.datasheet_folder = storage / 'Datasheets'
    nexar = [(environment[a], environment[b]) for a, b in (
        ('NEXAR_CLIENT_ID', 'NEXAR_CLIENT_SECRET'), ('NEXAR_EVAL_CLIENT_ID', 'NEXAR_EVAL_CLIENT_SECRET'))
        if environment.get(a) and environment.get(b)]
    digikey = ((environment['DIGIKEY_CLIENT_ID'], environment['DIGIKEY_CLIENT_SECRET'])
               if environment.get('DIGIKEY_CLIENT_ID') and environment.get('DIGIKEY_CLIENT_SECRET') else None)

    def datasheet_sources():
        from alx.providers.datasheet_fetch import api_client
        from alx.providers.web_fetch import HttpWebFetchProvider
        return api_client(), nexar, digikey, HttpWebFetchProvider()
    provider.datasheet_sources = datasheet_sources if (nexar or digikey) else None
    permission = 'design.kicad'
    # D-074: one capability. export and write_back change his project and need
    # his approval of the exact call; every other action is free.
    return KiCadRuntime(DEFINITIONS, {
        KICAD: AuthorityPolicy(frozenset({permission}), approval_required=True,
                               approval_exempt_when=tuple((('action', name),) for name in FREE_ACTIONS)),
    }, build_kicad_executors(provider, call_id_source), frozenset({permission}), provider)
