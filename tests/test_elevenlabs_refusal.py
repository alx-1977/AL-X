"""ElevenLabs' own reason for a refused request reaches the log (19 Nov 2026)."""

import unittest

from alx.providers.elevenlabs import _refusal


class RefusalReasonTests(unittest.TestCase):
    def test_detail_status_and_message(self) -> None:
        body = b'{"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}'
        self.assertEqual(_refusal(body), "quota_exceeded This request exceeds your quota.")

    def test_plain_detail_and_unreadable_bodies(self) -> None:
        self.assertEqual(_refusal(b'{"detail": "Invalid API key"}'), "Invalid API key")
        self.assertEqual(_refusal(b"Unauthorized"), "Unauthorized")
        self.assertEqual(_refusal(b""), "no reason given")


if __name__ == "__main__":
    unittest.main()
