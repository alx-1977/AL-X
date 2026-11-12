"""AL/X shows Friedl something on her screen (D-073).

One capability, `show_on_screen`: a title, an information column and up to two
board views, drawn over the moving wave the reader screen uses. She decides
what to show and what each mark means; the viewer only draws it. Board views
are read from KiCad files, his (read only, through a copy) or her own work
copies, so what he sees is his actual copper. A reference is a section of a
datasheet page shown as printed, never redrawn, with her boxes on it.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from typing import Any, Mapping

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    SideEffect,
    StructuredSchema,
    ValueKind,
)

SHOW_ON_SCREEN = "show_on_screen"
TONES = ("ok", "bad", "info")
MAX_ITEMS = 12
MAX_VIEWS = 2
MAX_PAGES = 2
MAX_BOXES = 8
SIDES = ("right", "left", "below")
_FAILURES = ("arguments_unusable", "board_unavailable", "path_denied", "section_failed",
             "section_too_large", "engine_unavailable", "unavailable", "document_unavailable",
             "page_failed")

_STRING = StructuredSchema(ValueKind.STRING)
_NUMBER = StructuredSchema(ValueKind.NUMBER)
_NAMES = StructuredSchema(ValueKind.ARRAY, items=_STRING)
_POINT = StructuredSchema(ValueKind.ARRAY, items=_NUMBER)
_TONED = StructuredSchema(ValueKind.OBJECT, {"text": _STRING, "tone": _STRING}, ("text",),
                          extra_properties=False)
_ITEM = StructuredSchema(ValueKind.OBJECT, {"title": _STRING, "detail": _STRING, "tone": _STRING},
                         ("title",), extra_properties=False)
_DIM = StructuredSchema(ValueKind.OBJECT, {"from": _POINT, "to": _POINT, "text": _STRING,
                                           "tone": _STRING}, ("from", "to", "text"),
                        extra_properties=False)
_VIEW = StructuredSchema(
    ValueKind.OBJECT,
    {"label": _STRING, "sublabel": _STRING, "tone": _STRING,
     "board": _STRING, "work_copy_id": _STRING, "window": _POINT,
     "refs": _NAMES, "nets": _NAMES, "planes": _NAMES,
     "marks": StructuredSchema(ValueKind.OBJECT), "rotate": StructuredSchema(ValueKind.BOOLEAN),
     "dims": StructuredSchema(ValueKind.ARRAY, items=_DIM)},
    ("label", "window"), extra_properties=False)
_BOX = StructuredSchema(ValueKind.OBJECT, {"at": _POINT, "label": _STRING, "tone": _STRING,
                                           "side": _STRING}, ("at", "label"), extra_properties=False)
_PAGE = StructuredSchema(
    ValueKind.OBJECT,
    {"page": StructuredSchema(ValueKind.INTEGER), "section": _POINT, "caption": _STRING,
     "boxes": StructuredSchema(ValueKind.ARRAY, items=_BOX)},
    ("page", "section"), extra_properties=False)
_REFERENCE = StructuredSchema(
    ValueKind.OBJECT,
    {"label": _STRING, "sublabel": _STRING, "document": _STRING,
     "pages": StructuredSchema(ValueKind.ARRAY, items=_PAGE)},
    ("label", "document", "pages"), extra_properties=False)

DEFINITION = CapabilityDefinition(
    SHOW_ON_SCREEN,
    "Show Friedl something on your screen, over the moving wave: a title, a verdict, a short "
    "column of items and up to two board views side by side - his board on the left, your "
    "fix on a work copy on the right - so he can accept the change or not. Say one line and "
    "let the screen carry the detail. title is the part or subject only (e.g. AE1); context "
    "names the project. Tones: ok (green, correct), bad (red, needs attention), info (cyan). "
    "Lead the items with what is wrong; what is correct gets one quiet item. A view reads a "
    "window [x0, y0, x1, y1] in mm of a KiCad board - board is a path in his design folders "
    "(read through a copy, never changed), work_copy_id one of your own copies. Include every "
    "part of the circuit, from its connector on. refs are the parts to draw, nets the tracks, "
    "vias and copper shapes, planes the zone nets to show faintly. marks colours a part, "
    "'net:<name>' or 'keepout' by tone; mark what differs red on his board and green on your "
    "copy; unmarked copper is blue. rotate turns the view 90 degrees. dims draws dimension "
    "lines between two points in mm. reference is the datasheet section the layout follows, "
    "shown as printed behind a link, never redrawn: document is the PDF in his design "
    "folders, each page a page number and a section [x0, y0, x1, y1] in points from the top "
    "left of the page, boxes the places where his layout differs, in the same points, each "
    "with a short label such as his value. Showing replaces what is on screen; he closes it. "
    "shown is true only when the screen reported drawing it; when false it is not on screen "
    "(closed, not open or broken), so say that, never that it is showing. When shown, the "
    "screen itself comes back to you as a picture with this result: look at it before you "
    "say anything about it, and if something is missing or wrong on it, fix it and show "
    "again rather than describe the gap. Names not on the board are refused; every "
    "connection of a part you name is drawn, with its silkscreen marks. Two views are "
    "headed Current (his board) and Amended (your copy), in that order.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"title": _STRING, "subtitle": _STRING, "context": _STRING, "verdict": _TONED,
         "items": StructuredSchema(ValueKind.ARRAY, items=_ITEM), "note": _STRING,
         "said": _STRING, "views": StructuredSchema(ValueKind.ARRAY, items=_VIEW),
         "reference": _REFERENCE},
        ("title",), extra_properties=False),
    StructuredSchema(
        ValueKind.OBJECT, {"shown": StructuredSchema(ValueKind.BOOLEAN),
                           "version": StructuredSchema(ValueKind.INTEGER),
                           "views": StructuredSchema(ValueKind.ARRAY,
                                                     items=StructuredSchema(ValueKind.OBJECT)),
                           "reference_pages": StructuredSchema(ValueKind.INTEGER),
                           "image_sha256": StructuredSchema(ValueKind.STRING),
                           "screen_image": StructuredSchema(ValueKind.STRING),
                           "nets_drawn": StructuredSchema(ValueKind.ARRAY,
                                                          items=StructuredSchema(ValueKind.ARRAY,
                                                                                 items=StructuredSchema(ValueKind.STRING)))},
        ("shown", "version"), extra_properties=False),
    SideEffect.NONE,
    _FAILURES,
)
DEFINITIONS = (DEFINITION,)


def _json_value(value: Any) -> Any:
    """Copy frozen structured containers into frontend JSON containers."""
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class DisplayBoard:
    """What is on the viewer now; the screen polls it and opens on a new version."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Each run has its own id, so a screen left open across a restart
        # never mistakes a new display for one it has already seen.
        self._id = uuid.uuid4().hex
        self._version = 0
        self._display: dict[str, Any] | None = None
        # Datasheet section images of the current version, served apart from
        # the display itself, which her screen polls every two seconds.
        self._assets: tuple[bytes, ...] = ()
        # The newest version the screen reported drawing. Publishing is not
        # showing: on 2026-11-10 the screen had crashed and she still told
        # Friedl it was on screen, because publishing alone said "shown".
        self._drawn = 0
        self._drawn_changed = threading.Condition(self._lock)

    def publish(self, display: Mapping[str, Any], assets: tuple[bytes, ...] = ()) -> int:
        with self._lock:
            self._version += 1
            self._display = dict(display)
            self._assets = tuple(assets)
            return self._version

    def asset(self, board_id: str, version: int, index: int) -> bytes | None:
        """An image of the version on screen now; anything else is gone."""
        with self._lock:
            if board_id != self._id or version != self._version or not 0 <= index < len(self._assets):
                return None
            return self._assets[index]

    def mark_drawn(self, board_id: str, version: int) -> None:
        """The screen drew this version (it reports after drawing it)."""
        with self._lock:
            if board_id == self._id and 0 < version <= self._version:
                self._drawn = max(self._drawn, version)
                self._drawn_changed.notify_all()

    def wait_drawn(self, version: int, timeout: float) -> bool:
        """Whether the screen draws this version (or a newer one) in time."""
        with self._lock:
            return self._drawn_changed.wait_for(lambda: self._drawn >= version, timeout)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"id": self._id, "version": self._version,
                    "display": _json_value(self._display)}


def _tone(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if value not in TONES:
        raise ValueError(f"tone must be one of {', '.join(TONES)}")
    return value


def _text(value: Any, limit: int, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{name} is text of at most {limit} characters")
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("a position is a number")
    return float(value)


def _reference(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check a reference and work out, mechanically, where each box falls in its image."""
    label = _text(value.get("label"), 40, "reference.label")
    sublabel = _text(value.get("sublabel"), 60, "reference.sublabel")
    document = value.get("document")
    if not isinstance(document, str) or not document.strip():
        raise ValueError("reference.document is the path of a PDF")
    pages = list(value.get("pages") or [])
    if not 1 <= len(pages) <= MAX_PAGES:
        raise ValueError(f"reference has 1 to {MAX_PAGES} pages")
    shown, requests = [], []
    for page in pages:
        section = [_number(v) for v in page.get("section") or ()]
        if len(section) != 4 or section[0] >= section[2] or section[1] >= section[3]:
            raise ValueError("a section is [x0, y0, x1, y1] in points, x0 < x1, y0 < y1")
        x0, y0, x1, y1 = section
        boxes = list(page.get("boxes") or [])
        if len(boxes) > MAX_BOXES:
            raise ValueError(f"at most {MAX_BOXES} boxes on a page")
        placed = []
        for box in boxes:
            at = [_number(v) for v in box.get("at") or ()]
            if len(at) != 4 or at[0] >= at[2] or at[1] >= at[3]:
                raise ValueError("a box is [x0, y0, x1, y1] in points, x0 < x1, y0 < y1")
            if at[2] <= x0 or at[0] >= x1 or at[3] <= y0 or at[1] >= y1:
                raise ValueError("a box lies outside its section")
            side = box.get("side") or "right"
            if side not in SIDES:
                raise ValueError(f"side is one of {', '.join(SIDES)}")
            width, height = x1 - x0, y1 - y0
            placed.append({"x": (max(at[0], x0) - x0) / width, "y": (max(at[1], y0) - y0) / height,
                           "w": (min(at[2], x1) - max(at[0], x0)) / width,
                           "h": (min(at[3], y1) - max(at[1], y0)) / height,
                           "label": _text(box.get("label"), 40, "box.label"),
                           "tone": _tone(box.get("tone")) or "bad", "side": side})
        shown.append({"asset": len(requests), "w": x1 - x0, "h": y1 - y0,
                      "caption": _text(page.get("caption"), 60, "caption"), "boxes": placed})
        requests.append({"document": document, "page": page.get("page"), "section": section})
    return {"label": label, "sublabel": sublabel, "pages": shown}, requests


def build_display_executors(
    board: DisplayBoard,
    read_section: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    call_id_source: Callable[[], str],
    draw_wait_seconds: float = 12.0,
    read_page: Callable[[Mapping[str, Any]], bytes] | None = None,
    capture: Callable[[str, int], str | None] | None = None,
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def failed(code: str, reason: str = "") -> CapabilityResult:
        failure = {"code": code, **({"reason": reason[:300]} if reason else {})}
        return CapabilityResult(call_id_source(), SHOW_ON_SCREEN, CapabilityResultState.FAILED,
                                failure=failure)

    def show(arguments: Mapping[str, Any]) -> CapabilityResult:
        try:
            display: dict[str, Any] = {
                "title": _text(arguments.get("title"), 24, "title"),
                "subtitle": _text(arguments.get("subtitle"), 24, "subtitle"),
                "context": _text(arguments.get("context"), 80, "context"),
                "note": _text(arguments.get("note"), 400, "note"),
                "said": _text(arguments.get("said"), 300, "said"),
            }
            if not display["title"].strip():
                raise ValueError("title is required")
            verdict = arguments.get("verdict")
            display["verdict"] = None if not verdict else {
                "text": _text(verdict.get("text"), 120, "verdict.text"),
                "tone": _tone(verdict.get("tone"))}
            items = list(arguments.get("items") or [])
            if len(items) > MAX_ITEMS:
                raise ValueError(f"at most {MAX_ITEMS} items")
            display["items"] = [{"title": _text(item.get("title"), 80, "item.title"),
                                 "detail": _text(item.get("detail"), 200, "item.detail"),
                                 "tone": _tone(item.get("tone"))} for item in items]
            views = list(arguments.get("views") or [])
            if len(views) > MAX_VIEWS:
                raise ValueError(f"at most {MAX_VIEWS} views")
            for view in views:
                _text(view.get("label"), 40, "label")
                _text(view.get("sublabel"), 60, "sublabel")
                if bool(view.get("board")) == bool(view.get("work_copy_id")):
                    raise ValueError("each view names a board or a work_copy_id, not both")
                for key, value in dict(view.get("marks") or {}).items():
                    _tone(value)
                for dim in view.get("dims") or ():
                    _tone(dim.get("tone"))
                    if len(dim["from"]) != 2 or len(dim["to"]) != 2:
                        raise ValueError("a dimension runs between two [x, y] points")
            reference, page_requests = (None, []) if not arguments.get("reference") \
                else _reference(arguments["reference"])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            return failed("arguments_unusable", str(error))
        if views and read_section is None:
            return failed("unavailable", "KiCad is not available to read boards")
        if page_requests and read_page is None:
            return failed("unavailable", "datasheet pages cannot be shown here")
        drawn = []
        for view in views:
            try:
                geometry = read_section({key: view.get(key) for key in
                                         ("board", "work_copy_id", "window", "refs", "nets", "planes")})
            except Exception as error:  # noqa: BLE001 - reported as her evidence
                code = getattr(error, "code", None) or (error.args[0] if error.args else "")
                code = code if code in _FAILURES else "section_failed"
                detail = getattr(error, "recovery", None) or {}
                return failed(code, str(detail.get("detail", "") if isinstance(detail, Mapping) else ""))
            # A name that is not on the board is a mistake to correct, never
            # something to draw as nothing (2026-11-12: two feeds went missing).
            missing = {kind: list(names) for kind, names in (geometry.get("missing") or {}).items() if names}
            if missing:
                return failed("arguments_unusable", "not on this board: " + "; ".join(
                    f"{kind} {', '.join(names)}" for kind, names in sorted(missing.items())))
            drawn.append({
                "label": _text(view.get("label"), 40, "label"),
                "sublabel": _text(view.get("sublabel"), 60, "sublabel"),
                "tone": view.get("tone"), "rotate": bool(view.get("rotate")),
                "marks": dict(view.get("marks") or {}), "dims": list(view.get("dims") or ()),
                "geometry": geometry,
            })
        # Two views are always his board as it is and her amended copy, under
        # the same two headings (Friedl, 2026-11-12); her own label moves under.
        if len(drawn) == 2:
            for item, heading in zip(drawn, ("Current", "Amended")):
                item["sublabel"] = item["sublabel"] or item["label"]
                item["label"] = heading
        display["views"] = drawn
        assets = []
        for request in page_requests:
            try:
                assets.append(bytes(read_page(request)))
            except Exception as error:  # noqa: BLE001 - reported as her evidence
                code = getattr(error, "code", None) or (error.args[0] if error.args else "")
                code = code if code in _FAILURES else "page_failed"
                detail = getattr(error, "recovery", None) or {}
                return failed(code, str(detail.get("detail", "") if isinstance(detail, Mapping) else ""))
        display["reference"] = reference
        version = board.publish(display, tuple(assets))
        # The screen polls every two seconds, then loads and draws the page.
        shown = board.wait_drawn(version, draw_wait_seconds)
        # D-075: what she put on his screen comes back to her as a picture, so
        # she sees what he sees before she says anything about it.
        seen = None
        if shown and capture is not None:
            try:
                seen = capture(board.snapshot()["id"], version)
            except Exception:  # noqa: BLE001 - a missing picture is reported as such
                seen = None
            # Versions only rise: if this one is still current after the
            # picture, the picture is of this one. Otherwise it may not be.
            if board.snapshot()["version"] != version:
                seen = None
        return CapabilityResult(
            call_id_source(), SHOW_ON_SCREEN, CapabilityResultState.SUCCEEDED,
            {"shown": shown, "version": version,
             **({"image_sha256": seen} if seen else {"screen_image": "not captured"}),
             "nets_drawn": [item["geometry"].get("nets_drawn", []) for item in drawn],
             "views": [{"label": item["label"],
                        "parts": sorted(part["ref"] for part in item["geometry"].get("parts", ())),
                        "tracks": len(item["geometry"].get("tracks", ()))} for item in drawn],
             "reference_pages": len(assets)})

    return {SHOW_ON_SCREEN: show}


__all__ = ["DEFINITIONS", "DisplayBoard", "SHOW_ON_SCREEN", "build_display_executors"]
