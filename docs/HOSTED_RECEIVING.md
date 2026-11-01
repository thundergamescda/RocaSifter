# Hosted receiving prototype

This source is isolated in `Development/receiving-saga-prototype`. It has not
enabled receiving, migrated a merchant database, made a real Square mutation or
replaced the frozen published pilot. Passing fictional-provider tests does not
establish live Square/device acceptance or commercial readiness.

## Authority and physical delivery

A central physical claim must precede receiving. Accepted-trade receiving uses
the claim's exact accepted trade/hash and server-derived line identity/quantity;
it cannot invent purchase cost, another credit or a cash payment. Inventory-only
receiving requires explicit external ownership and an open claim. Admission of
ANY catalog or inventory mutation permanently blocks subsequent new financial
acceptance of that inventory-only claim. The PostgreSQL guard also protects an
older server that omits the application check.

Plans freeze the original manifest, reviewed source snapshot, selected/excluded
budgets, label stock/layout/DPI, provider identity/location, catalog source and
template evidence. One physical claim retains one receiving operation. Before
the first provider admission, an explicit reviewed revision uses the same
operation and preserves old plan hashes. After admission, uncertainty never
permits a changed source, location, price, body, key or continuation claim.

Every new provider step checks a live enabled owner/manager session, its current
database role, enrolled MFA/version and proof age (at most 15 minutes). Recovery
is read-only. A second PC's currently authorized manager may explicitly authorize
the stock step of the SAME confirmed-catalog operation after strict local
canonical binding; its client must persist the new authorization request first.
It cannot create a second inventory step/key. Role/session changes stop later
admissions; an already admitted call can finish and its outcome is retained.

## Square access and owner setup

Standard `/api/square/connect` with `{}` still requests the existing read-only
profile. Explicit receiving consent uses:

```json
{"profile":"receiving","receiving_permissions_confirmed":true}
```

The owner sees why `ITEMS_WRITE` and `INVENTORY_WRITE` are requested and must have
fresh enrolled MFA. The five-minute state pins purpose, app, environment, session
and requested scopes. Callback and final encrypted credential storage recheck
owner authority. Requested permissions and a saved connection never prove grants
or enable receiving. The server introspects the exact token before mutation and
uses that same token for the admitted call; it never sends Square credentials
to the desktop. Unsupported Sandbox proof remains closed.

`/receiving/catalog` lets an owner review an existing provider variation against
full card/slab/sports identity, retained SKU, effective price, location and
tracking. Approval is a fresh-MFA application mapping, without a Square catalog
edit. SKU/name/price alone never automatically adopts an unmanaged variation.

`/receiving/price-templates` reviews a source mapping's existing tax/category and
location settings before allowing a new-price sibling family. Required tax
settings must be explicit/readable; unknown defaults are not approved. The source
SKU may be an exact trusted printable string, including spaces, punctuation or
Unicode. New price SKUs use the existing deterministic legacy hash and are
checked for seller-wide ambiguity. The source SKU and old stock are preserved.
Template approval is not an Idaho credit-redemption tax approval.

Revoking a source mapping or template revokes the recursively delegated family
of generated mappings/templates under an authority-allocation lane. It retains
records and uncertain provider work. A late catalog acknowledgement cannot
recreate active delegated authority after revocation. A new independent mapping
requires another explicit owner review.

## Catalog then stock

Existing approved, tracked, same-price variations use one inventory step and no
catalog mutation. A new price uses an owner-approved existing-parent template:

1. Persist the exact encrypted catalog request, UUID key, API version and source
   version guards before HTTP. Copy the complete parent and all existing
   siblings/business fields, then append the new price variations. Coalesce
   duplicate reviewed price targets while retaining every original row alias.
2. Validate the returned mappings and preserved objects, or recover a lost
   acknowledgement from exact desired catalog state with operation/review
   markers, SKU, price and complete old-field preservation. A SKU by itself is
   not sufficient evidence. Keep the seller lane occupied during ambiguity.
3. Return `catalog_confirmed` with authoritative minimal resolutions. No stock
   is added by GET or Recover. A fresh explicit Authorize then admits the exact
   inventory step, with another current role/session/grant check.
4. Apply additive `NONE` to `IN_STOCK` adjustments. New API `2026-09-16` uses
   both `from_location_id` and `to_location_id`, equal for one location. Legacy
   local requests keep their old API/body/key and are never rerouted.
5. Validate the successful exact-request acknowledgement or complete matching
   history (reference, application, amount, time, target, states and both
   locations). Current stock count alone is not proof of this receive. Recheck
   catalog/SKU/policy before issuing an immutable received/label receipt.

The new-price request preserves full existing-parent fields and sibling order,
with bounded object/byte limits. Global SKU searches and history consume complete
bounded pages or fail for review. Old price variants are not repriced/deleted.
New parent creation remains disabled and unsupported until a separately approved
real-store catalog/tax template and acceptance path are implemented.

New private additions pin name format version 2, displaying a readable price
such as `Sticker price · $12.50`. Saved additions without that pin retain their
original cents-based names, marker and exact encrypted provider request bytes.
An upgrade never rewrites an admitted catalog journal to improve its label.

Execution leases/fencing prevent stale workers advancing another attempt.
Durable seller occupancy outlives the lease. Lost responses, crashes, malformed
responses and partial catalog success retain exact journals and review state;
there is no automatic compensation, rekey, deletion or blind retransmission.
Provider idempotency is not promised to last indefinitely.

## Desktop proof contract

`/api/receiving/readiness` distinguishes implementation capability, server/store
write gates and new-price gate. `client_label_profile` is exactly
`{stock:2x1|3x1|4x2, layout:standard|fold|side, dpi:203|300}` and is frozen in the
plan and receipt. The desktop must prove exact target SKU geometry/device support
before committing; the server does not certify a printer or scanner.

`create_price` plans expose a source-family binding and a known target SKU, with
the new variation ID initially null. The plan hash and targets stay immutable.
Confirmed-catalog status exposes a minimal proof/hash and resolutions containing
all coalesced reviewed row IDs, actual variation, target SKU/price, template and
resolved binding evidence. Final confirmed targets retain source binding
ID/version/evidence and the matching resolved target binding. The receipt echoes
the catalog proof/resolution hashes and list. The desktop must validate the full
source-to-resolution transition rather than accepting a success boolean.

An authorized owner or manager on another computer may explicitly Authorize
stock on the same confirmed-catalog operation after proving the exact canonical
claim/trade/source/price/profile/resolution binding. Read-only recovery creates
no stock authorization. Concurrent authorizations still use the one durable
server inventory body/key and seller lane.

Typed plan/operation/request responses have an exact 8 MiB (8,388,608 bytes)
compact UTF-8 JSON limit. Other response routes retain their independent limits;
request input remains 512 KiB. Before creating/revising a plan or admitting a new
provider step, the server measures a conservative terminal projection including
all repeated receipt rows, resolution aliases, request-status wrapper and future
255-character provider IDs. An oversized result returns
`RECEIVING_PROJECTION_LIMIT` before a new provider action. It does not truncate
rows or return raw provider bodies. A rejected uncreated plan still requires the
exact-key retirement protocol before a desktop intent can be corrected; an
admitted operation must retain its original journal for review/recovery.

## Correcting rejected plans safely

The client persists the original plan body/key before network. The server
registers only immutable hashes and typed workflow/accepted-receipt references
before provider preflight. Generic 4xx, timeout or `found:false` never proves a
pending desktop intent can be cleared.

`POST /api/receiving/requests/<original_key>/retire` accepts only:

```json
{"original_plan":"the exact original saved JSON object, including request_key",
 "retire_uncreated_request":true}
```

The illustrative string above represents the full original object. Retirement
locks the same request key and physical claim, matches the registered wire hash
where present and proves no operation exists. An immutable tombstone includes
the exact original canonical JSON SHA256 and either
`registered_original_wire` or `unregistered_key_no_operation` as its basis.
It permanently blocks late creation under that key, including an earlier
in-flight preflight. Invalid unregistered bodies can be retired authoritatively;
this is a server tombstone, not an inference from a prior error. The desktop
clears only after matching that proof and durably recording its local transition.

If any operation already exists for the original request or same claim, the
server returns its recoverable ID rather than retiring the intent. Existing
accepted/admitted/uncertain operations keep their original identity and journals.
Changed original wire bodies conflict. Correction uses a new explicitly reviewed
key only after confirmed retirement; it never evades a physical operation slot.

## Evidence and remaining gates

Focused pure, HTTP, real PostgreSQL and actual Chrome fixtures cover source and
template authority, per-step concurrency, lost responses/crashes, full sibling
preservation, price splits/coalesced aliases, arbitrary source SKUs, both modern
inventory location fields, permanent claim/lane behavior and retirement races.
Independent failing-then-fixed evidence includes delegated authority surviving
source revocation. Reports distinguish fictional transport, actual database and
real browser scope. Root consolidation must rerun on the final frozen snapshot.

Privacy export projects only the explicitly scoped safe receiving evidence
linked through the exact accepted customer/trade. Raw encrypted requests, other
catalog siblings, credentials and inventory-only/competing draft intentions are
excluded. Operational retention/offboarding, actual provider and device tests,
App Marketplace requirements, production activation, legal/tax approval and the
separate new-parent path remain release gates. All receiving mutation switches
default off; these tests are not a blanket consumer-ready or flawless claim.
