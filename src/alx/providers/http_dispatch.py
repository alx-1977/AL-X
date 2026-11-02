"""Delivery receipts for a single HTTP model request, independent of its result."""

from collections.abc import Callable
from typing import TypeVar

import httpx

from alx.contracts import ModelRequest


Response = TypeVar('Response')


def dispatch_with_receipt(request: ModelRequest, send: Callable[[], Response]) -> Response:
    """A response or read failure proves dispatch; connection/pool failures do not.

    For streams, send enters the response context, before any body parsing.
    A write failure can leave only a partial request, so it is not a receipt.
    """
    try:
        response = send()
    except (httpx.ReadTimeout, httpx.ReadError, httpx.RemoteProtocolError):
        if request.on_dispatched is not None:
            request.on_dispatched()
        raise
    if request.on_dispatched is not None:
        request.on_dispatched()
    return response
