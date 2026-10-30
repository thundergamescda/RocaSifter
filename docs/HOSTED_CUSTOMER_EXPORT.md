# Hosted customer export — isolated security development

Implemented October 28, 2026, **only in `Development/next-security`**. This
source does not change the verified project-root checkpoint/download and has
not been deployed to a store or used to export actual customer records.

The feature prepares a copy of one verified customer's structurally linked
records in one hosted store. It does not certify compliance, determine legal
request deadlines, search every system, or mark a legal rights request fulfilled.
Deletion, anonymization, Square offboarding and record-retention decisions
remain separate release gates.

## Owner workflow and authorization

The owner opens **Customer data export** from the hosted workspace and selects
the independently verified person. Lookup displays only a name and account ID;
it returns at most 100 search matches, so narrow a broad search. That lookup
limit does not limit the selected customer's exported history.

The owner must type the selected account ID, choose an access/portability/
correction-review purpose and verification method, supply an internal case
reference, and attest to identity, authority, scope and private copy handling.
An authorized representative requires an additional authority attestation.
The app records the owner's attestation; it does not independently verify a
person's identity. Do not enter identity-document numbers, payment details or
contact information in the case-reference field.

Owner role and the same active session are checked at start, during generation
and at final confirmation. Staff revocation, logout, expiry or an incompatible
MFA proof stops the export. Sign-in must be within the last 15 minutes: the MFA
proof time is used for an enrolled account, otherwise session creation time.
These freshness and resource limits are engineering defaults, not record-
retention periods or a determination of legal identity-verification requirements.
The export form never requests passwords, authenticator or recovery codes.

Cookie requests require the existing CSRF protection and origin/host boundaries;
store scope comes from the authenticated session. The explicit expected-store
and typed-account checks also reject a stale view. A manager/buyer/cashier cannot
use the privacy lookup, export or confirmation APIs.

The privacy page listens to the portal's random, non-personal auth epoch through
storage events and BroadcastChannel. A change aborts the request, clears buffers
and customer details, and prevents a late download after another sign-in.
Delayed old refresh/search errors cannot invalidate a newer successful view.
Immediately before download, the page rechecks its generation, account, auth
epoch, current session-specific CSRF value and the server verification audit.

## Exact hosted selection

`hosted/privacy.py` defines explicit column projections and tenant/customer
joins. No authentication, credential, session, invite, MFA, recovery or Square
token table is selected. The version-2 export has these 18 collections:

| Collection | Link and contents |
| --- | --- |
| `customer` | Selected account identity/contact, Square reference, recorded purpose, balance and version. |
| `credit_entries` | All entries for that account, including cents, balances, references, reasons and actor IDs. |
| `credit_lots`, `lot_mutations` | Account lots and their issuance/debit/return provenance. Both sides of mutation links must belong to the account. |
| `reservations`, `lot_reservations` | Account reservations and their lot allocations, including consumed/restored amounts. |
| `reservation_terms`, `square_settlements`, `credit_returns` | Stored selected checkout/settlement/return evidence linked through account reservations and entries. No provider request is made. |
| `trade_receipts`, `trade_lines` | Account drafts/final receipts, complete accepted-receipt copies, offer facts, lines and recorded source hashes. |
| `physical_intakes`, `physical_intake_requests` | Original full source manifests, physical reference, linked-delivery attestation, consumption state and request evidence for consumed claims bound to the account's accepted trade. Excluded source rows remain original evidence. Unconsumed claims, competing draft references and unrelated claims require separate ownership review and are omitted. |
| `idempotency_records` | Existing request metadata/results directly linked to this account's entries, reservations or trades. |
| `reconciliation_exceptions` | Exceptions with a directly linked account reservation. |
| `origin_policies`, `policy_revocations` | Policy versions/revocations referenced by this account's lots. |
| `audit` | Actions with direct customer/entry/reservation/trade/intake links, using an explicit detail projection. |

Cross-customer association inconsistencies fail the export; they are not
silently treated as legitimate links. Credential-like JSON keys are removed
defensively and counted. Authentication data is not included merely because
an audit row is relevant. Raw internal audit fields outside the subject-facing
projection are excluded. Stored evidence hashes describe the original evidence;
they need not match a redacted projection. The independent export hash covers
the actual emitted content.

The scope manifest explicitly excludes duplicate or unlinked accounts/mentions,
local desktop records and journals, scanner/printer files, previous exports,
support copies, backups and provider-held records. Ambiguous/unlinked events
are not guessed into the selection. Free-text references or notes can contain
incidental third-party details and require owner review before delivery. A
complete hosted snapshot is not a complete worldwide subject-access search.

## Consistency, bounded resources and interrupted-copy handling

Export reads use one PostgreSQL **REPEATABLE READ, READ ONLY** transaction.
Independent counts and all selected records come from that same snapshot,
including while other requests commit new credit. There is no last-100-entry
cutoff. Each collection has deterministic ordering and uses a named server
cursor rather than materializing the full query result.

Current engineering limits are:

- One active export per service worker; a pool smaller than three connections
  is refused so fresh authorization/audit checks retain capacity.
- Cursor fetch of one row, at most 50 records per page, a 1 MiB single-record
  default and a 2 MiB page limit with room reserved for the envelope.
- A 120-second generation limit, 128 MiB stream limit, per-query timeout and
  idle-snapshot timeout. Exceeding a limit fails explicitly instead of truncating.
- A 32 MiB browser download buffer. A larger copy is rejected clearly with no
  completed download; an operator-assisted larger export remains separate work.

New export payloads are not persisted in a server file/table. Cursors and the
snapshot close on interruption, and the worker slot is released. The browser
discards canceled, incomplete, oversized or invalid data and revokes temporary
object URLs. A successfully downloaded file is a new copy under the owner's
control; automatic deletion of that file, local copies or backups is not
implemented or promised.

## Format and verification

`POST /api/privacy/customers/<customer UUID>/export` returns UTF-8 NDJSON:

1. A `manifest` line identifies the schema/version, store/account, snapshot,
   all collection counts, attestation metadata, limits and scope exclusions.
2. Ordered `page` lines carry records with page sequence and record counts.
3. One final `complete` line carries observed counts, page count, byte count
   before the footer, redaction count and a SHA256 chain over manifest/pages.

For verification, start with 32 zero bytes. For each exact UTF-8 manifest/page
line including its LF, compute `SHA256(previous_digest || line_bytes)`. The
footer is excluded. Check its digest, bytes, page sequence and all counts against
both the manifest and observed records. A missing/invalid footer, extra trailing
data, error line, changed session or transport failure authorizes no completed
download.

After browser verification, `POST /api/privacy/exports/<export UUID>/confirm`
checks the same owner/session, persisted normal stream-end audit, exact counts
and digest. Confirmation is idempotent. Only then does the UI trigger a file
download. Neither the hash nor confirmation is a publisher signature or proof
that a customer received the file.

Audit events distinguish authorization/start, server stream ending and
**client-reported verification**. They contain IDs, method/purpose, a hashed
internal case reference, counts and digest—not names, contacts, exported payloads
or plaintext credentials. A case-reference hash is not an anonymity guarantee.
An interrupted or unverified response does not create a client-verified audit.
Even the verified event explicitly states that customer delivery is unconfirmed.

## Scoped evidence as of October 28

- `test_hosted_privacy`: 17 pure/HTTP cases passed, including attestations,
  role/tenant/CSRF/origin boundaries, recency, expiry/revocation, reserved pool
  capacity, cancellation, audit failure, more than 100 entries and seven 700 KiB
  records split across bounded pages without losing a record/count/hash.
- `test_hosted_privacy_postgres`: five cases passed within the complete
  121-case PostgreSQL18.6+TLS run `20261028T223546241790Z`: zero skips/failures/
  errors, exact coverage and stable source. All tables were disposable
  `ss_test_<uuid>` fixtures; no merchant tables or provider calls were used.
- `Testing/next-security/privacy-export/privacy-user-acceptance.json/log`:
  nine actual Chrome cases passed with stable source, including a real
  fictional 275-entry download, missing/corrupt footer rejection, buffer refusal,
  actual portal cross-tab logout/new sign-in, delayed old refresh/search401,
  revocation clearing and desktop/mobile layouts with no JavaScript errors or
  external requests. The temporary downloaded fixture file was removed after
  verification; no actual customer export was performed.

Historical failed fixture runs are retained separately. The first SQL fixture
did not advance the balance before issuing each lot; the next attempted to
modify an immutable session timestamp. They were corrected to follow real
transaction ordering and use an injected clock. An early browser fixture tried
sign-in before logout completed and now waits for the completed transition.
These are not passing evidence. Final combined unit/SQL counts are recorded by
the source-aware runners; do not transfer these results to another build or to
the immutable project-root download.
