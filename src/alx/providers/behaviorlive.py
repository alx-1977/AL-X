"""Read one BHL reader's schedule from BehaviorLive. Reads only (D-039)."""

from __future__ import annotations

import re
from typing import Any, Mapping

import httpx

from alx.contracts.readers import ReaderAccessError


DEFAULT_CONFIG_URL = "https://behaviorlive.com/api/attendance/{reader}/config"
_READER_UID = re.compile(r"[0-9a-f]{8}")
# A schedule is a day of events; anything near this is not one.
MAX_CONFIG_BYTES = 1_000_000


class BehaviorLiveConfig:
    def __init__(self, url_template: str = DEFAULT_CONFIG_URL,
                 timeout_seconds: int = 20) -> None:
        if "{reader}" not in url_template:
            raise ValueError("url_template must contain {reader}")
        self._template = url_template
        self._timeout = timeout_seconds

    def config(self, reader_uid: str) -> Mapping[str, Any]:
        # The UID is placed in a URL, so only a well-formed one is accepted.
        if not isinstance(reader_uid, str) or not _READER_UID.fullmatch(reader_uid):
            raise ReaderAccessError("reader_uid_invalid")
        failure = ""
        try:
            response = httpx.get(
                self._template.format(reader=reader_uid), timeout=self._timeout,
                follow_redirects=False,
            )
        except Exception:
            failure = "connection_failed"
        if failure:
            raise ReaderAccessError(failure)
        if response.status_code == 404:
            raise ReaderAccessError("reader_not_configured")
        if response.status_code >= 400:
            raise ReaderAccessError("request_rejected")
        if len(response.content) > MAX_CONFIG_BYTES:
            raise ReaderAccessError("response_too_large")
        try:
            body = response.json()
        except Exception:
            failure = "schedule_unreadable"
        if failure:
            raise ReaderAccessError(failure)
        if not isinstance(body, dict):
            raise ReaderAccessError("schedule_unreadable")
        return body
