# Customer export v3: linked receiving evidence

Development prototype only. This does not change the frozen Phase 1 application,
publish a service, perform a merchant export or determine legal compliance.

## Ownership and scope

An authorized owner with a sign-in within 15 minutes can prepare an attested
hosted snapshot. MFA proof is used when enrolled. Identity verification, purpose,
case-reference hash, scope review and private-copy handling remain required.

Receiving records are selected only through the current tenant, an operation
whose workflow is `accepted_trade`, a consumed physical claim, and that claim's
bound accepted cash/credit receipt for the selected customer. A competing draft
pointing at the same claim does not establish ownership. Inventory-only and
unlinked operations are expressly withheld for separate owner review. A mismatched
accepted receipt/claim/manifest binding stops export rather than silently assigning
it to a customer.

Format 3 has 25 counted collections. Seven receiving collections extend format 2:
`receiving_operations`, `receiving_plan_revisions`, `receiving_steps`,
`receiving_requests`, `receiving_request_outcomes`, `receiving_attempts`, and
`receiving_exceptions`. Relevant audit records are selected by these operation
IDs with the same tenant boundary. Request outcomes join on both tenant and
request key. Histories are streamed completely, without a last 100-row cutoff.

## Explicit projection

The copy includes reviewed selected card identities, quantities and prices;
source/claim/accepted-receipt bindings; plan revisions; retained SKU/variation
references; receipt copies; proof kind and hashes; request intent hashes; endpoint,
API version, provider idempotency key, authorized staff reference and timestamps;
attempt/outcome codes and exception occurrence metadata.

Catalog admission at ordinal 0 is projected as endpoint/version/body hash,
idempotency key, actor and dates. The operation's `catalog_proof` exports only
kind and request/response/evidence hashes. `catalog_resolutions` exports only
reviewed row, identity, SKU, variation/parent, price and template-reference fields.
Raw provider/catalog data inside either JSON object is excluded defensively.

It excludes encrypted provider bodies, staff-session hashes, authority hashes,
tokens, MFA material, raw provider responses and full catalog parents/siblings.
Future JSON fields are excluded unless explicitly reviewed for this export.
Nested credential keys are defensively removed and counted. Nested foreign
customer/store references, malformed receiving scalar fields or proof hashes
stop the stream. Original stored hashes refer to original evidence; the exported
projection is independently protected by the file's content chain and counts.
Owner-specific mapping/template snapshots are outside this customer export.

The snapshot uses bounded, single-row cursor fetches, page byte limits and a
complete hash/count footer. Logout, staff revocation, expired/replaced sessions,
processing limits, missing records or interrupted streams prevent a completed
download. The browser buffers only within its stated engineering limit, clears
on an auth transition, and requires a persisted final verification audit before
offering a download. These limits are not retention policy.

## Scoped evidence

- `Testing/receiving-saga/security-focused-tests.json`: 42 pure/mocked HTTP cases
  (22 privacy, 13 catalog preservation and 7 owner-template cases), stable source
  with hashes and a byte-hashed log. No database/provider calls. The earlier
  `privacy-v3-pure-tests.json` is a historical checkpoint before catalog extension.
- `Testing/receiving-saga/privacy-v3/privacy-user-acceptance.json`: 11 actual Chrome
  cases, stable source, finished 2026-11-01T05:23:38Z; no JavaScript errors, failed
  static assets or external requests. The fictional 601623-byte copy verified 25
  collection counts, 275 credit entries and 127 receiving request records. Tests
  include excluded original rows, safe receiving proof, foreign nested JSON,
  unsupported format, altered/truncated streams, byte limits, cross-tab sign-out,
  delayed old401 responses and server revocation. Desktop/mobile screenshots
  were visually inspected. This is loopback/mock evidence, not hosted/provider
  or physical-device acceptance.
- Actual PostgreSQL: the final scoped run
  `Testing/oauth-receiving/postgres-runs/20261101T052438929246Z` passed all 16 cases
  (9 privacy, 7 owner templates) in 16.032 seconds, with zero skips, failures or
  errors, exact case coverage and stable source. This is disposable local
  PostgreSQL with injected fictional providers, not hosted or live Square proof.
- Historical PostgreSQL: 8 cases passed in the diagnostic run
  `Testing/oauth-receiving/postgres-runs/20261031T054245763606Z`, but the runner
  correctly rejected the overall run because `hosted/trades.py` and its receiving
  test fixture changed while it ran. Preserve this as source-drift evidence;
  repeat on the final frozen prototype before release. An earlier source-stable
  eight-case run053958023147Z predates final proof-hash/byte assertions.
- `privacy-harness-callback-failure.json/.log` preserve the initial Chrome harness
  failure: Playwright supplied a request object into an optional callback argument,
  selecting the wrong corruption fixture. The corrected single-argument callback
  passed all 11 cases. This file is not passing release evidence.
- The initial template SQL test helper collided with an inherited fixture's
  `self.preview` dictionary, causing six fixture errors before domain calls in
  run `20261101T052146373572Z`. The renamed `template_preview` helper passed its
  separate seven-case run and the final combined sixteen-case run. Preserve the
  failure evidence; it is not a product-pass claim.

Actual PostgreSQL cases cover a received accepted-trade operation with revisions,
lost acknowledgement/recovery, more than 121 replay records and full footer bytes;
same operation/request IDs in a different tenant; competing-customer and
inventory-only exclusion; no encrypted/session/authority payload; corrupt binding
and raw nested customer data; source business-record counts remain unchanged.

## Remaining gates

The catalog-step projection is implemented, with a focused SQL projection fixture;
the complete integrated new-price saga and final frozen unit/SQL/browser suite
still require acceptance. No provider catalog write is inferred from a synthetic
projection fixture. Raw template/provider snapshots remain excluded.

No deletion, anonymization, statutory retention, backup/offboarding purge,
support-copy search, actual customer delivery or data monetization is implemented
by this change. Local desktops, downloads, scanners, backups and records held by
Square remain explicitly outside the hosted snapshot. Owner/legal retention and
offboarding decisions and real hosted operational acceptance remain open.
