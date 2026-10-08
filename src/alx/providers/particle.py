"""Particle Cloud product devices: list them (D-039), call their functions
(D-040), read their variables and hear what they publish (D-043)."""

from __future__ import annotations

import csv
import io
import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import quote

import httpx

from alx.contracts.readers import ReaderAccessError, ReaderDevice


PARTICLE_API_URL = "https://api.particle.io"
_PER_PAGE = 100
_MAX_PAGES = 20
_DEVICE_ID = re.compile(r"[0-9a-f]{24}")
_API_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_EVENT_STREAM = re.compile(r"/events(/|$)")
_API_REPLY_BYTES = 1_000_000
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_usage_csv(text: str) -> tuple[dict[str, Any], ...]:
    """The device rows of a Particle usage report, after its preamble."""
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines)
                  if line.startswith("Date,Device ID")), None)
    if start is None:
        raise ReaderAccessError("usage_unreadable")
    rows = []
    for row in csv.DictReader(io.StringIO("\n".join(lines[start:]))):
        try:
            operations = int(row.get("Data Operations") or 0)
        except ValueError:
            raise ReaderAccessError("usage_unreadable") from None
        rows.append({
            "date": row.get("Date", ""), "device_id": row.get("Device ID", ""),
            "device_name": row.get("Device Name", ""), "product_id": row.get("Product ID", ""),
            "product_name": row.get("Product Name", ""),
            "connectivity": row.get("Connectivity", ""),
            "data_operations": operations, "firmware_version": row.get("Firmware Version", ""),
            "device_os": row.get("Device OS", ""),
        })
    return tuple(rows)
_FUNCTION = re.compile(r"[A-Za-z0-9_]{1,12}")


class ParticleCloud:
    def __init__(self, access_token: str, timeout_seconds: int = 20,
                 base_url: str = PARTICLE_API_URL) -> None:
        if not isinstance(access_token, str) or not access_token.strip():
            raise ValueError("access_token must not be blank")
        self._token = access_token.strip()
        self._timeout = timeout_seconds
        self._base_url = base_url.rstrip("/")

    def devices(self, product_id: int) -> tuple[ReaderDevice, ...]:
        """Every device in one product, every page."""
        found: list[ReaderDevice] = []
        for page in range(1, _MAX_PAGES + 1):
            body = self._get(
                f"/v1/products/{int(product_id)}/devices",
                {"perPage": str(_PER_PAGE), "page": str(page)},
            )
            items = body.get("devices") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise ReaderAccessError("response_invalid")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ReaderAccessError("response_invalid")
                found.append(ReaderDevice(
                    item["id"], str(item.get("name") or ""), int(product_id),
                    bool(item.get("online")), str(item.get("last_heard") or ""),
                ))
            meta = body.get("meta") if isinstance(body, dict) else None
            total = meta.get("total_pages") if isinstance(meta, dict) else None
            if not isinstance(total, int) or page >= total:
                return tuple(found)
        raise ReaderAccessError("response_invalid")

    def call_function(
        self, product_id: int, device_id: str, function: str, argument: str
    ) -> int:
        """Call one exposed function on one device; its integer return value.

        Particle answers 404 when the device is not connected, 400 when the
        function is not exposed, and 408 when the device did not answer in
        time, so the call may or may not have reached it.
        """
        if not _DEVICE_ID.fullmatch(device_id or "") or not _FUNCTION.fullmatch(function or ""):
            raise ReaderAccessError("arguments_unusable")
        body = self._request(
            "POST", f"/v1/products/{int(product_id)}/devices/{device_id}/{function}",
            data={"arg": argument},
            missing="device_offline", bad_request="function_not_exposed",
        )
        value = body.get("return_value") if isinstance(body, dict) else None
        if not isinstance(value, int) or isinstance(value, bool):
            raise ReaderAccessError("response_invalid")
        return value

    def read_variable(self, product_id: int, device_id: str, name: str) -> Any:
        """One exposed variable's current value, read from the device now."""
        if not _DEVICE_ID.fullmatch(device_id or "") or not _FUNCTION.fullmatch(name or ""):
            raise ReaderAccessError("arguments_unusable")
        body = self._request(
            "GET", f"/v1/products/{int(product_id)}/devices/{device_id}/{name}",
            missing="device_offline", bad_request="variable_not_exposed",
        )
        if not isinstance(body, dict) or "result" not in body:
            raise ReaderAccessError("response_invalid")
        return body["result"]

    def ping(self, product_id: int, device_id: str, timeout_seconds: float = 45) -> bool:
        """Whether the device answers the cloud now (D-043).

        A cloud API call answered with a keep-alive: no data operation, about
        122 bytes of cellular data. Particle waits about 30 seconds for an
        answer before reporting it offline. Unlike the device list's "online",
        which can lag a lost connection by most of an hour, this asks now.
        """
        if not _DEVICE_ID.fullmatch(device_id or ""):
            raise ReaderAccessError("arguments_unusable")
        try:
            response = httpx.put(
                f"{self._base_url}/v1/products/{int(product_id)}/devices/{device_id}/ping",
                timeout=timeout_seconds,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except Exception:
            raise ReaderAccessError("connection_failed") from None
        if response.status_code in (401, 403):
            raise ReaderAccessError("permission_denied")
        if response.status_code >= 400:
            raise ReaderAccessError("request_rejected")
        try:
            body = response.json()
        except Exception:
            raise ReaderAccessError("response_invalid") from None
        if not isinstance(body, dict) or not isinstance(body.get("online"), bool):
            raise ReaderAccessError("response_invalid")
        return body["online"]

    def stream_events(
        self, product_id: int, prefix: str,
        on_event: Callable[[str, str, str, str], None],
        read_timeout_seconds: float = 300,
    ) -> None:
        """Deliver each event the product's devices publish under `prefix`.

        Particle's server-sent event stream: an outgoing connection, so nothing
        has to reach this machine from outside. Returns when the stream ends or
        stalls (no data, not even a keep-alive, within the read timeout); the
        caller reconnects. `on_event(name, device_id, data, published_at)`.
        """
        # The prefix is one path segment: Particle answers 404 to an event name
        # whose slash is left unencoded.
        url = (f"{self._base_url}/v1/products/{int(product_id)}/events/"
               f"{quote(prefix, safe='')}")
        timeout = httpx.Timeout(self._timeout, read=read_timeout_seconds)
        name = ""
        try:
            with httpx.stream("GET", url, timeout=timeout,
                              headers={"Authorization": f"Bearer {self._token}"}) as response:
                if response.status_code in (401, 403):
                    raise ReaderAccessError("permission_denied")
                if response.status_code >= 400:
                    raise ReaderAccessError("request_rejected")
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        name = line[len("event:"):].strip()
                    elif line.startswith("data:") and name:
                        try:
                            payload = json.loads(line[len("data:"):].strip())
                        except ValueError:
                            name = ""
                            continue
                        if isinstance(payload, dict):
                            on_event(name, str(payload.get("coreid") or ""),
                                     str(payload.get("data") or ""),
                                     str(payload.get("published_at") or ""))
                        name = ""
                    elif not line:
                        name = ""
        except ReaderAccessError:
            raise
        except Exception:
            raise ReaderAccessError("connection_failed") from None

    # ---- full account access (D-045) -------------------------------------

    def api(self, method: str, path: str, query: dict[str, Any] | None = None,
            body: Any = None) -> tuple[int, Any]:
        """Any Particle Cloud API call, as (HTTP status, decoded body).

        The token goes to Particle's API address only: `path` must be a /v1/
        path on it, never a full URL. Particle's event streams never end and are
        refused here (AL/X already listens to them). Replies are capped in size.
        A refusal is returned with its status and body, not raised, so AL/X
        sees what Particle said.
        """
        method = (method or "").upper()
        if method not in _API_METHODS:
            raise ReaderAccessError("arguments_unusable")
        if (not isinstance(path, str) or not path.startswith("/v1/") or "://" in path
                or ".." in path or any(ch in path for ch in "?#\\ ")):
            raise ReaderAccessError("arguments_unusable")
        if _EVENT_STREAM.search(path) and method == "GET":
            raise ReaderAccessError("event_stream_not_supported")
        try:
            response = httpx.request(
                method, f"{self._base_url}{path}", params=query or None,
                json=body if body is not None and method != "GET" else None,
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.TimeoutException:
            raise ReaderAccessError("device_timeout") from None
        except Exception:
            raise ReaderAccessError("connection_failed") from None
        content = response.content[:_API_REPLY_BYTES]
        try:
            decoded: Any = json.loads(content) if content else None
        except ValueError:
            decoded = content.decode("utf-8", "replace")
        if len(response.content) > _API_REPLY_BYTES:
            decoded = {"truncated": True, "partial": content.decode("utf-8", "replace")}
        return response.status_code, decoded

    def usage(self, start: str, end: str, devices: Sequence[str] = (),
              wait_seconds: float = 120, sleep: Callable[[float], None] = time.sleep,
              ) -> tuple[dict[str, Any], ...]:
        """Data operations per device per day, from Particle's usage report.

        Requests a devices report for the dates (YYYY-MM-DD, inclusive) on the
        account's active service agreement, waits for it, and reads its CSV. The
        download link is pre-signed by Particle's storage, so the token is not
        sent there. Particle also emails the account owner a copy.
        """
        if not (_DATE.fullmatch(start or "") and _DATE.fullmatch(end or "")) or end < start:
            raise ReaderAccessError("arguments_unusable")
        status, agreements = self.api("GET", "/v1/user/service_agreements")
        active = [item for item in (agreements or {}).get("data", ())
                  if isinstance(item, dict)
                  and (item.get("attributes") or {}).get("state") == "active"]
        if status != 200 or not active:
            raise ReaderAccessError("usage_unavailable")
        payload: dict[str, Any] = {"report_type": "devices", "date_period_start": start,
                                   "date_period_end": end}
        if devices:
            payload["devices"] = list(devices)
        status, created = self.api(
            "POST", f"/v1/user/service_agreements/{active[0]['id']}/usage_reports", body=payload)
        report_id = ((created or {}).get("data") or {}).get("id") if isinstance(created, dict) else None
        if status not in (200, 201) or not report_id:
            raise ReaderAccessError("usage_unavailable")
        waited = 0.0
        while True:
            status, report = self.api("GET", f"/v1/user/usage_reports/{report_id}")
            attributes = ((report or {}).get("data") or {}).get("attributes") or {} \
                if isinstance(report, dict) else {}
            if status == 200 and attributes.get("state") == "available":
                break
            if status != 200 or attributes.get("state") not in ("pending", "processing"):
                raise ReaderAccessError("usage_unavailable")
            if waited >= wait_seconds:
                raise ReaderAccessError("usage_not_ready")
            sleep(5)
            waited += 5
        url = attributes.get("download_url") or ""
        if not url.startswith("https://"):
            raise ReaderAccessError("usage_unavailable")
        try:
            download = httpx.get(url, timeout=self._timeout)
        except Exception:
            raise ReaderAccessError("connection_failed") from None
        if download.status_code != 200:
            raise ReaderAccessError("usage_unavailable")
        return parse_usage_csv(download.text)

    def _get(self, path: str, params: dict[str, str]) -> Any:
        return self._request("GET", path, params=params, missing="product_not_found")

    def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None,
        data: dict[str, str] | None = None, missing: str, bad_request: str = "request_rejected",
    ) -> Any:
        failure = ""
        try:
            headers = {"Authorization": f"Bearer {self._token}"}
            url = f"{self._base_url}{path}"
            if method == "POST":
                response = httpx.post(url, data=data, timeout=self._timeout, headers=headers)
            else:
                response = httpx.get(url, params=params, timeout=self._timeout, headers=headers)
        except httpx.TimeoutException:
            failure = "device_timeout" if method == "POST" else "connection_failed"
        except Exception:
            failure = "connection_failed"
        if failure:
            raise ReaderAccessError(failure)
        if response.status_code in (401, 403):
            raise ReaderAccessError("permission_denied")
        if response.status_code == 404:
            raise ReaderAccessError(missing)
        if response.status_code == 400:
            raise ReaderAccessError(bad_request)
        if response.status_code == 408:
            raise ReaderAccessError("device_timeout")
        if response.status_code == 429:
            raise ReaderAccessError("rate_limited")
        if response.status_code >= 400:
            raise ReaderAccessError("request_rejected")
        try:
            return response.json()
        except Exception:
            failure = "response_invalid"
        raise ReaderAccessError(failure)
