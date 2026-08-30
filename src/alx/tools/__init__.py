"""Primitive capability definitions and executor factories."""

from alx.tools.mail import (
    ACKNOWLEDGE_MAIL_MESSAGE,
    DEFINITIONS,
    MARK_MAIL_MESSAGE_SEEN,
    MOVE_MAIL_MESSAGE_TO_TRASH,
    SEND_DEFINITIONS,
    SEND_MAIL_REPLY,
    READ_MAIL_MESSAGE,
    build_mail_executors,
    build_send_executors,
)

__all__ = [
    "ACKNOWLEDGE_MAIL_MESSAGE",
    "DEFINITIONS",
    "MARK_MAIL_MESSAGE_SEEN",
    "MOVE_MAIL_MESSAGE_TO_TRASH",
    "SEND_DEFINITIONS",
    "SEND_MAIL_REPLY",
    "READ_MAIL_MESSAGE",
    "build_mail_executors",
    "build_send_executors",
]
