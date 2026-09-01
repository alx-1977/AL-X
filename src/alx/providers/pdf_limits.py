"""Process-wide resource ceilings for parsing untrusted PDF content streams."""

from __future__ import annotations

import pypdf.filters as pdf_filters


# pypdf otherwise permits an individual compressed stream to expand to 75 MB
# before refusing it.  Mail attachments are untrusted, and page extraction can
# turn that decoded stream into far more parser state, so cap the decoder rather
# than trying to infer an unknown expansion ratio from the compressed bytes.
PDF_DECODED_STREAM_BYTES = 4 * 1024 * 1024


def enforce_pdf_decode_limits() -> None:
    """Keep every pypdf decoder at or below AL/X's content-stream ceiling.

    These limits are intentionally process-wide.  Every PDF handled by this
    runtime is untrusted, and restoring pypdf's larger defaults after one call
    would create a race with concurrent mail or DHL extraction.
    """

    for name in (
        "ZLIB_MAX_OUTPUT_LENGTH",
        "LZW_MAX_OUTPUT_LENGTH",
        "RUN_LENGTH_MAX_OUTPUT_LENGTH",
        "JBIG2_MAX_OUTPUT_LENGTH",
        "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
    ):
        current = getattr(pdf_filters, name)
        # pypdf uses zero to mean "unlimited", so it must not win a numeric
        # minimum against the finite AL/X ceiling.
        bounded = (
            PDF_DECODED_STREAM_BYTES
            if not isinstance(current, int) or current <= 0
            else min(current, PDF_DECODED_STREAM_BYTES)
        )
        setattr(pdf_filters, name, bounded)
