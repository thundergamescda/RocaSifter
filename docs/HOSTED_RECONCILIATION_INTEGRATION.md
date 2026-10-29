# Hosted credit reconciliation integration

Status: implemented locally; **worker disabled by default, no live activation**.
Reconciliation data introduced in schema version 2 includes immutable terms/settlements, origin-preserving credit lots,
durable exceptions and job leases. `hosted/settlement.py` atomically consumes or
releases reservations and restores only proven itemized returned credit.
`hosted/reconciliation.py` retrieves authoritative tenant resources using the
existing read-only OAuth adapter. Neither module performs Square writes.

## Provider contracts checked on 2026-10-28

- Square defines `COMPLETED` orders as fully paid and terminal; `CANCELED`
  orders are unpaid and terminal. Open/draft/stale snapshots retain the hold.
  [OrderState](https://developer.squareup.com/reference/square/enums/OrderState)
- Match the configured catalog discount ID and its order-local UID. Consume
  `applied_money`; the declared `amount_money`, discount name, order total and
  other coupons cannot authorize the credit amount.
  [OrderLineItemDiscount](https://developer.squareup.com/reference/square/objects/OrderLineItemDiscount)
- The completed order's discount `catalog_version` must equal the immutable
  approved version. Before spending or releasing completed zero-use credit, the
  worker retrieves that exact `CatalogDiscount` version through tenant OAuth and
  verifies object ID/version/type, matching amount-discount type, USD fixed money
  where present, and explicit `modify_tax_basis`. There is no latest-version or
  default-tax-basis fallback. Unavailable/malformed/mismatched evidence preserves
  the full hold and creates durable review; lease/revocation checks still apply.
  A minimal catalog proof hash/version/tax-basis record is committed in the
  existing audit transaction; raw catalog metadata is not persisted.
  [Versioned catalog retrieval](https://developer.squareup.com/docs/catalog-api/retrieve-catalog-objects),
  [Catalog tax basis](https://developer.squareup.com/reference/square/enums/CatalogDiscountModifyTaxBasis)
- A return identifies the original sale through `source_order_id`; returned
  discounts reference the original `source_discount_uid`. Restoration uses the
  returned tracked discount's `applied_money`, capped by the original credit
  actually consumed, including all prior restorations.
  [OrderReturn](https://developer.squareup.com/reference/square/objects/OrderReturn),
  [OrderReturnDiscount](https://developer.squareup.com/reference/square/objects/OrderReturnDiscount)
- A cash/card refund amount does not establish how much store credit should be
  restored. The current planner verifies retrieved completed payment refunds
  against the return order's documented `tender_id + '_' + id` references.
  Multiple refunded tenders produce one aggregate credit-return source key.
  [Refunds and exchanges](https://developer.squareup.com/docs/orders-api/order-returns-exchanges),
  [PaymentRefund](https://developer.squareup.com/reference/square/objects/PaymentRefund)

Custom/unlinked returns, exchanges, missing allocation and zero-payment return
paths are not guessed or allocated proportionally. Positive credit-return plans
require explicit itemized discount allocation and completed refund proof.
Those unsupported paths need an owner-approved policy and actual Square/device
fixtures before automatic handling. Missing tracked allocation produces no
restoration, regardless of cash refunded.

## Persisted evidence and origin allocation

`record_reservation_terms` writes server-owned merchant/environment, location,
customer, USD amount, catalog discount/version/tax-basis, order discount UID and
minimum order version, store policy version and approved origin-lot allocations
into `ss_reservation_terms` in the same transaction as the hold. Its canonical
hash is verified when read. SQL triggers reject evidence updates/deletes.
Browser-supplied approval/allocation data is never accepted. Existing incomplete
reservations stay in durable review; no snapshot is inferred from current store
settings during settlement.

`ss_square_settlements` binds the actual debit entry/amount, discount UID, terms
hash, consumed lot allocations and terminal order version. Reservation state
tracks the highest observed version. Old snapshots cannot overwrite a terminal
result. Terminal conflicts create durable tenant-scoped exceptions and audit;
they never rewrite a debit or create a new hold.

`ss_credit_returns` uniquely binds tenant/return-order/source-sale, actual returned
credit, original settlement, completed refund references, allocation/proof hash
and ledger entry. Cap checks and restoration run under the original customer's
lock. Multiple refunded tenders cannot issue duplicate credit. Restored lots
retain the consumed origin policy and parent-lot provenance; a revoked policy
does not become eligible again merely because credit is restored.

`ss_credit_origin_policies` defaults to unapproved. FIFO allocation uses only
explicitly owner-approved, unrevoked origins compatible with the verified catalog
discount/version/tax basis. Opening balances and generic corrections create
unknown/unapproved lots. Manager removals burn only unreserved lots. The lot sum
must equal the wallet balance; unmatched historical balances require an explicit
owner-reviewed migration. There is **no automatic legacy balance backfill**.
Policy approval/classification UI and legal evidence remain release work; no
Idaho tax classification or real policy approval has been invented.

## Worker contract

The worker uses `FOR UPDATE SKIP LOCKED`, bounded attempts/backoff, expiring lease
tokens and ownership-checked heartbeat/finish updates. Provider reads occur
outside ledger locks. It processes current orders, follows payment-to-order
references, and retrieves return/refund proof before restoration. Retries and
lease recovery cannot spend twice. Exhausted/ambiguous jobs become durable
exceptions; they do not expire/release holds. A tracked discount used without a
bound reservation becomes review, rather than a blind customer debit.

The adapter locks customer before reservation/lots and commits debit, lot changes,
immutable settlement, reservation state and audit together. Human role checks
belong at the API/enqueue boundary. The worker's CLI exits without database or
provider work unless `SS_RECONCILIATION_ENABLED` is explicitly enabled. Do not
enable it in production before provider/device/tax acceptance. Exception-review
UI, audited resolution controls and operational monitoring remain required.

Square's Order reference says native orders not created through the API can lack
version numbers. This initial snapshot contract requires versioned API-bound
orders; native/versionless and zero-payment return paths remain review-only until
their reconciliation/device contract is proven.
[Order.version](https://developer.squareup.com/reference/square/objects/Order)

## Evidence

Local fictional-resource checks:
`python -m unittest -v test_hosted_credit_lots test_hosted_settlement test_hosted_reconciliation`
passes 62 tests. Private PostgreSQL checks:
`python -m unittest -v test_hosted_settlement_postgres test_hosted_reconciliation_postgres`
contain 51 real-transaction cases and create/drop only fresh `ss_test_<uuid>`
schemas. They cover concurrent replay/holds/returns, policy revocation,
cross-tenant rejection, partial consumption, source provenance, stale/changed
terminal resources, audit-fault rollback, job leases, retry interruption and
revocation during reads. Fictional approval fixtures hard-refuse normal schemas.
Local runs skip both PostgreSQL classes without `DATABASE_URL`; that is not
passing cloud evidence. No live Square, tax or hardware acceptance is claimed.

The consolidated development run
`Testing/next-security/postgres-runs/20261028T224636577691Z` passed all 129 cases
on disposable local PostgreSQL 18.6 with TLS, including these 51 reconciliation
cases and the existing trade/MFA/privacy/integration cases. Expected, started and
successful case IDs matched exactly; zero skips/failures/errors and unchanged
source hashes were recorded. This is actual isolated SQL evidence, not proof of
Square consent, provider checkout behavior, cloud operation, hardware or Idaho
tax approval. Those release gates remain closed.
