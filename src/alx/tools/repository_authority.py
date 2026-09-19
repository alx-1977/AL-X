"""One capability for repository work, carrying one enumerated operation.

Reached the way every capability is: AL/X proposes a structured call, the broker
validates it, the safety gate authorises it, and the executor performs one
bounded operation.

One capability rather than one per operation. The alternative was tried: a
capability per verb meant the catalogue grew whenever she met an ordinary git
question, and each addition needed design, review and a merge before she could
answer it. What she is choosing between is operations on one repository, not
between unrelated authorities, so the operation is an argument and the authority
is the capability.

The catalogue entry names every operation, so nothing here is hidden from her:
she is choosing from a list she can read, not guessing at a command language.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)
from alx.contracts.repository_authority import (
    REPOSITORY_FAILURES,
    Operation,
    RepositoryAuthorityError,
    RepositoryRequest,
)

LOGGER = logging.getLogger(__name__)

REPOSITORY_OPERATION = "repository_operation"

_STRING = StructuredSchema(ValueKind.STRING)
_OBJECT = StructuredSchema(ValueKind.OBJECT, extra_properties=True)


_PURPOSE = (
    "Perform one repository operation on the configured checkout and report "
    "what it did, including where the affected ref started and ended. "
    "Operations: "
    + ", ".join(sorted(item.value for item in Operation))
    + ". Arguments depend on the operation: a revision or ref is named by "
    "`revision`, `base`, `head`, `branch`, `start_point`, `onto` or "
    "`ancestor`/`descendant`; `paths` names files to stage; `message` carries "
    "a commit message; `mode` selects soft, mixed or hard for reset; `path` "
    "names a worktree. Ordinary destructive work is permitted — a feature "
    "branch may be deleted, force-pushed, reset or rebased. The single "
    "refusal is an operation that would irrecoverably destroy the canonical "
    "AL/X system itself, which is reported as `self_preservation`. Decides "
    "nothing about whether an operation is a good idea."
)


DEFINITION = CapabilityDefinition(
    REPOSITORY_OPERATION,
    _PURPOSE,
    StructuredSchema(
        ValueKind.OBJECT,
        {"operation": _STRING, "arguments": _OBJECT},
        ("operation",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "repository": _STRING,
            "operation": _STRING,
            "succeeded": StructuredSchema(ValueKind.BOOLEAN),
            "source_ref": _STRING,
            "source_sha": _STRING,
            "resulting_ref": _STRING,
            "resulting_sha": _STRING,
            "remote": _STRING,
            "failure_code": _STRING,
            "refusal_reason": _STRING,
        },
        ("repository", "operation", "succeeded"),
        extra_properties=True,
    ),
    SideEffect.EFFECTFUL,
    REPOSITORY_FAILURES,
    transmits_authored_text=True,
)

DEFINITIONS = (DEFINITION,)


def build_repository_operation_executors(
    authority: Any, call_id_source: Callable[[], str]
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    """Bind the one capability to the provider that performs it."""

    def perform(values: Mapping[str, Any]) -> CapabilityResult:
        call_id = call_id_source()
        raw = str(values.get("operation", "") or "").strip().lower()
        try:
            operation = Operation(raw)
        except ValueError:
            LOGGER.info("Unknown repository operation requested: %r", raw)
            return CapabilityResult(
                call_id,
                REPOSITORY_OPERATION,
                CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        arguments = values.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            return CapabilityResult(
                call_id,
                REPOSITORY_OPERATION,
                CapabilityResultState.FAILED,
                failure={"code": "arguments_unusable"},
            )
        try:
            outcome = authority.perform(RepositoryRequest(operation, dict(arguments)))
        except RepositoryAuthorityError as error:
            return CapabilityResult(
                call_id,
                REPOSITORY_OPERATION,
                CapabilityResultState.FAILED,
                failure={"code": error.code},
            )
        if not outcome.succeeded:
            # A refusal is still evidence: the record says which operation was
            # attempted and why it did not happen, which is what she needs to
            # choose differently rather than merely to know it failed.
            return CapabilityResult(
                call_id,
                REPOSITORY_OPERATION,
                CapabilityResultState.FAILED,
                outcome.as_values(),
                failure={"code": outcome.failure_code or "operation_refused"},
            )
        return CapabilityResult(
            call_id,
            REPOSITORY_OPERATION,
            CapabilityResultState.SUCCEEDED,
            outcome.as_values(),
        )

    return {REPOSITORY_OPERATION: perform}


__all__ = [
    "DEFINITION",
    "DEFINITIONS",
    "REPOSITORY_OPERATION",
    "build_repository_operation_executors",
]
