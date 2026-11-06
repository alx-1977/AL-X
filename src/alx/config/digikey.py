"""Production DigiKey credentials; loaded by the runtime's environment loader."""

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class DigiKeySettings:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)
    account_id: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "DigiKeySettings":
        return cls(*(environment.get(key, "").strip() for key in (
            "DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "DIGIKEY_ACCOUNT_ID"
        )))

    @property
    def is_usable(self) -> bool:
        return bool(self.client_id and self.client_secret and self.account_id
                    and self.account_id.isascii() and self.account_id.isdigit()
                    and int(self.account_id) > 0)
