# Xero Email Bills — Implementation Evidence

**Status:** Implemented under D-016 through D-022. Real ordinary supplier bills
have been created, attached and authorised in the live FireFli organisation.
No DHL bill has yet been written live. Bill writes run unattended under D-018;
only discarding a draft still asks.

## Goal and scope

AL/X can search historical supplier mail by structured criteria, traverse
bounded nested ZIP attachments, prepare an accounts-payable bill, create or
update it as a Xero draft, attach exact source documents, authorise the matching
draft, and read the result back. The specialised analysis preserves V1's useful
DHL behaviour as one two-stage capability: a Customs Worksheet plus its
matching SAD 500 arriving before the invoice draft a provisional bill for the
duty and VAT SARS assessed, and the DHL invoice that follows completes that
same bill in place. A duty-tax-paid invoice is self-reconciled from its MyBill
CSV and posted directly to Import/Export Fees; freight is recognised and
returned unposted. Which branch runs is decided by the documents themselves.

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
- A DHL import is one capability, `process_dhl_import`, authorised as a
  deterministic sequence by D-021/D-022 and holding every DHL bill branch.
  Its branch is chosen from document evidence, never from wording. The
  DHL supplier is configuration (`ALX_XERO_DHL_SUPPLIER_NAME`, resolved to a
  contact by exact name at run time), so the capability
  takes no contact argument and a wrong supplier cannot be handed to it. That
  contact and the three account codes are all verified against the live
  organisation before any write: a contact absent from this organisation, or
  archived, refuses with `contact_not_found`. An ordinary supplier bill is posted by `capture_supplier_invoice`,
  which refuses a DHL document rather than flattening duty, import VAT and
  clearance onto one default account.
- Law 0: each production outcome has exactly one path. The five granular write
  capabilities that could each independently reach a posted bill
  (`execute_xero_bill`, `create_xero_draft_bill`, `update_xero_draft_bill`,
  `attach_mail_document_to_xero_bill`, `authorise_xero_bill`) and the two
  earlier DHL primitives (`analyze_dhl_customs_documents`,
  `reconcile_dhl_import_documents`) are deleted, not withheld. Their general
  proposal objects and entry points — `reconcile`, `_parse_invoices`,
  `Charge`, and `Shipment` — remain deleted. D-022 adds only a structured
  evidence reader used internally by the surviving capability; it cannot write
  or independently achieve the production outcome. Their steps are
  private implementation inside the surviving capability. Xero lookup and
  read-back remain separate non-effectful primitives. Every result returns to
  the Core.
- OAuth state and tokens are durable across restart. Tokens are encrypted with
  an independent local key restricted to the current user; refresh rotation is
  serialised across callers by the SQLite transaction.

## Evidence is re-verified, never assumed

Because the sequence runs unattended, neither stage trusts stored Xero state as
a proxy for the documents it came from. Three defects found in review are now
covered by tests:

- A resumed provisional draft is checked against the customs evidence — the
  supplier, currency and both customs lines — not merely its status and total.
  A draft edited elsewhere returns `draft_changed` instead of completing.
- The invoice stage re-reads the customs worksheet and SAD 500 *from the bill's
  own attachments* and re-derives duty and import VAT from them. A bill whose
  evidence was deleted or replaced returns `customs_evidence_missing` and is
  never authorised. Every required document is verified byte-for-byte on the
  bill immediately before authorisation.
- The invoice is attached before the bill is renamed, so a failure mid-sequence
  leaves it answering to its provisional number and a later run recovers it.
  Renaming first stranded a bill neither stage could find.
- Where a run failed after the rename — an authorisation Xero rejected — the
  bill no longer answers to its provisional number. A later run finds it by the
  invoice number it now carries and finishes that same bill, rather than
  abandoning it or creating a second. Only a DRAFT is recovered this way, so an
  authorised import is never processed twice.
- Xero accepting an update is not proof of what it stored. The bill is read
  back fresh and compared against the exact payload that was sent — supplier,
  invoice number, currency, `LineAmountTypes`, both dates, the line count, and
  every line's description, quantity, unit amount, account code and tax amount
  in order — *before* the irreversible authorisation. The comparison is
  fail-closed: a blank or absent value is a difference, never an acceptable
  absence. An earlier version summed line amounts by account, which hid a bill
  whose per-line values had all been replaced, and tolerated a missing currency
  and a changed `LineAmountTypes` — the latter changing how Xero reads every
  amount without changing any number.
- A duty-tax-paid draft is compared to the exact payload before and after its
  PDF is attached, then read back again after authorisation. A mutation during
  attachment returns `draft_changed`; it never reaches authorisation.

Dates are stated, never guessed. The provisional bill is dated from the
assessment date SARS encodes in the declaration number; an identifier carrying
no real date refuses rather than sending an empty date to Xero. The completed
bill uses the invoice's own date, and an invoice stating none is refused —
substituting the due date would recreate V1's date-guessing defect. Both the
ISO and the DD/MM/YYYY forms DHL South Africa uses are read.

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

Production Xero writes have since been made for two ordinary supplier bills:
both were created, attached, authorised and read back, and their source mail
filed. DHL writes remain fixture/fake-Xero proven only.

## Goal state and restart behaviour

The existing Core checkpoints every effectful call before dispatch. A
Xero-specific restart test drives an approved `capture_supplier_invoice`
through the Core, closes and reopens the goal store, proves the capture result
remains in the active goal, and drives a separate read-back result through the
Core before AL/X responds. A DHL import is covered by `RestartContinuityTests`,
which models a restart by discarding every in-process object and building a
fresh analyzer, adapter and executor over the same Xero organisation: the
invoice stage resumes the provisional draft in the new process, a repeated
customs stage resumes the one draft rather than creating a second, and an
import interrupted by a failed attachment completes on a later run. OAuth state
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
`AL-X` is public, so they are deliberately not copied here. Five deterministic
sanitized PDFs and three sanitized MyBill CSVs are committed under
`tests/fixtures/dhl/`: a Customs Worksheet
and SAD 500 for each observed declaration, plus a sanitized MyBill invoice
(`invoice_cptir_sanitized.pdf`) in the same Crystal Reports geometry, whose
parsed values — `CPTIR00273840`, waybill `1921099471`, total `508.76`, invoice
date `2026-08-24`, due `2026-09-07` — are the sanitized equivalents of the live
invoice and are asserted by the committed tests. They retain only the identifiers and
totals below and reproduce the relevant Crystal Reports text-run geometry,
including the VAT value appearing on the neighbouring `TOTAL 12B/Ad Val.` run.
The committed tests parse those PDFs and CSVs and assert every value and all
three classifications. The duty-tax-paid fixture preserves the verified
breakdown 136.55 + 22.21 + 350.00 = 508.76; the parser reads each net `XCn
Total`, never the pre-discount gross charge.

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
- The customs stage requires one Customs Worksheet and its matching SAD 500,
  and drafts V1's `DHL-WAYBILL-<waybill>` provisional bill. The invoice stage
  finds that same draft by the same identifier, completes it in place and
  authorises it. A customs invoice with no matching draft returns to AL/X
  rather than creating a second bill. A duty-tax-paid invoice needs no customs
  draft: one reconciled CSV plus its matching PDF creates one bill, with each
  component on account 426 and NoTax. Any stated tax, unknown charge kind,
  mismatch, or imbalance refuses before authorisation. Freight returns
  unposted under D-022.
- Clearance is never parsed from the invoice. It is the invoice total less the
  duty and import VAT already verified from the customs documents, so it
  reconciles exactly instead of guessing charge-code layouts.
- Import VAT is claimable and customs duty is not, so they never merge; DHL
  charges no VAT of its own, so every line posts NoTax. The three accounts are
  configuration (`ALX_XERO_IMPORT_VAT_ACCOUNT`, `ALX_XERO_CUSTOMS_DUTY_ACCOUNT`,
  `ALX_XERO_CLEARANCE_ACCOUNT`), defaulting to V1's 820, 426 and 425.
- The initial OAuth connection requires running `scripts/connect_xero.py` and
  completing Xero's browser consent. This is authentication, not a second
  conversational path.

## Change evidence

- **Primitives added:** structured mail search; list/read nested mail
  attachments; process a DHL import (both stages); capture a supplier invoice;
  search Xero contacts; list accounts and tax rates; find/read an AP bill;
  discard a draft AP bill.
- **Superseded paths deleted:** `execute_xero_bill`, `create_xero_draft_bill`,
  `update_xero_draft_bill`, `attach_mail_document_to_xero_bill`,
  `authorise_xero_bill`, `analyze_dhl_customs_documents` and
  `reconcile_dhl_import_documents`, together with the `RECOVERY_ONLY_CAPABILITIES`
  register and `XeroRuntime.recovery_definitions` that kept them dispatchable.
  `tests/test_single_production_path.py` asserts their absence from the
  registry, executors, policies and source, and its mutation tests prove the
  suite fails when one is restored.
- **New primitive justification:** mail attachments and Xero accounting effects
  are external capabilities AL/X did not possess.
- **DHL production outcome:** `process_dhl_import` is the sole effectful path.
  Its provider analyzer only classifies and extracts structured evidence and
  has no Xero dependency or write surface. No new capability was added for
  duty-tax-paid invoices.
- **Raw-language flow:** person → Conversation Gateway → Core → structured
  capability call. No tool or provider accepts a transcript, prompt or intent.
- **Restart and failure behaviour:** customs restart coverage remains; the
  duty-tax-paid branch resumes the same DRAFT after an attachment or
  authorisation failure and refuses any changed draft before retrying.
- **Safety/approval:** D-018 standing supplier-bill authority is unchanged.
  D-022 fixes account 426 and NoTax; the live account must be ACTIVE with
  `TaxType=NONE` before any write. Freight has no write authority.
- **Verification:** 611 tests pass; `check_architecture.py`,
  `check_governance.py`, and `git diff --check` pass. No exception requested.
- **Unresolved evidence:** no DHL bill has been posted to the live organisation;
  the first real DHL write still requires observation and read-back verification.
- **Goal state and restart:** unchanged Core checkpointing. Capture survives a
  goal-store restart and returns to the Core for read-back. The DHL stages have
  restart coverage: `RestartContinuityTests` discards every in-process object
  and rebuilds the analyzer, adapter and executor over the same organisation,
  proving the invoice stage resumes, the customs stage resumes one draft, and a
  restart after a failed attachment still recovers.
- **Tests added and gates run:** `tests/test_single_production_path.py` (11
  tests: superseded-path absence, CSV-implementation deletion, and Law 0
  mutation tests); and in `tests/test_dhl_reconciliation.py`
  `DhlImportLifecycleTests`, `ConfiguredSupplierAndAccountsTests`,
  `TamperedDraftTests`, `CustomsEvidenceOnTheBillTests`,
  `PartialFailureRecoveryTests`, `StoredUpdateVerificationTests` and
  `RestartContinuityTests`. Full suite 584 tests passing; `scripts/check_governance.py` and
  `scripts/check_architecture.py` both pass.
- **Exceptions:** none.
- **Unresolved evidence:** the private original PDFs cannot run in public CI;
  their exact hash inventory runs locally and sanitized layout equivalents run
  in CI. The two-stage DHL lifecycle is proved against committed fixtures and a
  fake Xero only. No DHL import has yet been posted to the live organisation, so
  the invoice-stage read-back and the provisional-to-final transition remain
  unproven against real Xero responses, and the configured supplier name has
  not been resolved against the live FireFli organisation.
