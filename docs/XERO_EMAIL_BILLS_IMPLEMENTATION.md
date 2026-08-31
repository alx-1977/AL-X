# Xero Email Bills — Implementation Evidence

**Status:** Implemented under D-016, D-017, D-018, D-019 and D-020. Real bills
have been created, attached and authorised in the live FireFli organisation.
Bill writes run unattended under D-018; only discarding a draft still asks.

## Goal and scope

AL/X can search historical supplier mail by structured criteria, traverse
bounded nested ZIP attachments, prepare an accounts-payable bill, create or
update it as a Xero draft, attach exact source documents, authorise the matching
draft, and read the result back. The specialised analysis preserves V1's useful
DHL behaviour: analyse a Customs Worksheet plus its matching SAD 500 before the
invoice arrives, create a provisional proposal, and later reconcile the MyBill
GDB CSV against the same evidence while refusing unknown or inconsistent money.

This does not add general Xero conversation, contact mutation, payments, bank
reconciliation, sales invoices, quotes, purchase orders, journals, reports or
payroll.

## Architecture boundaries

- Raw person language still enters only the Conversation Gateway and Core.
- Mail, DHL and Xero receive structured identifiers and values only.
- Mail search, attachment listing and attachment reading are reusable
  primitives. Search selects the mailbox read-only, uses `BODY.PEEK`, and does
  not change observation state. It fetches headers plus IMAP `BODYSTRUCTURE`,
  not message bodies or attachment payloads. Attachment text
  is transient; stable metadata is retained under D-013 provenance.
- Nested ZIP members are exposed as bounded virtual attachments. The archive is never
  extracted to disk, path components are discarded, and member count and byte
  limits prevent an unbounded archive expansion.
- DHL customs analysis and reconciliation are deterministic document-analysis
  primitives with no Xero or conversational authority.
- Xero lookup, draft creation/update, source attachment, authorisation and read-back
  remain separate primitives. Every result returns to the Core.
- OAuth state and tokens are durable across restart. Tokens are encrypted with
  an independent local key restricted to the current user; refresh rotation is
  serialised across callers by the SQLite transaction.

## Safety and authority

D-016 records Friedl's product and deployment decision, and D-018 replaced the
per-write approval with configured standing authority for supplier bills.
Discarding a draft keeps its own permission and asks by default.
The draft primitives refuse unbalanced lines, account/tax pairs absent from the
live configured organisation, and an existing supplier/invoice pair. Duplicate
lookup uses Xero's documented `InvoiceNumbers` and `ContactIDs` collection
filters. Authorisation refuses a missing bill, a non-draft, a changed invoice
number or total, and any explicitly required attachment whose bytes cannot be
read back from Xero with the approved SHA-256 digest. `HasAttachments` alone is
not proof. Malformed money returned by Xero fails closed. Xero acceptance does
not prove completion; the separate read primitive exists for verification.

Production Xero writes have since been made: two supplier bills were created,
attached, authorised and read back, and their source mail filed.

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

## V1 fixture and archive evidence

The original documents are tracked in the private V1 `JARVIS` repository;
`AL-X` is public, so they are deliberately not copied here. Four deterministic,
sanitized PDFs are committed under `tests/fixtures/dhl/`: a Customs Worksheet
and SAD 500 for each observed declaration. They retain only the identifiers and
totals below and reproduce the relevant Crystal Reports text-run geometry,
including the VAT value appearing on the neighbouring `TOTAL 12B/Ad Val.` run.
The committed tests parse those PDFs and assert every value.

The production parser was also exercised read-only against both retained V1
worksheet fixtures:

- SHA-256 `05645ee75f300b3b81e5be4ce9d1a3ab2eac9a65e996997cfa96535e52f56b8e`
  → declaration `DFM202604215028901`, waybill `8339567983`, duty `15.60`, VAT
  `1100.55`, total `1116.15`.
- SHA-256 `7ae9f4cc3a7235db8104b076f8a7bf3c2bb02d14126dfde52abf9f1a2e05e1e6`
  → declaration `DFM202607195025382`, waybill `7096903730`, duty `38.25`, VAT
  `168.75`, total `207.00`.

The retained V1 DHL archive set was inspected without extraction to disk. Its
nested paths expose 16 relevant occurrences comprising eight byte-unique
documents: four worksheet files and four SAD 500 files, representing two
declarations. A local-only automated inventory test asserts all eight private
SHA-256 values, all 16 occurrence counts, and every parsed declaration whenever
the sibling V1 checkout is present. CI runs the sanitized layout fixtures. This
distinction is explicit: CI proves the parser against the relevant layouts and
values, while the private-source hash check is reproducible only on a machine
with authorised access to V1. Unrelated and malformed PDF members are refused.
The evidence also confirmed that SAD 500 identity must be matched by declaration
number rather than by the first 10-digit value in the document.

## Known limits

- Text-layer PDFs are supported. Scanned image-only invoices need a separately
  reviewed OCR/document-vision capability.
- A missing Xero contact blocks rather than creating one; contact mutation is
  outside D-016.
- The customs-first primitive requires one Customs Worksheet and its matching
  SAD 500. It proposes V1's `DHL-WAYBILL-<waybill>` provisional identifier; an
  independently approved Xero draft call creates it, and a later independently
  approved update call replaces the same draft with the final MyBill values.
- The Core remains responsible for attaching every required DHL document,
  including invoice, worksheet and SAD 500, and supplies each exact filename
  and digest to authorisation. No provider privately sequences that workflow.
- The initial OAuth connection requires running `scripts/connect_xero.py` and
  completing Xero's browser consent. This is authentication, not a second
  conversational path.

## Change evidence

- **Primitives added:** structured mail search; list/read nested mail
  attachments; analyse customs-first DHL evidence; reconcile final DHL import
  documents; search Xero contacts; list accounts and tax rates; find/read an AP
  bill; create/update a draft AP bill; attach an exact mail document; authorise
  an AP bill.
- **New primitive justification:** mail attachments and Xero accounting effects
  are external capabilities AL/X did not possess.
- **Raw-language flow:** person → Conversation Gateway → Core → structured
  capability call. No tool or provider accepts a transcript, prompt or intent.
- **Exceptions:** none.
- **Unresolved evidence:** the private original PDFs cannot run in public CI;
  their exact hash inventory runs locally and sanitized layout equivalents run
  in CI. Real Xero OAuth consent and a non-destructive live read remain to be
  performed before any approved real bill write.
