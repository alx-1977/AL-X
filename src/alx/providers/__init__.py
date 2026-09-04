"""Isolated provider adapters; no provider owns AL/X state or reasoning policy."""

from alx.providers.cartesia import CartesiaTranscriber
from alx.providers.elevenlabs import ElevenLabsSynthesizer
from alx.providers.openai import OpenAIReasoningModel
from alx.providers.xai import XAIReasoningModel
from alx.providers.icloud_mail import ICloudMailAdapter, SQLiteMailObservationState
from alx.providers.mail_poller import MailPoller
from alx.providers.web_fetch import HttpWebFetchProvider
from alx.providers.web_url import is_public_address, parse_public_url
from alx.providers.icloud_mail_send import ICloudMailSender
from alx.providers.xero import SQLiteXeroOAuth, XeroAccountingAdapter
from alx.providers.dhl import DhlImportAnalyzerAdapter

__all__ = [
    "HttpWebFetchProvider",
    "is_public_address",
    "parse_public_url",
    "CartesiaTranscriber",
    "ElevenLabsSynthesizer",
    "OpenAIReasoningModel",
    "XAIReasoningModel",
    "ICloudMailAdapter",
    "MailPoller",
    "ICloudMailSender",
    "SQLiteMailObservationState",
    "SQLiteXeroOAuth",
    "XeroAccountingAdapter",
    "DhlImportAnalyzerAdapter",
]
