"""Start the launcher that restarts AL/X onto committed main (D-055).

The only thing here is one fixed command: wait a few seconds, then run the
repository's launcher, `scripts/alx restart`, which re-executes committed
main's own copy of itself, stops this checkout's runtime, starts committed
main and waits until it answers. It runs in its own session, so stopping this
process does not stop it. Nothing AL/X composed reaches the command line.
"""

from __future__ import annotations

import os
import subprocess  # noqa: S404 - the one self-restart launch site
from pathlib import Path

# Long enough for the turn that asked for the restart to finish and be heard.
RESTART_DELAY_SECONDS = 5


def restart_command(repository_root: Path) -> list[str]:
    """The exact argv: a delay, then the launcher's own restart."""
    launcher = Path(repository_root) / "scripts" / "alx"
    return [
        "/bin/bash", "-c", 'sleep "$1"; exec /bin/bash "$0" restart',
        str(launcher), str(RESTART_DELAY_SECONDS),
    ]


def restart_environment() -> dict[str, str]:
    """This process's environment, less the launcher's own marker.

    The launcher that started this runtime marked itself as committed main's
    copy, and the runtime inherited the mark. Passed on, it would make the
    launcher run the checkout's copy of itself, which may be a feature
    branch's, instead of re-executing committed main's.
    """
    return {key: value for key, value in os.environ.items()
            if key != "ALX_LAUNCHER_COMMITTED"}


def start_restart(repository_root: Path) -> None:
    """Start the restart detached, its output in the runtime directory."""
    root = Path(repository_root)
    log = root / ".alx" / "runtime" / "self-restart.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as output:
        subprocess.Popen(  # noqa: S603 - fixed argv, no model input
            restart_command(root),
            cwd=str(root),
            env=restart_environment(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            shell=False,
        )


__all__ = ["RESTART_DELAY_SECONDS", "restart_command", "restart_environment", "start_restart"]
