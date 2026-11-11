"""Compose the structured DigiKey order read; it is offered through `xero` (D-074)."""

from collections.abc import Mapping

from alx.config.digikey import DigiKeySettings
from alx.providers.digikey import DigiKeyProvider


DIGIKEY_ORDER_READ_PERMISSION = "digikey.order.read"


def build_digikey_provider(environment: Mapping[str, str]) -> DigiKeyProvider | None:
    """The configured DigiKey order reader, or None when DigiKey is not configured."""
    settings = DigiKeySettings.from_environment(environment)
    if not settings.is_usable:
        return None
    return DigiKeyProvider(settings.client_id, settings.client_secret, settings.account_id)
