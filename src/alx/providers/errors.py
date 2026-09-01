"""Sanitised provider failures that never expose credentials or payloads.

A provider request carries private material: a mail body sent for reasoning,
AL/X's response sent for synthesis, Friedl's audio sent for transcription. The
underlying client library keeps the request on its exception, so chaining that
exception would retain the payload on the failure AL/X propagates.

`raise ... from None` alone is not enough. Python still records the original as
`__context__` when the raise happens inside an `except` block, so the payload
remains reachable. These failures therefore sever both links explicitly.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """A provider failure carrying a code, never the request that caused it."""

    def __init__(self, provider: str, reason: str) -> None:
        self.provider = provider
        self.reason = reason
        super().__init__(f"{provider} provider failure: {reason}")


def status_code_of(error: BaseException) -> int | None:
    """The HTTP status of a failed provider call, or None.

    A status code is a number the provider assigned to the outcome. It carries
    no part of the request, so it is safe to keep where the exception itself is
    not: 403 tells Friedl the credit ran out, where `HTTPStatusError` alone
    left him watching a silent runtime with no way to know why.

    The response body is deliberately not read. It is untrusted external text
    and may quote the request back.
    """
    response = getattr(error, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def raise_provider_failure(provider: str, reason: str) -> None:
    """Raise a provider failure carrying no reference to the request.

    This must be called after the `except` block has exited, not inside it.
    Inside a handler the interpreter attaches the active exception as
    `__context__` at raise time, which no constructor can prevent and which
    `raise ... from None` only marks as suppressed rather than removing. Once
    the handler has exited there is no active exception to attach, so the
    payload-bearing request cannot be reached from the failure at all.

    The usual shape is to record a sanitised code in the handler and raise
    afterwards:

        try:
            ...
        except SomeError as error:
            code = type(error).__name__
        else:
            return result
        raise_provider_failure("openai", code)
    """
    raise ProviderError(provider, reason)
