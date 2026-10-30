# Central physical intake claims (development schema 5)

New hosted cash agreements and store-credit trade acceptance require a central
physical intake claim for every tenant. This closes the demonstrated case where
the same CSV imported on two computers produced different local receipt IDs and
two credits. Existing accepted receipts and their exact request journals remain
readable/recoverable without reissuing credit or fabricating original manifests.
Schema installation adds nullable links to old receipts; it does not classify,
backfill, reverse or move existing customer balances.

## Staff authority and physical truth

An enabled owner/manager can create/read a claim using a current store session.
NEW financial acceptance and a separate identical physical-delivery attestation
require enrolled MFA with a proof no older than 15 minutes. The transaction locks
the current staff, MFA and session records and checks the database role, tenant,
session ownership, expiry and MFA version. Submitted actor/MFA flags are not
authority. Aged proof does not prevent recovery of a committed acceptance; the
authenticated API and current role still apply.

A hash does not establish that a physical delivery exists. All computers must
use the same store batch reference/tag for one physical stack. Managers explicitly
attest a separate delivery when identical evidence represents additional actual
goods. A new PC UUID, request key, local receipt ID or location cannot silently
grant another credit. Deceptively different input cannot be detected perfectly
by software; central intake procedures remain necessary.

## Manifest and claim API

The shared `intake_manifest.py` helper captures original rows BEFORE staff edit,
include/exclude or change prices. It accepts only card identity, original
quantities, optional source positions and the CSV source hash. It does not accept
customer contact, payment credentials, pricing or credit amounts. Its canonical
digest binds original order/positions; its semantic fingerprint groups identical
identities/quantities independently of order, CSV encoding and local row IDs.
The original identity key preserves distinct grades, grade qualifiers,
certifications, sports serials and manufacturers. No identity is inferred from a
SKU alone. Incomplete slabs are an explicit capture/review gate, not stripped.

`POST /api/intake-claims`:

```json
{
  "request_key": "persist-before-network",
  "physical_reference": "store-batch-2026-001",
  "source_manifest": {"version": 1, "kind": "manual", "rows": [], "sha256": "helper-generated"},
  "physical_delivery_verified": true
}
```

The illustrative manifest above needs real nonempty helper-generated rows/hash.
Batch references have 3–100 ASCII letters/digits/spaces or `._:/-`, begin with a
letter/digit, and are trimmed/casefolded. Request keys use 1–150 ASCII letters,
digits, `_` or `-`. The helper caps a manifest at 1,000 original rows, 100,000
units per row, 1,000,000 total units and 512 KiB; the API's overall request limit
is also 512 KiB. Larger inputs require an explicit supported workflow, not silent
source splitting to evade a claim.

Responses include `physical_intake_id`, `version`, `state`,
`physical_reference`, `manifest_hash`, `semantic_hash`, the immutable
`source_manifest`, `bound_trade_id`, `additional_delivery_of`, timestamps and
policy facts. A claim is initially `open` at version 0. Equivalent exact source
retries recover the original claim; changed manifests sharing a raw or semantic
source return a conflict and the existing safe claim ID for explicit review.
The server never substitutes a reordered client manifest for the stored one.

`GET /api/intake-claims/requests/<request_key>` returns `{found,result}` for the
original immutable create result, even if the claim was subsequently consumed.
`GET /api/intake-claims/<uuid>` returns current state. Recovery must inspect the
latter before starting a new acceptance. A client can explicitly select an
existing claim after a definitive conflict only after validating its original
source-line quantities; a pending additional-delivery request must not be
silently downgraded to a base claim.

A legitimate additional identical delivery adds `additional_delivery_of`,
`additional_delivery_confirmed:true` and a 10–200 character
`additional_delivery_reason` to the create body. The prior claim must be in the
same tenant and matching source family. Fresh enrolled MFA is mandatory. Exact
committed request replay returns its original result rather than creating again.

## Trade binding, one acceptance, and recovery

Trade create/update may include `physical_intake_id` and
`physical_manifest_hash`. Every reviewed line retains its original
`source_line_key` from the manifest helper. Explicit reviewed identity corrections
are permitted; their original key and aggregate original quantity cap remain.
Split rows, different local IDs or excluded rows cannot multiply that budget.

Multiple drafts can reference an open claim. The acceptance transaction locks the
claim, validates original evidence and stored reviewed line hashes/totals, requires
`claim_reviewed:true`, and exclusively consumes it for one trade. The immutable
receipt includes direct physical ID/hash links and original, selected and
excluded quantity maps. An excluded quantity is not available for a later trade
or automatic continuation. The claim becomes `consumed`, version 1, permanently
bound to that accepted trade. Cash means an agreement: no payment is executed.
Credit still originates as `trade_unreviewed`; it is not an approved tax lot or
checkout authorization.

An old pending create/accept journal keeps its exact request body, key and
fingerprint. Recover the original create first. A confirmed unaccepted draft
with a genuine original manifest can be attached by a NEW versioned PUT request;
never rewrite the pending create. A legacy receipt without original evidence
requires reimport/review. Old accepted receipts recover their exact original
result without needing a new claim, a new proof or another issuance.

Draft void still permits the current enabled owner, manager or buyer with a
current session (fresh MFA is not required). Void and accept serialize on the
same trade row. A confirmed void leaves an open claim available for a replacement
draft. It cannot release a consumed claim or reverse final credit. For an
uncertain legacy accept, a separate versioned void can either confirm a still
draft receipt or conflict because acceptance already committed; recover the
accepted result in the latter case. Preserve the original journal until terminal
state is confirmed.

Actionable typed errors include `PHYSICAL_INTAKE_CLAIM_REQUIRED` (428),
`SOURCE_MANIFEST_REQUIRED` (428), `PHYSICAL_INTAKE_REVIEW_REQUIRED` (428),
`PHYSICAL_INTAKE_CONFLICT` (409), and `MFA_STEP_UP_REQUIRED` (403). Responses may
include only the current tenant's relevant physical claim ID. Authentication,
current-role and CSRF errors retain their existing status codes.

## Verification and remaining work

The preserved pre-fix evidence records two credits for one fictional CSV across
two local receipt IDs. After the guard, independent PostgreSQL tests observe
two unclaimed acceptance rejections and zero credit; claimed parallel acceptance
permits one credit. Focused tests cover duplicate request/source recovery,
tenant/role/session/MFA enforcement, selected/excluded budgets, cash agreement,
void/replacement, failure rollback, immutability and a real schema4-to-schema5
upgrade with an existing accepted credit and exact old request journal. Tests
create/drop only `ss_test_<uuid>` PostgreSQL schemas and forbid provider HTTP.

No hosted receiving/catalog/inventory write API is activated by this change.
Future receiving may bind the SAME consumed claim only to its matching
`bound_trade_id` and immutable accepted quantities: one matching inventory
operation, no second receipt/continuation. Existing trusted identity plus exact
price/SKU mappings come first. New parent creation remains OFF until an owner
reviews a server catalog/tax template; unsafe SKU-only adoption and a new price
under an unmanaged existing parent remain review gates. Square credentials stay
server encrypted, and new provider steps still need verified actual OAuth grants,
fresh MFA, location authority, durable exact request bodies/keys and uncertainty
recovery. Checkout, reconciliation activation, Idaho tax approval, commercial
release and production migration/deployment remain separate gates.
