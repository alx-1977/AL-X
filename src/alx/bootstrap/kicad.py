"""Compose D-058 capabilities through the existing broker and safety gate."""
from dataclasses import dataclass

from alx.config.settings import KiCadSettings
from alx.providers.kicad import KiCadProvider
from alx.safety import AuthorityPolicy
from alx.tools.kicad import DEFINITIONS, build_kicad_executors


@dataclass(frozen=True)
class KiCadRuntime:
    definitions: tuple
    policies: dict
    executors: dict
    permissions: frozenset


def build_kicad_runtime(call_id_source, environment, storage_root, runner=None):
    settings = KiCadSettings.from_environment(environment, storage_root)
    provider = KiCadProvider(settings, **({'runner': runner} if runner is not None else {}))
    permission = 'design.kicad'
    return KiCadRuntime(DEFINITIONS, {
        d.capability_id: AuthorityPolicy(frozenset({permission}),
            approval_required=d.capability_id == 'write_back_kicad_board')
        for d in DEFINITIONS
    }, build_kicad_executors(provider, call_id_source), frozenset({permission}))
