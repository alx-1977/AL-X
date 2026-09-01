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
- **Amendment, 2026-08-30, approved by Friedl.** The first live reply sent without Friedl hearing the wording, and the ten minute expiry recorded above was never applied. Both are corrected, and the mechanism is recorded honestly here:
  - Outbound text AL/X composed may only carry wording that already appeared in something she said to Friedl. A send carrying unheard wording is refused and the reason returned to her; nothing is transmitted and nothing is silently rewritten.
  - **Correction, 2026-08-30.** This was recorded as "a property of the artifact, not a required sequence". That was accurate when the rule accepted any wording AL/X had ever stated, but not after it was narrowed on 2026-08-30 to her most recent response. The rule now orders three things: she states the wording, Friedl answers, the send is permitted. Friedl approved that safeguard, so the deterministic behaviour has owner authorisation under Law 16, but the earlier description understated it and is corrected here.
  - The ordering constrains sending alone. No goal status or lifecycle governs it, AL/X needs no permission to ask a question, and she may draft, reconsider, or abandon a message in any order. It exists so that what she reports sending is what actually left.
  - **Known limits, recorded honestly.** "Heard" means the response was persisted as a durable turn; speech synthesis happens afterwards and can fail, so wording can count as heard when Friedl heard nothing. The check covers composed text, currently the body and subject, not the recipient list.
  - The approval window is now applied when an approval is granted, from `ALX_MAIL_APPROVAL_TTL_SECONDS`, currently ten minutes.
  - Friedl acknowledges the reply after hearing it. Friedl chose this over sending unheard on the grounds that AL/X has no track record yet and a transmitted message cannot be recalled. Moving to sending without acknowledgement would be a further decision taken on recorded evidence, not a configuration flag or a hidden mode.
- **Boundary:** This authorises replying only. It does not authorise composing new correspondence, forwarding, attachments, contact lookup beyond addresses observed on the message being answered, or any mailbox mutation beyond the recoverable Trash already authorised in D-010.
- **Not an exception:** This is a deployment authorisation under Law 19, not an exception to any Law. `governance/EXCEPTIONS.md` remains empty.
- **Review condition:** To be revisited if the approval mechanism changes, if any message is ever sent without a matching approval, if per-reply approval proves unworkable in daily use and a bounded standing authority is proposed, or if sending beyond replies is proposed.

## D-012 — Diagnostics privacy boundary

- **Date:** 2026-08-30
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-30.** Approved on the plain-language
  statement of the rule: *when AL/X hits an error, she writes down what went
  wrong, never what she was working on.* Friedl approved that, having been
  told the two consequences it carries: diagnosis is harder because failures
  report a code rather than content, and any future crash-reporting or
  monitoring tool needs his approval before use. The wording below is a
  reviewer's draft that Friedl accepted; the remaining detail records how the
  rule is enforced and where enforcement is imperfect, and adds no obligation
  beyond the rule itself. Friedl noted this may be revisited if it proves
  inconvenient in practice.
- **Decision:** AL/X runtime diagnostics may contain only structured, explicitly selected operational facts such as sanitised codes, identifiers and durations. They may not export exception objects, traceback frames, captured locals, provider request or response objects, credentials, raw user language, domain-document content, or other payload-bearing runtime state.

  Provider failures must shed their underlying request through both `__cause__` and `__context__`. Runtime handlers log sanitised structured failures without tracebacks.

  Adding an error-reporting, crash-reporting or observability integration that receives exception state requires a separate privacy review and Friedl's approval before production use.
- **Scope:** System-wide. This is not a mail-specific rule. It applies to every private payload AL/X processes: mail bodies, Friedl's speech and its transcription, AL/X's own responses, goal and memory content, and any domain document a future capability handles.
- **Why it is needed:** The provider adapters chained the underlying HTTP exception onto every failure, so a mail body sent for reasoning stayed reachable on an object that travels up through the Core. Fixing that revealed the larger point: the exception chain was one route among several, and nothing recorded which routes were closed, which were merely unused, and which could be reopened by adding a dependency.
- **What is guaranteed, and what is not.** These are separate promises and are recorded separately because conflating them produced two successive overclaims:
  1. **Guaranteed by construction.** A provider failure retains no request. `__cause__` and `__context__` are severed by raising after the handler exits, where there is no active exception to attach. `raise ... from None` is insufficient and is prohibited for this purpose: it clears `__cause__` but the interpreter still records `__context__`.
  2. **Guaranteed by construction.** Ordinary diagnostics stay clean. `format_exception` and every log AL/X writes render a provider name and an error code only.
  3. **Enforced by the architecture gate, within a stated limit.** `scripts/check_architecture.py` parses the source and rejects the diagnostic routes it knows: traceback rendering and frame extraction, `exc_info`, `stack_info` and `capture_locals`, `logger.exception`, assignment to `sys.excepthook`, and sinks named `capture_exception`, `record_exception` and similar. Gate tests prove each is rejected.

     **The gate matches names, not meaning.** It cannot recognise every possible error-reporting API. A sink named `report_failure(error)` would pass it. The gate raises the cost of adding one accidentally; it does not make it impossible. This is why promise 4 below is not redundant, and why the separate-review requirement is the actual control rather than a formality.
  4. **Prohibited by rule, not prevented by code.** Exporting a locals-capturing traceback from a payload-carrying path. Nothing in the code can stop an operator running a debugger or a future integration doing this.
- **Known limit, recorded honestly.** Severing the exception chain does not empty the stack. A traceback built with `capture_locals=True` walks every frame, and any frame processing a mail body still holds it. Clearing the adapter's own locals would not close this, because the calling Core frame holds the same content independently; a test demonstrates that. Diagnostics capturing locals cannot be made safe at the provider boundary for a stack that is by design processing private material. Promise 3 is what protects the frames, and it holds only while AL/X emits no such diagnostics.
- **Boundary:** This governs diagnostics and failure handling. It does not authorise or alter retention, deletion, or any mutation, and it makes no promise about what a model provider retains at its own end.
- **Not an exception:** This is a privacy boundary consistent with Law 15's retention controls, not an exception to any Law. `governance/EXCEPTIONS.md` remains empty.
- **Review condition:** To be revisited if an error-reporting, crash-reporting or observability integration is proposed, if the architecture gate's prohibited list is changed, or if any payload is ever observed in a diagnostic.

## D-013 — Mail retention: content expires on schedule, a reference survives

- **Date:** 2026-08-30
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-30.** Approved on the plain-language statement of the rule, below, having been told what it costs.

- **The rule, as Friedl approved it.** *When AL/X reads an email, fragments of it end up written to her own database on this Mac. Those copies expire thirty days after they are written, whether or not she is still working on something. What survives is a bookmark, not a copy: enough to know a message was there and to go and read it again from iCloud. If it is gone from iCloud, it is gone, and she says so rather than guessing.*

- **Why it is needed.** Friedl does not keep mail: he receives it, acts on it, and clears it. But clearing the inbox never removed AL/X's local copies, and nothing enforced a deadline on them. This makes her copies behave the way his inbox already does. It also replaces a safeguard that was removed: a string-similarity guard that tried to stop mail content reaching durable state and could not be made to work, because no threshold separates a faithful summary from a copied fragment. Retention by provenance replaces detection by resemblance.

- **What Friedl was told it costs.** On a task running longer than the retention window, AL/X may have to re-read a message she would otherwise have remembered. That will look like forgetfulness and is not. Given that his mail tasks complete in minutes against a thirty day window, this is expected to be rare.

- **The deadline.** Thirty days from the moment a record is written. Friedl chose this knowing it is generous for how he works, on the basis that the missing control was enforcement, not the number.

- **Exact scope of what expires.** Every durable record derived from a mail message, whatever its shape:
  - the body, quoted passages, and any prose AL/X wrote that carries the message's content;
  - **structured facts equally**: prices, dates, account numbers, quoted terms, extracted fields. Structure is not a retention loophole. If it could be extracted first and kept forever, the rule would mean nothing.
  - The raw provider body remains transient throughout and is never durable in the first place.

- **What survives as a tombstone.** The durable mail reference (mailbox, UID validity, UID), the record's own identity, its provenance, its timestamps, and the reason it expired. Nothing else: **no subject, no summary, no extracted fact**, unless separately authorised.

- **A tombstone is not evidence.** It preserves identity so AL/X knows something was there. It cannot support a claim. If expired content supported a completion criterion, that criterion becomes unsupported until she re-reads the message or finds other evidence.

- **Re-reading starts a new clock.** A newly created record gets its own independent thirty days. Re-reading never renews an expired record, or the deadline would be defeated by touching a message.

- **If the message is gone from iCloud, it is gone.** AL/X must not reconstruct expired content from memory, inference, or context. She continues on other evidence if she has it, or is genuinely blocked and says so.

- **What "expires" means.** Logical inaccessibility through AL/X: the record is removed and she cannot reach it. It is **not** secure byte erasure. Deleted bytes remain in the database file until overwritten, verified on this system. Turning that into erasure would require enabling `secure_delete` and running `VACUUM`, which carries a cost on every write and remains a separate decision.

- **Keeping something longer requires Friedl.** If particular mail-derived information should outlive its deadline, it takes his explicit approval, such as an approved promotion into durable memory. There is no automatic path.

- **Boundary.** This decides the policy. It does not by itself authorise activating scheduled deletion or running a first purge over existing records; those remain separate authorisations on recorded evidence. It governs mail-derived records and makes no claim about what a model provider retains at its own end.

- **Not an exception:** This implements the retention and deletion controls Law 15 requires, and preserves Law 8 correctly: an unfinished goal continues, but private content does not gain indefinite life merely because a goal stays open. `governance/EXCEPTIONS.md` remains empty.

- **Review condition:** To be revisited if thirty days proves wrong in daily use, if re-reading proves disruptive, if secure erasure is proposed, or if any mail-derived record is ever observed outliving its deadline.

## D-014 — Mail attention is paced one item at a time

- **Date:** 2026-08-30
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-30.** Friedl approved the pause-first recommendation before adding Seen/Unseen mutation, after discussing the distinction between replying, dismissing, and later mail.
- **The rule, as Friedl approved it.** Once AL/X announces a mail item, later mail waits until that item is dealt with. Dismissing it, asking for the next item, or a confirmed successful reply releases it. A failed or uncertain reply does not. Moving it to Trash also releases it because the item has left the inbox.
- **Why it is needed.** Announcing several messages back-to-back gives Friedl no natural space to answer and makes references such as “reply to that” ambiguous. The pause makes the current item stable across silence, other turns, and restarts.
- **Mechanism.** Observation state, not conversational phrase matching, enforces the pause. A delivered observation remains presented and blocks promotion of pending observations until AL/X uses the existing structured local acknowledgement capability or a successful Trash action acknowledges it. The Core still interprets whether Friedl dismissed an item, completed a reply, or asked to move on; no frontend or mail adapter interprets his language.
- **Batch handling.** Friedl may still ask AL/X to work through several messages. AL/X handles and releases them one at a time; each newly promoted event returns to the Core for evaluation rather than being hidden inside a fixed adapter workflow.
- **Amendment by D-015, approved 2026-08-30.** A confirmed reply begins the approved Seen/Trash follow-up; it no longer releases the item immediately. The item is released after that follow-up succeeds or AL/X evaluates a failure and obtains whatever further direction is required.
- **Boundary.** Local acknowledgement changes attention state only. It does not mark a message Seen, alter flags, move it, delete it, or imply that Friedl has read it. D-015 separately authorises the evidence-bound post-reply Seen/Trash behavior without changing this primitive.
- **Not an exception:** This is deterministic observable behavior explicitly requested and approved under Laws 3 and 16. It does not create an exception to any Law. `governance/EXCEPTIONS.md` remains empty.
- **Review condition:** Revisit if mail remains stuck after a successful reply, if queued items are announced before release, if batch handling becomes cumbersome, or when Seen/Unseen behavior is proposed for authorisation.

## D-015 — Replied mail becomes Seen and is tidied when attachment-free

- **Date:** 2026-08-30
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-30.** Friedl described the rule in plain language, accepted the recommendation to implement queue pacing first, and then authorised this mailbox-state slice to proceed.
- **The rule, as Friedl approved it.** Announcing a message alone does not mark it Seen. Dismissing or skipping it leaves it Unseen so he can return to it. Once a reply is confirmed successful, the source message becomes Seen. If it has no attachments it is also moved to recoverable Trash; if it has attachments it remains in place.
- **Why it is needed.** A successful reply means Friedl has attended to the message fully, while a dismissal often means only “not now.” Keeping that distinction in iCloud makes Unseen useful as a return-to-later signal and prevents completed, attachment-free correspondence accumulating in the inbox.
- **New primitive.** `mark_mail_message_seen` sets only the standard IMAP `\Seen` flag on one stable mailbox/UID-validity/UID reference. It cannot clear Seen, move, delete, reply, or select another message. This is a genuinely new external capability and has its own `mail.seen` permission.
- **Attachment evidence.** The provider parses the same source message used by the successful reply and returns a Boolean attachment fact. Only explicit MIME attachments or parts carrying a filename count. The model cannot assert this fact to create Trash authority.
- **Standing authority is exact and evidence-bound.** A fully successful `send_mail_reply` result grants a scope for `mark_mail_message_seen` on that same source reference. It grants a scope for `move_mail_message_to_trash` only when that provider-derived result says there are no attachments. Failed, partial, interrupted, malformed, mismatched, or attachment-unknown replies grant neither scope. Other Seen or Trash actions continue to require exact per-item conversational approval under the ordinary safety gate.
- **Deterministic behavior.** Friedl explicitly approved the post-reply sequence: set Seen, then keep and locally release when attachments exist, or move to recoverable Trash when they do not. Each capability result still returns to AL/X for evaluation; application code does not privately run the next step or manufacture a response.
- **Queue behavior.** The mail item stays current while these post-reply actions are evaluated. Successful Trash releases it. An attachment-bearing message is locally acknowledged only after Seen succeeds. Dismissal uses local acknowledgement alone and therefore cannot change Seen or mailbox state.
- **Recoverability and limits.** Trash remains the server-designated recoverable Trash mailbox and no `EXPUNGE` occurs. This decision does not authorise permanent deletion, clearing Seen, moving to any other mailbox, sending attachments, or treating inline body graphics without a filename as user attachments.
- **Relationship to D-010.** D-010's exact conversational approval remains the rule for ordinary Trash requests. This decision adds only the evidence-derived, exact post-reply standing scope described above; it does not create general unattended Trash authority.
- **Not an exception:** The behavior and fixed sequence were explicitly requested and approved under Laws 3 and 16, and the production mutation was authorised under Law 19. No Law exception is created; `governance/EXCEPTIONS.md` remains empty.
- **Review condition:** Revisit if any dismissed message becomes Seen, any attachment-bearing message is moved automatically, any action targets a different reference, an ambiguous send creates authority, or Friedl's daily use suggests a different attachment or inbox policy.

## D-016 — Email-originated supplier bills and DHL documents in Xero

- **Date:** 2026-08-30
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-30.** Friedl approved proceeding after describing the useful outcome as finding supplier bills that arrive by email, creating and authorising the corresponding bills in Xero, and preserving the DHL customs-document handling that worked well in AL/X V1.
- **The first scope.** This is an accounting-ingestion capability, not a general Xero conversational feature. AL/X may read invoice attachments from an identified mail message; extract and evaluate their structured accounting facts; inspect the configured Xero organisation for contacts, accounts, tax settings and duplicates; create or update an accounts-payable bill; attach the source documents; authorise the bill; and read it back for verification. The existing DHL evidence rules may be adapted: reconcile MyBill charges against the relevant customs worksheet and SAD 500 evidence, refuse unknown or inconsistent money, and attach the supporting documents.
- **Primitive boundary.** Mail attachment retrieval, document reading, Xero lookup, draft creation or update, document attachment, authorisation and read-back remain separately inspectable structured capabilities. AL/X alone decides which are useful and evaluates every result. No mail observer, parser, provider, route or frontend owns the invoice-to-bill sequence or speaks for AL/X.
- **Authority boundary.** Creating, changing, attaching evidence to, or authorising a production Xero bill is consequential. This decision authorises implementation and governed local deployment of those primitives, but does not grant unattended standing authority. Each effectful call requires an exact, expiring approval unless Friedl later approves a narrower evidence-bound standing rule on observed results. Reading Xero and preparing structured proposals do not create write authority.
- **Accounting safeguards.** A bill may not be claimed complete merely because Xero accepted a request. AL/X must read back the resulting bill and compare identifiers, status and monetary totals with the source evidence. Duplicate detection, configured account/tax mappings, decimal arithmetic and refusal on inconsistent or missing required evidence are implementation requirements, not model discretion.
- **DHL continuity.** V1 is evidence and reusable domain logic, not architecture authority. Its positional customs-worksheet parsing, multi-shipment reconciliation, charge classification, duplicate prevention and content-based document identification may be ported behind the approved boundaries. Its phrase routing, background sequencing, hard-coded workflow ownership or direct production authority may not be copied.
- **Boundary.** No payment, bank reconciliation, contact mutation, sales invoice, quote, purchase order, credit note, journal, payroll action or general Xero interaction is authorised by this decision. No Law exception is created; `governance/EXCEPTIONS.md` remains empty.
- **Review condition.** Revisit after real bill evidence exists, before granting unattended authority, or if any duplicate, incorrect amount, incorrect tax treatment, missing document, unapproved write or read-back mismatch occurs.

## D-017 — Invoice reachability and two-stage DHL continuity

- **Date:** 2026-08-31
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-31.** After independent review showed that D-016's first implementation could not find historical invoice mail or preserve the working two-stage DHL outcome from V1, Friedl identified supplier-invoice and DHL processing as critical and authorised continuing with V1 as working domain evidence.
- **Mail reachability.** AL/X may search the configured mail account using structured criteria such as mailbox, sender, subject text, date range, Seen state and attachment presence. Search is a read-only primitive that returns stable message references; it does not interpret Friedl's wording, announce mail, change observation state or choose an invoice workflow. The forward-only observer remains responsible only for new-mail attention and must not flood AL/X with old mail on startup.
- **Archive reachability.** Mail attachment access may traverse nested archives under explicit depth, member-count and expanded-byte limits. It must expose stable, hash-bound virtual attachments without writing archive contents to disk, reject unsafe or excessive archives, and preserve provenance to the source message.
- **Two-stage DHL outcome.** Customs documents may arrive before the MyBill invoice. AL/X must be able to analyse and preserve the worksheet and SAD 500 evidence, create a provisional draft when useful, and later find or recover that evidence, update the same draft from the invoice, attach every required source, authorise it and read it back. The provisional identifier convention from V1 may be reused as accounting-domain evidence. AL/X chooses whether and when to use each primitive; no observer, provider or deterministic application workflow sequences those steps.
- **Xero safeguards.** Invoice-number duplicate lookup must use the documented Xero collection filter rather than treating a supplier invoice number as an InvoiceID. Draft creation and update must verify supplied account and tax identifiers against the configured organisation before writing. Malformed Xero money fails closed. Authorisation requires byte-for-byte verification of every explicitly required attachment already stored on that exact bill, not merely Xero's `HasAttachments` flag.
- **DHL evidence.** Customs Worksheet and SAD 500 documents are identified from content and remain distinct evidence. Positional extraction must be tested against the retained real V1 fixtures and must refuse ambiguous identity, missing totals, unexpected documents and inconsistent money. Scanned image-only invoices remain outside this slice and must block honestly pending a separately reviewed OCR capability.
- **Authority and retention.** Search, document reading, analysis and Xero reads are non-effectful. Creating or updating a draft, attaching a document and authorising a bill each retain exact expiring approval under D-016. Mail-derived facts and references retain D-013 provenance and expiry; this decision does not activate scheduled deletion.
- **Boundary.** This extends D-016 only far enough to make its approved supplier-bill and DHL outcomes reachable. It does not authorise contact mutation, payments, bank work, general Xero interaction, permanent mail deletion, autonomous background writes or a second goal/evidence store. No Law exception is created; `governance/EXCEPTIONS.md` remains empty.
- **Review condition.** Revisit after real read-only Xero evidence and representative DHL documents pass, before the first production write, or if search returns the wrong message, an archive limit is bypassed, evidence cannot be resumed after restart, an incorrect account/tax pair is accepted, or an attachment mismatch reaches authorisation.

## D-018 — Unattended supplier-bill writes

- **Date:** 2026-08-31
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-31.** Friedl directed that AL/X process supplier bills without asking for approval at each write and report the completed work afterwards. He weighed the risk explicitly: a bill is not a payment, an incorrect bill is deleted in Xero, and re-proving carried-over V1 behaviour at his expense is not an acceptable cost. This supersedes the per-write approval reserved in D-016 and D-017.
- **Scope.** Creating a draft bill, updating a draft bill, attaching a source document and authorising a bill may proceed under standing authority for supplier bills of any amount. Friedl declined an amount threshold and said he will impose one later if problems appear.
- **Unchanged safeguards.** The authority changes; the accounting evidence rules do not. Draft creation still refuses unbalanced lines, account and tax identifiers absent from the live organisation, and a duplicate supplier/invoice pair. Attachment still requires an exact SHA-256 match against the identified mail attachment. Authorisation still requires a DRAFT bill, a matching invoice number and total, and byte-for-byte verification of every required attachment already stored on that bill. Every write is read back and verified.
- **Boundary.** Standing authority covers supplier accounts-payable bills only. No payment, bank reconciliation, contact mutation, sales invoice, quote, purchase order, credit note, journal or payroll action is authorised. The `accounting.payments` and `accounting.banktransactions` scopes remain unrequested, so no authorised bill can move money.
- **Configuration.** The authority is carried by `ALX_XERO_UNATTENDED_BILL_WRITES`, which defaults to attended. Unattended operation is an explicit deployment choice, not a code default, and can be withdrawn by changing one environment value.
- **Unproven at approval.** No bill had been created by V2 in Xero when this was approved. Friedl accepted that risk for the first four bills. The duplicate-detection defect found on 2026-08-31 is fixed and covered by tests.
- **Review condition.** Revisit if an incorrect bill is created, a duplicate reaches Xero, an attachment is attached to the wrong bill, a read-back mismatch occurs, or Friedl decides an amount threshold or a return to attended writes is warranted.

## D-019 — Discarding a draft supplier bill on request

- **Date:** 2026-08-31
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-31.** Friedl asked that AL/X be able to delete a bill when he asks her to, and chose to start with drafts after being shown that Xero treats a draft deletion and an authorised void as materially different acts.
- **Scope.** AL/X may discard one exact accounts-payable bill whose current status is DRAFT or SUBMITTED. She must first read the bill and verify its invoice number and total against the values she was working from, and must verify afterwards that Xero recorded the discard.
- **Explicitly excluded.** Voiding an AUTHORISED, PAID or already discarded bill is not authorised by this decision. That reverses an accounting entry, cannot be undone, and may affect a filed VAT period. A non-draft is refused with `bill_not_draft`. Extending to voiding requires a separate decision.
- **Authority.** Discarding carries its own permission, `xero.bill.delete`, and its own approval setting, `ALX_XERO_UNATTENDED_BILL_DELETES`, defaulting to attended. The unattended bill-write authority in D-018 deliberately does not carry deletion with it: creating a wrong bill is undone by discarding it, while discarding the wrong bill destroys prepared work. A test asserts that unattended writes leave deletion attended.
- **Primitive justification.** Discarding a bill is an external Xero effect AL/X did not previously possess. It is not a rewording of creation, update or authorisation. It requires no additional OAuth scope; `accounting.invoices` already covers it.
- **Review condition.** Revisit if a bill is discarded that Friedl did not intend, if a non-draft reaches the discard path, or if Friedl decides voiding authorised bills should also be available.

## D-020 — Deterministic execution, mail filing, and default coding

- **Date:** 2026-08-31
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-08-31.** Recorded after review found three behaviours that were implemented without a decision naming them. The behaviours are what Friedl asked for; the omission was failing to record them.
- **Deterministic bill execution.** `capture_supplier_invoice` may read one identified attachment, extract it through a bounded specialist, resolve the supplier, resolve the accounting treatment, create or resume the draft, attach the source, authorise it and verify it, without returning to AL/X between those steps. This is the deterministic sequence Law 2 permits when it says a capability may contain whatever mechanical steps its outcome requires. It replaces the eight-reasoning-call path that made a single invoice cost 143,036 estimated input tokens. AL/X still decides whether to use it, still evaluates its result, and anything without one objectively correct outcome returns to her.
- **Blueprint correction.** `docs/ARCHITECTURE_BLUEPRINT.md` names `process_DHL_invoice_workflow` as a prohibited journey. That example predates the three-law rewrite and is corrected there: a capability is prohibited when it interprets what Friedl wants or decides business meaning, not when it performs several mechanical steps toward one outcome.
- **Mail filing supersedes D-010's mailbox limit.** A processed supplier invoice is moved to the mailbox named by `ALX_MAIL_PROCESSED_MAILBOX`. D-010 authorised Trash only and forbade other mailbox moves; Friedl chose filing over deletion in V1 on advice and confirmed that choice here, so this supersedes that limit for this one capability. The destination is configuration: the capability takes no mailbox argument, nothing is deleted, and the message stays in the account. With no mailbox configured it refuses.
- **Default expense coding.** Where a supplier's own bills give no single answer, the bill posts to `ALX_XERO_DEFAULT_ACCOUNT_CODE` with its tax type taken from the document. A supplier that codes consistently keeps its own treatment, so the default applies only where history is silent or disagrees. Friedl accepted the loss of expense-category detail to make capture work unattended, and recorded in `TODO.md` that this is revisited once real bills exist to learn from.
- **Missing due date.** Xero requires a due date on a bill. Where an invoice states none, the invoice date is used, so the bill reads as due on receipt. This can make a bill look due sooner but never hides one that is overdue, and V1 made the same choice.
- **Boundary.** No payment, bank reconciliation, contact mutation, sales document, journal or payroll action is authorised. Mail deletion is unchanged.
- **Review condition.** Revisit if a bill is created against the wrong supplier or with an unintended account, if a message is filed that was not processed, or if the default account obscures something Friedl needs to see.

## D-021 — `process_dhl_import` is the sole deterministic DHL production path

- **Date:** 2026-09-01
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-09-01.** Recorded after review confirmed that the implemented two-stage DHL capability was sequencing steps that D-017 reserved to AL/X, and that the deterministic-execution authority in D-020 named `capture_supplier_invoice` only. Friedl directed that the capability be authorised rather than dismantled, because the reasoning that justified D-020 applies with more force to an import that has more mechanical steps, not fewer.
- **Deterministic DHL execution.** `process_dhl_import` may read the exact documents it is given, classify them, verify the customs evidence, create or resume the provisional draft, re-verify that draft against the evidence, attach every source document, authorise the bill and read it back, without returning to AL/X between those steps. This extends D-020's deterministic-execution reasoning to this capability. Law 2 permits a capability to contain whatever mechanical steps its outcome requires; AL/X still decides whether to use it, still evaluates its result, and anything without one objectively correct outcome still returns to her.
- **Supersedes only incompatible wording in D-017.** D-017's two-stage DHL clause ends "AL/X chooses whether and when to use each primitive; no observer, provider or deterministic application workflow sequences those steps." That sentence is superseded for this capability alone. Everything else in D-016 and D-017 stands unchanged: the evidence rules, the Xero safeguards, the archive and reachability limits, the retention terms and the boundary list. AL/X still chooses whether and when to invoke `process_dhl_import`; what is authorised is the sequencing *inside* it.
- **One outcome, one path.** A DHL import is posted only by `process_dhl_import`; an ordinary supplier bill only by `capture_supplier_invoice`, which refuses a DHL document rather than flattening duty, import VAT and clearance onto one account. Recognition is from document content, never wording. No alternative, recovery-only or granular DHL write path may be retained. The MyBill GDB CSV reconciliation implementation carried over from V1 is superseded by the invoice-PDF path and is deleted from production source under Law 0.
- **Evidence must be re-verified, not assumed.** Because the sequence runs unattended, a resumed draft is not trusted on status and total alone: the supplier, currency, line accounts and amounts are checked against the customs evidence before the import continues, and every required customs document is verified byte-for-byte as still stored on the bill before authorisation. A bill whose evidence has been altered or removed returns to AL/X rather than being authorised.
- **Dates are stated, never guessed.** A date is written only where a document states it. The customs stage uses the assessed date the worksheet carries; the invoice stage uses the invoice's own date. Where an invoice states no date, D-020's rule stands and the invoice date is used for the due date — but the due date is never substituted for a missing invoice date, which would post an invoice under a date no document asserts.
- **Supplier identity is configuration, not a parameter.** The DHL contact is resolved at run time by exact supplier name from `ALX_XERO_DHL_SUPPLIER_NAME`, matched against active contacts in the live organisation, as V1 identified DHL. Friedl is never asked to discover or configure a Xero identifier. The capability takes no contact argument, so a wrong or unintended supplier cannot be supplied to it. A name that matches no active contact refuses; a search that returns only unrelated contacts refuses rather than settling for one.
- **Account mappings are validated.** The import VAT, customs duty and clearance account codes are checked against the live configured organisation before any write, exactly as `capture_supplier_invoice` validates its own coding. An account absent from the organisation refuses rather than posting.
- **Authority.** Unchanged. A DHL import is a supplier-bill write and carries D-018's standing authority under `ALX_XERO_UNATTENDED_BILL_WRITES`. No payment, bank reconciliation, contact mutation, sales document, journal or payroll action is authorised. No Law exception is created; `governance/EXCEPTIONS.md` remains empty.
- **Unproven at approval.** No DHL import had been posted to the live organisation when this was recorded. The lifecycle is proved against fixtures and a fake Xero only.
- **Review condition.** Revisit if a DHL import posts against the wrong supplier or account, if a bill is authorised whose customs evidence is missing or altered, if a partially updated bill cannot be recovered, if the two stages ever produce two bills for one waybill, or before granting any further deterministic sequencing authority.

## D-022 — DHL duty-tax-paid (export) invoices post to Import/Export Fees

- **Date:** 2026-09-01
- **Decision owner:** Friedl
- **Status: APPROVED by Friedl, 2026-09-01.** Recorded after the first real export invoice was retrieved and inspected. AL/X had assumed an export was a freight shipment and had no path for it; the document showed otherwise. Friedl renamed Xero account 426 from customs duty to **Import/Export Fees** and set its default tax to No VAT (0%), and directed that every line of a duty-tax-paid invoice posts there.
- **A DHL import is not two shapes but three.** Each is identified from the document's own evidence, never from wording and never from whether a draft happens to exist:
  - **Customs import** — carries a declaration number and the SARS pass-through codes. Duty and import VAT are assessed by a Customs Worksheet and SAD 500 that arrive first, so this remains the two-stage path D-021 authorises.
  - **Duty tax paid (export)** — carries duty and regulatory charge codes, **no declaration number** and **no weight charge**. No worksheet or SAD 500 exists, so there is no second document to cross-check against.
  - **Freight** — carries a **weight charge**, and neither a declaration nor SARS pass-through codes. Friedl confirmed these arrive as clearly separate invoices.
  The discriminators are ordered: a weight charge means freight; otherwise a declaration number means customs; otherwise duty and regulatory charges mean duty tax paid. Verified against four real invoices — V1's CPTR001005873, CPTZR00026033 and CPTZR00028679, and CPTIR00273840.
- **Duty-tax-paid accounting.** Every line posts to `ALX_XERO_CUSTOMS_DUTY_ACCOUNT` (426, Import/Export Fees), each charge as its own line so the invoice's own breakdown is preserved rather than merged into one figure. Verified on CPTIR00273840: import/export duties 136.55 + regulatory charges 22.21 + duty tax paid 350.00 = 508.76, the invoice total exactly.
- **Reconciliation is self-consistency only.** With no worksheet or SAD 500 there is no second source, so the guarantee is weaker than the customs flow's two-document cross-check and the implementation must say so rather than imply equivalence. The invoice's own components must sum to its stated total, and a bill whose components do not reconcile is refused rather than posted.
- **Tax is never invented and never dropped.** Lines post `NoTax`. An invoice stating any tax is refused rather than posted as untaxed: V1 recorded that letting Xero apply an account's default rate once invented R452.76 of input VAT that was never charged. Account 426 now defaults to No VAT, which removes that risk for this account but not the reverse one.
- **The structured invoice is the evidence.** DHL MyBill sends the machine-readable CSV in the same email as the PDF. The CSV states the charge codes, per-line amounts, discounts, weight charge, declaration number and tax, so classification and reconciliation read it rather than recovering figures from a rendered PDF. The PDF remains the human-readable source document and is what gets attached to the bill.
- **One capability.** These are deterministic branches inside `process_dhl_import`, not new capabilities and not alternative paths. Law 0 is unchanged: one production outcome, one authoritative path.
- **Freight is not authorised by this decision.** Friedl confirmed freight invoices arrive separately and are visibly distinct. Their accounting treatment is not decided here; a freight invoice is recognised and returned to AL/X rather than posted, until a real one is inspected and its treatment recorded.
- **Unproven at approval.** No duty-tax-paid invoice had been posted to the live organisation when this was recorded. Account 426, its No VAT default and the DHL contact were each confirmed read-only against the live FireFli organisation.
- **Review condition.** Revisit if a duty-tax-paid invoice posts against the wrong account, if a freight or customs invoice is misclassified as duty tax paid, if an invoice carrying tax reaches a NoTax posting, or before freight handling is added.
