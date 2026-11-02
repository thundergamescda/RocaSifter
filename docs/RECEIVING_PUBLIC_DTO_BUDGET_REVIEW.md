# Receiving public result budget: independent review

This is scoped prototype evidence using fictional provider objects, private
disposable PostgreSQL schemas and loopback Flask/desktop fixtures. It does not
prove actual Square responses, production capacity, printer behavior or complete
commercial readiness. No live Square HTTP, merchant data, deployment or production
migration was used. This review added tests and documentation only.

## Implemented boundary

Typed receiving operation/plan/request results use compact UTF-8 JSON and an
8,388,608-byte maximum. The server measures the largest expected terminal result
before a new operation/revision or provider admission. That reservation includes
the `found/result` recovery wrapper, repeated receipt targets, generated opaque
variation IDs, catalog resolutions and coalesced row aliases. A shared receipt
builder is used for the reservation and actual outcome.

The desktop increases its response allowance only for the exact typed receiving
method/path pairs. Auth, customer, readiness, location and retirement transport
limits remain 2,000,000 bytes. HTTP request bodies remain limited to 512 KiB.
Unrelated server JSON serialization is unchanged. There is no row truncation,
generic proxy, local Square fallback or permission change in this transport fix.

Saved catalog resolution/proof keys are strict. A 256-character generated ID or
an unexpected raw provider field does not become a public result: the original
admitted journal remains uncertain until read-only evidence recovery succeeds.

## Maximal public dimensions

The independent pure fixture contains 1,000 reviewed price-split rows, 100 distinct
targets and 1,000 original quantity groups. It uses fictional legal 255-character
opaque IDs with four-byte Unicode, punctuation/interior whitespace and retained
SKUs up to 100 characters. These are declared data-shape tests; they do not assert
that a real Square account issues those exact IDs or that labels can print every
Unicode SKU.

| Terminal request-status wrapper | Compact UTF-8 bytes | Default ASCII JSON bytes |
| --- | ---: | ---: |
| Same-price projection | 6,943,150 | 17,978,958 |
| Conservative new-price reservation | 6,398,468 | 14,787,876 |

The difference makes the exact compact serialization contract necessary. Tests
preserve all 1,000 receipt targets and exact provider/SKU values. The catalog case
reserves worst-case future generated IDs and verifies an actual supported result
fits inside that reservation. A direct result at exactly 8 MiB is accepted;
adding a byte or the recovery wrapper beyond the cap is rejected explicitly.

## Evidence

- `Testing/receiving-saga/receiving-wire-budget/20261101T222050407822Z/`
  records 11 passing pure/HTTP/desktop-transport cases in 9.881 seconds, with zero
  skips/failures/errors and stable source. Eight are the independent module
  `test_hosted_receiving_projection_budget`; three are the desktop transport and
  encrypted-journal restart fixture. The latter verifies an exact-8-MiB response,
  lost commit response, restart and receipt recovery with one fictional stock
  effect. Delivered UTF-8/LF log SHA-256:
  `1c2133835f3cc2d1d18a6b72276780a9bf922ec542e723e85668f9f0ce8d0c51`.
- `Testing/oauth-receiving/postgres-runs/20261101T221931442661Z/` records 10
  actual PostgreSQL cases in 8.976 seconds, zero skips/failures/errors, exact
  expected/started/successful coverage and stable source. Three are independent
  adversarial cases; seven are the backend projection suite. Delivered log
  SHA-256:
  `31a929363f3a3ce18d727c509f16ec83dd177eaf47b300cbaf55edece50ac349`.
- The independent SQL cases compare the complete operation, provider steps,
  credit entries and credit lots before/after a limit-minus-one rejection. An
  unadmitted old plan retains financial eligibility; a catalog-admitted plan
  retains its financial block, admits no stock on rejection, and later completes
  the same operation once. A 1,000-row completed result with long four-byte IDs
  and Unicode SKU round-trips byte-for-byte through actual hosted HTTP status/get
  and the desktop parser without a second provider effect.
- The backend SQL cases cover oversize creation/revision, rechecking old plans
  before catalog or inventory admission, exact-key retirement/correction, and
  malformed catalog output retaining its original uncertain journal. All provider
  effects are injected fictional objects and external HTTP is forbidden.

## Failed attempts retained

The first independent SQL attempts failed in test setup: a missing reused
`approve` helper, then an invalid test-only `created_at` sort column. Those logs
remain in runs `20261101T221856827796Z` and `20261101T221910410857Z`. A first
combined unit capture in `receiving-wire-budget/20261101T222100Z/` failed because
Windows sandbox temporary-folder ACLs prevented the desktop fixtures creating
their isolated database. The successful rerun used an explicit disposable test
folder. These failed attempts are not passing evidence and required no product
change.

## Remaining limits

No new concrete under-reservation, row-loss or recovery-retry defect was found in
this boundary review. Old or corrupted data exceeding the supported contract
must still fail explicitly and retain its original journal for owner review.
These measurements are serialization/admission checks, not latency, peak-memory
or production-load benchmarks. Actual provider/device acceptance and the final
frozen complete suites remain separate release gates.
