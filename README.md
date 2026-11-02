# AL/X

This is the clean rebuild of AL/X.

## Current phase

The governed foundation runtime is under incremental implementation. Domain integrations and workflows remain out of scope until the foundation proof passes. The first voice work is limited to the single Conversation Gateway and replaceable reasoning, transcription, and synthesis provider boundaries approved by Friedl.

The founding **Laws of AL/X**, foundation architecture, and initial voice boundary are owner-approved. Every increment must pass the executable governance and architecture gates before review.

## Mandatory instructions

Every model and contributor must begin with `AGENTS.md`, which requires the complete reading of the canonical laws and their enforcement specification. Model-specific instruction files only point to that authority; they do not maintain separate copies of the rules.

## Current foundation

- `docs/ARCHITECTURE_BLUEPRINT.md` describes the accepted single-path, model-independent AL/X foundation.
- `docs/FOUNDATION_PROOF.md` defines the approved behavioural demonstration that must pass before real integrations are accepted.
- `docs/TECHNICAL_PLAN.md` defines the minimal implementation sequence.
- `IDENTITY_AND_MEMORY.md` contains Friedl-approved identity principles, origin memories, and autobiographical-memory boundaries subordinate to the Laws.
- `src/alx/conversation` contains the only conversational ingress and the independent durable conversation store; goals are optional state attached to that thread.
- `src/alx/providers` isolates external reasoning, STT, and TTS APIs behind provider-neutral contracts.
- `src/alx/core/model_reasoner.py` is the single provider-neutral bridge from approved identity, durable state, and primitive schemas to an authoritative Core decision.
- `src/alx/memories` persists Core-selected factual, relationship, and autobiographical memories with provenance, revision history, person isolation, and retention controls. The Core may retrieve a narrowly scoped selection through structured metadata, while the store never interprets the conversation or decides semantic relevance.
- `.env.example` lists provider selection and model settings; provider and model choices are never embedded into Core behaviour.

## Local voice runtime

The first permanent interface is voice-only. A temporary one-time **Start AL/X** control grants the browser microphone permission and then disappears completely; it is not push-to-talk or intended as permanent AL/X interface design. After that, Cartesia's configured turn detector identifies speech boundaries automatically. The browser transports PCM audio and plays ElevenLabs audio, while the existing Conversation Gateway and Core remain the only conversational path and reasoning authority.

Start the local runtime from the repository root:

```bash
scripts/alx start
```

The launcher runs committed local main and passes this checkout as --checkout.

Then open `http://127.0.0.1:8765`, the default interface address. Runtime policy, provider selection, models, voice, turn-detection profile, and the runtime's isolated storage location all come from `.env`; none are selected by frontend code.

Optional Claude subscription authentication is configured with
`CLAUDE_CODE_OAUTH_TOKEN` in `.env`, supplied privately by Friedl using
`claude setup-token`. With the setting absent, the existing renewable login in
`~/.claude-alx` is used. API keys and provider-routing environment overrides
are excluded from the Claude subprocess environment in both modes.

**Current limitation:** the installed Claude Code 2.1.316 cannot verify the
required setup-token account identity. Its embedded `auth status --json`
implementation reports `authMethod: "oauth_token"` and
`apiProvider: "firstParty"`, but supplies `email` and `subscriptionType` only
for `authMethod: "claude.ai"` (the saved login). Token status omits those fields;
saved-login metadata cannot identify the token owner. AL/X therefore fails
closed with `subscription_token_identity_unverifiable` when a setup token is
configured: no reasoning, research or usage operation is enabled by that token.
The token environment is wired for all three paths, but cannot be used until
the CLI can affirmatively verify `alx@fire-fli.co.za` on a claude.ai subscription.
There is no fallback to the saved login or metered billing in token mode.
The CLI's own setup-token success text names `CLAUDE_CODE_OAUTH_TOKEN` as the
supported mechanism; no token was generated or used to establish this evidence.

## Source of authority

Friedl is the product owner and final authority for what AL/X is, how she behaves, and how she may be built. Models and implementers may propose changes, but may not weaken, reinterpret, or silently create exceptions to an approved law.

## Previous system

The previous JARVIS/AL/X repository is reference material only. This project must not import, depend on, or copy its orchestration architecture. Useful domain behaviour may be studied and reimplemented later, one primitive capability at a time, after the new foundation is proven.
