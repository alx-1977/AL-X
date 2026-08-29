# AL/X Decision Record

This file records approved product and architecture decisions that guide implementation without altering the Laws of AL/X. Decisions cannot create exceptions to a law.

## D-001 — Foundation architecture accepted

- **Date:** 2026-08-26
- **Decision owner:** Friedl
- **Decision:** The six-part, single-path architecture in `docs/ARCHITECTURE_BLUEPRINT.md` describes the required AL/X foundation.
- **Meaning:** Friedl accepted the observable behaviour and meaningful trade-offs in plain language. Models and implementers retain responsibility for technical correctness and proof.

## D-002 — Runtime model evaluation order

- **Date:** 2026-08-26
- **Decision owner:** Friedl
- **Decision:** Test Grok first as the AL/X runtime model. If it fails the foundation requirements, test OpenAI next.
- **Constraints:** Model selection remains configuration behind the single model adapter. Changing the runtime model cannot create another conversation path or require feature code.
- **Development distinction:** OpenAI/Codex may be used for implementation and code review regardless of which model runs AL/X. Development-tool usage does not make that provider a second AL/X authority.
- **Evaluation:** Correctness, pivoting, tool use, continuity, safety, citations, conversational character, usage, and cost are measured. A law-critical failure is disqualifying.

## D-003 — Reuse the original video background

- **Date:** 2026-08-26
- **Decision owner:** Friedl
- **Decision:** Reuse the original AL/X video background when frontend work begins. Do not spend time or usage creating a replacement now.
- **Boundary:** Only the visual media asset may be copied after its exact source is verified. Old frontend logic, orchestration, handlers, routes, and state are not authorised for migration.
- **Timing:** No visual or frontend work occurs during the foundation proof.

## D-004 — Voice providers and final activation behaviour

- **Date:** 2026-08-27
- **Decision owner:** Friedl
- **Decision:** Use Cartesia as the initial speech-to-text provider and ElevenLabs, with the configured preferred voice, for text-to-speech. Both remain replaceable adapters. When AL/X moves to her own machine, local speech-to-text may be restored with a stronger local model without changing the conversation, Core, memory, or frontend authority boundaries.
- **Single path:** Speech-to-text transcribes only. Every final transcript enters the same Conversation Gateway and AL/X Core used by every other conversational input. Text-to-speech receives only AL/X's authoritative response.
- **Activation:** A temporary click control is acceptable for the first executable slice. It is not the final interaction model and must not become a separate conversation route.
- **Prohibition:** Wake words and phrase-based voice activation or routing are not part of AL/X. The eventual ambient system may use local voice activity and speaker evidence, but only the AL/X Core may infer from the full conversational context whether Friedl is addressing her.
- **Initial configured models:** Friedl selected `grok-4.6` for reasoning, Cartesia `ink-2` for transcription, and ElevenLabs `eleven_v3` with his existing configured voice for synthesis. These are initial configuration choices, not permanent architecture.
- **Initial turn profile:** Friedl selected Cartesia's Responsive starting profile: start threshold `0.7`, eager-end threshold `0.5`, end threshold `0.4`, and end timeout `4500 ms`. The values remain configurable and will be tuned from observed conversations.

## D-005 — OpenAI becomes the next Core model candidate

- **Date:** 2026-08-28
- **Decision owner:** Friedl
- **Evidence:** Two measured Grok 4.6 priority-tier voice turns took 80.35 and 93.47 seconds in Core reasoning, while transcription and synthesis were comparatively small parts of the round trip.
- **Decision:** Continue the approved evaluation order by testing OpenAI GPT-5.6 Sol as the authoritative AL/X Core through the same provider-neutral reasoning boundary. Begin with the model's configured `medium` reasoning effort and retain the measurements needed to compare quality, latency, cache use, and reasoning tokens.
- **Constraints:** Cartesia remains the speech-to-text adapter, ElevenLabs remains the text-to-speech adapter, and AL/X's durable goal and memory stores remain authoritative. The provider change creates no new conversation path, workflow, intent router, or frontend authority.

## D-006 — Conversation is continuous; goals are optional attached state

- **Date:** 2026-08-28
- **Decision owner:** Friedl
- **Decision:** Preserve one authoritative Core and one continuous durable conversation thread. Goals are optional state attached to that conversation and never the container that owns it.
- **State authority:** The model may propose a goal mutation separately from its response. Deterministic Core code validates and reduces that proposal. Goal completion is Core-derived from explicit success criteria, sourced durable evidence, and the absence of blockers, outstanding work, or unresolved dispatches; the model cannot author a completed goal directly.
- **Failure behavior:** Rejection of an optional goal proposal does not invalidate an otherwise safe, correct response. There is no blanket correction retry. A materially goal-dependent response fails closed without changing durable goal state or dispatching a capability.
- **Evidence:** New goal evidence carries durable source references that the Core validates against retained conversation turns and completed capability attempts before persistence.
- **Constraints:** This creates no second agent, conversation path, router, frontend authority, workflow, or Law exception.

## D-007 — First permanent local voice-to-Core runtime authorised

- **Date:** 2026-08-28
- **Decision owner:** Friedl
- **Decision:** Friedl authorised implementation and evaluation of the first permanent local voice-to-Core runtime after reviewing and approving its provider-neutral design, temporary activation control, single-path boundary, and incremental test criteria.
- **Approved scope:** The local browser interface, WebSocket audio transport, Cartesia transcription adapter, the selected configurable reasoning adapter, ElevenLabs synthesis adapter, independent conversation migration, development diagnostics, and the composition root required to run that slice.
- **Required boundaries:** Speech transcription has no reasoning authority; every final transcript enters the same Conversation Gateway and sole AL/X Core; only the Core response may reach synthesis; the frontend has no goal, workflow, capability-selection, or business authority; all providers remain replaceable configuration.
- **Deployment boundary:** This authorises the local development runtime and its governed evaluation. It does not authorise any domain integration, production write, autonomous deployment, or Law exception.
- **Acceptance evidence:** Friedl completed multi-turn live voice tests, including audible playback, recovery after a rejected optional goal proposal, and conversation continuity across a runtime restart/update.

## D-008 — ElevenLabs pronunciation vocabulary authorised before Email

- **Date:** 2026-08-29
- **Decision owner:** Friedl
- **Decision:** Keep ElevenLabs as the speech-synthesis provider and add a persistent, versioned pronunciation dictionary before implementing Email.
- **Authority boundary:** The canonical vocabulary remains local and inspectable. ElevenLabs receives the unchanged authoritative AL/X response, a provider-side spoken rendering where actual tests prove normalization is insufficient, and only the active remote dictionary/version locator on each synthesis request. Pronunciation logic cannot enter the Core, conversation, frontend, or Email capability.
- **Initial rules:** Prefer portable aliases for engineering notation, technical acronyms, and approved names. The remote dictionary is a deployed version, not the source of truth.
- **Normalization evidence:** With the configured `eleven_v3` voice and native normalization forced on, `R2,000` and `R2 000.50` were rendered naturally, while compact `R2000` was not rendered as rand. Deterministic provider-bound preprocessing is therefore authorised only for the failed compact rand form.
- **Constraints:** Written/displayed text remains unchanged; dictionary updates are versioned; representative audio is tested against the actual configured model and voice before acceptance. No Law exception is created.

## D-009 — Ambiguous compact R-number speech rendering removed

- **Date:** 2026-08-29
- **Decision owner:** Friedl
- **Decision:** Do not interpret compact `R<number>` forms as South African currency in the ElevenLabs adapter. Ambiguous forms such as `R5`, `R10`, `R100`, and `R2000` pass to ElevenLabs unchanged rather than being assigned a meaning outside the Core.
- **Native normalization:** Continue relying on the configured ElevenLabs model's verified native normalization for unambiguous currency forms such as `R2,000` and `R2 000.50`.
- **Future boundary:** Contextual speech metadata may be considered later if compact currency becomes important. This decision does not authorise that architecture and creates no Law exception.

## D-010 — Production mail mutation authorised: move to recoverable Trash

- **Date:** 2026-08-29
- **Decision owner:** Friedl
- **Decision:** Friedl authorises the `move_mail_message_to_trash` capability to operate against his real iCloud account in the local AL/X runtime. This is the production-deployment authorisation Law 19 requires for a capability that modifies production data.
- **Exact scope:** One capability, `move_mail_message_to_trash`, moving one identified message from its mailbox to the server-designated recoverable Trash mailbox. No other mailbox mutation is authorised.
- **Why it is needed:** Dismissing and deleting mail is one of the three original purposes of the email integration. Without it AL/X can read and acknowledge mail but cannot act on it.
- **Recoverability:** The operation is an IMAP `UID MOVE` to the mailbox the server flags `\Trash`. It is not `EXPUNGE` and performs no permanent deletion. A moved message remains recoverable from Trash under Friedl's normal iCloud retention.
- **Required safeguards, verified present:**
  - the capability is classified `SideEffect.EFFECTFUL`;
  - it requires the distinct `mail.trash` permission, separate from reading;
  - it requires an exact, per-message approval grounded in Friedl's own latest conversational turn; a general instruction cannot authorise it;
  - the approval scope is equality-matched, so any change of message or mailbox invalidates it;
  - the Trash mailbox is discovered from the server's own flag rather than a hard-coded name.
- **Boundary:** This authorises Trash only. It does not authorise permanent deletion, expunge, archive, move to any other mailbox, flag changes, mark-read as an external mutation, sending, or drafting. Each of those remains a separate governed decision.
- **Not an exception:** This is a deployment authorisation under Law 19, not an exception to any Law. `governance/EXCEPTIONS.md` remains empty.
- **Review condition:** To be revisited if the approval mechanism changes, if an unapproved move is ever observed, or if permanent-deletion authority is later proposed.

## D-011 — Production mail sending authorised: reply to an existing message

- **Date:** 2026-08-29
- **Decision owner:** Friedl
- **Decision:** Friedl authorises the `send_mail_reply` capability to transmit from his configured iCloud identity in the local AL/X runtime. This is the production-deployment authorisation Law 19 requires for a capability that acts on production systems.
- **Exact scope:** One capability, `send_mail_reply`, sending one reply to an existing message. Nothing else is authorised: no new thread, no forward, no bulk send, no scheduled or unattended send, no attachment.
- **Why it is needed:** Replying is one of the three original purposes of the email integration, and the one Friedl expects to use most. Without it AL/X can read, dismiss, and trash mail but cannot answer it.
- **Irreversibility:** Unlike a move to Trash, a transmitted message cannot be recalled. The safeguards below exist because this action cannot be undone.
- **Required safeguards, verified present:**
  - the capability is classified `SideEffect.EFFECTFUL`;
  - it requires a distinct send permission, separate from reading, observing, and trashing;
  - every send requires its own approval; approving one reply authorises no other;
  - the approval is bound by equality to the exact arguments, so changing one word of the body, a recipient, the subject, or the threading target invalidates it;
  - the approval expires after a short configured window, so a stale authorisation cannot send later;
  - an approval is consumed once used and cannot send a second message;
  - the sender identity is fixed by configuration; `OutboundReply` has no sender field and no capability accepts one;
  - a failure carries a sanitised code only, never a credential or message body.
- **Read-back before sending:** AL/X states the exact reply before asking. Because the approval binds to those exact arguments, the message Friedl hears is the message that leaves, or none is sent.
- **Boundary:** This authorises replying only. It does not authorise composing new correspondence, forwarding, attachments, contact lookup beyond addresses observed on the message being answered, or any mailbox mutation beyond the recoverable Trash already authorised in D-010.
- **Not an exception:** This is a deployment authorisation under Law 19, not an exception to any Law. `governance/EXCEPTIONS.md` remains empty.
- **Review condition:** To be revisited if the approval mechanism changes, if any message is ever sent without a matching approval, if per-reply approval proves unworkable in daily use and a bounded standing authority is proposed, or if sending beyond replies is proposed.
