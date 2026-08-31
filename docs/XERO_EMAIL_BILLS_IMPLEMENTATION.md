# Xero Email Bills — Implementation Evidence

**Status:** Implemented locally under D-016; production OAuth connection and
real Xero writes have not been exercised in this change.

## Goal and scope

AL/X can inspect supplier-invoice attachments received through the existing
iCloud mail capability, prepare an accounts-payable bill, create it as a Xero
draft, attach the exact source document, authorise the matching draft, and read
the result back. The first specialised analysis preserves V1's useful DHL
behaviour: parse a MyBill GDB CSV, parse the positional totals in SARS Customs
Worksheets, reconcile duty, import VAT and DHL service charges, and refuse
unknown or inconsistent money.

This does not add general Xero conversation, contact mutation, payments, bank
reconciliation, sales invoices, quotes, purchase orders, journals, reports or
payroll.

## Architecture boundaries

- Raw person language still enters only the Conversation Gateway and Core.
- Mail, DHL and Xero receive structured identifiers and values only.
- Mail attachment listing and reading are reusable primitives. Attachment text
  is transient; stable metadata is retained under D-013 provenance.
- ZIP members are exposed as bounded virtual attachments. The archive is never
  extracted to disk, path components are discarded, and member count and byte
  limits prevent an unbounded archive expansion.
- DHL reconciliation is a deterministic document-analysis primitive with no
  Xero or conversational authority.
- Xero lookup, draft creation, source attachment, authorisation and read-back
  remain separate primitives. Every result returns to the Core.
- OAuth state and tokens are durable across restart. Tokens are encrypted with
  an independent local key restricted to the current user; refresh rotation is
  serialised across callers by the SQLite transaction.

## Safety and authority

D-016 records Friedl's product and deployment decision. Every Xero write still
requires an exact, expiring approval. No unattended standing authority exists.
The draft primitive refuses unbalanced lines and an existing supplier/invoice
pair. Authorisation refuses a missing bill, a non-draft, a changed invoice
number or total, and a bill without a supporting attachment. Xero acceptance
does not prove completion; the separate read primitive exists for verification.

No production Xero request was made while implementing or testing this slice.

## Goal state and restart behaviour

The existing Core checkpoints every effectful call before dispatch. A
Xero-specific restart test creates an approved draft, closes and reopens the
goal store, proves the draft result remains in the active goal, and drives a
separate read-back result through the Core before AL/X responds. OAuth state
and encrypted tokens have independent restart tests, including concurrent
single-use refresh-token rotation.

## Privacy and retention

Mail body and attachment text remain transient provider values. Mail-derived
metadata, extracted accounting facts and DHL proposals carry the existing
30-day D-013 provenance deadline. Raw attachment bytes are streamed from the
identified message to Xero only for an exact, hash-bound approved attachment
call and are not written to an AL/X artifact directory.

Scheduled enforcement of those expiry dates remains the explicit item in
`TODO.md`; this change does not activate deletion or perform a purge.

## Known limits

- Text-layer PDFs are supported. Scanned image-only invoices need a separately
  reviewed OCR/document-vision capability.
- A missing Xero contact blocks rather than creating one; contact mutation is
  outside D-016.
- DHL reconciliation currently waits until the invoice CSV and all relevant
  Customs Worksheets are available. It does not create V1's provisional
  worksheet-only draft.
- At least one source document must be attached before authorisation. The Core
  remains responsible for attaching every relevant DHL document, including the
  invoice, worksheet and SAD 500, before claiming the goal complete.
- The initial OAuth connection requires running `scripts/connect_xero.py` and
  completing Xero's browser consent. This is authentication, not a second
  conversational path.

## Change evidence

- **Primitives added:** list/read mail attachments; reconcile DHL import
  documents; search Xero contacts; list accounts and tax rates; find/read an AP
  bill; create a draft AP bill; attach an exact mail document; authorise an AP
  bill.
- **New primitive justification:** mail attachments and Xero accounting effects
  are external capabilities AL/X did not possess.
- **Raw-language flow:** person → Conversation Gateway → Core → structured
  capability call. No tool or provider accepts a transcript, prompt or intent.
- **Exceptions:** none.
- **Unresolved evidence:** real Xero OAuth consent and a non-destructive live
  read remain to be performed before any approved real bill write.
