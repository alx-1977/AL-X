"""Compose public web reading, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot propose a
page read at all. That is the difference between web access being off and web
access merely failing: an unregistered capability is honestly absent, while a
registered one that always fails would look like a broken world rather than a
runtime that was never given the authority.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.providers import HttpWebFetchProvider
from alx.safety import AuthorityPolicy
from alx.tools import ASK_WEB_PAGE, WEB_DEFINITION, build_web_executors


LOGGER = logging.getLogger(__name__)

# Reading the public web is its own authority under D-025. Holding it does not
# follow from any other permission: research.spend buys model tokens and grants
# no network access, and this grants no model spend and no authenticated
# browsing.
WEB_READ_PERMISSION = "web.read"


@dataclass(frozen=True, slots=True)
class WebRuntime:
    """The one public-web capability, or nothing at all."""

    provider: Any
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_web_runtime(
    enabled: bool,
    call_id_source: Callable[[], str],
) -> WebRuntime | None:
    """Compose the public page reader when this runtime is authorised."""
    if not enabled:
        LOGGER.info("Public web reading is not enabled: no web capability")
        return None
    provider = HttpWebFetchProvider()
    LOGGER.info("Public web reading enabled: %s", ASK_WEB_PAGE)
    return WebRuntime(
        provider=provider,
        definitions=(WEB_DEFINITION,),
        policies={
            # Not approval gated. The network boundary, the resource bounds
            # and the read-only method are the control; asking Friedl to
            # approve each page would make reading something he directs
            # rather than something she does while thinking.
            ASK_WEB_PAGE: AuthorityPolicy(frozenset({WEB_READ_PERMISSION})),
        },
        executors=build_web_executors(provider, call_id_source),
        permissions=frozenset({WEB_READ_PERMISSION}),
    )


__all__ = ["WEB_READ_PERMISSION", "WebRuntime", "build_web_runtime"]
