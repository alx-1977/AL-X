"""Platform boundary for one governed Sandbox execution, under D-027."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from alx.contracts.sandbox import SandboxOutcome, SandboxRequest
from alx.providers.sandbox_workspace import SessionPaths


class SandboxRunner(ABC):
    """One confined execution and its platform-owned process lifecycle."""

    @abstractmethod
    def available(self) -> bool:
        """Whether this platform can actually confine a process."""

    @abstractmethod
    def run(
        self,
        request: SandboxRequest,
        paths: SessionPaths,
        launched: "Callable[[], None] | None" = None,
    ) -> SandboxOutcome:
        """Execute one program and return what it did.

        `launched` is called once when execution begins, distinguishing a
        pre-launch failure from machine time that must count against the fuse.
        """

    @abstractmethod
    def reap_orphans(self) -> int:
        """Safely end runs this backend can prove a dead runtime left behind."""
