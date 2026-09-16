"""Language-blind canonical repository lifecycle capabilities."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from alx.contracts import CapabilityDefinition, CapabilityResult, CapabilityResultState, SideEffect, StructuredSchema, ValueKind
from alx.contracts.repository_runtime import REPOSITORY_RUNTIME_FAILURES, RepositoryRuntimeError


INSPECT_REPOSITORY_STATE = "inspect_repository_state"
SYNCHRONIZE_LOCAL_MAIN = "synchronize_local_main"
_EMPTY = StructuredSchema(ValueKind.OBJECT, {}, (), extra_properties=False)
_STRING = StructuredSchema(ValueKind.STRING)
_OUTPUT = StructuredSchema(ValueKind.OBJECT, {
    "repository_identity": _STRING, "branch": _STRING, "local_before": _STRING,
    "origin_main": _STRING, "local_after": _STRING, "transition": _STRING,
}, ("repository_identity", "branch", "local_before", "origin_main", "local_after", "transition"), extra_properties=False)

INSPECT_DEFINITION = CapabilityDefinition(
    INSPECT_REPOSITORY_STATE, "Inspect the configured canonical repository state. The checkout, remote, branch, and refs are fixed by runtime configuration.",
    _EMPTY, _OUTPUT, SideEffect.NONE, REPOSITORY_RUNTIME_FAILURES)
SYNCHRONIZE_DEFINITION = CapabilityDefinition(
    SYNCHRONIZE_LOCAL_MAIN, "Fast-forward the configured canonical local main from its configured canonical origin main when deterministic repository checks permit it. It cannot select a repository, remote, branch, ref, or command.",
    _EMPTY, _OUTPUT, SideEffect.EFFECTFUL, REPOSITORY_RUNTIME_FAILURES)


def build_repository_runtime_executors(inspect: Callable[[], Any], synchronize: Callable[[], Any], call_id_source: Callable[[], str]) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def execute(capability_id: str, operation: Callable[[], Any]) -> CapabilityResult:
        call_id = call_id_source()
        try:
            state = operation()
        except RepositoryRuntimeError as error:
            return CapabilityResult(call_id, capability_id, CapabilityResultState.FAILED,
                                    failure={"code": error.code, "phase": error.phase})
        except Exception:
            return CapabilityResult(call_id, capability_id, CapabilityResultState.FAILED,
                                    failure={"code": "repository_root_unusable"})
        return CapabilityResult(call_id, capability_id, CapabilityResultState.SUCCEEDED, state.as_values())

    return {
        INSPECT_REPOSITORY_STATE: lambda arguments: execute(INSPECT_REPOSITORY_STATE, inspect),
        SYNCHRONIZE_LOCAL_MAIN: lambda arguments: execute(SYNCHRONIZE_LOCAL_MAIN, synchronize),
    }
