"""Sanitised provider failures that never expose credentials."""


class ProviderError(RuntimeError):
    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider} provider failure: {reason}")
