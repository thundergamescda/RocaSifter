# Customer export v4: classified receiving plan intents

Prototype development only. No real customer export, production migration,
Square request, deployment or legal-compliance determination occurred.
Earlier v3 evidence remains in `Testing/receiving-saga/privacy-v3/` and its
historical documentation remains in `PRIVACY_RECEIVING_EXPORT_V3.md`.

## Exact ownership boundary

Format 4 contains 26 counted collections. It adds `receiving_plan_intents` to
format 3 and requires all of these conditions before including an intent:

- The current tenant owns the intent, physical claim and trade receipt.
- The claim is consumed and bound to the selected customer's accepted cash or
  credit trade; the receipt points back to the same physical claim.
- Immutable intent metadata explicitly says `accepted_trade` and matches the
  bound accepted trade ID and receipt hash.

Claim ID alone is insufficient. Rejected inventory-only requests, competing
draft/receipt references, unlinked claims and legacy intents lacking classified
metadata are withheld. Unregistered retirement tombstones have no trustworthy
customer association and are also withheld for separate owner review.
No original plan body is stored in the intent table or included in this export.

The new collection projects the request key, normalized-intent/wire hashes,
claim/workflow/accepted-receipt bindings, staff reference, creation/outcome times,
created operation reference and request state. A registered retirement includes
its original-wire SHA-256 and `registered_original_wire` basis. The export checks
the stored retirement key, state, wire hash and null operation/receiving IDs before
opening a customer stream. Corrupt proof fails explicitly.

Retirement audit records use the same classified intent selection. Their details
include only the approved wire hash projection. An unrelated tombstone, even on
the same claim or with a colliding request key in another tenant, does not become
customer evidence by inference.

## Receiving proof and download behavior

The v3 safeguards remain: explicit reviewed-line projections; safe catalog and
inventory proof hashes; no encrypted bodies, session/authority data, raw provider
responses or full catalog/template snapshots; rejection of nested foreign
customer/store references and malformed scalar/hash data. Version 4 also preserves
safe source-binding/template references and coalesced review-row aliases.

Streaming uses a consistent read-only snapshot, exact record counts, bounded
single-row fetches and page bytes, and a complete content-chain footer. The browser
expects exactly version 4 and 26 collection counts. It rejects version 3, altered,
truncated or oversized streams before download. Auth-transition clearing, current
owner/session checks, recent sign-in, attestations and final persisted verification
audit remain required. Engineering limits are not record-retention policy.

## Scoped evidence

- Actual PostgreSQL run `Testing/oauth-receiving/postgres-runs/20261101T220303506416Z`
  passed all 26 cases in 41.333 seconds: 15 independent catalog/retirement safety
  cases and 11 privacy cases, with zero skips/failures/errors, exact case coverage
  and stable source. Its delivered log SHA-256 is
  `3c025f0034eca0b7eca6dbfbda090ae04eaca4ceef7b9301ab1876a794a33ef8`.
- The privacy fixture exercises 121 retired classified intents plus a created
  plan, complete streamed counts/bytes, retirement audit, exact wire hashes,
  rejected inventory-only and competing-draft attempts, unclassified/foreign
  records, immutable-corrupt retirement proof, source-record preservation and
  current owner/session checks. It uses disposable local PostgreSQL with fictional
  data and injected provider objects, not a hosted or actual Square account.
- Actual Chrome `Testing/receiving-saga/privacy-v4/privacy-user-acceptance.json`
  passed 11 cases on stable source, finished `2026-11-01T22:05:58Z`. The fictional
  602801-byte download verified 26 counts, 275 credit entries, 127 receiving
  requests, intent retirement metadata and projected catalog/inventory proof.
  JavaScript errors, external requests and failed static assets were all zero.
  It checks old-version rejection, buffer bounds, nested foreign payload,
  corruption/interruption, cross-tab sign-out/new sign-in, late old 401 responses
  and server revocation. This is a loopback mock API/browser test, not SQL or
  provider proof by itself.
- Focused pure/HTTP checks passed 47 cases (24 privacy, 16 catalog helper and
  7 owner-template cases) in 1.990 seconds on stable source with zero
  skips/failures/errors. They include the versioned catalog-name recovery vectors.
  Reports are `Testing/receiving-saga/privacy-v4/security-focused-tests.json` and
  `.log`; delivered log SHA-256 is
  `4d568ae5b40d7358e8050771ac6bf95b4610a21fbc85761eaf03a433cfb93a5f`.
  Whole-prototype frozen acceptance remains a separate release task.

## Limits and owner decisions

This hosted snapshot does not inspect local desktop files/journals, scanner or
printer files, downloads, backups, support copies, duplicate accounts, incidental
free-text mentions or records held by Square. Unclassified records need separate
owner review. No deletion, anonymization, offboarding purge, customer delivery,
data sale or retention decision is performed. Legal/policy decisions and actual
hosted operational acceptance remain open; this does not claim to fulfill every
legal customer-rights request.
