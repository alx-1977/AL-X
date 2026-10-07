"""Particle Cloud product devices: list them (D-039), call their functions
(D-040), read their variables and hear what they publish (D-043)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from alx.contracts.readers import ReaderAccessError, ReaderDevice


PARTICLE_API_URL = "https://api.particle.io"
_PER_PAGE = 100
_MAX_PAGES = 20
_DEVICE_ID = re.compile(r"[0-9a-f]{24}")
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
