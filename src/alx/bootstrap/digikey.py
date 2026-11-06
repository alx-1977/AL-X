"""Compose the structured production order read; bill authority stays in Xero."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from alx.config.digikey import DigiKeySettings
from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers.digikey import DigiKeyProvider
from alx.safety import AuthorityPolicy
from alx.tools.digikey import LOOKUP_DEFINITION, LOOKUP_DIGIKEY_ORDER, build_digikey_executors


@dataclass(frozen=True, slots=True)
class DigiKeyRuntime:
    provider: DigiKeyProvider
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_digikey_runtime(environment: Mapping[str, str], call_id_source: Callable[[], str]) -> DigiKeyRuntime | None:
    settings = DigiKeySettings.from_environment(environment)
    if not settings.is_usable:
        return None
    provider = DigiKeyProvider(settings.client_id, settings.client_secret, settings.account_id)
    permissions = frozenset({"digikey.order.read"})
    return DigiKeyRuntime(provider, (LOOKUP_DEFINITION,),
                          {LOOKUP_DIGIKEY_ORDER: AuthorityPolicy(permissions)},
                          build_digikey_executors(provider, call_id_source), permissions)
