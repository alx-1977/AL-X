# DigiKey orders

The runtime loads these provider settings through its existing environment/.env
loader. Configure production credentials and `DIGIKEY_ACCOUNT_ID=15219690`:

- `DIGIKEY_CLIENT_ID`
- `DIGIKEY_CLIENT_SECRET`
- `DIGIKEY_ACCOUNT_ID`

No environment file is changed by this implementation. The refresh token is
never read or used. Each lookup obtains a two-legged client-credentials token
from `https://api.digikey.com/v1/oauth2/token` and sends the client and account
headers to `https://api.digikey.com/orderstatus/v4`.

`xero` `lookup_digikey` (D-074) accepts exactly one decimal-string `sales_order_number`
or `invoice_number`. It returns decimal-string amounts, every order line, and
invoice/shipment facts. Absent or empty shipping/tax charges become zero; other missing facts remain empty strings.
Invoice lookup pages through account history (`Shared=true`, from 1900 to
today), retrieves each distinct sales-order detail and matches exact invoice IDs
there, rather than relying on invoice fields in the history response. Incomplete search or multiple matches is refused. It makes no
production write.

The published v4 SalesOrder schema guarantees `TotalPrice`, but does **not**
guarantee a shipping/tax breakdown or invoice date. Merchandise subtotal and
extensions are calculated from QuantityOrdered and UnitPrice; ShippingCharges,
SalesTax and InvoiceDate are preserved when supplied. Line TotalPrice is
documented as including charges/taxes and is not substituted for merchandise
extension. No difference is guessed to be shipping or tax. The fixture uses the
published v4 shape with synthetic merchandise; success tests add explicit
charge values. It is not a retained live account response.

`xero` `capture_digikey_bill` accepts the same identifier and optional boolean
`authorise` (default true) and `rounding_adjustment` (default false). It requires one invoice, all accepted quantities
shipped to that invoice, one invoice/ship date, currency and a reconciled charge
breakdown. Nonzero shipping becomes its own line. Rounded goods and shipping
line amounts plus tax are compared with the DigiKey total. With explicit
`rounding_adjustment=true`, a nonzero difference of at most 0.05 in absolute
value becomes one signed Rounding adjustment line with the goods account/tax
treatment. Otherwise the difference returns rounding_allocation_unresolved
without writing. Quantities and precise unit prices are preserved. An unresolved breakdown, tax
allocation, partial shipment, multiple invoices, conflicting dates or totals
returns `needs_judgment` with the found order and commits nothing. Nonzero tax
without line allocation evidence also returns for judgment.

Production supplier-bill outcome: both mail/PDF and DigiKey source adapters feed
`tools/xero.py::_capture_structured_invoice`, which owns supplier/account/tax
resolution and calls the sole private `_commit_decided_bill` primitive. That
primitive owns duplicates, draft resumption, account/tax validation, creation,
authorisation and read-back. The former PDF-owned resolution/commit sequence
has been moved into this shared core; no competing writer is retained.
Attachments remain mandatory for PDF capture and are absent for DigiKey.

The DigiKey capture registration exists only when DigiKey and Xero are composed;
it does not require a PDF extractor. It uses the existing Xero bill-write
permission and standing/attended authority setting under D-018. Read capability
composition and the provider path appear in `runtime.setup.integrations`.
Inputs are structured identifiers; raw conversation and authoritative goal state
remain in the existing Core/durable stores. There is no new dialogue or goal
state. Duplicate checks make restart/retry refuse an authorised bill or resume
only an exactly matching draft. No deployment or production API calls were made.

Tests: `tests/test_digikey.py` covers HTTP authentication and redaction,
paginated invoice search, normalization, write-free judgment outcomes, shared
resolution/duplicate/commit behavior, authority and an AST mutation restoring a
second bill writer. Existing PDF capture tests cover its preserved behavior.
No governance exception is requested.
