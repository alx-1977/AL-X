"""The one production path for a DHL import: customs documents, then invoice.

A DHL import arrives in two parts. The customs documents come first, carrying
the duty and VAT SARS assessed, and a provisional bill is drafted from them
because DHL's own charges are not yet known. The invoice follows days later,
and completes that same bill in place.

Those are two stages of one capability and one evolving Xero bill, not two
capabilities or alternative routes. Which branch runs is decided by the
documents themselves: customs evidence begins, its invoice completes; a
duty-tax-paid invoice posts on its own; freight returns to AL/X unposted.

The accounting treatment is V1's, proven in production: import VAT is claimable
and duty is not, so they never merge; DHL charges no VAT of its own, so every
line is posted NoTax; and clearance is never re-parsed from the invoice but
derived as the invoice total less the duty and VAT already verified from the
customs documents.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from alx.contracts import (
    CapabilityDefinition,
    CapabilityResult,
    CapabilityResultState,
    DhlDocumentError,
    DhlImportAnalyzer,
    MailAccessError,
    MailAccount,
    MailReference,
    SideEffect,
    StructuredData,
    StructuredSchema,
    ValueKind,
    XeroAccessError,
    XeroAccountingAccount,
    xero_date,
)
from alx.contracts.provenance import RetentionPolicy
from alx.specialists import resolve_supplier


PROCESS_DHL_IMPORT = "process_dhl_import"

TOLERANCE = Decimal("0.01")

_STRING = StructuredSchema(ValueKind.STRING)
_BOOLEAN = StructuredSchema(ValueKind.BOOLEAN)
_ANY_OBJECT = StructuredSchema(ValueKind.OBJECT)

_SOURCE = StructuredSchema(
    ValueKind.OBJECT,
    {
        "mailbox_id": _STRING,
        "uid_validity": _STRING,
        "uid": _STRING,
        "attachment_id": _STRING,
        "expected_sha256": _STRING,
    },
    ("mailbox_id", "uid_validity", "uid", "attachment_id", "expected_sha256"),
    extra_properties=False,
)

DEFINITION = CapabilityDefinition(
    PROCESS_DHL_IMPORT,
    "Process the exact DHL documents supplied: customs evidence and its invoice form one two-stage import; a reconciled duty-tax-paid invoice posts directly; freight returns unposted. The branch follows from document evidence.",
    StructuredSchema(
        ValueKind.OBJECT,
        {"documents": StructuredSchema(ValueKind.ARRAY, items=_SOURCE)},
        ("documents",),
        extra_properties=False,
    ),
    StructuredSchema(
        ValueKind.OBJECT,
        {
            "completed": _BOOLEAN,
            "stage": _STRING,
            "returned_for": _STRING,
            "detail": _STRING,
            "waybill": _STRING,
            "bill": _ANY_OBJECT,
            "attached": StructuredSchema(ValueKind.ARRAY, items=_STRING),
            "steps": StructuredSchema(ValueKind.ARRAY, items=_STRING),
        },
        (
            "completed",
            "stage",
            "returned_for",
            "detail",
            "waybill",
            "bill",
            "attached",
            "steps",
        ),
        extra_properties=False,
    ),
    SideEffect.EFFECTFUL,
    (
        "arguments_unusable",
        "attachment_unavailable",
        "source_mismatch",
        "connection_failed",
        "not_connected",
        "permission_denied",
        "rate_limited",
        "request_rejected",
        "response_invalid",
        "bill_not_found",
        "bill_not_draft",
        "duplicate_found",
        "account_mapping_invalid",
        "supporting_document_missing",
        "supporting_document_mismatch",
        "not_a_dhl_document",
        "documents_ambiguous",
        "invoice_number_missing",
        "waybill_missing",
        "invoice_total_missing",
        "invoice_currency_missing",
        "invoice_date_missing",
        "invoice_date_invalid",
        "invoice_date_ambiguous",
        "invoice_amount_invalid",
        "invoice_format_invalid",
        "invoice_too_many_rows",
        "worksheet_identity_ambiguous",
        "worksheet_total_missing",
        "not_customs_worksheet",
        "sad500_identity_ambiguous",
        "worksheet_pdf_invalid",
        "worksheet_too_large",
        "dhl_supplier_not_configured",
        "contact_not_found",
    ),
)

DEFINITIONS = (DEFINITION,)


def _required(values: Mapping[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name)
    return value.strip()


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation as error:
        raise ValueError("amount") from error
    if not amount.is_finite():
        raise ValueError("amount")
    return amount


def _agrees(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= TOLERANCE


def provisional_number(waybill: str) -> str:
    """V1's identifier for a bill drafted before its invoice exists."""
    return f"DHL-WAYBILL-{waybill}"


def build_dhl_executors(
    mail: MailAccount,
    analyzer: DhlImportAnalyzer,
    account: XeroAccountingAccount,
    call_id_source: Callable[[], str],
    import_vat_account: str,
    customs_duty_account: str,
    clearance_account: str,
    supplier_name: str = "",
    clock: Callable[[], datetime] | None = None,
) -> Mapping[str, Callable[[StructuredData], CapabilityResult]]:
    """The one DHL import path.

    D-021: the supplier is configuration, not a parameter, so a wrong contact
    cannot be supplied to this capability. DHL is resolved by its exact
    supplier name against the live organisation, as V1 did, so no Xero
    identifier has to be discovered or configured by hand. The account codes
    are verified the same way before any write.
    """
    now = clock or (lambda: datetime.now(UTC))

    def failed(code: str) -> CapabilityResult:
        return CapabilityResult(
            call_id_source(),
            PROCESS_DHL_IMPORT,
            CapabilityResultState.FAILED,
            failure={"code": code},
        )

    def returned(
        stage: str,
        reason: str,
        detail: str,
        waybill: str = "",
        bill: Mapping[str, Any] | None = None,
        steps: Sequence[str] = (),
        references: Sequence[MailReference] = (),
    ) -> CapabilityResult:
        return CapabilityResult(
            call_id_source(),
            PROCESS_DHL_IMPORT,
            CapabilityResultState.SUCCEEDED,
            {
                "completed": False,
                "stage": stage,
                "returned_for": reason,
                "detail": detail,
                "waybill": waybill,
                "bill": dict(bill or {}),
                "attached": (),
                "steps": tuple(steps),
            },
            provenance=(
                RetentionPolicy().direct_mail(now(), tuple(references))
                if references
                else None
            ),
        )

    def _validate_configuration() -> str:
        """The failure code for an unusable configuration, or "" if it is good.

        D-021 requires the configured supplier and the three account codes to
        be verified against the live organisation before any write, the same
        validation `capture_supplier_invoice` applies to its own coding.
        """
        accounts = {
            str(item.get("Code") or ""): item
            for item in account.list_accounts()
            if str(item.get("Status") or "").upper() == "ACTIVE"
        }
        for label, code in (
            ("import VAT", import_vat_account),
            ("customs duty", customs_duty_account),
            ("clearance", clearance_account),
        ):
            if code not in accounts:
                return "account_mapping_invalid"
        duty_account = accounts[customs_duty_account]
        if str(duty_account.get("TaxType") or "").upper() != "NONE":
            return "account_mapping_invalid"
        return ""

    def _resolve_contact() -> tuple[str, str]:
        """DHL's ContactID from its exact supplier name, or a failure code.

        V1 identified DHL by name and never held an identifier. The shared
        resolver is reused unchanged: it matches only the supplier's own name
        among active contacts and refuses to settle for whatever else a search
        returned, which is what stops a bill posting against another company.
        """
        resolved = resolve_supplier(
            account.search_contacts(supplier_name), supplier_name
        )
        if not resolved["resolved"]:
            return "", "contact_not_found"
        return str(resolved["contact_id"]), ""

    def _read(source: Mapping[str, Any]) -> tuple[MailReference, Any, bytes]:
        reference = MailReference(
            _required(source, "mailbox_id"),
            _required(source, "uid_validity"),
            _required(source, "uid"),
        )
        attachment, payload = mail.read_attachment(
            reference, _required(source, "attachment_id")
        )
        if attachment.sha256 != _required(source, "expected_sha256"):
            raise DhlDocumentError("source_mismatch")
        return reference, attachment, payload

    def _line(description: str, amount: Decimal, account_code: str) -> dict[str, Any]:
        return {
            "Description": description,
            "Quantity": 1.0,
            "UnitAmount": float(amount),
            "AccountCode": account_code,
            # DHL charges no VAT of its own on these: the import VAT is SARS's,
            # already stated on the SAD 500, and is claimed as a whole line.
            "TaxAmount": 0.0,
        }

    def _verified_attachment(invoice_id: str, filename: str, digest: str) -> bool:
        for item in account.list_bill_attachments(invoice_id):
            if str(item.get("FileName") or "") != filename:
                continue
            payload = account.read_bill_attachment(
                invoice_id,
                str(item.get("AttachmentID") or ""),
                str(item.get("MimeType") or ""),
            )
            if hashlib.sha256(payload).hexdigest() == digest:
                return True
        return False

    def _attach(invoice_id: str, attachment: Any, payload: bytes) -> None:
        stored = {
            str(item.get("FileName") or "")
            for item in account.list_bill_attachments(invoice_id)
        }
        if attachment.filename not in stored:
            account.attach_bill_document(
                invoice_id, attachment.filename, attachment.media_type, payload
            )
        if not _verified_attachment(invoice_id, attachment.filename, attachment.sha256):
            raise DhlDocumentError("supporting_document_mismatch")

    def _expected_lines(
        waybill: str, duty: Decimal, vat: Decimal
    ) -> list[dict[str, Any]]:
        """The provisional bill's lines, derived only from customs evidence."""
        lines = []
        if vat > 0:
            lines.append(
                _line(
                    f"Import VAT (claimable, per SAD 500) — waybill {waybill}",
                    vat,
                    import_vat_account,
                )
            )
        if duty > 0:
            lines.append(
                _line(
                    f"Customs duty (not claimable) — waybill {waybill}",
                    duty,
                    customs_duty_account,
                )
            )
        return lines

    def _differs_from_sent(stored: Mapping[str, Any], sent: Mapping[str, Any]) -> str:
        """Why the bill Xero stored is not the one that was sent, or "".

        Exact and fail-closed. Every field the update set is compared to what
        came back, including each line's description, quantity, unit amount,
        account and tax amount in order. Summing amounts by account hid a bill
        whose per-line values had been replaced; a missing value is a
        difference, never an acceptable absence.
        """
        stored_contact = stored.get("Contact")
        stored_contact_id = (
            str(stored_contact.get("ContactID") or "")
            if isinstance(stored_contact, Mapping)
            else ""
        )
        if stored_contact_id != str(sent["Contact"]["ContactID"]):
            return f"its supplier is {stored_contact_id or 'unset'!r}"
        if str(stored.get("Type") or "") != str(sent["Type"]):
            return f"its type is {stored.get('Type') or 'unset'!r}"
        if str(stored.get("InvoiceNumber") or "") != str(sent["InvoiceNumber"]):
            return f"its number is {stored.get('InvoiceNumber') or 'unset'!r}"
        if str(stored.get("Reference") or "") != str(sent["Reference"]):
            return f"its reference is {stored.get('Reference') or 'unset'!r}"
        # A blank currency is a difference, not an acceptable absence.
        if str(stored.get("CurrencyCode") or "") != str(sent["CurrencyCode"]):
            return f"its currency is {stored.get('CurrencyCode') or 'unset'!r}"
        # Inclusive or Exclusive would make Xero read these amounts as
        # tax-bearing, changing what is posted without changing any number.
        if str(stored.get("LineAmountTypes") or "") != str(sent["LineAmountTypes"]):
            return (
                f"its line amount type is "
                f"{stored.get('LineAmountTypes') or 'unset'!r}"
            )
        if xero_date(stored.get("Date")) != sent["Date"]:
            return f"its date is {xero_date(stored.get('Date'))!r}"
        if xero_date(stored.get("DueDate")) != sent["DueDate"]:
            return f"its due date is {xero_date(stored.get('DueDate'))!r}"

        stored_lines = [
            line for line in stored.get("LineItems") or () if isinstance(line, Mapping)
        ]
        sent_lines = list(sent["LineItems"])
        if len(stored_lines) != len(sent_lines):
            return f"it carries {len(stored_lines)} lines, not {len(sent_lines)}"
        for index, (was_stored, was_sent) in enumerate(
            zip(stored_lines, sent_lines), start=1
        ):
            if str(was_stored.get("AccountCode") or "") != str(
                was_sent["AccountCode"]
            ):
                return (
                    f"line {index} posts to "
                    f"{was_stored.get('AccountCode') or 'unset'!r}, not "
                    f"{was_sent['AccountCode']!r}"
                )
            if str(was_stored.get("Description") or "") != str(
                was_sent["Description"]
            ):
                return f"line {index} is described as {was_stored.get('Description') or 'unset'!r}"
            for field in ("Quantity", "UnitAmount", "TaxAmount"):
                if was_stored.get(field) is None:
                    return f"line {index} states no {field}"
                if not _agrees(
                    _money(was_stored.get(field)), _money(was_sent[field])
                ):
                    return (
                        f"line {index} states {field} "
                        f"{was_stored.get(field)}, not {was_sent[field]}"
                    )
        return ""

    def _disagrees_with_evidence(
        bill: Mapping[str, Any],
        contact_id: str,
        duty: Decimal,
        vat: Decimal,
        extra_accounts: Sequence[str] = (),
    ) -> str:
        """Why a draft does not match the customs evidence, or "" if it does.

        A resumed draft is not trusted on its status and total alone. This runs
        unattended under D-021, so the supplier, currency and the two customs
        lines are each checked against the evidence before the import goes on.
        A bill that has been edited elsewhere returns to AL/X.
        """
        contact = bill.get("Contact")
        actual_contact = (
            str(contact.get("ContactID") or "") if isinstance(contact, Mapping) else ""
        )
        if actual_contact != contact_id:
            return f"the draft belongs to contact {actual_contact or 'unknown'!r}"
        currency = str(bill.get("CurrencyCode") or "")
        if currency and currency != "ZAR":
            return f"the draft is in {currency}, not ZAR"
        posted: dict[str, Decimal] = {}
        for line in bill.get("LineItems") or ():
            if not isinstance(line, Mapping):
                continue
            code = str(line.get("AccountCode") or "")
            if code not in (
                import_vat_account,
                customs_duty_account,
                *extra_accounts,
            ):
                return f"the draft carries an unexpected account {code!r}"
            amount = _money(
                line.get("LineAmount")
                if line.get("LineAmount") is not None
                else _money(line.get("Quantity")) * _money(line.get("UnitAmount"))
            )
            posted[code] = posted.get(code, Decimal("0")) + amount
        if not _agrees(posted.get(import_vat_account, Decimal("0")), vat):
            return (
                f"the draft posts import VAT "
                f"{posted.get(import_vat_account, Decimal('0'))}, not {vat}"
            )
        if not _agrees(posted.get(customs_duty_account, Decimal("0")), duty):
            return (
                f"the draft posts customs duty "
                f"{posted.get(customs_duty_account, Decimal('0'))}, not {duty}"
            )
        return ""

    def customs_stage(
        contact_id: str,
        documents: Sequence[tuple[MailReference, Any, bytes]],
        references: Sequence[MailReference],
    ) -> CapabilityResult:
        steps = ["read_documents"]
        evidence = analyzer.customs_evidence([payload for _r, _a, payload in documents])
        steps.append("verified_customs_evidence")
        if not evidence["verified"]:
            return returned(
                "customs_documents",
                "customs_evidence_unverified",
                "; ".join(evidence["problems"]),
                evidence.get("waybill", ""),
                steps=steps,
                references=references,
            )

        waybill = evidence["waybill"]
        number = provisional_number(waybill)
        duty = _money(evidence["duty"])
        vat = _money(evidence["vat"])
        lines = _expected_lines(waybill, duty, vat)
        if not lines:
            return returned(
                "customs_documents",
                "customs_evidence_unverified",
                "the customs documents state neither duty nor import VAT",
                waybill,
                steps=steps,
                references=references,
            )

        # Xero requires a date. The assessment date is stated by the
        # declaration identifier; without one, nothing is guessed.
        assessed_on = str(evidence.get("assessed_on") or "")
        if not assessed_on:
            return returned(
                "customs_documents",
                "assessment_date_missing",
                (
                    f"declaration {evidence['declaration']} does not state an "
                    "assessment date"
                ),
                waybill,
                steps=steps,
                references=references,
            )

        existing = account.find_bill(number, contact_id)
        if existing is not None:
            status = str(existing.get("Status") or "")
            if status != "DRAFT":
                return returned(
                    "customs_documents",
                    "duplicate_bill",
                    f"a {status or 'non-draft'} bill already exists for {number}",
                    waybill,
                    existing,
                    steps,
                    references,
                )
            # The draft is resumed only if it still matches the evidence it
            # was drafted from. Status and total alone would accept a bill
            # edited to a different supplier, currency or accounts.
            disagreement = _disagrees_with_evidence(existing, contact_id, duty, vat)
            if disagreement:
                return returned(
                    "customs_documents",
                    "draft_changed",
                    f"{number} no longer matches its customs evidence: "
                    f"{disagreement}",
                    waybill,
                    existing,
                    steps,
                    references,
                )
            invoice_id = str(existing.get("InvoiceID") or "")
            steps.append("resumed_verified_draft")
        else:
            created = account.create_draft_bill(
                {
                    "Type": "ACCPAY",
                    "Contact": {"ContactID": contact_id},
                    "InvoiceNumber": number,
                    "Date": assessed_on,
                    "DueDate": assessed_on,
                    "CurrencyCode": "ZAR",
                    "Reference": (
                        f"DHL import; waybill {waybill}; "
                        f"declaration {evidence['declaration']}"
                    ),
                    "LineAmountTypes": "NoTax",
                    "LineItems": lines,
                    "Status": "DRAFT",
                }
            )
            invoice_id = str(created.get("InvoiceID") or "")
            steps.append("created_provisional_draft")
        if not invoice_id:
            raise XeroAccessError("response_invalid")

        for _reference, attachment, payload in documents:
            _attach(invoice_id, attachment, payload)
        steps.append("attached_and_verified_documents")

        final = account.read_bill(invoice_id)
        steps.append("read_back")
        if final is None or str(final.get("Status") or "") != "DRAFT":
            return returned(
                "customs_documents",
                "read_back_mismatch",
                "the provisional draft did not read back as a draft",
                waybill,
                final,
                steps,
                references,
            )
        expected_total = duty + vat
        if not _agrees(_money(final.get("Total")), expected_total):
            return returned(
                "customs_documents",
                "read_back_mismatch",
                f"the draft totals {final.get('Total')}, not {expected_total}",
                waybill,
                final,
                steps,
                references,
            )
        steps.append("verified")
        return CapabilityResult(
            call_id_source(),
            PROCESS_DHL_IMPORT,
            CapabilityResultState.SUCCEEDED,
            {
                # The stage succeeded, and the shipment is not finished: the
                # invoice completes this same bill when it arrives.
                "completed": True,
                "stage": "customs_documents",
                "returned_for": "",
                "detail": (
                    f"provisional bill {number} drafted for duty {duty} and "
                    f"import VAT {vat}; awaiting the DHL invoice"
                ),
                "waybill": waybill,
                "bill": dict(final),
                "attached": tuple(a.filename for _r, a, _p in documents),
                "steps": tuple(steps),
            },
            provenance=RetentionPolicy().direct_mail(now(), tuple(references)),
        )

    def invoice_stage(
        contact_id: str,
        invoice: tuple[MailReference, Any, bytes],
        references: Sequence[MailReference],
    ) -> CapabilityResult:
        steps = ["read_documents"]
        _reference, attachment, payload = invoice
        fields = analyzer.invoice_fields(payload)
        steps.append("read_invoice")
        waybill = fields["waybill"]
        total = _money(fields["total"])
        invoice_date = str(fields["invoice_date"] or "")
        if not invoice_date:
            return returned(
                "dhl_invoice",
                "invoice_date_missing",
                (
                    f"invoice {fields['invoice_number']} states no invoice "
                    "date; the due date is not a substitute for one"
                ),
                waybill,
                steps=steps,
                references=references,
            )

        draft = account.find_bill(provisional_number(waybill), contact_id)
        if draft is None:
            # A previous run may have renamed the bill and then failed before
            # authorising it. It no longer answers to the provisional number,
            # so it is looked up by the invoice number it now carries and the
            # same run finishes it. Without this the bill is stranded: neither
            # name finds it and the import can never complete.
            resumed = account.find_bill(fields["invoice_number"], contact_id)
            if resumed is not None and str(resumed.get("Status") or "") == "DRAFT":
                draft = resumed
                steps.append("recovered_renamed_draft")
        if draft is None:
            # No customs stage ran for this shipment, which is ordinary for an
            # export or a local delivery. Creating a second bill here would be
            # the duplicate this capability exists to prevent, so the decision
            # is Friedl's.
            return returned(
                "dhl_invoice",
                "no_matching_draft",
                (
                    f"no provisional bill {provisional_number(waybill)} exists; "
                    "this shipment has no customs stage"
                ),
                waybill,
                steps=steps,
                references=references,
            )
        invoice_id = str(draft.get("InvoiceID") or "")
        steps.append("found_provisional_draft")

        current = account.read_bill(invoice_id)
        if current is None:
            raise XeroAccessError("bill_not_found")
        if str(current.get("Status") or "") != "DRAFT":
            return returned(
                "dhl_invoice",
                "bill_not_draft",
                f"the matching bill is {current.get('Status')}, not a draft",
                waybill,
                current,
                steps,
                references,
            )

        # The duty and VAT are re-derived from the customs documents stored on
        # this bill, never from its line items. Trusting the lines would let a
        # bill whose evidence was deleted or edited reach authorisation with
        # nothing supporting its figures.
        customs_records = []
        for item in account.list_bill_attachments(invoice_id):
            stored = account.read_bill_attachment(
                invoice_id,
                str(item.get("AttachmentID") or ""),
                str(item.get("MimeType") or ""),
            )
            if analyzer.classify(stored) in ("customs_worksheet", "sad_500"):
                customs_records.append((item, stored))
        customs_payloads = [stored for _item, stored in customs_records]
        if not customs_payloads:
            return returned(
                "dhl_invoice",
                "customs_evidence_missing",
                (
                    "the provisional bill no longer carries the customs "
                    "worksheet and SAD 500 it was drafted from"
                ),
                waybill,
                current,
                steps,
                references,
            )
        try:
            stored_evidence = analyzer.customs_evidence(customs_payloads)
        except DhlDocumentError as error:
            return returned(
                "dhl_invoice",
                "customs_evidence_missing",
                (
                    "the customs documents stored on the bill are no longer "
                    f"usable evidence: {error.code}"
                ),
                waybill,
                current,
                steps,
                references,
            )
        if not stored_evidence["verified"]:
            return returned(
                "dhl_invoice",
                "customs_evidence_unverified",
                "; ".join(stored_evidence["problems"]),
                waybill,
                current,
                steps,
                references,
            )
        if stored_evidence["waybill"] != waybill:
            return returned(
                "dhl_invoice",
                "customs_evidence_mismatch",
                (
                    f"the bill's customs evidence is for waybill "
                    f"{stored_evidence['waybill']}, not {waybill}"
                ),
                waybill,
                current,
                steps,
                references,
            )
        stored_duty = _money(stored_evidence["duty"])
        stored_vat = _money(stored_evidence["vat"])
        # Every customs document found above, plus the invoice, must still be
        # on the bill when it is authorised.
        required_documents = [
            (str(item.get("FileName") or ""), hashlib.sha256(stored).hexdigest())
            for item, stored in customs_records
        ]
        required_documents.append((attachment.filename, attachment.sha256))
        steps.append("re_verified_customs_evidence")

        # The draft must still agree with that evidence before it is completed.
        # A bill recovered from an interrupted run already carries its
        # clearance line, so that account is expected here; any other account
        # is still refused.
        disagreement = _disagrees_with_evidence(
            current,
            contact_id,
            stored_duty,
            stored_vat,
            extra_accounts=(clearance_account,),
        )
        if disagreement:
            return returned(
                "dhl_invoice",
                "draft_changed",
                f"the provisional bill no longer matches its evidence: "
                f"{disagreement}",
                waybill,
                current,
                steps,
                references,
            )
        steps.append("verified_draft_against_evidence")

        clearance = total - stored_duty - stored_vat
        if clearance < 0:
            return returned(
                "dhl_invoice",
                "invoice_below_customs_total",
                (
                    f"the invoice totals {total} but the verified duty and VAT "
                    f"already come to {stored_duty + stored_vat}"
                ),
                waybill,
                current,
                steps,
                references,
            )

        # Rebuilt from the re-verified evidence, not copied from the draft.
        lines = _expected_lines(waybill, stored_duty, stored_vat)
        if clearance > 0:
            lines.append(
                _line(
                    f"DHL clearance and processing — waybill {waybill}",
                    clearance,
                    clearance_account,
                )
            )

        # The invoice is attached before the bill is renamed. Renaming first
        # would strand a bill that no longer answers to its provisional number
        # if attachment then failed, leaving neither stage able to find it.
        _attach(invoice_id, attachment, payload)
        steps.append("attached_and_verified_invoice")

        completed_bill = {
            "Type": "ACCPAY",
            "Contact": {"ContactID": contact_id},
            "InvoiceNumber": fields["invoice_number"],
            # D-021: the invoice's own date, never the due date standing in
            # for it. D-020's rule still fills a missing due date from the
            # invoice date, but not the reverse.
            "Date": invoice_date,
            "DueDate": fields["due_date"] or invoice_date,
            "CurrencyCode": "ZAR",
            "Reference": str(current.get("Reference") or ""),
            "LineAmountTypes": "NoTax",
            "LineItems": lines,
        }
        updated = account.update_draft_bill(invoice_id, completed_bill)
        steps.append("completed_the_same_draft")
        if str(updated.get("InvoiceID") or "") != invoice_id:
            raise XeroAccessError("source_mismatch")

        # Xero accepting the update is not proof of what it stored. The bill is
        # read back fresh and compared field by field before the irreversible
        # step: an accepted request that stored a different supplier, currency,
        # date or coding must never reach authorisation.
        before = account.read_bill(invoice_id)
        if before is None:
            raise XeroAccessError("bill_not_found")
        mismatch = _differs_from_sent(before, completed_bill)
        if not mismatch:
            if str(before.get("Status") or "") != "DRAFT":
                mismatch = f"it is {before.get('Status')}, not a draft"
            elif not _agrees(_money(before.get("Total")), total):
                mismatch = f"it totals {before.get('Total')}, not {total}"
        if mismatch:
            return returned(
                "dhl_invoice",
                "update_not_stored",
                f"the bill Xero stored is not the one that was sent: {mismatch}",
                waybill,
                before,
                steps,
                references,
            )
        steps.append("verified_stored_update")

        # Every source document must still be on the bill, byte-for-byte, at
        # the moment it is authorised.
        for filename, digest in required_documents:
            if not _verified_attachment(invoice_id, filename, digest):
                return returned(
                    "dhl_invoice",
                    "supporting_document_missing",
                    f"{filename} is not stored on the bill being authorised",
                    waybill,
                    updated,
                    steps,
                    references,
                )
        steps.append("verified_every_required_document")

        account.authorise_bill(invoice_id)
        steps.append("authorised")

        final = account.read_bill(invoice_id)
        steps.append("read_back")
        if (
            final is None
            or str(final.get("InvoiceID") or "") != invoice_id
            or str(final.get("InvoiceNumber") or "") != fields["invoice_number"]
            or str(final.get("Status") or "") != "AUTHORISED"
            or not _agrees(_money(final.get("Total")), total)
            or not final.get("HasAttachments", False)
        ):
            return returned(
                "dhl_invoice",
                "read_back_mismatch",
                "the completed bill does not match the invoice",
                waybill,
                final,
                steps,
                references,
            )
        steps.append("verified")
        return CapabilityResult(
            call_id_source(),
            PROCESS_DHL_IMPORT,
            CapabilityResultState.SUCCEEDED,
            {
                "completed": True,
                "stage": "dhl_invoice",
                "returned_for": "",
                "detail": (
                    f"bill {fields['invoice_number']} completed for {total}: "
                    f"duty {stored_duty}, import VAT {stored_vat}, "
                    f"clearance {clearance}"
                ),
                "waybill": waybill,
                "bill": dict(final),
                "attached": (attachment.filename,),
                "steps": tuple(steps),
            },
            provenance=RetentionPolicy().direct_mail(now(), tuple(references)),
        )

    def duty_tax_stage(
        contact_id: str,
        structured: tuple[MailReference, Any, bytes],
        invoice: tuple[MailReference, Any, bytes],
        references: Sequence[MailReference],
    ) -> CapabilityResult:
        """Post one self-reconciled duty-tax-paid invoice under D-022."""
        steps = ["read_documents"]
        _csv_reference, _csv_attachment, csv_payload = structured
        _pdf_reference, pdf_attachment, pdf_payload = invoice
        evidence = analyzer.invoice_evidence(csv_payload)
        fields = analyzer.invoice_fields(pdf_payload)
        steps.append("read_structured_invoice_and_pdf")
        waybill = str(evidence.get("waybill") or "")

        if evidence.get("kind") != "dhl_duty_tax_invoice":
            return returned(
                "dhl_duty_tax_invoice",
                "documents_ambiguous",
                "the structured invoice is not duty-tax-paid evidence",
                waybill,
                steps=steps,
                references=references,
            )
        if not evidence.get("verified"):
            return returned(
                "dhl_duty_tax_invoice",
                "invoice_does_not_reconcile",
                "; ".join(str(item) for item in evidence.get("problems") or ()),
                waybill,
                steps=steps,
                references=references,
            )
        if evidence.get("tax_present"):
            return returned(
                "dhl_duty_tax_invoice",
                "invoice_tax_present",
                "the structured invoice states tax in one or more tax fields",
                waybill,
                steps=steps,
                references=references,
            )

        # D-022: the MyBill CSV is the authoritative structured accounting
        # evidence. The PDF corroborates it where it states a value, so an
        # absent PDF field is not a disagreement.
        #
        # The invoice date must be treated that way: the PDF parser returns an
        # empty invoice date by design rather than read the bare DATE label,
        # which on a DHL invoice matches the due date and would post the bill
        # under a date no document asserts. Comparing that empty value with
        # `!=` read absence as conflict and refused a correct invoice.
        csv_invoice_date = str(evidence.get("invoice_date") or "")
        pdf_invoice_date = str(fields.get("invoice_date") or "")

        # The CSV is authoritative, so the date the bill is posted under comes
        # from it. Without a usable one there is nothing to post against and
        # nothing to corroborate, so this refuses before any Xero write rather
        # than sending an empty or invented date.
        if not xero_date(csv_invoice_date):
            return returned(
                "dhl_duty_tax_invoice",
                "invoice_date_missing",
                "the structured invoice states no usable invoice date",
                waybill,
                steps=steps,
                references=references,
            )

        mismatches = []
        for label, csv_value, pdf_value in (
            ("invoice number", evidence.get("invoice_number"), fields.get("invoice_number")),
            ("waybill", waybill, fields.get("waybill")),
        ):
            if str(csv_value or "") != str(pdf_value or ""):
                mismatches.append(f"{label} differs between CSV and PDF")
        if not _agrees(_money(evidence.get("total")), _money(fields.get("total"))):
            mismatches.append("total differs between CSV and PDF")
        if csv_invoice_date and pdf_invoice_date and csv_invoice_date != pdf_invoice_date:
            mismatches.append("invoice date differs between CSV and PDF")
        csv_due = str(evidence.get("due_date") or "")
        pdf_due = str(fields.get("due_date") or "")
        if csv_due and pdf_due and csv_due != pdf_due:
            mismatches.append("due date differs between CSV and PDF")
        if mismatches:
            return returned(
                "dhl_duty_tax_invoice",
                "supporting_document_mismatch",
                "; ".join(mismatches),
                waybill,
                steps=steps,
                references=references,
            )
        if str(evidence.get("currency") or "") != "ZAR":
            return returned(
                "dhl_duty_tax_invoice",
                "invoice_currency_invalid",
                f"the structured invoice is in {evidence.get('currency') or 'no currency'}",
                waybill,
                steps=steps,
                references=references,
            )

        invoice_number = str(evidence["invoice_number"])
        invoice_date = str(evidence["invoice_date"])
        due_date = str(evidence.get("due_date") or fields.get("due_date") or invoice_date)
        total = _money(evidence["total"])
        lines = [
            _line(
                f"{item['description']} ({item['code']}) — waybill {waybill}",
                _money(item["amount"]),
                customs_duty_account,
            )
            for item in evidence.get("lines") or ()
        ]
        if not lines:
            return returned(
                "dhl_duty_tax_invoice",
                "invoice_does_not_reconcile",
                "the structured invoice states no duty or regulatory lines",
                waybill,
                steps=steps,
                references=references,
            )
        sent = {
            "Type": "ACCPAY",
            "Contact": {"ContactID": contact_id},
            "InvoiceNumber": invoice_number,
            "Date": invoice_date,
            "DueDate": due_date,
            "CurrencyCode": "ZAR",
            "Reference": f"DHL duty tax paid; waybill {waybill}",
            "LineAmountTypes": "NoTax",
            "LineItems": lines,
            "Status": "DRAFT",
        }

        existing = account.find_bill(invoice_number, contact_id)
        if existing is None:
            created = account.create_draft_bill(sent)
            invoice_id = str(created.get("InvoiceID") or "")
            steps.append("created_duty_tax_draft")
        else:
            invoice_id = str(existing.get("InvoiceID") or "")
            if str(existing.get("Status") or "") != "DRAFT":
                return returned(
                    "dhl_duty_tax_invoice",
                    "duplicate_bill",
                    f"a {existing.get('Status') or 'non-draft'} bill already exists",
                    waybill,
                    existing,
                    steps,
                    references,
                )
            steps.append("resumed_duty_tax_draft")
        if not invoice_id:
            raise XeroAccessError("response_invalid")

        before = account.read_bill(invoice_id)
        if before is None:
            raise XeroAccessError("bill_not_found")
        mismatch = _differs_from_sent(before, sent)
        if not mismatch and str(before.get("Status") or "") != "DRAFT":
            mismatch = f"it is {before.get('Status')}, not a draft"
        if not mismatch and not _agrees(_money(before.get("Total")), total):
            mismatch = f"it totals {before.get('Total')}, not {total}"
        if mismatch:
            return returned(
                "dhl_duty_tax_invoice",
                "draft_changed",
                f"the duty-tax-paid draft differs from the invoice: {mismatch}",
                waybill,
                before,
                steps,
                references,
            )
        steps.append("verified_duty_tax_draft")

        # D-022 names the PDF as the human-readable source stored with the bill.
        _attach(invoice_id, pdf_attachment, pdf_payload)
        steps.append("attached_and_verified_invoice")
        if not _verified_attachment(invoice_id, pdf_attachment.filename, pdf_attachment.sha256):
            return returned(
                "dhl_duty_tax_invoice",
                "supporting_document_missing",
                f"{pdf_attachment.filename} is not stored on the bill",
                waybill,
                before,
                steps,
                references,
            )

        # Attachment is an external write too. Re-read after it and compare
        # the exact payload again immediately before authorisation, rather
        # than assuming the draft verified before attachment is unchanged.
        before_authorisation = account.read_bill(invoice_id)
        if before_authorisation is None:
            raise XeroAccessError("bill_not_found")
        mismatch = _differs_from_sent(before_authorisation, sent)
        if not mismatch and str(before_authorisation.get("Status") or "") != "DRAFT":
            mismatch = f"it is {before_authorisation.get('Status')}, not a draft"
        if not mismatch and not _agrees(
            _money(before_authorisation.get("Total")), total
        ):
            mismatch = f"it totals {before_authorisation.get('Total')}, not {total}"
        if mismatch:
            return returned(
                "dhl_duty_tax_invoice",
                "draft_changed",
                f"the duty-tax-paid draft changed before authorisation: {mismatch}",
                waybill,
                before_authorisation,
                steps,
                references,
            )
        steps.append("re_verified_before_authorisation")

        account.authorise_bill(invoice_id)
        steps.append("authorised")
        final = account.read_bill(invoice_id)
        final_mismatch = _differs_from_sent(final or {}, sent)
        if (
            final is None
            or str(final.get("Status") or "") != "AUTHORISED"
            or final_mismatch
            or not _agrees(_money(final.get("Total")), total)
            or not final.get("HasAttachments", False)
        ):
            return returned(
                "dhl_duty_tax_invoice",
                "read_back_mismatch",
                "the authorised duty-tax-paid bill does not match its evidence",
                waybill,
                final,
                steps,
                references,
            )
        steps.append("verified")
        return CapabilityResult(
            call_id_source(),
            PROCESS_DHL_IMPORT,
            CapabilityResultState.SUCCEEDED,
            {
                "completed": True,
                "stage": "dhl_duty_tax_invoice",
                "returned_for": "",
                "detail": f"bill {invoice_number} authorised for {total}",
                "waybill": waybill,
                "bill": dict(final),
                "attached": (pdf_attachment.filename,),
                "steps": tuple(steps),
            },
            provenance=RetentionPolicy().direct_mail(now(), tuple(references)),
        )

    def process(arguments: StructuredData) -> CapabilityResult:
        try:
            sources = arguments.get("documents")
            if not isinstance(sources, (tuple, list)) or not sources:
                raise ValueError("documents")
            read: list[tuple[MailReference, Any, bytes]] = []
            for source in sources:
                if not isinstance(source, Mapping):
                    raise ValueError("documents")
                read.append(_read(source))
            references = [reference for reference, _a, _p in read]

            # The stage follows from what the documents are, never from wording.
            kinds = [analyzer.classify(payload) for _r, _a, payload in read]
            invoices = [
                item for item, kind in zip(read, kinds) if kind == "dhl_invoice"
            ]
            duty_tax = [
                item
                for item, kind in zip(read, kinds)
                if kind == "dhl_duty_tax_invoice"
            ]
            freight = [
                item for item, kind in zip(read, kinds) if kind == "dhl_freight_invoice"
            ]
            structured_customs = [
                item
                for item, kind in zip(read, kinds)
                if kind == "dhl_customs_invoice"
            ]
            customs = [
                item
                for item, kind in zip(read, kinds)
                if kind in ("customs_worksheet", "sad_500")
            ]
            unknown = [kind for kind in kinds if kind in ("unrecognised", "unreadable")]
            if unknown:
                return returned(
                    "",
                    "documents_ambiguous",
                    f"a document is not recognised DHL evidence: {unknown[0]}",
                    references=references,
                )
            if freight:
                return returned(
                    "dhl_freight_invoice",
                    "freight_not_authorised",
                    "the documents describe DHL freight, whose accounting treatment is not approved",
                    references=references,
                )
            # Only branches that can reach Xero require Xero configuration.
            # Freight is deliberately returned unposted and must not depend on
            # an accounting connection merely to be recognised.
            if not supplier_name:
                return failed("dhl_supplier_not_configured")
            unusable = _validate_configuration()
            if unusable:
                return failed(unusable)
            contact_id, unresolved = _resolve_contact()
            if unresolved:
                return failed(unresolved)
            if duty_tax:
                if len(duty_tax) != 1 or len(invoices) != 1 or customs or structured_customs:
                    return returned(
                        "dhl_duty_tax_invoice",
                        "documents_ambiguous",
                        "duty-tax-paid processing requires exactly one MyBill CSV and one PDF invoice",
                        references=references,
                    )
                return duty_tax_stage(contact_id, duty_tax[0], invoices[0], references)
            if structured_customs:
                if len(structured_customs) != 1 or len(invoices) != 1 or customs:
                    return returned(
                        "dhl_customs_invoice",
                        "documents_ambiguous",
                        "customs invoice processing requires one MyBill CSV and one PDF invoice",
                        references=references,
                    )
                evidence = analyzer.invoice_evidence(structured_customs[0][2])
                fields = analyzer.invoice_fields(invoices[0][2])
                if (
                    str(evidence.get("invoice_number") or "") != str(fields.get("invoice_number") or "")
                    or str(evidence.get("waybill") or "") != str(fields.get("waybill") or "")
                    or not _agrees(_money(evidence.get("total")), _money(fields.get("total")))
                ):
                    return returned(
                        "dhl_customs_invoice",
                        "supporting_document_mismatch",
                        "the MyBill CSV and PDF do not identify the same invoice",
                        references=references,
                    )
                return invoice_stage(contact_id, invoices[0], references)
            if invoices and customs:
                return returned(
                    "",
                    "documents_ambiguous",
                    "customs evidence and an invoice were supplied together; "
                    "each stage takes its own documents",
                    references=references,
                )
            if len(invoices) > 1:
                return returned(
                    "",
                    "documents_ambiguous",
                    "more than one DHL invoice was supplied",
                    references=references,
                )
            if invoices:
                return invoice_stage(contact_id, invoices[0], references)
            if customs:
                return customs_stage(contact_id, customs, references)
            return returned(
                "",
                "documents_ambiguous",
                "no DHL customs evidence or invoice was supplied",
                references=references,
            )
        except ValueError:
            return failed("arguments_unusable")
        except MailAccessError as error:
            return failed(error.code)
        except DhlDocumentError as error:
            return failed(error.code)
        except XeroAccessError as error:
            return failed(error.code)

    return {PROCESS_DHL_IMPORT: process}
