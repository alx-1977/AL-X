"""The numbers of a land-pattern drawing that is a picture (2026-11-14).

In a rehearsal of the library audit two of five datasheets (830053099,
TL3340AF160QG) drew their land pattern as a picture, so the PDF held no text
for check_layout to read and nothing was measured. That morning AL/X had
called one a match and the other slightly different by eye. The section is
rendered at high resolution and read by macOS's own text recognition, built
once from picture_text.swift; the words come back with their places in
points, like the PDF's own text, and are checked the same way.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from alx.providers.layout_check import LayoutCheckError

SOURCE = Path(__file__).with_name("picture_text.swift")
DPI = 600
MAX_PIXELS = 6000  # the longer side of the rendered section
# Recognition sometimes reads a mark beside a number as one stray non-Latin
# letter (Д.90). Only that goes: Fig.3, PIN1, ±0.1, +0.1, a bracketed inch
# value ([.035) and unit words stay as printed, so none becomes a dimension.
_STRAY = re.compile(r"^[^\x00-\x7f±°µΩ](?=[.,]?\d)")


def engine(cache_root: Path, build: Callable[[Path, Path], Any]) -> Path:
    """The recognizer, built from its source once per version of that source.

    A failed build is remembered, so it is not tried again on every check
    (review of D-081): a new version of the source tries once more.
    """
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12]
    target = cache_root / ("picture-text-" + digest)
    failed = cache_root / ("picture-text-" + digest + ".failed")
    if target.is_file():
        return target
    if failed.is_file():
        raise LayoutCheckError("engine_unavailable", "text recognition could not be built: "
                               + failed.read_text(errors="replace")[:300])
    cache_root.mkdir(parents=True, exist_ok=True)
    partial = cache_root / ("picture-text-" + uuid.uuid4().hex)
    try:
        executed = build(SOURCE, partial)
        detail = (getattr(executed, "stderr", "") or "")[-300:]
        built = not getattr(executed, "returncode", 1) and partial.is_file()
    except Exception as error:  # noqa: BLE001 - a timeout or missing compiler is a fact
        built, detail = False, type(error).__name__
    if not built:
        partial.unlink(missing_ok=True)
        failed.write_text(detail or "no program")
        raise LayoutCheckError("engine_unavailable", "text recognition could not be built: " + detail)
    partial.replace(target)
    return target


def clean(token: str) -> str:
    """A recognised word as a number would be printed: Д.90 -> 0.90, .60 -> 0.60."""
    token = _STRAY.sub("", token.strip())
    return "0" + token if token[:1] in (".", ",") and token[1:2].isdigit() else token


def picture_words(document: Path, page: int, section: tuple[float, float, float, float], *,
                  scratch_root: Path, renderer: Path, recognizer: Path,
                  execute: Callable[[Path, list[Any], Path], Any],
                  cache: Path | None = None) -> list[tuple[float, float, float, float, str]]:
    """The words recognised in a section of a PDF page, with boxes in points from the page's top left.

    `cache` keeps them for this document, page and section, so a check run
    again with readings does not read the picture again (review of D-081).
    """
    if cache is not None and cache.is_file():
        try:
            return [tuple(word) for word in json.loads(cache.read_text())]
        except (OSError, ValueError, TypeError):
            pass
    words = _recognise(document, page, section, scratch_root=scratch_root, renderer=renderer,
                       recognizer=recognizer, execute=execute)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        pending = cache.with_name(cache.name + "." + uuid.uuid4().hex)
        pending.write_text(json.dumps(words))
        pending.replace(cache)
    return words


def _recognise(document, page, section, *, scratch_root, renderer, recognizer, execute):
    x0, y0, x1, y1 = section
    dpi = min(DPI, MAX_PIXELS * 72 / max(x1 - x0, y1 - y0, 1))
    scale = dpi / 72
    run = scratch_root / ("picture-" + uuid.uuid4().hex)
    run.mkdir(parents=True)
    try:
        shutil.copyfile(document, run / "document.pdf")
        # The confinement reads nothing in the home folder outside the run
        # folder, and her storage is there: the program runs from a copy
        # inside it (review of D-081: Vision failed where it was built).
        shutil.copy2(recognizer, run / "picture-text")
        executed = execute(renderer, ["-f", page, "-l", page, "-r", "%g" % dpi,
                                      "-x", int(x0 * scale), "-y", int(y0 * scale),
                                      "-W", max(1, int((x1 - x0) * scale)), "-H", max(1, int((y1 - y0) * scale)),
                                      "-png", "document.pdf", "section"], run)
        images = sorted(run.glob("section*.png"))
        if getattr(executed, "returncode", 1) or not images:
            raise LayoutCheckError("page_failed", (getattr(executed, "stderr", "") or "")[-500:])
        executed = execute(run / "picture-text", [images[0].name], run)
        if getattr(executed, "returncode", 1):
            raise LayoutCheckError("page_failed", (getattr(executed, "stderr", "") or "")[-500:])
        # Back to points: the rendered section starts at (x0, y0) of the page.
        origin_x, origin_y = int(x0 * scale) / scale, int(y0 * scale) / scale
        words = []
        for row in (getattr(executed, "stdout", "") or "").split("\n"):
            cells = row.split("\t")
            if len(cells) != 5:
                continue
            try:
                left, top, width, height = (float(cell) / scale for cell in cells[:4])
            except ValueError:
                continue
            text = clean(cells[4])
            if text:
                words.append((round(origin_x + left, 2), round(origin_y + top, 2),
                              round(origin_x + left + width, 2), round(origin_y + top + height, 2), text))
        return words
    finally:
        shutil.rmtree(run, ignore_errors=True)


__all__ = ["DPI", "SOURCE", "clean", "engine", "picture_words"]
