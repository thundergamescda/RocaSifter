# Independent catalog-to-inventory review

Scope: the prototype's server receiving path, immutable journals and plan-request
retirement. All effects were confined to injected fictional provider objects and
disposable local PostgreSQL schemas. No Square HTTP, merchant data, production
migration, deployment, printer or actual checkout was used.

## Confirmed defect, preserved failure and correction

Revoking a source mapping originally left its generated price mappings active.
After finishing a new-price operation, a later physical intake at that generated
price could enter the generic existing-price path and bypass the source mapping's
revocation. The original two-step operation checked source authority, so the
later-plan shortcut contradicted that boundary.

Independent test
`test_source_mapping_revocation_blocks_reuse_of_its_generated_price_authority`
failed with `ReceivingError not raised` before the fix. The source-stable failure
is preserved in
`Testing/oauth-receiving/postgres-runs/20261101T214852547883Z/`.
Delivered failure-log SHA-256:
`bcbe493fa0241e7d3b5ff9c190e0c3bfbe9fbf156ef40c945249904f2c95c770`.
This is an actual failed safety assertion, not passing release evidence.

The backend now cascades source binding → template → generated binding authority
revocation and serializes registry creation/revocation. A catalog result arriving
after source revocation can retain provider proof, but cannot leave a newly active
delegated mapping. It does not delete historical receipts, replay provider writes
or release an uncertain seller lane. Independent tests verify both later-plan
rejection and revocation during an in-flight catalog response.

## Verified safety cases

The independent PostgreSQL module contains 16 cases covering:

- Partial catalog effects, late acknowledgements after read recovery, mismatched
  request hashes and failed database outcome recording preserve the original
  operation and do not prove inventory or create a second catalog write.
- Catalog request history remains catalog evidence. Inventory admission requires
  a separate freshly authorized action; no receipt is produced early.
- Old sibling prices, descriptions, tax/category references, archive state and
  versions cannot drift between catalog and inventory unnoticed. Same-ID category
  renaming, deletion or unreadability invalidates reviewed policy too.
- Current staff/session authority, merchant/application/environment and write
  flags are rechecked before the second step. A separate physical claim cannot
  take the occupied seller lane between the catalog and stock actions.
- A crash after catalog admission retains the durable request and permanently
  blocks new cash/credit acceptance on an inventory-only claim, including a direct
  SQL probe representing older code without the current API check.
- Request retirement locks the original key and claim. A late in-flight planner
  cannot persist after a retirement proof; an existing operation, changed wire
  body or key belonging to another action cannot be retired as an uncreated plan.
- New plans pin readable currency names without changing the old sibling name,
  SKU, price or business data. Catalog-only completion does not issue stock;
  separately authorized inventory still runs once.

The first 15 cases, plus 11 privacy cases, passed on stable source in run
`20261101T220303506416Z`: 26 tests, 41.333 seconds, zero skips/failures/errors and
exact expected/started/successful case coverage. The log and source hashes are in
that run's `postgres-summary.json` and `postgres-tests.log`.

After the name-format change, run `20261101T221142854449Z` passed all 31 selected
PostgreSQL cases: 16 independent adversarial cases and 15 backend pipeline cases,
including legacy saved-body recovery. There were zero skips/failures/errors,
complete expected/started/successful case coverage and no source drift. The
delivered log SHA-256 is
`5b3b782c8a7119116537d3731aacb9a28cf66d14df84118795d25c484e6510ec`.
This is a selected diagnostic run, not the complete hosted or desktop suite.

The pure catalog helper separately checks full parent/sibling preservation,
complete unique ID mappings, operation/review marker, exact price/tracking/location,
resource bounds and request reconstruction. A retained SKU by itself is not proof
of this operation's creation. Later sibling additions are tolerated only while
all frozen expected business facts remain preserved.

## Retirement and name-format contracts

Retirement is an explicit server action. Generic 400/409 responses or `found:false`
do not authorize clearing an uncertain request.
The proof binds request key, original canonical wire SHA-256, `retired=true`,
`state=retired_before_creation`, null receiving ID and the explicit basis
`registered_original_wire` or `unregistered_key_no_operation`. Existing operations
return a conflict and operation ID for recovery. Clients must durably preserve
that exact proof before allowing a reviewed correction; timeouts remain uncertain.

New currency names use a pinned private addition template format version 2,
for example `Sticker price · $12.50`, using integer cents. Previously saved
additions without a pin retain legacy cents names and exact old marker/body bytes.
The golden legacy catalog-request SHA-256 is
`2af736e6accdb49faa3fd7b7f1d05e7aabb4688843034b805caf23096b8b1eef`.
The SKU and selling-price algorithms are unchanged. Cross-version marker/body
reconstruction mismatches fail closed.

## Remaining release gates

This is scoped development evidence, not whole-prototype or consumer acceptance.
Final frozen suites, actual OAuth/provider behavior and controlled Square tests,
physical scanner/printer/device trials, hosted restore/operations and legal/tax
decisions remain separate. Server and tenant write flags remain an explicit
admission boundary. Unknown outcomes never justify replacing a saved provider key,
silently editing old stock or falling back to a desktop Square token.
