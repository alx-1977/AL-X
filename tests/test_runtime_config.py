from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.bootstrap import build_runtime_providers  # noqa: E402
from alx.config import ConfigurationError, LiveVoiceSettings, RuntimeSettings  # noqa: E402
from alx.providers import (  # noqa: E402
    CartesiaTranscriber,
    ElevenLabsSynthesizer,
    XAIReasoningModel,
)


def environment(**changes: str) -> dict[str, str]:
    values = {
        "ALX_REASONING_PROVIDER": "xai",
        "ALX_REASONING_MODEL": "reasoning-model",
        "ALX_REASONING_API_KEY": "reasoning-secret",
        "ALX_STT_PROVIDER": "cartesia",
        "ALX_STT_MODEL": "stt-model",
        "ALX_STT_API_KEY": "stt-secret",
        "ALX_STT_API_VERSION": "stt-version",
        "ALX_STT_TURN_START_THRESHOLD": "0.7",
        "ALX_STT_TURN_EAGER_END_THRESHOLD": "0.5",
        "ALX_STT_TURN_END_THRESHOLD": "0.4",
        "ALX_STT_TURN_END_TIMEOUT_MS": "4500",
        "ALX_TTS_PROVIDER": "elevenlabs",
        "ALX_TTS_MODEL": "tts-model",
        "ALX_TTS_API_KEY": "tts-secret",
        "ALX_TTS_VOICE_ID": "voice-id",
    }
    values.update(changes)
    return values


class RuntimeConfigurationTests(unittest.TestCase):
    def test_live_voice_runtime_policy_is_configuration(self) -> None:
        settings = LiveVoiceSettings.from_environment(
            environment(
                ALX_INTERFACE_HOST="127.0.0.1",
                ALX_INTERFACE_PORT="8765",
                ALX_RUNTIME_STORAGE_ROOT=".alx/runtime",
                ALX_PRIMARY_PERSON_ID="friedl",
                ALX_GOAL_RETENTION_DAYS="3650",
                ALX_CORE_STEP_BUDGET="8",
            )
        )
        self.assertEqual(settings.port, 8765)
        self.assertEqual(settings.storage_root, Path(".alx/runtime"))
        self.assertEqual(settings.primary_person_id, "friedl")
        self.assertEqual(settings.core_step_budget, 8)

    def test_every_provider_and_model_is_configuration(self) -> None:
        settings = RuntimeSettings.from_environment(environment())
        self.assertEqual(settings.reasoning.provider, "xai")
        self.assertEqual(settings.reasoning.model, "reasoning-model")
        self.assertEqual(settings.speech_to_text.model, "stt-model")
        self.assertEqual(settings.speech_to_text.turn_start_threshold, 0.7)
        self.assertEqual(settings.speech_to_text.turn_end_timeout_ms, 4500)
        self.assertEqual(settings.text_to_speech.model, "tts-model")
        self.assertEqual(settings.text_to_speech.voice_id, "voice-id")

    def test_current_provider_key_names_are_compatible_but_generic_keys_win(self) -> None:
        values = environment()
        values.pop("ALX_REASONING_API_KEY")
        values["XAI_API_KEY"] = "current-key"
        settings = RuntimeSettings.from_environment(values)
        self.assertEqual(settings.reasoning.api_key, "current-key")
        values["ALX_REASONING_API_KEY"] = "generic-key"
        settings = RuntimeSettings.from_environment(values)
        self.assertEqual(settings.reasoning.api_key, "generic-key")

    def test_generic_endpoints_override_compatible_provider_names(self) -> None:
        values = environment(
            ALX_REASONING_BASE_URL="https://generic-model.example/",
            XAI_BASE_URL="https://provider-model.example",
            ALX_TTS_VOICE_ID="generic-voice",
            ELEVENLABS_VOICE_ID="provider-voice",
        )
        settings = RuntimeSettings.from_environment(values)
        self.assertEqual(settings.reasoning.base_url, "https://generic-model.example")
        self.assertEqual(settings.text_to_speech.voice_id, "generic-voice")

    def test_missing_selection_and_unknown_adapter_fail_closed(self) -> None:
        values = environment()
        values.pop("ALX_REASONING_MODEL")
        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_environment(values)
        settings = RuntimeSettings.from_environment(
            environment(ALX_REASONING_PROVIDER="not-installed")
        )
        with self.assertRaises(ConfigurationError):
            build_runtime_providers(settings)

    def test_invalid_turn_profile_fails_closed(self) -> None:
        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_environment(
                environment(ALX_STT_TURN_END_THRESHOLD="0.6")
            )
        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_environment(
                environment(ALX_STT_TURN_END_TIMEOUT_MS="12000")
            )

    def test_composition_root_returns_only_neutral_runtime_ports(self) -> None:
        providers = build_runtime_providers(RuntimeSettings.from_environment(environment()))
        self.assertIsInstance(providers.reasoning, XAIReasoningModel)
        self.assertIsInstance(providers.speech_to_text, CartesiaTranscriber)
        self.assertIsInstance(providers.text_to_speech, ElevenLabsSynthesizer)


if __name__ == "__main__":
    unittest.main()
