# Proposal — Self-repair: AL/X diagnoses and fixes herself against real services

**Status:** PROPOSAL. Not approved, not implemented. Requires Friedl's approval as a
decision record (proposed D-060) before any code is written. AL/X's unpushed draft of a
D-060 "trial run" on `feature/trial-run-capability` is superseded by this proposal and is
to be dropped.
**Authority:** Subordinate to the Laws of AL/X, `IDENTITY_AND_MEMORY.md` and
`docs/LAW_ENFORCEMENT.md`.
**Sources:** Friedl's requirements of 2026-10-21; independent design reviews of the same
brief by Grok and by Codex; the usage-reader incident (#168, #169, #170). Vendor
capabilities cited in §4 come from those reviews and are **not yet verified** against
installed versions (see §15).

---

## 1. Summary for Friedl

AL/X should be able to find and fix her own bugs, including bugs that only show up
against real services, without you or another AI diagnosing them for her.

The design has one idea at its centre. **The coding agent never holds a password.** A
small trusted program, the *broker*, holds every login and is the only door to real
services. AL/X and her coding agent ask the broker to look at a real service, or to run
their new code against it. The broker does it and keeps the complete raw result. Looking
is allowed freely. Anything that changes something — send, delete, post, pay — needs your
approval first, and that is enforced by the broker, not left to an instruction.

Four things follow:

- **She sees everything.** Every live run keeps the raw output (terminal bytes, the full
  email, the API reply) before anything cleans it up. Today's three failed usage-reader
  rounds would each have shown their cause the first time.
- **She tests before paying for a review.** A check refuses a Greptile request until the
  changed code has been seen working against the real service.
- **One sandbox.** The separate experiment sandbox is removed; experiments, coding,
  diagnosis and testing use one work room.
- **New logins need almost no work.** Reading through a new login works through the same
  door. For each new service AL/X drafts a one-page note of what is safe to do with it,
  and you approve it once.

What you give up: some care about what reaches OpenAI and Anthropic. Passwords never do,
but emails or screens shown to the coding agent do. §9 sets the rule.

What you still decide: changes to the broker's own rules, anything that sends, deletes,
posts or pays, and the one-page note for each new service.

---

## 2. The problem

Today a fix is made by the coding agent (Codex) in a locked box: no network, no logins,
no runtime data, no git. Its tests and AL/X's verification use made-up samples only. Bugs
where her code meets a real service therefore pass every test and fail live.

Evidence from 2026-10-21:

- **Usage reader** (drives the Claude CLI's `/usage` screen in a pseudo-terminal). Three
  merged fixes failed live, each costing a review, a merge and a restart. The causes were
  terminal codes the renderer rejected, keys pressed during Claude's startup redraw, and a
  no-break space after the prompt. An outside agent with an ordinary terminal found all
  three in minutes by running the real program. AL/X could not: her reader kept only
  cleaned text, and she had no way to run anything against the real service.
- **Email.** A DigiKey shipping email read as "." because the reader prefers a near-empty
  plain-text part over the HTML part. Only the real message shows that.
- **DHL.** API credentials exist; nothing uses them.

The paid review was being used as the debugger. A review checks whether a change is
acceptable; a live run checks whether it works. The live run has to come first.

## 3. Requirements (Friedl, 2026-10-21)

1. **Test before review.** No paid review for code not seen working against the real
   service.
2. **Full visibility.** Raw output, logs, runtime data, real screens; the ability to run
   what is needed to diagnose and test.
3. **No restriction that brings us back here.** Where something is a genuine security
   risk she asks Friedl first, but the ability exists.
4. **Future-proof.** New logins and services work without a per-service whitelist in the
   execution engine.
5. **One sandbox**, not two.
6. **Simple and cheap** in Claude allowance and review credits.
7. (Implied) **Friedl is not in the loop** for routine diagnosis and repair.

## 4. What the vendors provide

Both reviews agree that the established pattern is *isolated execution + brokered
credentials + recorded evidence + controlled promotion*, and that no vendor ships the
whole loop.

- **Anthropic.** Claude Code's sandbox (Seatbelt on macOS) routes shell traffic through a
  local proxy that checks the destination and can substitute a placeholder credential for
  the real one on the way out. The Agent SDK has permission hooks. Anthropic recommends
  external credential proxies or custom tools for authenticated access. Limits: the proxy
  does not understand the action inside a connection; file tools, MCP servers and hooks
  are not inside the Bash sandbox.
- **OpenAI.** Codex has OS-enforced sandboxing, permission profiles and network controls.
  Its network proxy speaks HTTP and SOCKS; in its limited mode only GET, HEAD and OPTIONS
  pass and other TCP is blocked. This matches what we measured: with
  `--enable network_proxy`, IMAP and everything else were blocked. The Agents API docs
  recommend keeping credentials outside execution environments with injection at a
  trusted proxy; that API is not part of the ChatGPT subscription.
- **Docker Sandboxes** offers local microVMs with a host-side credential proxy (HTTP
  injection only).
- **Replay tools** (for example VCR.py) record HTTP for tests but cannot prove that today's
  service, login or terminal timing works.

The part we must build ourselves is small: a broker that knows the difference between
looking at a service and changing it, for IMAP, REST APIs and terminal programs alike.

## 5. Architecture

Five parts. Three exist today.

| Part | Exists? | What it does |
| --- | --- | --- |
| **Core** | yes | Decides meaning, what to try, and whether a fix worked. Keeps a case file per bug. Commits, pushes, requests review, merges, restarts. |
| **Work room** | replaces two | The one sandbox. Coding sessions, experiments, diagnostic programs and tests run here. |
| **Broker** | new | Holds every login. The only door to real services. Performs *observe* freely and *act* only with authority. Writes evidence and signed run records. |
| **Evidence store** | new | Raw captures, case files, run records and authority records on disk, outside git. |
| **Review gate** | new | Refuses a paid review unless a signed live run of the exact candidate passed. |

Plus one small addition: a **supervisor** outside AL/X that restarts the approved runtime
and keeps crash evidence, because she cannot repair herself while the process that
reasons is down. It has no judgement and no voice.

### 5.1 Core

Core owns interpretation and judgement, unchanged. For each bug it keeps a one-page
**case file**: symptom, evidence references, current hypothesis, what was tried, result,
and its own assessment of what a live run proved and what remains uncertain. The next
reasoning step loads the case file, not the history. Core creates authority records when
an action needs one and runs promotion (commit, push, review, merge, restart) exactly as
today under D-026, D-046 and D-046a.

### 5.2 Work room

One implementation, one lifecycle, a fresh disposable instance per job.

- **May:** run any program; edit the candidate copy of the code; read evidence and
  runtime-data snapshots that Core grants; call the broker; use network through the
  broker.
- **May not:** read the Keychain, `.env` or any credential; write git metadata; hold
  GitHub credentials; write AL/X's live runtime storage; modify the broker or its rules.
- **Candidate copy, not the live checkout.** The job works on a private copy, so a
  half-finished edit can never be what a restart loads.
- **Experiments** get a scratch folder instead of a repository copy. Same room, a
  different writable folder, chosen by Core at launch.
- **AL/X's own verification runs here too.** `pytest` executes repository code, so a fixed
  command list is not a security boundary.
- **On this Mac:** native macOS confinement, starting from the existing coding
  containment, preferably under a dedicated unprivileged macOS user. That separates it
  from the Keychain that holds Claude's and GitHub's logins. **On dedicated hardware:** the
  same interface behind a VM; the broker stays on the host. Mac-specific integrations need
  a macOS guest.

### 5.3 Broker

Ordinary Python that Core starts. The running broker is the last version Friedl
approved, never the file a job just edited. It reads logins from wherever AL/X keeps them
(today `.env`; the Keychain later). Passwords stay in its memory and never appear in
arguments, logs, evidence or model context.

It performs two kinds of operation.

**Observe** is a closed list of mechanical operations that change nothing:

- Run a local program in a pseudo-terminal and keep the raw bytes with timings and input
  events. This covers the Claude usage screen.
- HTTPS GET and HEAD with the matching credential attached by the broker.
- IMAP `EXAMINE`, `SEARCH` and `FETCH` with `BODY.PEEK`, so looking never marks mail
  read. The capture includes the full MIME tree, each part's type, size and encoding, and
  which part the parser chose.
- Further operations a connection note names as safe (§6).

**Act** is everything else: POST, PUT, DELETE, SMTP, IMAP store/delete/move, payments,
IoT commands, webhooks. The broker refuses *act* unless Core presents an authority record
bound to the exact account, operation, parameters and expiry, with replay protection.
Send, delete, post and pay require Friedl's approval in that record. If an operation is
not clearly observe or act, the broker refuses it and returns it to Core as ambiguous. If
an *act* times out with an unknown result, the broker reports the uncertainty and never
retries on its own.

### 5.4 Running the candidate code live

Fetching real data is not enough. The changed code itself must meet the real service,
otherwise the review gate only proves that the old code works. Two forms:

- **Parsers and API clients** (mail, DHL, Xero reads): the broker fetches the real bytes,
  and the candidate code runs on them in the work room. For an API client, the
  candidate's requests go through the broker's observe operations; the broker attaches
  the credential and refuses anything outside the connection note.
- **Interactive programs** (the Claude usage screen and the like): the broker owns the
  real session and pseudo-terminal. The candidate code in the work room drives it through
  the broker, which forwards keystrokes and returns raw output. The candidate never holds
  the login and cannot reach anything else through that session.

### 5.5 Evidence store

A directory on this Mac, outside git, never sent to a reviewer. For every live run:

| Evidence | Kept |
| --- | --- |
| Terminal | Original bytes, chunk boundaries, timestamps, input events, terminal size, locale, program version, exit status |
| HTTP | Request and response metadata, status, body bytes, timing; never authentication material |
| Email | Original MIME bytes and structure |
| Screens | Timestamped captures tied to the session and action |
| Runtime data | Consistent snapshots with schema and code revision |
| Test runs | Candidate digest, environment, command, granted authority, outputs, result |

Originals are never normalised or cleaned. Parsed views are stored beside them. Each
capture gets a local summary: unusual bytes and non-ASCII code points, terminal codes,
MIME tree, status line, and a short window around the anomaly. The summary is what goes
to a model by default (§9). A capture that fails a test becomes a regression fixture in
the repository once secrets and personal content are removed. That is how the live
usage-screen recording became the tests for #170.

### 5.6 Review gate

Before any paid review, a script checks that:

1. law-gates and the required tests passed for the exact candidate;
2. if the change touches code that talks to a service or the broker, a broker-signed live
   run of that exact candidate exists and passed;
3. Core's case file records what that run proves and what it does not.

The gate checks existence, signature and match. It does not judge adequacy; AL/X does.
An inconclusive live run blocks review. A change that touches no service needs no live
run, and Core records why. A service change that cannot be run live stays *unverified*
and needs Friedl's explicit exception before review. The signing key belongs to the
broker; the work room cannot read it, so a job cannot forge a passing run.

## 6. New services: the connection note

Classifying by HTTP method alone is wrong. A GET can spend quota, and a POST can be a
harmless search. Each service therefore gets a short **connection note**:

- the account and environment it represents;
- which operations are observe (allowed freely), with limits on rate and spend;
- which operations are act, and which of those need Friedl;
- what evidence may be kept, and what may be shown to a model.

AL/X can discover a new login, read the service's documentation and draft the note.
Friedl approves it once, and after that it works with no code change in the broker or the
work room. Until a note exists, a new login supports only the generic observe list (§5.3).
This is the honest form of "no per-service whitelist": the execution engine has no
hard-coded service list, and authority over what a service may do is still explicit.

Preference order for any credential: (1) a credential the service itself limits
(read-only account, scoped token); (2) broker-enforced operations; (3) short-lived,
explicitly authorised elevated access.

## 7. Enforced and instructed

| Concern | Enforced by | Instructed only |
| --- | --- | --- |
| Who can reach a real service | Broker is the only process with credentials | |
| Who can see a password | Broker only | |
| Look versus change | Broker's observe list, connection notes, authority records | |
| Push, merge, restart | Work room has no GitHub credentials and no git write; Core's promotion path only | |
| Test before review | Review gate | |
| Broker rules change | Running broker is the approved version; changes need Friedl | |
| What a bug means, what to edit | | Core, from the case file |
| How to write the fix | | The coding agent |

An instruction is a hint. A missing credential, a refused operation or a signature check
is a lock.

## 8. Credentials

- No password, token, cookie or authenticated browser profile is placed in the work room.
- All of AL/X's logins, current and future, live with the broker. No per-login list
  decides who may use them; connection notes decide what may be done with them.
- GitHub credentials are not available to the broker's observe operations either.
  Publishing is Core's promotion path only.
- Measured on 2026-10-21: an empty `GH_CONFIG_DIR` locks out `gh` inside a Codex
  sandbox. Clearing git's credential helper is untested, and the Keychain is shared by
  the macOS user. That is why §5.2 prefers a separate macOS user over environment tricks.

## 9. What reaches model providers

Passwords never do. Content does whenever it is shown to a model. Rule:

- Core and the coding agent receive the **summary** of a capture by default.
- Core may widen what a model sees with a recorded decision when the summary is not
  enough: the bytes around an anomaly, one MIME part.
- Full mail bodies, invoices and personal data stay on this Mac unless widened that way.
- A pull request names the run record that passed; it never attaches screens, mail or
  responses.

## 10. Retention

Proposed, for Friedl's acceptance: routine captures 7 days; captures attached to an open
case file kept until it closes, then 30 days; run records and case-file summaries kept as
part of AL/X's history. Deletion covers derived summaries. Local deletion cannot recall
content already sent to a provider; §9 limits that.

## 11. Fit with the laws and existing decisions

**Law 0.** One production outcome: *AL/X investigates a failure, changes a candidate,
obtains live evidence and advances it through the existing promotion path.* One door to
real services: the broker. The existing providers (`icloud_mail`, `xero`, `dhl`,
`particle`, `claude_usage`) remain the authoritative integrations and move behind the
broker. No parallel "diagnostic" copy of any integration is built. Paths to delete when
replaced:

- the D-027 experiment sandbox (`bootstrap/sandbox.py`, `tools/sandbox.py`,
  `run_sandbox_experiment`, its registrations);
- the separate coding containment as an independent owner (`coding_containment.py`, the
  Codex permission profile), merged into the work room;
- the post-session allowlisted command executor as the thing that counts, once run
  records count;
- direct credential reads in providers, once they go through the broker.

**Law 1.** Core alone interprets. The broker, gate and work room take structured inputs
and do not read Friedl's words or decide meaning. Capture content is external, untrusted
data and is never followed as an instruction.
**Law 2.** The broker and gate are named deterministic sequences, recorded in the
decision.
**Law 3.** Anything ambiguous, including an operation that is neither clearly observe nor
act, or an inconclusive run, returns to Core. Authority records are durable. Case files,
evidence and run records survive restarts.

**Decisions this would amend or replace:** D-027 (replaced by the work room), D-028 and
D-052/D-052a/D-052b (coding containment and storage access move to the work room's rules:
read-only runtime snapshots, no live storage writes), D-020-style deterministic-execution
records for the broker and gate, D-046/D-046a (unchanged allowance; the gate adds a
precondition), D-026 (unchanged; the gate checks evidence, not reviews), D-059 (the usage
reader moves behind the broker).

## 12. Risks

**Accepted:**

- Some services have no truly side-effect-free read. Prefer peek and examine, and record
  it in the connection note where none exists.
- A summary can hide a bug. The raw capture remains, and Core can widen it.
- Live services are flaky. An inconclusive run blocks review rather than passing.
- Native macOS confinement shares a kernel. Accepted on the development Mac; a VM on
  dedicated hardware.
- Vendor sandbox flags will keep changing. Safety lives in the broker and the account
  boundary, not in those flags.

**Not accepted:**

- the coding agent holding credentials or reading the Keychain;
- the coding agent pushing, merging or restarting;
- send, delete, post or pay without Friedl's recorded approval;
- "test before review" as an instruction only, or any flag that skips the gate;
- a job editing the running broker or its rules;
- evidence, mail or secrets in the repository or sent to a reviewer;
- unreviewed code replacing the running runtime.

**To watch over the next year:** prompt injection through mail, web pages or terminal
banners (the broker never acts on text it fetched; send and pay still wait for Friedl);
stale fixtures; CLI redraw changes; credential-bearing screenshots; dependency install
scripts; ungoverned MCP connections; privilege creep through broker rule changes. Pin the
relevant tool versions, and run a small boundary test when any of them changes.

## 13. Cost

- Live runs and summaries are local Python, so they cost no tokens.
- Core reads the case file, not a replay of the whole session.
- The coding session is kept across investigation steps. Targeted tests run while
  editing; the full suite runs once per final candidate.
- One Greptile review, after a live run has passed.
- Measure first: Core's ~55,000-token context per step should be broken down into fresh,
  cached and output tokens before more infrastructure is added (separate work).

## 14. Build order

Each phase ends with a demonstration on a real case. Stop after each phase and use it on
the next real failure before building more.

1. **Raw capture.** The usage reader already returns `raw_output_tail` (#170). Extend this
   to full timed captures with input events, stored in the evidence store, with a summary.
   *Done when* a deliberately broken renderer's failure is diagnosable from the capture
   alone.
2. **Broker, observe only.** Socket API; operations: pseudo-terminal session, IMAP peek
   with MIME tree, HTTPS GET. *Act* returns "needs authority". The work room calls it.
   *Done when* the DigiKey "." email is diagnosed and fixed by AL/X from a broker capture,
   with no outside help.
3. **Review gate and case files.** *Done when* a review request on a service change
   without a signed passing run is refused, and one with it goes through.
4. **One work room.** Merge the experiment sandbox into it and delete D-027's paths. Run
   AL/X's verification inside it. Separate macOS user. *Done when* a scratch experiment
   and a coding job both run in it, and the old sandbox is gone.
5. **Act with authority.** Friedl's approval flow for send, delete, post and pay;
   connection notes for mail, Xero, DHL and Particle. Built before the first bug that is
   actually in a send or payment path.
6. **Supervisor.** Restart and crash evidence.
7. **Dedicated hardware.** The same interfaces behind a VM.

Before any phase grants live access, prove by test that candidate code cannot read
credentials, change production state, publish to GitHub, alter broker rules or forge a
run record.

## 15. Open questions for Friedl

1. Approve the direction (broker, one work room, review gate) as D-060?
2. Retention: 7 days routine, 30 days after an incident closes?
3. Who builds phases 1–3: Claude Code (the broker cannot be tested by AL/X before it
   exists), AL/X, or Claude Code for the broker with AL/X doing the rest?
4. A separate macOS user account for the work room on this Mac?
5. Start after the weekly allowance resets on 2026-10-26?

Before implementation, verify the vendor features cited in §4 against the installed
Claude Code and Codex versions, and verify that each lock in §7 holds, by test.

## 16. Considered and rejected

- **Give the coding agent every login and network.** Puts secrets into the agent's
  context and from there with the model provider; "don't send" becomes an instruction,
  not a lock. Claude Code's own safety classifier refused to build it.
- **Codex per-domain network rules.** Proxy-based; blocks IMAP and every non-HTTP
  protocol; cannot tell a fetch from a delete on an allowed host.
- **Trial run with a fixed list of trial-able services** (AL/X's D-060 draft). A
  per-service list returns us here with every new login, and it needed a one-service
  network room that does not exist.
- **Replay-only testing.** Cannot catch today's failures. Recorded captures remain useful
  as regression fixtures after a live run has passed.
