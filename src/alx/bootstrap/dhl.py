"""Compose the approved DHL document-analysis primitive."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Mapping

from alx.contracts import CapabilityDefinition, CapabilityResult, MailAccount, StructuredData
from alx.providers import DhlImportAnalyzerAdapter
from alx.safety import AuthorityPolicy
from alx.tools import (
    ANALYZE_DHL_CUSTOMS_DOCUMENTS,
    DHL_DEFINITIONS,
    RECONCILE_DHL_IMPORT_DOCUMENTS,
    build_dhl_executors,
)


DHL_DOCUMENT_PERMISSION = "dhl.documents.read"


@dataclass(frozen=True, slots=True)
class DhlRuntime:
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_dhl_runtime(
    mail: MailAccount,
    call_id_source: Callable[[], str],
) -> DhlRuntime:
    return DhlRuntime(
        DHL_DEFINITIONS,
        {
            ANALYZE_DHL_CUSTOMS_DOCUMENTS: AuthorityPolicy(
                frozenset({DHL_DOCUMENT_PERMISSION})
            ),
            RECONCILE_DHL_IMPORT_DOCUMENTS: AuthorityPolicy(
                frozenset({DHL_DOCUMENT_PERMISSION})
            )
        },
        build_dhl_executors(mail, DhlImportAnalyzerAdapter(), call_id_source),
        frozenset({DHL_DOCUMENT_PERMISSION}),
    )
