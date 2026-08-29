# AL/X Pronunciation Acceptance

**Status:** Accepted against the configured ElevenLabs model and voice
**Vocabulary:** `config/pronunciation/alx-vocabulary.v1.json` version 1.0.0
**Model under test:** Configured ElevenLabs `eleven_v3` voice
**Remote locator:** dictionary `ZlCeLofc0hxmUesbz79D`, version `PcyGfkIXIQTHSnPlqftx`

## Architecture evidence

- The canonical vocabulary is a versioned local JSON document.
- A management-only synchronizer creates or versions one persistent ElevenLabs dictionary.
- Runtime requests carry the authoritative response, the immutable dictionary/version locator, and no dictionary rule body.
- Written conversation storage and browser display are unchanged.
- The ElevenLabs adapter does not interpret ambiguous compact `R<number>` forms.
- No Core, conversation, frontend, goal, or future Email code owns pronunciation behavior.

## Native currency normalization

Audio was generated with the configured voice and `apply_text_normalization` forced to `on`, then transcribed with ElevenLabs Scribe v2 as an objective listening aid.

| Input | Observed transcript | Result |
| --- | --- | --- |
| `R2000` | `R 2000` | Fail: rand was not spoken naturally |
| `R2,000` | `2,000 rand` | Pass |
| `R2 000.50` | `Two thousand rand and fifty cents` | Pass |

The failed compact form is deliberately left unchanged because `R<number>` is ambiguous in engineering conversation. The verified unambiguous formats continue through native normalization.

## Persistent dictionary verification

- ElevenLabs returned 33 rules for the deployed dictionary.
- The local canonical vocabulary contains the same 33 rules.
- The latest remote version is the exact version referenced by `.env` and the deployment manifest.
- Runtime synthesis sends only this locator, never the full rule set.

## Actual pronunciation fixture

Each fixture entry was synthesized using the active dictionary and configured `eleven_v3` voice, then transcribed with ElevenLabs Scribe v2 as an objective listening aid. Scribe commonly formats spoken number words back into digits and spoken initialisms back into acronym text; those formatting differences do not alter the observed pronunciation.

| Category | Written input | Observed transcript | Result |
| --- | --- | --- | --- |
| Resistance | `10 Ω, 4.7 kΩ, and 2 MΩ` | `10 ohms, 4.7 kiloohms, and 2 megaohms` | Pass |
| Voltage | `3.3 V and 500 mV` | `3.3 volts and 500 millivolts` | Pass |
| Current | `2 A, 250 mA, and 10 µA` | `2 amps, 250 milliamps, and 10 microamps` | Pass |
| Power | `5 W and 500 mW` | `Five watts and 500 milliwatts` | Pass |
| Frequency | `2 Hz, 20 kHz, 100 MHz, and 2.4 GHz` | `Two hertz, 20 kilohertz, 100 megahertz, and 2.4 gigahertz` | Pass |
| Capacitance | `2 F, 10 µF, 100 nF, and 22 pF` | `Two farads, 10 microfarads, 100 nanofarads, and 22 picofarads` | Pass |
| Temperature | `85 °C` | `85 degrees Celsius` | Pass |
| Component values | `Fit a 10 kΩ resistor and a 100 nF capacitor.` | `Fit a 10 kiloohms resistor and a 100 nanofarads capacitor` | Pass |
| Compact R-number | `R2000` | `R 2000` | Passed through unchanged; not interpreted as currency |
| Comma rand | `R2,000` | `2,000 rand` | Pass through native normalization |
| Rand and cents | `R2 000.50` | `Two thousand rand and fifty cents` | Pass through native normalization |
| Names and acronyms | `AL/X reviews an Altium PCB BOM for JLCPCB, MPS, DHL, and Xero.` | `Alex reviews an Altium PCB BOM for JLCPCB, MPS, DHL, and Zero.` | Pass; initialisms normalized in transcript |

Adapter regression tests also prove that resistor references `R5` and `R10` pass through unchanged. Compact currency without separators is intentionally unsupported until a contextual design is approved. New terminology is added locally, deployed as a new remote version, and re-evaluated before its locator becomes active.
