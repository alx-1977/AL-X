"""Compose D-038 mail-document reading, or leave it unavailable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Mapping

from alx.config import LlamaParseSettings
from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    MailAccount,
    StructuredData,
)
from alx.providers import LlamaParseExtractor
from alx.safety import AuthorityPolicy
from alx.specialists.commercial_document import DOCUMENT_INSTRUCTION, DOCUMENT_SCHEMA
from alx.tools.documents import DEFINITIONS, READ_MAIL_DOCUMENT, build_document_executors


# D-038. Reading a mailed document sends it to LlamaCloud, as supplier-bill
# capture already does. Its own permission: no mail or Xero permission
# grants it, and it grants none of them.
DOCUMENT_READ_PERMISSION = "document.read"


@dataclass(frozen=True, slots=True)
class DocumentRuntime:
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_document_runtime(
    settings: LlamaParseSettings,
    mail_account: MailAccount,
    call_id_source: Callable[[], str],
    *,
    client: Any = None,
) -> DocumentRuntime | None:
    """The document reader, or None when LlamaParse is not configured."""
    if not settings.is_usable:
        return None
    extractor = LlamaParseExtractor(
        settings.api_key,
        settings.base_url,
        settings.timeout_seconds,
        settings.project_id,
        DOCUMENT_SCHEMA,
        DOCUMENT_INSTRUCTION,
        client,
    )
    return DocumentRuntime(
        definitions=DEFINITIONS,
        policies={
            READ_MAIL_DOCUMENT: AuthorityPolicy(frozenset({DOCUMENT_READ_PERMISSION})),
        },
        executors=build_document_executors(mail_account, extractor.read, call_id_source),
        permissions=frozenset({DOCUMENT_READ_PERMISSION}),
    )
