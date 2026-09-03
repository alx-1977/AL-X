# Proposal — the Diagnostic Console becomes typable

**Status:** DESIGN ONLY. Not implemented.
**Scope:** typed input to AL/X, in the console that already exists. No serial, no
build output, no command grammar.

## What is already there

| Question | Answer |
| --- | --- |
| Frontend component | `assets/index.html` — one `<aside class="diagnostics">` with a header, a stage line, `#diagnostic-log`, and a Clear button |
| How telemetry reaches it | `app.js::diagnostic(message, tone)` appends a `.diagnostic-line` (timestamp + text), caps the log at 80 lines, auto-scrolls |
| Event mechanism | One websocket at `/voice`. `socket.onmessage` dispatches on `message.type`: `session.ready`, `diagnostic`, `audio.end`, `phase` |
| Where voice text enters the backend | `live_voice.py:213` — a `ConversationTurn` with `origin=SPEECH_TRANSCRIPT` is built and handed to `gateway.receive_conversation_turn` |
| Where `conversation_id` is established | `server.py::_conversation_id` from the query string, else a fresh UUID. The browser already stores it: `localStorage["alx.conversation_id"]` |
| Where AL/X's final text exists before TTS | `live_voice.py:273` — `outcome.response`, passed to `_speak()` |
| Can TTS be disabled? | Not today. `providers.py:131` requires the ElevenLabs adapter, so a runtime without it refuses to start |
| Existing stream abstraction? | Partly. Every server→browser message already carries a `type`, and diagnostics additionally carry a `code`. That is a usable seam, but there is no notion of an output *stream* yet |

**Three findings make this small:**

1. **`ConversationOrigin.TYPED` already exists and is never used.** The contract
   reserved this seam and nothing consumes it.
2. **The websocket already receives frames from the browser.** `server.py:281`
   reads them and skips anything that is not `bytes`, so a text frame is
   currently discarded rather than rejected.
3. **The conversation id already round-trips to the browser** and is persisted
   in `localStorage`, so console input has one to reuse without inventing
   anything.

## 1. Smallest implementation

**Backend.** Where the audio loop discards non-bytes, accept a JSON text frame
of one shape and build the same `ConversationTurn` the transcript path builds,
with `origin=TYPED`. It joins the queue as another `("transcription", ...)`-class
item so it reaches the identical Core call.

**Frontend.** An input line inside the existing `<aside>`, below the log. Enter
submits and sends `{"type": "person.text", "content": "..."}`. Render
`You > …` through the existing `diagnostic()` renderer with a new tone, and
`ALX > …` when the response arrives.

Nothing else changes.

## 2. Frontend → backend → Gateway → Core

```
keyboard ──► ws text frame ──┐
                             ├──► ConversationTurn(origin=…) ──► ConversationGateway
microphone ──► STT ──────────┘                                        └──► CoreAgent
```

The two origins differ only in the `origin` field, which is provenance, and in
having skipped the transcriber. They converge *before* the gateway, so there is
one person-turn path, one Core call, one goal and memory treatment.

No terminal Core, no second backend, no keyword routing, no command parser.

## 3. Response → console

`outcome.response` already exists at `live_voice.py:273` and is already handed
to `_speak()`. Add one message beside it — `{"type": "alx.text", "content": …}` —
emitted from the same place, so the console mirrors the existing response rather
than re-deriving it.

This matters for Law 0: the console displays what the one response path
produced. It does not become a second response implementation, and the wording
is the Core's own, unaltered.

`FINISHED_SILENTLY` emits nothing, exactly as it renders no speech today.

## 4. Conversation continuity

The console reuses the socket's existing `conversation_id`. It is established
once per connection and already sent to the browser in `session.ready`.

**Nothing new is minted.** Typing and speaking in the same session continue the
same durable thread, the same goals and the same history — because they are
literally the same conversation object, not two that happen to agree.

## 5. Message types to add

Two, both minimal:

| Direction | Type | Payload |
| --- | --- | --- |
| browser → server | `person.text` | `{"content": "…"}` |
| server → browser | `alx.text` | `{"content": "…"}` |

The inbound one is the first non-audio frame the socket accepts, so it needs a
length bound and a rejection path for anything malformed.

## 6. Files likely to change

| File | Change |
| --- | --- |
| `src/alx/interfaces/server.py` | accept one text frame; emit `alx.text` |
| `src/alx/interfaces/live_voice.py` | build a `TYPED` turn; mirror the response |
| `assets/index.html` | one input line inside the existing aside |
| `assets/app.js` | submit on Enter; render `You >` / `ALX >`; handle `alx.text` |
| `assets/app.css` | minimal styling for the input line and two tones |
| `src/alx/bootstrap/providers.py` | optional: allow TTS to be absent (§8) |

## 7. Tests

- a typed frame produces exactly one `ConversationTurn` with `origin=TYPED`;
- it reaches the same gateway method the transcript path uses;
- typed and spoken turns in one session share one `conversation_id`;
- a typed turn continues an existing goal rather than starting a thread;
- `alx.text` carries exactly `outcome.response`, unaltered;
- `FINISHED_SILENTLY` emits no `alx.text`;
- an oversized or malformed frame is refused without ending the session;
- **the console cannot reach the Core except through the person-turn path** —
  asserted by execution, not source text;
- a typed turn works with TTS absent, and the Core's decision is identical
  either way.

## 8. Law 0 and gate additions

**Worth adding:** a rule that `ConversationTurn` is constructed in exactly one
module for person input. Two construction sites — one per input device — is
precisely the duplicate route that would appear if someone later added, say, a
REST endpoint.

**Worth stating in the proposal rather than the gate:** the console renders; it
never interprets. No `/command` grammar, no client-side parsing of what was
typed. The line goes to the Core verbatim.

**TTS switchability.** Today `providers.py` refuses to start without an
ElevenLabs adapter. For quiet evening testing this needs to become optional —
absent synthesizer means no audio, and *nothing else changes*. The Core's
behaviour, its decision to speak, and the conversation record must be identical
whether or not anything is audible. A silent runtime is a transport
configuration, never a different AL/X.

## 9. Designing now to avoid repainting later

The one thing worth deciding today, because retrofitting it means touching every
render call:

**Give each console line a `stream` label from the start.** `diagnostic()`
already takes a `tone`; add a `stream` alongside it, with `ALX` and `SYSTEM` as
the only values now. Existing telemetry becomes `SYSTEM`; typed exchanges become
`ALX`.

That is a few lines today and it means `SERIAL` and `BUILD` later are new
*values*, not a new renderer, plus a filter control the markup already has room
for in its header.

**The distinction to preserve is the one you named:** an output stream says
where displayed data came from; an input target says where the keyboard goes.
They are separate fields and must not be conflated, because the moment the
console infers its destination from content, it has started parsing meaning.
When a second target arrives it should be an explicit UI selection, never a
`/serial` prefix.

### Recorded design invariant — serial output is not Core context

Device or build output displayed in this console **must not become Core
context**. It is rendered to a human and stops there.

If AL/X later needs to inspect a serial log, she reads it through an explicit
capability, which makes the read a decision she made and bounds what enters her
reasoning. A noisy device streaming directly into her context would flood the
input ceiling with material nobody chose, and would let whatever a device
prints act as instructions to her.

That invariant is worth recording now, while the console still has exactly one
stream and the temptation does not exist yet.
