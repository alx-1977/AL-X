"""ElevenLabs-only pronunciation deployment and spoken-text rendering."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any

import httpx
from num2words import num2words

from alx.providers.errors import ProviderError


_RAND_AMOUNT = re.compile(
    r"(?<![A-Za-z0-9])R(?P<amount>\d+(?:\.\d{1,2})?)(?![A-Za-z0-9])"
    r"(?!,\d{3}(?:\D|$))(?! \d{3}(?:\D|$))(?!\.\d)"
)


@dataclass(frozen=True, slots=True)
class AliasRule:
    written: str
    spoken: str
    category: str
    case_sensitive: bool
    word_boundaries: bool

    def as_elevenlabs_rule(self) -> dict[str, Any]:
        return {
            "type": "alias",
            "string_to_replace": self.written,
            "alias": self.spoken,
            "case_sensitive": self.case_sensitive,
            "word_boundaries": self.word_boundaries,
        }


@dataclass(frozen=True, slots=True)
class PronunciationVocabulary:
    schema_version: int
    vocabulary_version: str
    dictionary_name: str
    description: str
    rules: tuple[AliasRule, ...]


@dataclass(frozen=True, slots=True)
class DictionaryLocator:
    dictionary_id: str
    version_id: str

    def __post_init__(self) -> None:
        if not self.dictionary_id.strip() or not self.version_id.strip():
            raise ValueError("pronunciation dictionary IDs must not be blank")

    def as_request_value(self) -> dict[str, str]:
        return {"pronunciation_dictionary_id": self.dictionary_id, "version_id": self.version_id}


def load_vocabulary(path: str | Path) -> PronunciationVocabulary:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValueError("unsupported pronunciation vocabulary schema")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("pronunciation vocabulary requires rules")
    rules = tuple(
        AliasRule(
            written=item["written"],
            spoken=item["spoken"],
            category=item["category"],
            case_sensitive=item["case_sensitive"],
            word_boundaries=item["word_boundaries"],
        )
        for item in raw_rules
    )
    written = [rule.written for rule in rules]
    if any(not value.strip() for value in written) or len(set(written)) != len(written):
        raise ValueError("pronunciation vocabulary rules must be non-blank and unique")
    for name in ("vocabulary_version", "dictionary_name", "description"):
        if not isinstance(data.get(name), str) or not data[name].strip():
            raise ValueError(f"pronunciation vocabulary requires {name}")
    return PronunciationVocabulary(
        1,
        data["vocabulary_version"],
        data["dictionary_name"],
        data["description"],
        rules,
    )


def render_spoken_text(text: str) -> str:
    """Render only provider-bound speech; the authoritative response is unchanged."""

    def replace(match: re.Match[str]) -> str:
        raw = match.group("amount").replace(",", "").replace(" ", "")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return match.group(0)
        rand = int(amount)
        cents = int((amount - rand) * 100)
        spoken = f"{num2words(rand, lang='en')} rand"
        if cents:
            unit = "cent" if cents == 1 else "cents"
            spoken += f" and {num2words(cents, lang='en')} {unit}"
        return spoken

    return _RAND_AMOUNT.sub(replace, text)


class ElevenLabsDictionaryManager:
    """Deploy a local vocabulary as a persistent, versioned ElevenLabs dictionary."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30)

    def deploy(
        self,
        vocabulary: PronunciationVocabulary,
        dictionary_id: str | None = None,
    ) -> DictionaryLocator:
        rules = [rule.as_elevenlabs_rule() for rule in vocabulary.rules]
        if dictionary_id:
            endpoint = f"{self._base_url}/v1/pronunciation-dictionaries/{dictionary_id}/set-rules"
            body: dict[str, Any] = {"rules": rules}
        else:
            endpoint = f"{self._base_url}/v1/pronunciation-dictionaries/add-from-rules"
            body = {
                "name": vocabulary.dictionary_name,
                "description": f"{vocabulary.description} Version {vocabulary.vocabulary_version}.",
                "rules": rules,
            }
        try:
            response = self._client.post(
                endpoint,
                headers={"xi-api-key": self._api_key, "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError("elevenlabs", type(error).__name__) from error
        deployed_dictionary_id = dictionary_id or result.get("id")
        version_id = result.get("version_id")
        if not isinstance(deployed_dictionary_id, str) or not isinstance(version_id, str):
            raise ProviderError("elevenlabs", "invalid_dictionary_response")
        return DictionaryLocator(deployed_dictionary_id, version_id)
