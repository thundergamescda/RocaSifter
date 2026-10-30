# Desktop to hosted trade-credit bridge

The desktop bridge starts from an unreceived, reviewed local trade-in batch and
an explicitly selected shared customer. Existing accepted local buys cannot be
converted into shared credit. The local wallet and shared ledger remain distinct.
Only included rows are sent: between one and one thousand reviewed items, with
their full card/slab identity. Excluded collection rows stay local and do not
block a selected trade because of incomplete excluded slab or price fields.

The operator opens Shared trade, chooses the shared customer, saves the hosted
draft and reviews the hosted customer, cards and credit offer. Hosted store
rates can differ from the local estimate; the saved hosted amount is shown
before acceptance. An owner or manager must confirm both customer agreement and
physical receipt of the included cards. Acceptance issues shared credit through
the hosted accepted-trade API. It does not pay cash, receive Square stock,
enable checkout or approve tax treatment. Issued trade credit remains in the
unreviewed policy class.

## Durability and recovery

1. The desktop saves a local intent and canonical source hash before mutation
   I/O. Pending intent locks receipt edits, row additions/splits, automatic price
   changes, price approvals, delete, local acceptance, labels/files and receiving.
2. The dedicated encrypted `cloud.pending` vault saves the exact mutation body
   and request key before sending create, accept or void. A confirmed response
   remains journaled until the corresponding local state has been saved.
3. After a lost response or restart, Check saved trade first checks the original
   request and reuses its exact body/key if necessary. It never substitutes a
   newly generated acceptance key for an uncertain action.
4. Confirmed acceptance is fetched as a hosted receipt. Its hash, source, customer,
   lines, offer and acceptance evidence must match before a local frozen
   `purchase.scope = hosted` mirror is saved. A mismatch stays locked and shows
   an owner-recovery error; the app does not guess or issue another credit.
5. Cancellation resolves an uncertain create first and explicitly voids the
   hosted draft. A missing request lookup alone never unlocks the local source.
   A pending acceptance is recovered as acceptance rather than converted into a
   void. A confirmed void unlocks the batch without issuing credit.

The source reference includes the original local receipt ID and SHA-256 of its
immutable CSV/manual source reference. This distinguishes coincident short
receipt IDs across computers while copied receipts keep the same hosted source.
After a confirmed void, a new explicit attempt retains the source reference and
sends `replace_voided_source: true`; the prior void binding stays in local
history. Accepted sources cannot be replaced. No per-attempt random namespace
is used to bypass hosted duplicate protection.

The bridge never sets `tracked_credit` or calls the local wallet issuance path.
The local wallet also rejects a shared-linked trade directly, including a
confirmed hosted purchase. The hosted receipt remains authoritative; a later
retail label-price correction does not rewrite accepted intake evidence.

## Integration

- `station_shared_trade.py`: source mapping, local intent, snapshot verification,
  create/accept/void orchestration and confirmed mirror.
- `station_cloud.py`: fixed-origin relay, staff sessions and encrypted exact-body
  pending journal. Generic credit recovery routes trade actions back to their
  saved local receipt.
- `POST /api/cloud/trade-prepare`, `trade-status`, `trade-accept`, `trade-cancel`:
  loopback relay endpoints with the same session/Origin boundary as Shared credit.
- `shared-trade.js` / `.css`: separate shared customer selection, hosted offer,
  explicit acceptance, pending recovery, cancellation and authoritative receipt
  view. Confirmed hosted purchases do not offer local-wallet issuance.

This changes the meaning of linked local receipts. Do not open a migrated pilot
profile with an older pre-bridge binary: it cannot enforce hosted scope or
pending locks. Keep the private pilot profile separate and retain a backup.
Safe downgrade of hosted-linked receipts is not supported.

## Validation and remaining gates

`test_shared_trade.py` uses a deterministic fictional transport and exercises
restart, lost responses, disk-write interruption, source mismatches, duplicate
binding, role/session/store restrictions, explicit agreement, cancellation and
local-wallet denial. Hosted PostgreSQL transactions are tested separately.
`tools/user_acceptance_shared_trade.py` uses real headless Chrome with the same
fictional hosted transport. It blocks external browser/backend requests and
Square uploads, and records source hashes and screenshots under Testing/2.6.0.

These tests do not prove a deployed hosted-to-desktop trade, live Square
checkout, multi-store production operation, tax approval, hardware behavior or
universal consumer readiness. The hosted trade routes must be deployed and
accepted in an isolated pilot before this bridge can be offered to real stores.
