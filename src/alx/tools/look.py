"""AL/X looks at a picture or a PDF page herself (D-075).

One capability, `look`: she names a file and, for a PDF, a page and optionally
a section of it. The image is made in her storage and handed to her with the
result, in the reasoning session that asked, for the step that follows. What
it shows, and what it means, is hers to judge; the result only describes the
image (size, page, grid) and names it by its digest.
"""

from __future__ import annotations

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

LOOK = "look"
_FAILURES = ("arguments_unusable", "path_denied", "document_unavailable", "page_failed",
             "engine_unavailable", "unavailable")
_STRING = StructuredSchema(ValueKind.STRING)
_NUMBER = StructuredSchema(ValueKind.NUMBER)

DEFINITION = CapabilityDefinition(
    LOOK,
    "Look at a picture or a PDF page yourself: the image comes back to you with this "
    "result, for your next step only. path is a PDF or picture (PNG, JPEG, GIF, TIFF, WebP, "
    "BMP) in his design folders or ALX_Storage (the Whiteboard included). For a PDF, page "
    "is the page number (from 1) and section [x0, y0, x1, y1] an optional part of it in "
    "points from the top left; a PDF image carries a faint grid labelled in those points, "
    "so you can read exactly where something is - look at the whole page first, then at a "
    "section to read detail. Images are scaled to at most 1568 pixels a side; a full image "
    "costs about as much allowance as a page of text, so look when seeing matters. The image "
    "is not kept in your goal or conversation: to see it again in a later step, look again.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"path": _STRING, "page": StructuredSchema(ValueKind.INTEGER),
         "section": StructuredSchema(ValueKind.ARRAY, items=_NUMBER)},
        ("path",), extra_properties=False),
    StructuredSchema(ValueKind.OBJECT, extra_properties=True),
    SideEffect.NONE,
    _FAILURES,
)
DEFINITIONS = (DEFINITION,)


def build_look_executors(
    look: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None,
    call_id_source: Callable[[], str],
) -> Mapping[str, Callable[[Mapping[str, Any]], CapabilityResult]]:
    def failed(code: str, reason: str = "") -> CapabilityResult:
        failure = {"code": code, **({"reason": reason[:300]} if reason else {})}
        return CapabilityResult(call_id_source(), LOOK, CapabilityResultState.FAILED, failure=failure)

    def run(arguments: Mapping[str, Any]) -> CapabilityResult:
        if look is None:
            return failed("unavailable", "looking needs the page renderer and picture converter")
        section = arguments.get("section")
        if section is not None and len(section) != 4:
            return failed("arguments_unusable", "section is [x0, y0, x1, y1] in points")
        try:
            values = look(arguments)
        except Exception as error:  # noqa: BLE001 - reported as her evidence
            code = getattr(error, "code", None) or (error.args[0] if error.args else "")
            detail = getattr(error, "detail", None)
            if detail is None:
                recovery = getattr(error, "recovery", None)
                detail = recovery.get("detail", "") if isinstance(recovery, Mapping) else ""
            return failed(code if code in _FAILURES else "page_failed", str(detail or ""))
        return CapabilityResult(call_id_source(), LOOK, CapabilityResultState.SUCCEEDED, dict(values))

    return {LOOK: run}


__all__ = ["DEFINITIONS", "LOOK", "build_look_executors"]
