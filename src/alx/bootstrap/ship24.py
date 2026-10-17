"""Compose the single Ship24 tracking path into the capability broker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers.ship24 import Ship24Provider
from alx.safety import AuthorityPolicy
from alx.tools.ship24 import DEFINITION, TRACK_SHIPMENT, build_ship24_executors


SHIP24_TRACK_PERMISSION = "shipment.track"


@dataclass(frozen=True, slots=True)
class Ship24Runtime:
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_ship24_runtime(
    call_id_source: Callable[[], str], environment: Mapping[str, str] | None = None,
) -> Ship24Runtime:
    provider = Ship24Provider(environment)
    return Ship24Runtime(
        (DEFINITION,),
        {TRACK_SHIPMENT: AuthorityPolicy(frozenset({SHIP24_TRACK_PERMISSION}))},
        build_ship24_executors(provider, call_id_source),
        frozenset({SHIP24_TRACK_PERMISSION}),
    )
