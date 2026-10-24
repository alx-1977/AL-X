"""Bounded external diagnostics, with credential-shaped text masked."""

import re

# Anything shaped like a credential is masked before she sees the line, and
# ordinary text, paths included, is left alone.
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[^\s\"',]+")
_ASSIGNED = re.compile(
    r"(?i)\b(token|api[_-]?key|access[_-]?key|secret[_-]?key|secret|password|signature|"
    r"authorization)([\"']?\s*[=:]\s*[\"']?)(?!\[masked\])([^\s\"',&]+)")
_PREFIXED = re.compile(r"\b(sk|xi|ghp|gho|ghs|github_pat|xox[abp])[-_][A-Za-z0-9_\-]{8,}")
# A long unbroken run of letters and digits (no slash, so never a path).
_LONG = re.compile(r"(?<![/\w])[A-Za-z0-9+_\-]{40,}={0,2}(?![/\w])")


def mask_credentials(text: str) -> str:
    text = _BEARER.sub(lambda match: f"{match.group(1)} [masked]", text)
    text = _ASSIGNED.sub(lambda match: f"{match.group(1)}{match.group(2)}[masked]", text)
    text = _PREFIXED.sub("[masked]", text)
    return _LONG.sub("[masked]", text)


MAX_SERVICE_MESSAGE = 1000


def service_message(value: object) -> str:
    """Mask before bounding so a cut credential cannot escape recognition."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return mask_credentials(str(value))[:MAX_SERVICE_MESSAGE]


def failure_details(error: BaseException) -> dict[str, str]:
    """Only explicitly supplied service diagnostics travel, never arbitrary errors."""
    message = getattr(error, "service_message", "")
    return {"service_message": service_message(message)} if message else {}


def service_response_message(body: object) -> str:
    """Keep only a service's error/message fields, never its whole JSON response."""
    import json
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (ValueError, TypeError):
            return service_message(body)
    if isinstance(body, dict):
        for name in ("message", "Message", "detail", "error", "error_description", "error_message"):
            value = body.get(name)
            if isinstance(value, (str, dict)):
                return service_response_message(value)
    return ""
