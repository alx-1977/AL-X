"""Transmit one prepared reply through SMTP.

This adapter transmits. It never composes, never chooses recipients, never
retries on its own, and never selects the sender identity: the sender is fixed
by configuration and injected here.

A retry after an ambiguous outcome is how duplicate mail is sent, so a failure
is reported rather than repeated.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.policy import EmailPolicy, default as default_policy
from email.utils import formatdate, make_msgid
from typing import Any

import certifi

from alx.contracts import MailSendError, OutboundReply, ReplyOutcome

# The SMTP submission port that requires STARTTLS, per RFC 6409.
_STARTTLS_PORT = 587

# RFC 5322 identifier headers. Their values are msg-id tokens and must reach the
# wire literally: a MIME-encoded identifier is opaque to other mail clients and
# silently breaks threading.
_IDENTIFIER_HEADERS = frozenset({"message-id", "in-reply-to", "references"})


class _IdentifierPreservingPolicy(EmailPolicy):
    """Fold ordinary headers normally, but never encode an identifier header."""

    def fold(self, name: str, value: Any) -> str:
        if name.lower() in _IDENTIFIER_HEADERS:
            return f"{name}: {value}\r\n"
        return super().fold(name, value)

    def fold_binary(self, name: str, value: Any) -> bytes:
        if name.lower() in _IDENTIFIER_HEADERS:
            return self.fold(name, value).encode("ascii", "surrogateescape")
        return super().fold_binary(name, value)


MESSAGE_POLICY = _IdentifierPreservingPolicy(
    utf8=default_policy.utf8, refold_source=default_policy.refold_source
)


def _sanitised(error: BaseException) -> str:
    """Return a failure label that cannot carry a credential or body."""
    return type(error).__name__


class ICloudMailSender:
    """Send one reply from the configured identity."""

    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        secret: str,
        timeout_seconds: int = 60,
        connection_factory: Callable[..., Any] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        message_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._address = address
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._connection_factory = connection_factory
        self._ssl_context = ssl_context
        self._message_id_factory = message_id_factory or make_msgid

    @property
    def address(self) -> str:
        return self._address

    def build_message(self, reply: OutboundReply, message_id: str) -> EmailMessage:
        """Assemble the MIME document. The sender is configuration, not input."""
        document = EmailMessage(policy=MESSAGE_POLICY)
        document["From"] = self._address
        document["To"] = ", ".join(reply.to)
        if reply.carbon_copy:
            document["Cc"] = ", ".join(reply.carbon_copy)
        document["Subject"] = reply.subject
        document["Date"] = formatdate(localtime=True)
        document["Message-ID"] = message_id
        if reply.in_reply_to:
            document["In-Reply-To"] = reply.in_reply_to
        if reply.references:
            document["References"] = " ".join(reply.references)
        document.set_content(reply.body)
        return document

    def _open(self) -> Any:
        context = self._ssl_context or ssl.create_default_context(cafile=certifi.where())
        try:
            if self._connection_factory is not None:
                connection = self._connection_factory(
                    self._host, self._port, timeout=self._timeout_seconds
                )
            elif self._port == _STARTTLS_PORT:
                connection = smtplib.SMTP(
                    self._host, self._port, timeout=self._timeout_seconds
                )
                connection.starttls(context=context)
            else:
                connection = smtplib.SMTP_SSL(
                    self._host, self._port,
                    timeout=self._timeout_seconds, context=context,
                )
        except Exception as error:
            raise MailSendError("connection_failed") from None
        try:
            connection.login(self._address, self._secret)
        except Exception:
            self._close(connection)
            raise MailSendError("authentication_failed") from None
        return connection

    @staticmethod
    def _close(connection: Any) -> None:
        try:
            connection.quit()
        except Exception:
            return

    def send_reply(self, reply: OutboundReply) -> ReplyOutcome:
        message_id = self._message_id_factory()
        document = self.build_message(reply, message_id)
        addresses = [*reply.to, *reply.carbon_copy]
        connection = self._open()
        try:
            try:
                refused = connection.send_message(
                    document, from_addr=self._address, to_addrs=addresses
                )
            except smtplib.SMTPRecipientsRefused:
                raise MailSendError("recipients_refused") from None
            except smtplib.SMTPResponseException:
                raise MailSendError("send_rejected") from None
            except Exception as error:
                # The server may or may not have accepted the message. It is
                # never retried here; the Core resolves an ambiguous outcome by
                # looking for the transmitted identifier.
                raise MailSendError("send_outcome_unknown") from None
        finally:
            self._close(connection)
        refused_addresses = tuple(sorted(refused or {}))
        return ReplyOutcome(
            transmitted_message_id=message_id,
            accepted=True,
            recipients_accepted=tuple(
                item for item in addresses if item not in refused_addresses
            ),
            recipients_refused=refused_addresses,
        )
