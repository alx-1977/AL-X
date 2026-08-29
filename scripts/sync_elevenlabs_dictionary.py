"""Deploy the canonical AL/X vocabulary and print its immutable locator."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from alx.bootstrap.live_voice import load_environment  # noqa: E402
from alx.providers.elevenlabs_pronunciation import (  # noqa: E402
    ElevenLabsDictionaryManager,
    load_vocabulary,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--vocabulary",
        type=Path,
        default=ROOT / "config/pronunciation/alx-vocabulary.v1.json",
    )
    parser.add_argument("--dictionary-id")
    arguments = parser.parse_args()
    environment = load_environment(ROOT / ".env")
    api_key = environment.get("ALX_TTS_API_KEY") or environment.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise SystemExit("ElevenLabs API key is not configured")
    base_url = environment.get("ALX_TTS_BASE_URL", "https://api.elevenlabs.io")
    vocabulary = load_vocabulary(arguments.vocabulary)
    locator = ElevenLabsDictionaryManager(api_key, base_url).deploy(
        vocabulary,
        arguments.dictionary_id
        or environment.get("ALX_TTS_PRONUNCIATION_DICTIONARY_ID"),
    )
    print(json.dumps({
        "vocabulary_version": vocabulary.vocabulary_version,
        "pronunciation_dictionary_id": locator.dictionary_id,
        "pronunciation_dictionary_version_id": locator.version_id,
    }, indent=2))


if __name__ == "__main__":
    main()
