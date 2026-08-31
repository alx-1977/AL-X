"""OAuth and Accounting API adapter for one configured Xero organisation."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken

from alx.contracts import XeroAccessError


AUTHORIZE_URL = "https://login.xero.com/identity/connect/authorize"
TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"
ACCOUNTING_URL = "https://api.xero.com/api.xro/2.0"

# Xero retains discarded invoices rather than removing them. A discarded bill
# is not an existing bill, so it must not block re-creating the same supplier
# invoice number.
_DISCARDED_STATUSES = frozenset({"DELETED", "VOIDED"})

# External protocol identifiers. D-016 deliberately excludes contact writes,
# payments, bank transactions, journals, reports, payroll, and sales work.
XERO_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "accounting.invoices",
    "accounting.contacts.read",
    "accounting.settings.read",
    "accounting.attachments",
)


def _raise_clean(code: str) -> None:
    """Raise after handlers exit so request-bearing exceptions aren't chained."""
    raise XeroAccessError(code)


@dataclass(frozen=True, slots=True)
class XeroConnection:
    access_token: str
    tenant_id: str


class SQLiteXeroOAuth:
    """Restart-safe OAuth state and atomically rotated encrypted tokens."""

    def __init__(
        self,
        path: Path,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        tenant_id: str = "",
        timeout_seconds: int = 60,
    ) -> None:
        self._path = path
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._tenant_id = tenant_id
        self._timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._token_key(path.with_suffix(".key")))
        database = self._db()
        try:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS xero_oauth_state (
                    state TEXT PRIMARY KEY,
                    verifier TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS xero_token (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    tenant_id TEXT NOT NULL,
                    tenant_name TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    scopes TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            database.commit()
        finally:
            database.close()

    @staticmethod
    def _token_key(path: Path) -> bytes:
        """Keep token encryption independent from OAuth-secret rotation."""
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            _raise_clean("token_key_invalid")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            pass
        except OSError:
            _raise_clean("token_key_invalid")
        else:
            try:
                os.write(descriptor, Fernet.generate_key())
            finally:
                os.close(descriptor)
        read_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(path, read_flags)
            current = os.fstat(descriptor)
            named = path.lstat()
            if (
                not stat.S_ISREG(current.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or (current.st_dev, current.st_ino) != (named.st_dev, named.st_ino)
            ):
                _raise_clean("token_key_invalid")
            os.fchmod(descriptor, 0o600)
            key = os.read(descriptor, 4096).strip()
        except XeroAccessError:
            raise
        except OSError:
            _raise_clean("token_key_invalid")
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        try:
            Fernet(key)
        except (TypeError, ValueError):
            _raise_clean("token_key_invalid")
        return key

    def _db(self) -> sqlite3.Connection:
        database = sqlite3.connect(str(self._path), timeout=self._timeout_seconds)
        database.row_factory = sqlite3.Row
        return database

    def _encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def _decrypt(self, value: str) -> str:
        failure = ""
        decrypted = b""
        try:
            decrypted = self._fernet.decrypt(value.encode())
        except InvalidToken:
            failure = "token_unreadable"
        if failure:
            _raise_clean(failure)
        return decrypted.decode()

    def begin_authorization(self, now: float | None = None) -> str:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        timestamp = time.time() if now is None else now
        database = self._db()
        try:
            database.execute(
                "DELETE FROM xero_oauth_state WHERE expires_at <= ?", (timestamp,)
            )
            database.execute(
                "INSERT INTO xero_oauth_state(state, verifier, expires_at) VALUES (?, ?, ?)",
                (state, verifier, timestamp + 600),
            )
            database.commit()
        finally:
            database.close()
        parameters = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "scope": " ".join(XERO_SCOPES),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(parameters)}"

    def exchange_code(self, code: str, state: str, now: float | None = None) -> str:
        timestamp = time.time() if now is None else now
        database = self._db()
        try:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT verifier, expires_at FROM xero_oauth_state WHERE state = ?",
                (state,),
            ).fetchone()
            database.execute("DELETE FROM xero_oauth_state WHERE state = ?", (state,))
            database.commit()
        finally:
            database.close()
        if row is None or row["expires_at"] <= timestamp:
            _raise_clean("oauth_state_invalid")
        token_data = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": row["verifier"],
            }
        )
        tenants = self._connections(str(token_data["access_token"]))
        tenant: Mapping[str, Any] | None
        if self._tenant_id:
            tenant = next(
                (item for item in tenants if item.get("tenantId") == self._tenant_id),
                None,
            )
            if tenant is None:
                _raise_clean("configured_tenant_unavailable")
        elif len(tenants) == 1:
            tenant = tenants[0]
        else:
            _raise_clean("tenant_selection_required")
        self._persist(token_data, tenant, timestamp)
        return str(tenant.get("tenantName") or tenant["tenantId"])

    def connection(self, now: float | None = None) -> XeroConnection:
        timestamp = time.time() if now is None else now
        with self._lock:
            database = self._db()
            try:
                database.execute("BEGIN IMMEDIATE")
                row = database.execute(
                    "SELECT * FROM xero_token WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    database.rollback()
                    _raise_clean("not_connected")
                if row["expires_at"] > timestamp + 60:
                    access = self._decrypt(row["access_token"])
                    tenant_id = row["tenant_id"]
                    database.commit()
                    return XeroConnection(access, tenant_id)
                refresh = self._decrypt(row["refresh_token"])
                token_data = self._token_request(
                    {"grant_type": "refresh_token", "refresh_token": refresh}
                )
                self._persist(token_data, dict(row), timestamp, database)
                database.commit()
                return XeroConnection(str(token_data["access_token"]), row["tenant_id"])
            finally:
                database.close()

    def _token_request(self, data: Mapping[str, str]) -> Mapping[str, Any]:
        failure = ""
        response = None
        try:
            response = httpx.post(
                TOKEN_URL,
                data=dict(data),
                auth=(self._client_id, self._client_secret),
                timeout=self._timeout_seconds,
            )
        except Exception:
            failure = "connection_failed"
        if failure:
            _raise_clean(failure)
        if response is None or response.status_code >= 400:
            _raise_clean("oauth_rejected")
        body: Any = None
        try:
            body = response.json()
        except Exception:
            failure = "oauth_response_invalid"
        if failure:
            _raise_clean(failure)
        if not isinstance(body, Mapping) or not all(
            body.get(name) for name in ("access_token", "refresh_token", "expires_in")
        ):
            _raise_clean("oauth_response_invalid")
        return body

    def _connections(self, access_token: str) -> list[Mapping[str, Any]]:
        failure = ""
        response = None
        try:
            response = httpx.get(
                CONNECTIONS_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=self._timeout_seconds,
            )
        except Exception:
            failure = "connection_failed"
        if failure:
            _raise_clean(failure)
        if response is None or response.status_code >= 400:
            _raise_clean("tenant_lookup_failed")
        values: Any = None
        try:
            values = response.json()
        except Exception:
            failure = "tenant_response_invalid"
        if failure:
            _raise_clean(failure)
        if not isinstance(values, list) or not values:
            _raise_clean("tenant_unavailable")
        return [item for item in values if isinstance(item, Mapping)]

    def _persist(
        self,
        token_data: Mapping[str, Any],
        tenant: Mapping[str, Any],
        now: float,
        database: sqlite3.Connection | None = None,
    ) -> None:
        owns_database = database is None
        target = database or self._db()
        try:
            target.execute(
                "INSERT INTO xero_token(singleton, tenant_id, tenant_name, access_token, refresh_token, expires_at, scopes, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET tenant_id=excluded.tenant_id, tenant_name=excluded.tenant_name, access_token=excluded.access_token, refresh_token=excluded.refresh_token, expires_at=excluded.expires_at, scopes=excluded.scopes, updated_at=excluded.updated_at",
                (
                    str(tenant.get("tenantId") or tenant.get("tenant_id") or ""),
                    str(tenant.get("tenantName") or tenant.get("tenant_name") or ""),
                    self._encrypt(str(token_data["access_token"])),
                    self._encrypt(str(token_data["refresh_token"])),
                    now + int(token_data["expires_in"]),
                    str(token_data.get("scope") or ""),
                    now,
                ),
            )
            if owns_database:
                target.commit()
        finally:
            if owns_database:
                target.close()


class XeroAccountingAdapter:
    def __init__(self, oauth: SQLiteXeroOAuth, timeout_seconds: int = 60) -> None:
        self._oauth = oauth
        self._timeout_seconds = timeout_seconds

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        media_type: str = "application/json",
        allow_not_found: bool = False,
        binary: bool = False,
    ) -> Any:
        connection = self._oauth.connection()
        failure = ""
        response = None
        try:
            headers = {
                "Authorization": f"Bearer {connection.access_token}",
                "Xero-tenant-id": connection.tenant_id,
                "Accept": "application/octet-stream" if binary else "application/json",
                "Content-Type": media_type,
            }
            if binary:
                headers["contentType"] = media_type
            response = httpx.request(
                method,
                f"{ACCOUNTING_URL}{path}",
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                content=content,
                timeout=self._timeout_seconds,
            )
        except Exception:
            failure = "connection_failed"
        if failure:
            _raise_clean(failure)
        if response is None:
            _raise_clean("response_missing")
        if allow_not_found and response.status_code == 404:
            return None
        if response.status_code in (401, 403):
            _raise_clean("permission_denied")
        if response.status_code == 429:
            _raise_clean("rate_limited")
        if response.status_code >= 400:
            _raise_clean("request_rejected")
        if binary:
            return bytes(response.content)
        body: Any = None
        try:
            body = response.json()
        except Exception:
            failure = "response_invalid"
        if failure:
            _raise_clean(failure)
        return body

    @staticmethod
    def _items(body: Any, key: str) -> tuple[Mapping[str, Any], ...]:
        values = body.get(key, ()) if isinstance(body, Mapping) else ()
        return tuple(item for item in values if isinstance(item, Mapping))

    def search_contacts(self, search_term: str) -> tuple[Mapping[str, Any], ...]:
        body = self._request("GET", f"/Contacts?SearchTerm={quote(search_term)}")
        return self._items(body, "Contacts")

    def list_accounts(self) -> tuple[Mapping[str, Any], ...]:
        return self._items(self._request("GET", "/Accounts"), "Accounts")

    def list_tax_rates(self) -> tuple[Mapping[str, Any], ...]:
        return self._items(self._request("GET", "/TaxRates"), "TaxRates")

    def find_bill(
        self, invoice_number: str, contact_id: str = ""
    ) -> Mapping[str, Any] | None:
        parameters: dict[str, str] = {"InvoiceNumbers": invoice_number}
        if contact_id:
            parameters["ContactIDs"] = contact_id
        body = self._request("GET", f"/Invoices?{urlencode(parameters)}")
        items = self._items(body, "Invoices")
        for item in items:
            if (
                item.get("Type") != "ACCPAY"
                or str(item.get("InvoiceNumber") or "") != invoice_number
                or str(item.get("Status") or "") in _DISCARDED_STATUSES
            ):
                continue
            contact = item.get("Contact")
            candidate_contact = (
                str(contact.get("ContactID") or "")
                if isinstance(contact, Mapping) else ""
            )
            if not contact_id or candidate_contact == contact_id:
                return item
        return None

    def read_bill(self, invoice_id: str) -> Mapping[str, Any] | None:
        body = self._request(
            "GET", f"/Invoices/{quote(invoice_id, safe='')}", allow_not_found=True
        )
        items = self._items(body, "Invoices") if body is not None else ()
        return (
            items[0]
            if items
            and items[0].get("Type") == "ACCPAY"
            and str(items[0].get("InvoiceID") or "") == invoice_id
            else None
        )

    def create_draft_bill(self, bill: Mapping[str, Any]) -> Mapping[str, Any]:
        body = self._request(
            "POST", "/Invoices", json_body={"Invoices": [dict(bill)]}
        )
        items = self._items(body, "Invoices")
        if not items:
            _raise_clean("response_invalid")
        return items[0]

    def update_draft_bill(
        self, invoice_id: str, bill: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        body = self._request(
            "POST",
            f"/Invoices/{quote(invoice_id, safe='')}",
            json_body={"Invoices": [{**dict(bill), "InvoiceID": invoice_id}]},
        )
        items = self._items(body, "Invoices")
        if not items:
            _raise_clean("response_invalid")
        return items[0]

    def attach_bill_document(
        self, invoice_id: str, filename: str, media_type: str, content: bytes
    ) -> Mapping[str, Any]:
        body = self._request(
            "PUT",
            f"/Invoices/{quote(invoice_id, safe='')}/Attachments/{quote(filename, safe='')}",
            content=content,
            media_type=media_type,
        )
        items = self._items(body, "Attachments")
        return items[0] if items else {"FileName": filename}

    def list_bill_attachments(
        self, invoice_id: str
    ) -> tuple[Mapping[str, Any], ...]:
        body = self._request(
            "GET", f"/Invoices/{quote(invoice_id, safe='')}/Attachments"
        )
        return self._items(body, "Attachments")

    def read_bill_attachment(
        self, invoice_id: str, attachment_id: str, media_type: str
    ) -> bytes:
        return self._request(
            "GET",
            f"/Invoices/{quote(invoice_id, safe='')}/Attachments/{quote(attachment_id, safe='')}",
            media_type=media_type,
            binary=True,
        )

    def authorise_bill(self, invoice_id: str) -> Mapping[str, Any]:
        body = self._request(
            "POST",
            f"/Invoices/{quote(invoice_id, safe='')}",
            json_body={
                "Invoices": [{"InvoiceID": invoice_id, "Status": "AUTHORISED"}]
            },
        )
        items = self._items(body, "Invoices")
        if not items:
            _raise_clean("response_invalid")
        return items[0]
