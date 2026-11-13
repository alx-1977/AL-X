"""A part's datasheet, fetched by part number (2026-11-13).

During the FireFli library audit AL/X could not download ten Würth datasheets:
Würth's site refuses automated downloads. Nexar (Octopart) keeps its own
copy of most datasheets and returns its link for a part number; DigiKey's
product service returns the manufacturer's link. DigiKey is asked first and
Nexar only when that gives no PDF: Nexar's allowance is small. Each link is fetched as a
plain request - never disguised as a browser - and kept only when it is a
real PDF. The file goes to her own storage, so no approval is needed.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Callable, Mapping

MAX_PDF_BYTES = 60_000_000
_SAFE = re.compile(r"[^A-Za-z0-9._+-]+")

NEXAR_TOKEN = "https://identity.nexar.com/connect/token"
NEXAR_API = "https://api.nexar.com/graphql"
DIGIKEY_TOKEN = "https://api.digikey.com/v1/oauth2/token"
DIGIKEY_DETAILS = "https://api.digikey.com/products/v4/search/{}/productdetails"
NEXAR_QUERY = ("query($q:String!){supSearchMpn(q:$q, limit:3){results{part{mpn manufacturer{name} "
               "bestDatasheet{url} documentCollections{name documents{name url}}}}}}")


class DatasheetError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def _plain(name: str) -> str:
    """A maker's name without accents or punctuation, lower case (Würth = Wurth)."""
    decomposed = unicodedata.normalize("NFKD", name)
    return re.sub(r"[^a-z0-9 ]+", "", "".join(c for c in decomposed if not unicodedata.combining(c)).lower())


def _same_maker(asked: str, found: str) -> bool:
    """Würth and Wurth Elektronik are one maker: compared on the first word, accents aside."""
    first = (_plain(asked).split() or [""])[0]
    return not first or first in _plain(found).split()


def _nexar_links(client: Any, credentials: list[tuple[str, str]], part: str,
                 manufacturer: str, tried: list) -> list[str]:
    for client_id, secret in credentials:
        try:
            token = client.post(NEXAR_TOKEN, data={"grant_type": "client_credentials", "client_id": client_id,
                                                   "client_secret": secret, "scope": "supply.domain"}).json()
            answer = client.post(NEXAR_API, json={"query": NEXAR_QUERY, "variables": {"q": part}},
                                 headers={"Authorization": "Bearer %s" % token.get("access_token", "")}).json()
        except Exception as error:  # noqa: BLE001 - one source failing is a fact, not the end
            tried.append({"source": "nexar", "url": "", "result": type(error).__name__})
            continue
        if answer.get("errors"):
            # A plan with no part allowance answers with an error: try the next key.
            tried.append({"source": "nexar", "url": "", "result": str(answer["errors"][0].get("message", ""))[:120]})
            continue
        links = []
        for result in ((answer.get("data") or {}).get("supSearchMpn") or {}).get("results") or []:
            found = result.get("part") or {}
            if str(found.get("mpn", "")).upper() != part.upper():
                continue
            maker = str((found.get("manufacturer") or {}).get("name", ""))
            if manufacturer and not _same_maker(manufacturer, maker):
                continue
            best = (found.get("bestDatasheet") or {}).get("url")
            links += [best] if best else []
            for collection in found.get("documentCollections") or []:
                links += [doc.get("url") for doc in collection.get("documents") or [] if doc.get("url")]
        return list(dict.fromkeys(link for link in links if link))
    return []


def _digikey_links(client: Any, credentials: tuple[str, str] | None, part: str, tried: list) -> list[str]:
    if not credentials:
        return []
    client_id, secret = credentials
    try:
        token = client.post(DIGIKEY_TOKEN, data={"grant_type": "client_credentials", "client_id": client_id,
                                                 "client_secret": secret}).json().get("access_token", "")
        response = client.get(DIGIKEY_DETAILS.format(part), headers={
            "Authorization": "Bearer %s" % token, "X-DIGIKEY-Client-Id": client_id})
    except Exception as error:  # noqa: BLE001
        tried.append({"source": "digikey", "url": "", "result": type(error).__name__})
        return []
    if response.status_code != 200:
        tried.append({"source": "digikey", "url": "", "result": "HTTP %d" % response.status_code})
        return []
    link = (response.json().get("Product") or {}).get("DatasheetUrl") or ""
    if link.startswith("//"):
        link = "https:" + link
    return [link] if link else []


def api_client():
    """The client for Nexar's and DigiKey's own APIs (fixed addresses, not arbitrary URLs)."""
    import httpx
    return httpx.Client(timeout=30, follow_redirects=False)


def fetch_datasheet(part: str, manufacturer: str, folder: Path, *, client: Any,
                    nexar: list[tuple[str, str]], digikey: tuple[str, str] | None,
                    download: Callable[[str], bytes]) -> dict[str, Any]:
    """The part's datasheet as a PDF in `folder`, from the first source that gives one."""
    part = (part or "").strip()
    if not part or len(part) > 80:
        raise DatasheetError("arguments_unusable", "part_number is the manufacturer part number")
    target = folder / (_SAFE.sub("_", part).strip("._") + ".pdf")
    if target.is_file() and target.read_bytes()[:5] == b"%PDF-":
        return {"path": str(target), "source": "already saved", "url": "", "tried": []}
    tried: list[dict[str, str]] = []
    # DigiKey first: free and unlimited. Nexar's allowance is small and its
    # paid plan is not an option (Friedl, 2026-11-13), so it is asked only
    # when DigiKey's link gives no PDF (Würth refuses automated downloads).
    sources = (("digikey", lambda: _digikey_links(client, digikey, part, tried)),
               ("nexar", lambda: _nexar_links(client, nexar, part, manufacturer, tried)))
    for source, find in sources:
        links = find()
        for link in links:
            # Datasheet links point anywhere on the web: fetched only through
            # the one guarded path (web_fetch), never directly.
            try:
                body = download(link)
            except Exception as error:  # noqa: BLE001 - each source failing is a fact
                tried.append({"source": source, "url": link,
                              "result": str(getattr(error, "detail", "") or getattr(error, "code", "")
                                            or type(error).__name__)[:80]})
                continue
            if not body.startswith(b"%PDF-") or len(body) > MAX_PDF_BYTES:
                tried.append({"source": source, "url": link, "result": "not a PDF"})
                continue
            folder.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".part")
            partial.write_bytes(body)
            partial.replace(target)
            return {"path": str(target), "source": source, "url": link, "tried": tried}
    raise DatasheetError("not_found", "no source gave a PDF for %s: %s" % (
        part, "; ".join("%s %s" % (item["source"], item["result"]) for item in tried)[:400]))


__all__ = ["DatasheetError", "api_client", "fetch_datasheet"]
