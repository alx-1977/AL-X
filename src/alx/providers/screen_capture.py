"""A picture of the viewer as drawn, for AL/X to see what Friedl sees (D-075).

Chrome, without a window, loads her own viewer page from her own server in
capture mode (it draws the current display but reports nothing as drawn) and
saves a screenshot. Nothing else is loaded: the address is her local viewer
only. The picture is kept in her look store and named by its digest.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from alx.providers.look_image import keep_image

# The size the model reads whole: a long side of 1568 pixels.
WIDTH, HEIGHT = 1568, 980


def capture_screen(
    board_id: str,
    version: int,
    *,
    base_url: str,
    browser: Path,
    store: Path,
    scratch_root: Path,
    execute: Callable[[Path, list[str], Path], Any],
) -> str | None:
    """Screenshot the viewer at this display version; the image's digest, or None."""
    if not browser.is_file():
        return None
    run = scratch_root / ("screen-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    try:
        url = f"{base_url}/show?capture={quote(board_id)}&version={int(version)}"
        executed = execute(browser, [
            "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            "--no-default-browser-check", f"--user-data-dir={run / 'profile'}",
            f"--window-size={WIDTH},{HEIGHT}", "--virtual-time-budget=6000",
            f"--screenshot={run / 'screen.png'}", url], run)
        output = run / "screen.png"
        if getattr(executed, "returncode", 1) or not output.is_file():
            return None
        return keep_image(store, output.read_bytes())
    finally:
        shutil.rmtree(run, ignore_errors=True)


__all__ = ["capture_screen"]
