"""What AL/X looks at: a picture or a section of a PDF page, as an image (D-075).

The file is copied into her storage and turned into a PNG there, under the
same kernel confinement as KiCad scripts: `pdftoppm` for a PDF page, `ffmpeg`
for a picture (it writes only its output; `sips` needs the system temp folder). A PDF page carries a faint grid in points from the top left of
the page, labelled at the edges, so she can read where something is and name
it exactly (a datasheet section for the viewer, a box around a dimension).
Nothing here interprets the image; she does.
"""

from __future__ import annotations

import hashlib
import shutil
import struct
import uuid
import zlib
from pathlib import Path
from typing import Any, Callable, Mapping

from alx.providers.page_image import PageError, render_section

# Claude reads an image at up to about 1568 pixels on its long side; more is
# scaled down on arrival and only costs allowance.
LONG_SIDE_PIXELS = 1568
PICTURES = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".webp", ".bmp")
MAX_PICTURE_BYTES = 60_000_000
KEEP_LOOKS = 200
GRID_STEPS = (1, 2, 5, 10, 20, 25, 50, 100, 200)
MAX_GRID_LINES = 20
TOP_MARGIN, LEFT_MARGIN = 18, 40
PNG = b"\x89PNG\r\n\x1a\n"

# 3x5 digits for the grid labels: no font is needed for numbers.
DIGITS = {
    "0": ("111", "101", "101", "101", "111"), "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"), "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"), "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"), "7": ("111", "001", "010", "010", "010"),
    "8": ("111", "101", "111", "101", "111"), "9": ("111", "101", "111", "001", "111"),
}


class LookError(PageError):
    """The same codes as a page render: arguments_unusable, document_unavailable,
    page_failed, engine_unavailable."""


def png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode 8-bit RGB pixels as a PNG."""
    rows = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (PNG + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(PNG) or data[12:16] != b"IHDR":
        raise LookError("page_failed", "not a PNG image")
    return struct.unpack(">II", data[16:24])


def ppm(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and RGB pixels of a binary PPM."""
    fields, index = [], 2
    while len(fields) < 3:
        while data[index:index + 1].isspace():
            index += 1
        if data[index:index + 1] == b"#":
            index = data.index(b"\n", index) + 1
            continue
        start = index
        while not data[index:index + 1].isspace():
            index += 1
        fields.append(int(data[start:index]))
    width, height, depth = fields
    pixels = data[index + 1:index + 1 + width * height * 3]
    if depth != 255 or len(pixels) != width * height * 3:
        raise LookError("page_failed", "the renderer's image is incomplete")
    return width, height, pixels


def grid_step(width_points: float, height_points: float) -> int:
    """The finest round step that keeps the grid to a readable number of lines."""
    longest = max(width_points, height_points)
    return next((step for step in GRID_STEPS if longest / step <= MAX_GRID_LINES), GRID_STEPS[-1])


def with_grid(width: int, height: int, pixels: bytes, section: tuple[float, float, float, float],
              step: int) -> tuple[int, int, bytes]:
    """The section with a faint grid every `step` points and labelled margins."""
    x0, y0, x1, y1 = section
    per_point = width / (x1 - x0)
    out_w, out_h = width + LEFT_MARGIN, height + TOP_MARGIN
    canvas = bytearray(b"\xff" * (out_w * out_h * 3))
    for y in range(height):
        start = ((y + TOP_MARGIN) * out_w + LEFT_MARGIN) * 3
        canvas[start:start + width * 3] = pixels[y * width * 3:(y + 1) * width * 3]

    def tint(x: int, y: int, colour: tuple[int, int, int], weight: float) -> None:
        if 0 <= x < out_w and 0 <= y < out_h:
            at = (y * out_w + x) * 3
            for k in range(3):
                canvas[at + k] = round(canvas[at + k] * (1 - weight) + colour[k] * weight)

    def label(text: str, x: int, y: int) -> None:
        for n, character in enumerate(text):
            for row, bits in enumerate(DIGITS[character]):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(2):
                            for dx in range(2):
                                tint(x + n * 8 + column * 2 + dx, y + row * 2 + dy, (0, 0, 0), 1.0)

    blue = (40, 110, 255)
    first = int(-(-x0 // step)) * step
    for value in range(first, int(x1) + 1, step):
        x = LEFT_MARGIN + round((value - x0) * per_point)
        for y in range(TOP_MARGIN, out_h):
            tint(x, y, blue, 0.35)
        label(str(value), x - 4 * len(str(value)), 4)
    first = int(-(-y0 // step)) * step
    for value in range(first, int(y1) + 1, step):
        y = TOP_MARGIN + round((value - y0) * per_point)
        for x in range(LEFT_MARGIN, out_w):
            tint(x, y, blue, 0.35)
        label(str(value), 2, y - 5)
    return out_w, out_h, bytes(canvas)


def page_box(document: Path, page: int) -> tuple[float, float, int]:
    """The page's width and height in points and the page count."""
    from pypdf import PdfReader
    from alx.providers.pdf_limits import enforce_pdf_decode_limits
    enforce_pdf_decode_limits()
    try:
        reader = PdfReader(str(document))
        count = len(reader.pages)
        if not 1 <= page <= count:
            raise LookError("arguments_unusable", f"the document has {count} pages")
        box = reader.pages[page - 1].mediabox
        return float(box.width), float(box.height), count
    except LookError:
        raise
    except Exception as error:  # noqa: BLE001 - an unreadable PDF is a fact for her
        raise LookError("document_unavailable", f"the PDF cannot be read ({type(error).__name__})") from error


def look(
    path: Path,
    arguments: Mapping[str, Any],
    *,
    store: Path,
    scratch_root: Path,
    renderer: Path,
    picture_converter: Path,
    execute: Callable[[Path, list[str], Path], Any],
) -> dict[str, Any]:
    """Make the image she asked for, keep it in her look store, describe it."""
    suffix = path.suffix.lower()
    if not path.is_file():
        raise LookError("document_unavailable", "no such file")
    if suffix == ".pdf":
        page = arguments.get("page", 1)
        if isinstance(page, bool) or not isinstance(page, int):
            raise LookError("arguments_unusable", "page is a page number from 1")
        page_width, page_height, count = page_box(path, page)
        section = arguments.get("section") or [0, 0, page_width, page_height]
        try:
            raw = render_section(path, {"page": page, "section": list(section)}, scratch_root=scratch_root,
                                 renderer=renderer, execute=execute,
                                 long_side=LONG_SIDE_PIXELS - LEFT_MARGIN, raw=True)
        except PageError as error:
            raise LookError(error.code, error.detail) from error
        width, height, pixels = ppm(raw)
        box = tuple(float(v) for v in section)
        step = grid_step(box[2] - box[0], box[3] - box[1])
        width, height, pixels = with_grid(width, height, pixels, box, step)
        image = png(width, height, pixels)
        facts = {"kind": "pdf_page", "page": page, "page_count": count,
                 "page_size_points": [round(page_width, 2), round(page_height, 2)],
                 "section_points": [round(v, 2) for v in box],
                 "grid": {"step_points": step, "origin": "top left of the page",
                          "labels": "points, along the top and left edges"},
                 "points_per_pixel": round((box[2] - box[0]) / (width - LEFT_MARGIN), 4)}
    elif suffix in PICTURES:
        if path.stat().st_size > MAX_PICTURE_BYTES:
            raise LookError("document_unavailable", "the picture is too large")
        if not picture_converter.is_file():
            raise LookError("engine_unavailable", "no picture converter installed")
        run = scratch_root / ("look-" + uuid.uuid4().hex)
        run.mkdir(parents=True)
        try:
            shutil.copyfile(path, run / ("picture" + suffix))
            # Scaled down to the long side the model reads, never up.
            executed = execute(picture_converter, [
                "-nostdin", "-loglevel", "error", "-i", "picture" + suffix, "-frames:v", "1",
                "-vf", f"scale=w='min({LONG_SIDE_PIXELS},iw)':h='min({LONG_SIDE_PIXELS},ih)'"
                ":force_original_aspect_ratio=decrease", "-y", "look.png"], run)
            output = run / "look.png"
            if getattr(executed, "returncode", 1) or not output.is_file():
                raise LookError("page_failed", (getattr(executed, "stderr", "") or "")[-1000:])
            image = output.read_bytes()
        finally:
            shutil.rmtree(run, ignore_errors=True)
        width, height = png_size(image)
        facts = {"kind": "picture"}
    else:
        raise LookError("arguments_unusable", "a PDF or a picture (PNG, JPEG, GIF, TIFF, WebP, BMP)")
    digest = hashlib.sha256(image).hexdigest()
    store.mkdir(parents=True, exist_ok=True)
    (store / (digest + ".png")).write_bytes(image)
    kept = sorted(store.glob("*.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    for old in kept[KEEP_LOOKS:]:
        old.unlink(missing_ok=True)
    return {"source": str(path), **facts, "pixels": [width, height], "image_sha256": digest}


def stored_image(store: Path, digest: Any) -> bytes | None:
    """A look's image by its digest, only if it is still exactly that image."""
    if not isinstance(digest, str) or len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
        return None
    try:
        data = (store / (digest + ".png")).read_bytes()
    except OSError:
        return None
    return data if hashlib.sha256(data).hexdigest() == digest else None


__all__ = ["LookError", "look", "stored_image"]
