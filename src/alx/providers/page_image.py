"""One section of a document page as an image, for the viewer (D-073).

The document is copied into AL/X's own storage and the copy is rendered there
by the page renderer under the same kernel confinement as KiCad scripts. The
image is the page as printed: nothing on it is redrawn or interpreted. Which
page, which section and what to mark on it is decided by AL/X.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_DOCUMENT_BYTES = 60_000_000
MAX_IMAGE_BYTES = 8_000_000
MAX_SECTION_POINTS = 3000
LONG_SIDE_PIXELS = 2000
MAX_DPI = 600
PNG = b"\x89PNG\r\n\x1a\n"


class PageError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def section_box(arguments: Mapping[str, Any]) -> tuple[int, tuple[float, float, float, float]]:
    """The page (from 1) and the section [x0, y0, x1, y1] in points from the top left."""
    page = arguments.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= 2000:
        raise PageError("arguments_unusable", "page is a page number from 1")
    box = arguments.get("section")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box)):
        raise PageError("arguments_unusable", "section is [x0, y0, x1, y1] in points from the top left")
    x0, y0, x1, y1 = (float(v) for v in box)
    if x0 < 0 or y0 < 0 or x0 >= x1 or y0 >= y1 or max(x1 - x0, y1 - y0) > MAX_SECTION_POINTS:
        raise PageError("arguments_unusable",
                        f"section needs 0 <= x0 < x1, 0 <= y0 < y1, at most {MAX_SECTION_POINTS} points a side")
    return page, (x0, y0, x1, y1)


def render_section(
    document: Path,
    arguments: Mapping[str, Any],
    *,
    scratch_root: Path,
    renderer: Path,
    execute: Callable[[Path, list[str], Path], Any],
) -> bytes:
    """Copy the document, render one section of one page, remove the copy."""
    page, (x0, y0, x1, y1) = section_box(arguments)
    if not document.is_file() or document.suffix.lower() != ".pdf":
        raise PageError("document_unavailable", "not a PDF file")
    if document.stat().st_size > MAX_DOCUMENT_BYTES:
        raise PageError("document_unavailable", "the document is too large")
    if not renderer.is_file():
        raise PageError("engine_unavailable", "no page renderer installed")
    dpi = max(36, min(MAX_DPI, int(LONG_SIDE_PIXELS * 72 / max(x1 - x0, y1 - y0))))
    scale = dpi / 72
    run = scratch_root / ("page-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    try:
        shutil.copyfile(document, run / "document.pdf")
        executed = execute(renderer, [
            "-f", page, "-l", page, "-r", dpi,
            "-x", round(x0 * scale), "-y", round(y0 * scale),
            "-W", round((x1 - x0) * scale), "-H", round((y1 - y0) * scale),
            "-png", "-singlefile", "document.pdf", "section"], run)
        output = run / "section.png"
        if getattr(executed, "returncode", 1) or not output.is_file():
            raise PageError("page_failed", (getattr(executed, "stderr", "") or "")[-1000:])
        if output.stat().st_size > MAX_IMAGE_BYTES:
            raise PageError("page_failed", "the section image is too large; choose a smaller section")
        data = output.read_bytes()
        if not data.startswith(PNG):
            raise PageError("page_failed", "the renderer did not produce an image")
        return data
    finally:
        shutil.rmtree(run, ignore_errors=True)


__all__ = ["PageError", "render_section", "section_box"]
