"""Current macOS development backend for the governed Sandbox."""

from alx.providers.sandbox_macos.runner import (
    CPU_GRACE_SECONDS,
    LIVE_RUN_NAME,
    SeatbeltSandboxRunner,
    process_identity,
)


__all__ = [
    "CPU_GRACE_SECONDS",
    "LIVE_RUN_NAME",
    "SeatbeltSandboxRunner",
    "process_identity",
]
