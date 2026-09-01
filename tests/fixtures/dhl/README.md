# Sanitized DHL layout fixtures

The original DHL documents are tracked in the private V1 `JARVIS` repository.
AL/X is public, so those documents must not be copied here.

These fixtures contain only declaration numbers, waybills, and totals already
recorded in `docs/XERO_EMAIL_BILLS_IMPLEMENTATION.md`. Their relative text-run
coordinates reproduce the Crystal Reports layout that matters to the parser,
including the non-obvious VAT placement beside `TOTAL 12B/Ad Val.`.

Regenerate the five deterministic PDFs with:

```sh
python3 tests/fixtures/dhl/build_sanitized_fixtures.py
```

`invoice_cptir_sanitized.pdf` is the DHL MyBill invoice layout, built by the
same script and committed alongside the others. It carries only the sanitized
invoice number, waybill, total and dates, and reproduces the geometry the
parser depends on: a labelled `HAWB`, the waybill repeating once per charge
line, and the `NET AMOUNT PAYABLE` total. `invoice_pdf()` in
`tests/test_dhl_reconciliation.py` reads that committed file and may rewrite
one stated value in place, preserving the byte length so the PDF stays valid.

The committed test asserts every extracted identity and amount. A separate
local-only inventory test checks the byte hashes and parser results of all
unique private V1 documents whenever the sibling `JARVIS` checkout exists.

The three `mybill_*_sanitized.csv` files preserve only the structured columns
needed to prove D-022's ordered classification: customs (declaration present),
freight (weight charge present), and duty-tax-paid (neither, with its three
approved charge descriptions). They are synthetic sanitized equivalents, not
copies of private invoice exports. Local tests separately classify all three
private V1 MyBill CSVs when that sibling checkout is available.
