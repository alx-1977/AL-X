"""Primitive capability definitions and executor factories."""

from alx.tools.mail import (
    ACKNOWLEDGE_MAIL_MESSAGE,
    DEFINITIONS,
    MOVE_MAIL_MESSAGE_TO_TRASH,
    READ_MAIL_MESSAGE,
    build_mail_executors,
)

__all__ = [
    "ACKNOWLEDGE_MAIL_MESSAGE",
    "DEFINITIONS",
    "MOVE_MAIL_MESSAGE_TO_TRASH",
    "READ_MAIL_MESSAGE",
    "build_mail_executors",
]
