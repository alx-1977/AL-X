"""Read Particle Cloud product devices. Reads only (D-039)."""

from __future__ import annotations

from typing import Any

import httpx

from alx.contracts.readers import ReaderAccessError, ReaderDevice


PARTICLE_API_URL = "https://api.particle.io"
_PER_PAGE = 100
_MAX_PAGES = 20


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

    def _get(self, path: str, params: dict[str, str]) -> Any:
        failure = ""
        try:
            response = httpx.get(
                f"{self._base_url}{path}", params=params, timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except Exception:
            failure = "connection_failed"
        if failure:
            raise ReaderAccessError(failure)
        if response.status_code in (401, 403):
            raise ReaderAccessError("permission_denied")
        if response.status_code == 404:
            raise ReaderAccessError("product_not_found")
        if response.status_code == 429:
            raise ReaderAccessError("rate_limited")
        if response.status_code >= 400:
            raise ReaderAccessError("request_rejected")
        try:
            return response.json()
        except Exception:
            failure = "response_invalid"
        raise ReaderAccessError(failure)
