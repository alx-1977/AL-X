"""Tests address the one `mail` capability by action (D-074)."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import ApprovalScope, CapabilityCall  # noqa: E402
from alx.tools.mail import MAIL, MailAction  # noqa: E402

SEARCH = MailAction.SEARCH.value
READ = MailAction.READ.value
LIST_ATTACHMENTS = MailAction.LIST_ATTACHMENTS.value
READ_ATTACHMENT = MailAction.READ_ATTACHMENT.value
ACKNOWLEDGE = MailAction.ACKNOWLEDGE.value
MARK_SEEN = MailAction.MARK_SEEN.value
FLAG = MailAction.FLAG.value
FILE = MailAction.FILE.value
TRASH = MailAction.TRASH.value


def by_action(executors: Mapping[str, Callable]) -> dict[str, Callable[[Mapping[str, Any]], Any]]:
    """The one mail executor, addressed by action as each job once was by id."""
    return {
        item.value: (lambda arguments, value=item.value: executors[MAIL]({"action": value, **arguments}))
        for item in MailAction
    }


def mail_arguments(action: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"action": action, **(arguments or {})}


def mail_call(call_id: str, action: str, arguments: Mapping[str, Any] | None = None,
              approval_id: str | None = None) -> CapabilityCall:
    return CapabilityCall(call_id, MAIL, mail_arguments(action, arguments), approval_id)


def mail_scope(action: str, arguments: Mapping[str, Any]) -> ApprovalScope:
    return ApprovalScope(MAIL, mail_arguments(action, arguments))


def is_mail(call: Any, action: str) -> bool:
    """Whether a recorded call is this mail action."""
    return (call is not None and call.capability_id == MAIL
            and call.arguments.get("action") == action)
