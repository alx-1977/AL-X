"""LlamaCloud structured invoice extraction behind a thin HTTP adapter.

This reads one document into AL/X's existing invoice field mapping and stops.
It does not choose a supplier, an account, a tax treatment, or whether a bill
should exist. Those remain Core judgment or deterministic capture code.

The live surface is LlamaExtract v2: upload bytes, start an inline-schema
extract job, poll until a terminal status. Parse-to-markdown is not used, and
LlamaCloud's stock invoice schema is not used: the caller supplies AL/X's
fields so downstream `checked_invoice` stays authoritative.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import httpx

from alx.contracts import SpecialistError


DEFAULT_BASE_URL = "https://api.cloud.llamaindex.ai"
FILES_PATH = "/api/v1/beta/files"
EXTRACT_PATH = "/api/v2/extract"
EXTRACT_TIER = "agentic"
# Extract v2 production pin: YYYY-MM-DD, not "latest". Official REST/SDK
# examples use 2026-03-31 with the agentic tier.
# https://developers.llamaindex.ai/llamaparse/extract/api/
# https://developers.llamaindex.ai/llamaparse/extract/guides/configuring-extract/
EXTRACT_VERSION = "2026-03-31"
EXTRACT_TARGET = "per_doc"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_PAGES = 20
POLL_INTERVAL_SECONDS = 1.0
TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_INVOICE_FIELDS = (
    "document_type",
    "supplier_name",
    "invoice_number",
    "invoice_date",
    "due_date",
    "currency",
    "subtotal",
    "tax_amount",
    "total",
    "description",
)
_ALLOWED_MEDIA = frozenset(
    {
        "application/pdf",
        "image/gif",
        "image/heic",
        "image/heif",
        "image/jpeg",
        "image/jpg",
        "image/png",
        "image/tif",
        "image/tiff",
        "image/webp",
    }
)
_IMAGE_SUFFIXES = frozenset(
    {".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
)


class LlamaParseInvoiceExtractor:
    """Extract one supplier invoice through LlamaCloud, then return fields."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: int = 60,
        project_id: str = "",
        data_schema: Mapping[str, Any] | None = None,
        instruction: str = "",
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must not be blank")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must not be blank")
        if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be a positive integer")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a positive integer")
        if not isinstance(data_schema, Mapping) or not data_schema:
            raise ValueError("data_schema must be a non-empty schema")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("instruction must not be blank")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._project_id = project_id.strip()
        self._data_schema = dict(data_schema)
        self._instruction = instruction
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
        )
        self._sleeper = sleeper or sleep
        self._clock = clock or monotonic
        self._poll_interval_seconds = poll_interval_seconds

    def extract(
        self,
        payload: bytes,
        media_type: str,
        filename: str,
        context_line: str = "",
    ) -> dict[str, str]:
        """Return AL/X invoice fields from one document. Decide nothing."""
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise SpecialistError("document_has_no_text")
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise SpecialistError("document_too_large")
        if not isinstance(media_type, str):
            raise SpecialistError("unsupported_media_type")
        if not isinstance(filename, str):
            filename = ""
        if not _usable_media(media_type, filename):
            raise SpecialistError("unsupported_media_type")
        # context_line is untrusted mail metadata (subject/filename). Extract v2
        # has no separate data field for it, so it is not sent: putting it in
        # system_prompt would give it the same authority as INSTRUCTION.
        safe_name = Path(filename).name or _default_filename(media_type)
        deadline = self._clock() + self._timeout_seconds
        file_id, project_id = self._upload(
            bytes(payload), media_type, safe_name, deadline
        )
        job_id = self._start_extract(file_id, project_id, deadline)
        result = self._poll_extract(job_id, project_id, deadline)
        return _invoice_fields(result)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": "alx-invoice-extract",
        }

    def _params(self, project_id: str = "") -> dict[str, str]:
        identity = self._project_id or project_id.strip()
        return {"project_id": identity} if identity else {}

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise SpecialistError("extraction_timeout")
        return remaining

    def _request(self, deadline: float, method: str, url: str, **kwargs) -> httpx.Response:
        remaining = self._remaining(deadline)
        kwargs["timeout"] = remaining
        kwargs.setdefault("headers", self._headers())
        failure = ""
        try:
            response = self._client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            failure = "extraction_timeout"
        except httpx.HTTPError:
            failure = "provider_failed"
        else:
            self._remaining(deadline)
            return response
        self._remaining(deadline)
        raise SpecialistError(failure)

    def _upload(
        self, payload: bytes, media_type: str, filename: str, deadline: float
    ) -> tuple[str, str]:
        kind = media_type.strip() or "application/octet-stream"
        response = self._request(
            deadline,
            "POST",
            self._url(FILES_PATH),
            params=self._params(),
            files={"file": (filename, payload, kind)},
            data={"purpose": "extract"},
        )
        body = _json_object(response)
        file_id = body.get("id")
        if not isinstance(file_id, str) or not file_id.strip():
            raise SpecialistError("provider_failed")
        project_id = body.get("project_id")
        project = project_id.strip() if isinstance(project_id, str) else ""
        return file_id.strip(), project

    def _start_extract(self, file_id: str, project_id: str, deadline: float) -> str:
        response = self._request(
            deadline,
            "POST",
            self._url(EXTRACT_PATH),
            params=self._params(project_id),
            json={
                "file_input": file_id,
                "configuration": {
                    "tier": EXTRACT_TIER,
                    "version": EXTRACT_VERSION,
                    "extraction_target": EXTRACT_TARGET,
                    "max_pages": MAX_PAGES,
                    "data_schema": self._data_schema,
                    "system_prompt": self._instruction,
                    "cite_sources": False,
                    "confidence_scores": False,
                },
            },
        )
        job_id = _json_object(response).get("id")
        if not isinstance(job_id, str) or not job_id.strip():
            raise SpecialistError("provider_failed")
        return job_id.strip()

    def _poll_extract(
        self, job_id: str, project_id: str, deadline: float
    ) -> Mapping[str, Any]:
        path = f"{EXTRACT_PATH}/{job_id}"
        while True:
            response = self._request(
                deadline,
                "GET",
                self._url(path),
                params=self._params(project_id),
            )
            body = _json_object(response)
            status = body.get("status")
            if not isinstance(status, str):
                raise SpecialistError("provider_failed")
            if status == "COMPLETED":
                result = body.get("extract_result")
                if not isinstance(result, Mapping):
                    raise SpecialistError("answer_not_structured")
                return result
            if status == "FAILED":
                raise SpecialistError("provider_failed")
            if status == "CANCELLED":
                raise SpecialistError("provider_failed")
            if status not in ("PENDING", "RUNNING"):
                raise SpecialistError("provider_failed")
            remaining = self._remaining(deadline)
            self._sleeper(min(self._poll_interval_seconds, remaining))


def _usable_media(media_type: str, filename: str) -> bool:
    kind = media_type.strip().lower()
    if kind in _ALLOWED_MEDIA or kind.startswith("image/"):
        return True
    suffix = Path(filename).suffix.lower()
    return suffix == ".pdf" or suffix in _IMAGE_SUFFIXES


def _default_filename(media_type: str) -> str:
    kind = media_type.strip().lower()
    if kind == "application/pdf":
        return "invoice.pdf"
    if kind.startswith("image/"):
        subtype = kind.split("/", 1)[-1].split(";")[0] or "bin"
        return f"invoice.{subtype}"
    return "invoice.bin"


def _json_object(response: httpx.Response) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise SpecialistError("provider_failed")
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise SpecialistError("provider_failed") from None
    if not isinstance(body, dict):
        raise SpecialistError("provider_failed")
    return body


def _invoice_fields(values: Mapping[str, Any]) -> dict[str, str]:
    return {name: _as_text(values.get(name)) for name in _INVOICE_FIELDS}


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        try:
            return format(Decimal(str(value)), "f")
        except InvalidOperation:
            return ""
    return ""
