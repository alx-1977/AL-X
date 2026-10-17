"""Ship24's one HTTP boundary for tracking one shipment.

POST /trackers/track is idempotent only for an identical payload. It may create
a quota-consuming tracker on the first call. The provider returns Ship24 facts
without classifying shipment meaning.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


_URL = "https://api.ship24.com/public/v1/trackers/track"
_EXHAUSTED_CODES = frozenset({
    "plan_limit_exceeded", "quota_exceeded", "monthly_quota_exceeded",
    "tracking_limit_exceeded", "trackers_limit_exceeded", "subscription_limit_exceeded",
})


class Ship24Error(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Ship24Error("provider_failed")
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _latest_event_time(events: list[dict[str, str]]) -> str | None:
    """Use the latest event only where Ship24's timestamps are comparable."""
    values = [event.get("time") for event in events]
    if not values or any(not value for value in values):
        return None
    if len(values) == 1:
        return values[0]
    try:
        parsed = [datetime.fromisoformat(value) for value in values if value]
        # Ship24 logistics dates may be date-only or offset-free local times.
        # Those cannot safely be ranked against another courier's clock.
        if any(item.tzinfo is None for item in parsed):
            return None
        return values[max(range(len(parsed)), key=lambda index: parsed[index])]
    except ValueError:
        return None


def _parse(payload: Any) -> dict[str, Any]:
    body = _object(payload)
    data = _object(body.get("data"))
    trackings = data.get("trackings")
    if not isinstance(trackings, list) or len(trackings) != 1:
        # One request can describe a tracking number shared by distinct parcels.
        # Choosing one of several would be a business judgment.
        raise Ship24Error("tracking_ambiguous" if isinstance(trackings, list) and trackings else "provider_failed")
    tracking = _object(trackings[0])
    tracker = _object(tracking.get("tracker"))
    shipment = _object(tracking.get("shipment"))
    number = _text(tracker.get("trackingNumber"))
    milestone = _text(shipment.get("statusMilestone"))
    if not number or not milestone:
        raise Ship24Error("provider_failed")
    raw_events = tracking.get("events")
    if not isinstance(raw_events, list):
        raise Ship24Error("provider_failed")
    events: list[dict[str, str]] = []
    for raw in raw_events:
        event = _object(raw)
        events.append({
            target: value for target, value in (
                ("time", _text(event.get("occurrenceDatetime"))),
                ("location", _text(event.get("location"))),
                ("description", _text(event.get("status"))),
                ("status", _text(event.get("statusCode"))),
                ("courier_code", _text(event.get("courierCode"))),
            ) if value is not None
        })
    courier_codes = tracker.get("courierCode")
    courier = None
    if isinstance(courier_codes, list) and len(courier_codes) == 1:
        courier = _text(courier_codes[0])
    else:
        found_codes = {event["courier_code"] for event in events if "courier_code" in event}
        if len(found_codes) == 1:
            courier = next(iter(found_codes))
    delivery = shipment.get("delivery")
    estimate = _text(_object(delivery).get("estimatedDeliveryDate")) if delivery is not None else None
    # Preserve the provider's event order and the exact timestamp strings.
    return {key: value for key, value in {
        "tracking_number": number,
        "courier": courier,
        "status": _text(shipment.get("statusCode")),
        "status_milestone": milestone,
        "estimated_delivery": estimate,
        "last_update": _latest_event_time(events),
        "events": events,
    }.items() if value is not None}


class Ship24Provider:
    def __init__(self, environment: Mapping[str, str] | None = None,
                 allowance_path: Path | None = None) -> None:
        self._environment = environment if environment is not None else os.environ
        self._allowance_path = allowance_path
        self._lock = threading.Lock()
        self._blocked_month: str | None = None
        if allowance_path is not None:
            with closing(sqlite3.connect(allowance_path)) as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS ship24_allowance "
                    "(month TEXT PRIMARY KEY, exhausted INTEGER NOT NULL)"
                )
                connection.commit()

    @staticmethod
    def _month() -> str:
        return datetime.now(UTC).strftime("%Y-%m")

    @staticmethod
    def _allowance_exhausted(response: httpx.Response) -> bool:
        # A transient 429 is not necessarily the monthly plan limit.
        remaining = response.headers.get("x-ship24-monthly-remaining")
        if remaining is not None and remaining.strip() == "0":
            return True
        try:
            errors = _object(response.json()).get("errors")
        except (ValueError, Ship24Error):
            return False
        return isinstance(errors, list) and any(
            isinstance(item, Mapping) and item.get("code") in _EXHAUSTED_CODES
            for item in errors
        )

    def track(self, tracking_number: str, courier_code: str | None = None) -> dict[str, Any]:
        token = self._environment.get("SHIP24_API_KEY", "").strip()
        if not token:
            raise Ship24Error("not_configured")
        payload: dict[str, Any] = {"trackingNumber": tracking_number}
        if courier_code:
            payload["courierCode"] = [courier_code]
        month = self._month()
        # Hold the ledger transaction over the request so concurrent processes
        # cannot send another tracker after this one learns the quota is gone.
        with self._lock:
            if self._allowance_path is None:
                if self._blocked_month == month:
                    raise Ship24Error("tracking_allowance_exhausted")
                response = self._post(token, payload)
                if self._allowance_exhausted(response):
                    self._blocked_month = month
                    if response.status_code >= 400:
                        raise Ship24Error("tracking_allowance_exhausted")
            else:
                with closing(sqlite3.connect(self._allowance_path, timeout=80)) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    blocked = connection.execute(
                        "SELECT exhausted FROM ship24_allowance WHERE month = ?", (month,)
                    ).fetchone()
                    if blocked and blocked[0]:
                        raise Ship24Error("tracking_allowance_exhausted")
                    response = self._post(token, payload)
                    if self._allowance_exhausted(response):
                        connection.execute(
                            "INSERT OR REPLACE INTO ship24_allowance VALUES (?, 1)", (month,)
                        )
                        connection.commit()
                        if response.status_code >= 400:
                            raise Ship24Error("tracking_allowance_exhausted")
                    else:
                        connection.commit()
        if response.status_code in (401, 403):
            raise Ship24Error("authentication_failed")
        if response.status_code == 429:
            raise Ship24Error("rate_limited")
        if response.status_code >= 400:
            try:
                errors = _object(response.json()).get("errors")
            except (ValueError, Ship24Error):
                errors = None
            if isinstance(errors, list) and any(
                isinstance(item, Mapping) and item.get("code") == "parcel_not_found"
                for item in errors
            ):
                raise Ship24Error("tracking_not_found")
            raise Ship24Error("provider_failed")
        try:
            return _parse(response.json())
        except ValueError:
            raise Ship24Error("provider_failed") from None

    @staticmethod
    def _post(token: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            return httpx.post(
                _URL, headers={"Authorization": f"Bearer {token}"},
                json=payload, timeout=75.0,
            )
        except httpx.RequestError:
            raise Ship24Error("connection_failed") from None
