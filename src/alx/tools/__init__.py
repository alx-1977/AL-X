"""Primitive capability definitions and executor factories."""

from alx.tools.mail import (
    DEFINITIONS,
    MAIL,
    MailAction,
    SEND_DEFINITIONS,
    SEND_MAIL_REPLY,
    build_mail_executors,
    build_send_executors,
)
from alx.tools.xero import (
    XERO,
    XeroAction,
    build_xero_executors,
    offered_xero_actions,
    xero_definition,
)
from alx.tools.continuity import (
    DEFINITIONS as CONTINUITY_DEFINITIONS,
    MINIMUM_HORIZON_SECONDS,
    OPEN_THOUGHT_LIMIT,
    PENDING_REVISIT_LIMIT,
    REQUEST_FUTURE_COGNITION,
    SET_OWN_TIME,
    SET_PRESENCE,
    WITHDRAW_FUTURE_COGNITION,
    build_continuity_executors,
)
from alx.tools.research import (
    build_research_executors,
    offered_research_actions,
    research_definition,
)
from alx.tools.dhl import build_dhl_executors

from alx.tools.code import (
    build_code_executors,
    code_definition,
)


__all__ = [
    "build_code_executors",
    "code_definition",
    "CONTINUITY_DEFINITIONS",
    "MINIMUM_HORIZON_SECONDS",
    "OPEN_THOUGHT_LIMIT",
    "PENDING_REVISIT_LIMIT",
    "REQUEST_FUTURE_COGNITION",
    "SET_OWN_TIME",
    "SET_PRESENCE",
    "WITHDRAW_FUTURE_COGNITION",
    "build_continuity_executors",
    "DEFINITIONS",
    "MAIL",
    "MailAction",
    "SEND_DEFINITIONS",
    "SEND_MAIL_REPLY",
    "build_mail_executors",
    "build_send_executors",
    "XERO",
    "XeroAction",
    "build_xero_executors",
    "offered_xero_actions",
    "xero_definition",
    "build_dhl_executors",
    "build_research_executors",
    "offered_research_actions",
    "research_definition",
]
