# Sanitized DHL layout fixtures

The original DHL documents are tracked in the private V1 `JARVIS` repository.
AL/X is public, so those documents must not be copied here.

These fixtures contain only declaration numbers, waybills, and totals already
recorded in `docs/XERO_EMAIL_BILLS_IMPLEMENTATION.md`. Their relative text-run
coordinates reproduce the Crystal Reports layout that matters to the parser,
including the non-obvious VAT placement beside `TOTAL 12B/Ad Val.`.

Regenerate the four deterministic PDFs with:

```sh
python3 tests/fixtures/dhl/build_sanitized_fixtures.py
```

The committed test asserts every extracted identity and amount. A separate
local-only inventory test checks the byte hashes and parser results of all
unique private V1 documents whenever the sibling `JARVIS` checkout exists.
