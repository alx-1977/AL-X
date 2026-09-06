"""Compose isolated experimentation, or leave it unavailable entirely.

Returning None leaves the capability unregistered, so AL/X cannot propose an
experiment at all. That is the difference between the sandbox being off and it
merely failing: an unregistered capability is honestly absent, while a
registered one that always fails would look like a broken world rather than a
runtime that was never given the authority.

The same applies to a platform with no supported confinement. D-027 requires
that such a platform register nothing rather than offer a weakened sandbox, so
a runner that reports itself unavailable composes nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from alx.contracts import CapabilityDefinition, CapabilityResult, StructuredData
from alx.contracts.sandbox import SandboxError, SandboxRequest
from alx.observability.sandbox_ledger import (
    SQLiteSandboxLedger,
    SandboxBudget,
    SandboxBudgetExceeded,
    SandboxLedgerCorrupt,
)
from alx.providers.sandbox_retention import SandboxRetention
from alx.providers.sandbox_runner import SandboxRunner, SeatbeltSandboxRunner
from alx.providers.sandbox_workspace import SandboxWorkspace
from alx.safety import AuthorityPolicy
from alx.tools.sandbox import (
    DEFINITION as SANDBOX_DEFINITION,
    RUN_SANDBOX_EXPERIMENT,
    build_sandbox_executors,
)


LOGGER = logging.getLogger(__name__)

# Running code locally is its own authority under D-027. Holding it does not
# follow from any other permission: web.read grants public network reads and no
# execution, research.spend buys model tokens and grants neither.
SANDBOX_EXECUTE_PERMISSION = "sandbox.execute"


@dataclass(frozen=True, slots=True)
class SandboxRuntime:
    """The one experimentation capability, or nothing at all."""

    runner: SandboxRunner
    workspace: SandboxWorkspace
    ledger: SQLiteSandboxLedger
    retention: SandboxRetention
    definitions: tuple[CapabilityDefinition, ...]
    policies: Mapping[str, AuthorityPolicy]
    executors: Mapping[str, Callable[[StructuredData], CapabilityResult]]
    permissions: frozenset[str]


def build_sandbox_runtime(
    enabled: bool,
    sandbox_root: Path | None,
    ledger_path: Path | None,
    call_id_source: Callable[[], str],
    denied_read_paths: tuple[Path, ...] = (),
    runner: SandboxRunner | None = None,
    budget: SandboxBudget | None = None,
) -> SandboxRuntime | None:
    """Compose the sandbox, or leave it unregistered."""
    if not enabled:
        LOGGER.info("Isolated experimentation is not enabled: no sandbox capability")
        return None
    if sandbox_root is None or ledger_path is None:
        LOGGER.info("Sandbox storage is not configured: no sandbox capability")
        return None

    try:
        sandbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        LOGGER.warning("Sandbox root could not be created: no sandbox capability")
        return None

    workspace = SandboxWorkspace(sandbox_root)
    selected = runner or SeatbeltSandboxRunner(workspace, denied_read_paths)
    if not selected.available():
        LOGGER.info("No supported confinement on this platform: no sandbox capability")
        return None

    try:
        ledger = SQLiteSandboxLedger(ledger_path, budget or SandboxBudget())
    except SandboxLedgerCorrupt:
        # An unusable ledger means running against a ceiling nobody is
        # measuring, so the capability is absent rather than unaccounted.
        LOGGER.warning("Sandbox ledger is unusable: no sandbox capability")
        return None

    retention = SandboxRetention(workspace)
    # Reap what a crash left behind before anything new is run. A further sweep
    # runs before each experiment, so retention does not depend on restarting.
    retention.sweep()

    def run_experiment(request: SandboxRequest) -> Any:
        # Retention runs before every run rather than only at construction. A
        # long-lived runtime would otherwise never sweep again, and experiment
        # bytes would outlive the TTL until the next restart.
        retention.sweep()

        try:
            reservation = ledger.reserve(request.wall_seconds)
        except SandboxBudgetExceeded as error:
            LOGGER.info("Sandbox run refused: %s", error.reason)
            raise SandboxError("budget_exhausted") from error
        except SandboxLedgerCorrupt as error:
            raise SandboxError("ledger_corrupt") from error

        # A reservation is released only when nothing was executed. Once the
        # process has run it has consumed wall time on this machine, so a
        # failure afterwards — reading output, hashing state, writing the
        # manifest — settles rather than abandons. Abandoning those would let
        # repeated post-execution failures spend the day's time without ever
        # appearing in either fuse.
        executed = False
        try:
            paths = workspace.prepare(
                request.experiment_id, request.session_id, request.run_id
            )
            started = monotonic()
            executed = True
            outcome = selected.run(request, paths)
        except BaseException:
            if executed:
                ledger.settle(reservation, monotonic() - started)
            else:
                ledger.abandon(reservation, "run_failed")
            raise
        ledger.settle(reservation, outcome.wall_seconds_used)
        return outcome

    executors = build_sandbox_executors(
        run_experiment, call_id_source, lambda: f"run-{uuid4().hex[:16]}"
    )

    LOGGER.info(
        "Isolated experimentation enabled: %s (%d runs, %d wall seconds per day)",
        RUN_SANDBOX_EXPERIMENT,
        ledger.budget.daily_runs,
        ledger.budget.daily_seconds,
    )
    return SandboxRuntime(
        runner=selected,
        workspace=workspace,
        ledger=ledger,
        retention=retention,
        definitions=(SANDBOX_DEFINITION,),
        policies={
            # Not approval gated. The confinement, the absence of network and
            # secrets, and the daily fuses are the control; asking Friedl to
            # approve each experiment would make experimentation something he
            # directs rather than something she does while thinking.
            #
            # Not conditional on CognitionOrigin either: D-027 records that
            # autonomous and person-originated cognition use the same governed
            # path, and that a host's memory limitation must not be pushed into
            # her authority model.
            RUN_SANDBOX_EXPERIMENT: AuthorityPolicy(
                frozenset({SANDBOX_EXECUTE_PERMISSION})
            ),
        },
        executors=executors,
        permissions=frozenset({SANDBOX_EXECUTE_PERMISSION}),
    )
