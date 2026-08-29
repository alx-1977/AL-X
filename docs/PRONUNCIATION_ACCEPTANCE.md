# AL/X Pronunciation Acceptance

**Status:** Local implementation passed; remote dictionary deployment blocked by API-key scope
**Vocabulary:** `config/pronunciation/alx-vocabulary.v1.json` version 1.0.0
**Model under test:** Configured ElevenLabs `eleven_v3` voice

## Architecture evidence

- The canonical vocabulary is a versioned local JSON document.
- A management-only synchronizer creates or versions one persistent ElevenLabs dictionary.
- Runtime requests carry the authoritative response, the immutable dictionary/version locator, and no dictionary rule body.
- Written conversation storage and browser display are unchanged.
- Rand fallback rendering exists only inside the ElevenLabs provider boundary.
- No Core, conversation, frontend, goal, or future Email code owns pronunciation behavior.

## Native currency normalization

Audio was generated with the configured voice and `apply_text_normalization` forced to `on`, then transcribed with ElevenLabs Scribe v2 as an objective listening aid.

| Input | Observed transcript | Result |
| --- | --- | --- |
| `R2000` | `R 2000` | Fail: rand was not spoken naturally |
| `R2,000` | `2,000 rand` | Pass |
| `R2 000.50` | `Two thousand rand and fifty cents` | Pass |

Only the failed compact form is deterministically rendered before synthesis. The original response remains unchanged.

## Pending evidence

The configured ElevenLabs API key currently lacks `pronunciation_dictionaries_read` and dictionary-write access. Until that scope is enabled, AL/X cannot create the persistent remote dictionary or run the actual engineering/acronym acceptance audio with its locator attached.

After the key is updated:

1. Run `PYTHONPATH=src python3 scripts/sync_elevenlabs_dictionary.py`.
2. Store the returned dictionary ID and version ID in the two configured `.env` keys.
3. Run the actual audio fixture against the selected voice.
4. Record every observed result here before the branch is accepted.
