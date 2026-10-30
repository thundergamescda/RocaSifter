# Hosted accepted trades — schema 3

This server workflow records reviewed card facts, a customer-bound offer and
explicit authorized acceptance. Only an accepted **server receipt** may issue
hosted trade credit. No local balance is mirrored automatically, no Square stock
is received, and no cash payment or checkout occurs through these endpoints.

## Roles and receipt states

Owner, manager and buyer may create/read/revise drafts. Custom percentages or
negotiated final offers require an owner/manager reason. Only owner/manager may
accept an offer; the staff member's enabled status and current database role are
checked again inside the transaction. Cashiers cannot read trade costs or trade
request journals. All queries derive tenant scope from the authenticated actor.

Drafts become `accepted_credit`, `accepted_cash` or `voided`. Final receipts and
their lines cannot be updated/deleted through SQL triggers or these APIs. A
draft may be voided with a reason; that retains its evidence and changes no money.
Reversal of an accepted purchase is a separate future reviewed workflow, not a
rewrite or deletion of the original receipt.

## API contract

- `POST /api/trades`: `request_key`, `customer_id`, `lines`; optional `title`,
  `source_reference`, `offer_terms`. A stable local receipt ID should be the source
  reference for desktop uploads. It is unique among every non-voided receipt,
  including accepted receipts forever. Exact source replay returns that binding;
  changed source contents conflict. After a confirmed void, a new request key
  plus `replace_voided_source:true` explicitly starts a replacement in the same
  stable source family. Concurrent replacements still create only one active
  draft. Do not append fresh attempt IDs to bypass source deduplication.
- `GET /api/trades/<uuid>`: persisted lines, customer, revision, formula/negotiated
  offer values and final immutable receipt when accepted.
- `GET /api/trades?q=&limit=&cursor=`: paginated summaries and `next_cursor`.
- `PUT /api/trades/<uuid>`: `request_key`, current `version`, and changed
  `customer_id`, `title`, `lines` or `offer_terms`. Draft only; version increments.
- `POST /api/trades/<uuid>/accept`: `request_key`, `version`, `customer_id`,
  `offer_type` (`credit` or `cash`), `expected_offer_cents`, `customer_agreed:true`
  and `items_received:true`. The expected amount is a stale-display check; the
  actual amount is calculated from locked server receipt facts.
- `POST /api/trades/<uuid>/void`: `request_key`, `version`, `reason`.

A line contains optional UUID `id`, `identity`, integer `quantity`, integer
`unit_intake_cents`, boolean `included` and boolean `reviewed`. `line_total_cents`
in a read response is server-derived and is omitted from write payloads. Identity requires
`name` and `game`; `kind` is `card` or `slab`. Optional card facts include `set`,
`number`, `language`, `printing`, `condition`, `year`, `sport`, `league`, `team`,
`player`, `serial_number`, `manufacturer` and `game_label`. An `other:*` game key
requires its readable label. All game names are preserved; no pricing feed or
scanner compatibility is invented merely because a name is accepted.

Slabs require `grading_company`, `grade` and `certification`; `grade_qualifier`
preserves distinctions such as pristine/black-label tiers. One certificate has
quantity one and cannot occur twice for the same grader in a receipt. An
uncertified slab must remain in the local/review workflow until a separate
identity contract is supported; do not drop slab facts to force a raw-card import.

Offers use server-saved cash/credit basis points (defaults 3,500/5,000, or
35%/50%) and the included intake total, rounded half up once in integer cents.
`offer_terms` may explicitly change basis points or provide `cash_override_cents`
or `credit_override_cents`; an authorized manager must give `override_reason`.
All included items must be reviewed. Receipt and line totals are recomputed and
checked before acceptance. The existing request-size boundary also applies;
oversized imports must fail explicitly with local source work retained.

## Atomic credit and recovery

Credit acceptance commits receipt state, immutable acceptance snapshot/hash,
staff/time/customer evidence, `trade:<server-trade-id>` ledger source, an
unapproved `trade_unreviewed` origin lot, audit and request journal in one
PostgreSQL transaction. A customer lock serializes concurrent acceptance with
other credit operations. Agreement and physical-custody checkboxes are staff
attestations; they are not an independently verified customer signature.

Same-key replay after an uncertain response returns the exact original result.
A new key with the same frozen customer/offer/amount and original/accepted
revision also returns that result without another issue. Changed terms conflict.
`/api/credit/requests/<key>` can confirm trade outcomes for authorized buying
roles. Its `balance_after_cents` is historical; refresh the account for today's
balance. Accepted credit does not imply eligibility for a Square discount:
`credit_policy_state=unreviewed` and `checkout_eligible=false` remain explicit.

Cash acceptance records the agreed amount only. `cash_payment_executed=false`
and `inventory_received=false` describe what this action did, not a promise that
cash was paid or Square counts were changed elsewhere. Customer payment and
inventory receiving remain separate tasks.

No existing customer balances are backfilled or classified. A wallet with
unmatched legacy origin history stays review-only; failed acceptance rolls back
without issuing credit. Tax policy, legal seller/ownership requirements, receipt
form/retention, data rights, accepted-trade reversal and actual device/customer
acceptance remain release gates. The workflow does not enable the reconciliation
worker, checkout, Square OAuth writes or pricing-data monetization.

## Evidence and deployment gate

`python -m unittest -v test_hosted_trades` passes 32 fictional parsing, pricing,
role and HTTP-boundary checks. `test_hosted_trades_postgres` adds 33 private
PostgreSQL cases: exact/lost-response replay, concurrent accept/import, mutually
exclusive cash/credit, stale/changed/customer/version conflicts, immutable SQL
evidence, staff reauthorization, source deduplication, origin policy, pagination,
cash no-payment behavior, audit/journal rollback and a real HTTP session/CSRF to
receipt/credit transaction. The private test class skips without `DATABASE_URL`;
a local skip is not cloud acceptance. Existing 51 hosted database/runtime
acceptance cases remain required alongside the new trade suite.

Schema 3 is additive and does not activate these workflows for old desktop PCs.
The deployment gate migrates, runs existing private acceptance plus the new
trade cases, then checks the actual WSGI runtime. No deployment or production
migration is performed by the trade tests or this document.
