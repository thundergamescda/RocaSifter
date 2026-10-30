# Stock & Sleeve security and privacy release gates

Reviewed October 28, 2026. Status: **commercial privacy/security sign-off is open**.
This is an engineering and owner decision register, not a compliance certificate,
legal opinion, public privacy policy, or permission to collect additional data.
Development and fictional-data testing can continue while these gates are closed.

The original review below describes the pre-development checkpoint. The dated
development addendum at the end updates MFA and hosted access-export progress
only in `Development/next-security`; it does not change the checkpoint/download
or close the other commercial gates.

## Historical review boundary and evidence

This review inspected source and existing redacted test evidence; it did not read
merchant records, credentials, payment details, or production database contents.
No app, schema, Square data, hosted configuration, or customer record was changed.
Repository paths below are relative to this project. The October 27 baseline in
`docs/SECURITY_PRIVACY_RELEASE_GATES.md` remains useful background; this register
records the more specific October 28 gaps.

`Testing/2.6.0/automated-summary.json` records a successful 290-test Windows run at
2026-10-28 17:19 UTC, exact source hashes, Python 3.12.14, and
`live_square_contacted: false`. It covers isolated unit/loopback integration
tests with fictional provider responses. PostgreSQL acceptance and real Chrome
walkthroughs have separate logs. That run predates the next accepted-trade
iteration; its count must not be presented as proof for later source changes.
Passing these tests does not establish legal compliance, an independent security
assessment, live Square/device acceptance, or a successful production restore.

## Data inventory from the inspected source

| Category and purpose | Current locations and recipients | Protection/evidence and remaining boundary |
| --- | --- | --- |
| Store and staff identity: store name/slug, staff email, role, enabled state, password hash, invite records | Hosted PostgreSQL `ss_tenants`, `ss_staff`, `ss_invites`; Render; authorized store owner and platform operators | Passwords use salted PBKDF2; invitation tokens are hashed, expire, and are single-use. Individual roles exist. MFA, account recovery, owner transfer, operator access review and a support-access process remain open. |
| Authentication/security metadata: hashed session tokens, hashed sign-in/invite counters, actor/action/time audit | Hosted `ss_sessions`, `ss_login_attempts`, `ss_audit`; encrypted desktop `cloud.session`; hosting/request metadata may also exist outside these tables | Hosted sessions expire after eight hours and disabling a staff account revokes its sessions. Hashing a low-entropy email/IP-derived counter is not a promise of anonymization. No approved audit/hosting-log retention schedule is implemented. |
| Shopper credit identity: display name, optional contact, optional Square customer ID, staff-recorded purpose | Hosted `ss_customers`; local credit SQLite; desktop account/search views; authorized staff of that store | Hosted create requires staff confirmation of authority. It does not capture a customer-facing notice version, individual marketing preference, or a complete consent/rights record. All signed-in store roles can currently read customer contact, balances and recent history. |
| Trade and financial records: receipt/reference, card rows and valuations, offers/accepted amounts, credits/debits, reasons, operator/time, balance versions, reservations and origin lots | Local receipt/credit SQLite, PDFs/CSVs; hosted ledger/reservations/lots/requests/audit; new hosted accepted-trade workflow requires a follow-up field review | Integer cents, transaction locks, idempotency and immutable reconciliation evidence protect money integrity. Names/references/free text can identify people. Financial integrity does not determine how long personal data may be retained. |
| Inventory and research: game/card/set/printing/condition, sports metadata, slab grader/certificate/grade, barcode/SKU, quantities/prices, scanner file names | Local SQLite, watched CSVs, output labels/PDFs/CSVs, pricing cache; approved Square catalog/inventory operations | Product data becomes customer information when linked to a seller/trade. Certificate IDs or filenames may be identifying. Scanner vendors, source exports, printer queues and copies outside this app need their own inventory and handling rules. |
| Square credentials, event and reconciliation data | Encrypted token blob in hosted `ss_square_connections`; local receiving credential vault; minimal event/job envelope; customer/order/location/discount IDs, selected settlement/return evidence | Server token encryption, environment checks, OAuth state/session binding and signed ingress exist. Jobs store IDs/body hashes rather than the full webhook. Return proof is a selected field set, not the complete provider response. Seller-content offboarding is incomplete; see G2. |
| Pending/retry and idempotency records | DPAPI-protected `cloud.pending` on Windows, local receiving journal, hosted `ss_requests` results and job/exception tables | Journal is written before a network mutation and preserves the same key/body for recovery. Signed-out views suppress pending details. It is a recovery record, not a routine disposable cache; deletion needs a verified outcome. |
| Generated/downloaded files, backups and support evidence | Local output directories, CSV export, updater `backups/`, downloaded pricing snapshots, platform database backups, screenshots/test logs/support copies | Local customer/receipt SQLite and generated files are **not application-encrypted**. DPAPI protects particular secrets/session/journal files, not the whole data directory. The updater copies data/output/settings as well as app files. No complete copy inventory or automatic disposal schedule exists. |

Primary code evidence: `hosted/schema.sql`, `hosted/auth.py`,
`hosted/security.py`, `hosted/ledger.py`, `hosted/square.py`,
`hosted/settlement.py`, `hosted/reconciliation.py`, `station_store.py`,
`credit_tracker.py`, `station_cloud.py`, `station_service.py`,
`station_pricing.py`, and `roca_update.py`.

The inspected app has no dedicated PAN/CVV, SSN, ID-image, or date-of-birth field
for credit tracking. Free-text contact/reference/reason/notes fields can still
receive sensitive material. A policy saying “do not enter it” is not technical
prevention. Add in-context minimization guidance and a reviewed redaction/support
process; do not ask stores to upload identification images as a convenient fix.

## Permissions and security evidence

Implemented controls include tenant scope from the authenticated server session;
role checks for privileged changes; expiring revocable opaque sessions; protected
cookie/CSRF or bearer boundaries; fixed-origin desktop HTTPS relay; bounded
requests; escaped UI output; CSV formula protection; encrypted secrets; OAuth
single-use state; signed webhook checks; and durable idempotent reconciliation.
Current tests cover tenant/role boundaries, invitation abuse, provider IDs,
session switching, delayed UI responses, uncertain writes, corrupt journals,
and stop/drain races. These controls reduce particular risks; they do not prove
every deployment path is secure.

`render.yaml` deliberately limits Gunicorn access logs to method, path and
status, omitting query strings and request bodies; application error handlers
log an error type and path. Preserve that configuration. Separately verify
platform/proxy/database/support logging and retention, since those layers are
not proved private by the application formatter.

Owner alone currently invites/disables non-owner staff and begins Square OAuth.
Manager/owner can make ledger corrections. Customer search, customer creation
and balance/history reads are broadly available to signed-in store staff.
Local Admin authorization is separate from hosted staff authorization. A shared
Windows login can expose the local database and OS-bound saved secrets to people
using that account; individual cloud logins do not isolate those local files.

Before commercial launch, approve an action/field permission matrix, test it
against every final route, and decide whether cashiers need contact/history
visibility. Establish individual operator accounts, MFA for hosting/repository/
Square administrators, recovery and break-glass controls, secure owner transfer,
device/session revocation, and time-limited auditable support access. Verify the
deployed database network rules and service-role privileges. Require a current
dependency/secret scan, release signing/update provenance review, and an
independent assessment of public authentication and tenant/money boundaries.
No such deployment-wide sign-off is established by this source review.

## Release gates and completion evidence

| Gate | Present gap | Evidence needed to close it | Accountable decision |
| --- | --- | --- | --- |
| **G1 — Entity, purposes, notices and contracts** | No approved operating entity/privacy contact, jurisdiction map, store agreement, public notice or complete processor/subprocessor register. A staff checkbox is not a public privacy notice. | Counsel-reviewed role/purpose map; actual notice shown at collection; documented store instructions and authority; DPA/service agreement; vendor list and change process; support-request handling and approved launch territory. | Owner and privacy/legal adviser |
| **G2 — Square disconnect/offboarding and provenance** | Revocation stops token use and cancels work, but stored seller content and copies survive. Evidence triggers reject UPDATE **and DELETE**. Provenance is insufficient for a complete purge without risking accounting/money integrity. | Inventoried source/provenance per field; approved disconnect and deletion design; tested revocation plus all-copy deletion/offboarding evidence; defined unresolved-financial-outcome handling; written resolution of contractual deletion versus required record retention. No broad destructive cleanup or retention override is authorized by this document. | Owner, technical lead, counsel and Square where clarification is needed |
| **G3 — Customer rights/export/correction/deletion** | No complete hosted rights workflow, full customer export, identity correction, anonymization/erasure process or request tracker. Account screens return only 100 entries. Local ledger CSV is not a cross-system export. | Fictional customer rights walkthrough described below, tenant-safe export of all relevant records, reviewed exception codes, identity verification, delivery security, response ownership/deadlines and deletion/backup verification. | Privacy owner and engineering |
| **G4 — Retention and copies** | Financial/audit/request/job records, unused invites, local outputs, updater backups and support evidence lack an approved implemented disposal schedule. Soft-deleted receipts remain recoverable. | Owner-approved category schedule with purpose/source, trigger, period, legal hold, disposal, copy propagation and proof; scheduled purge/dry-run tests; restoration cannot resurrect already-deleted personal data. | Owner, bookkeeper, counsel and operations |
| **G5 — Access and operational security** | Broad customer-read roles; MFA/recovery/operator permissions and workstation protection unverified; no independent final-build assessment. | Approved permission matrix with negative tests; controlled operator/support access; device/OS protection requirements; deployed configuration review; security assessment findings resolved and tested. | Security/operations lead and store owner |
| **G6 — Incident and restore readiness** | No assigned incident team/contact tree, exercised notification assessment, or measured full restore with keys, ledger reconciliation and deletion suppression. | Dated tabletop and isolated restore logs; agreed RPO/RTO; key recovery/rotation evidence; financial and privacy verification; approved incident/communications runbook. | Owner and operations, with counsel |
| **G7 — Provider and Marketplace rights** | Commercial pricing/scanner rights and final Square Marketplace approval are unverified. Reachable public data and API success are not commercial licenses or listing approval. | Provider terms/contract review, attribution/allowed use evidence, final OAuth/scope/revocation tests, Marketplace application review and written approval where required. | Owner and integrations lead |
| **G8 — Data monetization boundary** | No data-sale feature was found or approved. Future reuse has no rights/purpose/contract analysis. | Keep it outside initial ingestion. Any later proposal requires a separate source/rights/privacy review and owner decision before collecting or transmitting extra data. | Owner and counsel |

These are commercial release conditions. They do not require interrupting routine
fictional-data development to ask the owner each unresolved policy question.
Record decisions here; do not silently invent legal identity, retention periods,
customer promises, deletion exceptions, or a production financial migration.

## Retention: what the program currently does

| Record | Current behavior | Required decision/implementation |
| --- | --- | --- |
| Staff session | Eight-hour validity; expired hosted rows cleaned during a successful login; logout/disable revoke applicable sessions. | Document session/device policy and prove local expired/disabled handling. Expiry is not a promise that every related log disappears. |
| Invitation | Twenty-four-hour validity and used-token rejection; no routine invite-record purge found. | Approve invite audit minimum and purge/redaction period. |
| OAuth state | Five-minute validity; old states cleaned when beginning OAuth. | Confirm failed/abandoned flows and logs do not retain code/state secrets. |
| Sign-in/invite counters | Fifteen-minute budgets; rows older than one day removed on later sign-in/invite activity. | Decide inactive-system cleanup and security-monitoring retention; test operational logs separately. |
| Ledger, lots, trades, settlement/return proof, audit, requests and job/exception records | No general expiry/purge found. Immutable evidence and foreign keys constrain deletion. | Decide category-specific retention, lawful exceptions, provenance and safe archival/de-identification. Do not equate “immutable” with “retain all personal data forever.” |
| Local receipts, exports and updater backups | Receipt deletion is a recoverable `deleted_at` flag; generated files and backups persist. Local credit CSV exports all entries for that local account. | Treat soft delete as a UI feature, not erasure. Include staff PCs, printer/output folders, shared drives and support copies in disposal/offboarding. |
| Uncertain cloud mutation | Exact encrypted pending body persists until its outcome is resolved/recovered. | Keep financial recovery safe, restrict visibility, and define owner escalation for abandoned/broken journals. Do not discard it on logout or after an arbitrary timeout. |
| Hosted database backup | Render offers managed recovery/export features. This review did not inspect the purchased workspace recovery configuration or perform a restore. | Record actual plan/window, encrypted export destinations, access, keys and deletion lifecycle. Test a separate recovery instance before promising an RPO/RTO. |

Render documents paid-database point-in-time recovery windows of three days for
Hobby workspaces and seven days for Pro or higher, and seven-day retention for
logical exports. These are provider capabilities, not proof of this account's
configuration, a successful application restore, or disposal of downloaded
copies. Restoring creates a separate instance; validate its data before changing
service connections. [Render recovery and backup documentation](https://render.com/docs/postgresql-backups).

## Customer rights workflow to build and demonstrate

This is a proposed acceptance workflow, not a claim that the endpoints exist.

1. Receive a request through a named privacy channel. Record the store, request
   type, received date and assigned owner. Verify identity proportionately;
   avoid retaining extra identity documents. Determine applicable deadlines and
   authorized-agent requirements from the approved jurisdiction matrix.
2. Search with tenant scope and independently verify the customer match. Include
   duplicate accounts, contact, trades, full ledger/lots, reservations, selected
   provider evidence, generated files, devices and support copies. Distinguish
   data provided independently by the store from provider-derived data.
3. Produce a complete machine-readable export with understandable amounts,
   references, purposes and dates. Paginate beyond the 100-row UI limit; verify
   no other store/customer data or secrets appear and CSV cells are safe. Use
   access-controlled delivery and expire temporary export files.
4. Correct identity/contact through a controlled versioned change. Financial
   corrections remain append-only and attributable. Do not let an identity
   correction rewrite settled money, provenance or accepted agreement evidence.
5. For deletion, first evaluate each record and documented exception/hold. Remove
   unnecessary identity fields and eligible source content through an approved
   design; explain any retained categories and their actual basis. A remaining
   hash/ID may still be linkable. Deletion of an account with a balance, unsettled
   reservation or uncertain write needs a financially safe resolution.
6. Propagate approved actions to hosted data, local authorized devices, outputs,
   caches and support stores. Track backups and any deferred destruction. Keep
   a minimal restricted suppression/action record so restoring an old backup
   does not silently reintroduce the removed information.
7. Verify the result as the requesting store and as a different tenant. Record
   action/exception evidence without duplicating the deleted payload in logs.
   Supply an accurate response, including residual copy expiry where applicable.

## Incident and restoration procedure to exercise

The owner must assign incident lead, technical responder, store contact, legal
adviser and vendor escalation paths. Maintain contacts outside the affected app.
The following drill is proposed and **has not been completed by this review**:

1. Triage a fictional leaked session/token or cross-tenant report; record times,
   suspected systems, decisions and an incident ID. Restrict relevant access,
   revoke sessions/tokens where appropriate, and pause unsafe workers/mutations.
   Preserve restricted evidence before changing or destroying records.
2. Determine data categories, tenants, exposure period, key compromise and
   integrity impact. Preserve filtered logs and required forensic copies without
   adding plaintext secrets or full customer payloads to tickets/public CI.
3. Have counsel assess actual legal and contract notification triggers/deadlines
   for affected people and sellers. Do not apply one generic 24/72-hour rule to
   every incident. Only the authorized incident owner approves external notices.
4. Restore a fictional database into an isolated target with independent service
   credentials, external mutations/webhooks/consumers disabled and no production
   Square calls. Recover the correct schema and encryption-key versions; verify
   key rotation and historical encrypted data are recoverable.
5. Compare ledger totals, per-customer balances and held amounts, lot allocations,
   accepted trades, immutable evidence, pending request keys and job state before
   enabling any consumer. Avoid replaying a committed financial action after a
   database rollback. Reconcile the restore point against provider evidence in
   the separately approved reconciliation process.
6. Reapply completed rights/deletion actions and access revocations before
   reopening. Validate tenant separation, disabled staff, private pending views,
   support/operator permissions and export restrictions. Measure recovery time
   and data gap against the owner's agreed objectives.
7. Review and approve cutover/communications; record lessons and corrective tests.
   This document authorizes no restore over an important database and no external
   incident messages.

NIST SP 800-61 Revision 3 incorporates preparation, response and recovery into
cybersecurity risk management. It is useful guidance for this drill, not a legal
notification timetable or a certification of this application.
[NIST SP 800-61 Rev. 3, April 2025](https://csrc.nist.gov/pubs/sp/800/61/r3/final).

## Legal and platform facts verified from official sources

**Square.** The Developer Terms reviewed were updated October 27, 2026. They
require seller OAuth rather than collecting Square passwords, access/use within
seller permissions, stopping access and deleting a revoking seller's content.
They restrict redistribution, substantially competing services and seller
migration, and prohibit using Square data, anonymized or otherwise, to train or
enhance AI. Obtain a product-specific review of the planned integration and
future data uses. Do not assume seller consent overrides another contractual
restriction. [Square Developer Terms, sections 1–3](https://squareup.com/us/en/legal/general/developers).

G2 is a concrete implementation gap: `hosted/square.py::ingest` clears token
material/marks revocation and cancels pending work, but it does not purge stored
customer/provider references, immutable terms/settlement/return proof or local
and backup copies. `hosted/schema.sql` rejects deletion of several evidence
tables. Design the solution explicitly with legal/Square clarification where
necessary; neither silently retaining everything nor bypassing evidence
triggers with a broad delete is an approved resolution.

An independently supplied store receipt is not automatically Square-derived
merely because a later transaction links to it. Conversely, an order ID or
selected provider proof does not cease to be provider-derived because it was
copied into a financial record. Classify provenance at field/source level and
separate those records before deciding the action. Counsel/bookkeeper must
identify any actual statutory retention requirements and resolve conflicts;
this register assigns no accounting retention period and assumes no exception
to Square's requirements. G2 concerns provider offboarding; G3 also covers
separate shopper requests and independently collected data.

Square's collection guidance also calls for minimization, explicit permission,
respect for marketing opt-outs, HTTPS, and avoiding PII in persistent caches and
logs. The staff authority checkbox and minimal webhook envelope are partial
engineering measures, not proof of every customer's permission or compliant
marketing. [Square information-collection guidance](https://developer.squareup.com/docs/build-basics/general-considerations/collecting-information).

**United States and Idaho.** The FTC recommends a data inventory, collecting
only needed information, least privilege, a written retention/disposal policy,
service-provider security and incident planning. This review applies those as
engineering inputs and leaves law-specific applicability to counsel.
[FTC business data-security guide](https://www.ftc.gov/business-guidance/resources/protecting-personal-information-guide-business-0).
Idaho's AG points to Title 28 chapter 51. Its described 24-hour AG notice rule
applies to Idaho public agencies; the page distinguishes commercial entities.
That distinction does not settle notice duties to individuals, other states,
or contractual partners. Do not publish a blanket “Idaho businesses have no
breach-notice duty” statement.
[Idaho Attorney General breach guidance](https://www.ag.idaho.gov/consumer-protection/security-breaches/).

**Other customers and minors.** California's CCPA includes rights to know,
delete subject to exceptions, correct, and opt out of sale/sharing, including
recognized preference signals where applicable. Applicability and the
business/service-provider role need an actual operations/contract review; an
Idaho address or B2B sales model is insufficient to decide scope. Avoid copying
a threshold without checking the current law before launch.
[California AG CCPA guidance, updated August 28, 2026](https://www.oag.ca.gov/privacy/ccpa).
The FTC describes COPPA coverage for child-directed online services and general
audience services with actual knowledge of relevant online collection from
under-13 children; the FAQ flags the amended Rule. Selling card-store software
does not alone answer that test. Review the real collection flow before adding
shopper logins, youth promotions, ages or child-directed online features; do not
collect birth dates merely to avoid considering the issue.
[FTC COPPA guidance](https://www.ftc.gov/business-guidance/resources/complying-coppa-frequently-asked-questions).
Approve any additional state/country launch before collecting there; no EU/UK
or global-compliance conclusion was made in this review.

**Hosting and roles.** Render publishes a DPA and subprocessor mechanism; confirm
the actual agreement, account/workspace, region, authorized operators and
applicable subprocessors. Hosting certificates do not certify this application's
configuration or use. [Render DPA](https://render.com/dpa),
[Render security information](https://render.com/security).
The working contract model is stores directing shopper/trade processing and
Stock & Sleeve separately handling its own business/staff/security/support data.
That is a design assumption for legal review, not an established legal role.

## Data sales, analytics and commercial rights

No advertising SDK, third-party analytics SDK, customer-data resale feed,
cross-store profiling service, or model-training pipeline was found in the
inspected application source/assets. No runtime AI feature was found. This is
a source-review finding, not packet-capture evidence or a promise that hosting,
Square, pricing sites or support tools collect no metadata.

`station_pricing.py` retrieves fixed TCGCSV category/group/product/price paths;
those requests disclose network metadata and requested product groups. Manual
research links disclose the searched card identifiers to the chosen provider
when opened. Customer names/contact/reference fields should never be appended
to those searches. Commercial feed/vendor rights remain G7; public availability
does not prove a license. Existing scanner/export provenance and third-party
card images/trademarks also need provider-specific review before distribution.

The initial product should earn revenue through subscriptions, workflow features
and store-specific analytics within the agreed purpose. Any aggregate benchmark,
ad targeting, customer-data sale, third-party feed or AI reuse requires its own
written source/rights/purpose assessment before collection or development.
Removing names or obtaining a general opt-in does not establish permission for
all uses. An eventual sale of the software/IP also needs separate diligence for
contracts and any proposed transfer of store/customer data.

## Evidence pack required for a commercial decision

- Final-build source/installer hashes, exact test scope/results, deployed tenant
  and permissions evidence, independent security findings, and explicit live
  Square/device/tax/reporting limitations.
- Completed fictional rights request with over 100 entries, a duplicate identity,
  cross-tenant negative cases, eligible deletion and a documented exception;
  verified output/local-device/backup behavior.
- Seller revocation/offboarding demonstration that records stopped access,
  affected content/copies, financial resolution and approved disposal evidence.
- Retention schedule and purge dry-run/recovery evidence, plus an isolated
  backup restore and incident tabletop with measured outcomes and named owners.
- Approved notices/agreements, vendor/provider permissions, subprocessor list,
  staff training, customer support/privacy contact and launch jurisdictions.
- Release/support evidence screened for merchant data, credentials, query URLs,
  customer payloads and local paths before any public repository or download
  publication. A folder called “test results” is not automatically safe to ship.

Open owner decisions: legal entity/privacy contact; launch territories and store
contracts; collection/marketing rules; staff/support permissions; category
retention and financial record handling; incident team, backup objectives and
support hours; Square/provider legal review. No data monetization is included
in the initial scope.

## Development addendum — October 28, 2026

This addendum applies **only to the isolated `Development/next-security` source**.
The verified project-root checkpoint and its download remain separate. No new
security source was deployed and no actual customer record was exported. The
historical 290-test review above is preserved as dated evidence; it is not the
test count or certification for these later endpoints.

MFA and a scoped owner-authorized hosted customer export are now implemented in
that development copy, rather than being solely proposed. MFA adds optional
authenticator enrollment, challenges and recovery codes, encrypted secret
storage, version-bound sessions, revocation/epoch handling and database guards
against issuing incompatible sessions for an enrolled account. Optional
enrollment does not establish that every store or platform operator uses MFA.
Recovery/support governance and safe compatible rollback remain release work.

The new owner-only export requires recent sign-in, explicit verification method,
purpose, typed account confirmation, authority and copy-handling attestations.
It streams every explicitly linked hosted collection from one consistent
read-only snapshot, with complete counts and a hash footer. It includes linked
ledger/lots/reservations/trade receipt copies and selected stored Square
settlement/return evidence; it excludes credential/authentication tables and
other accounts. Fresh authorization is rechecked during generation and final
verification. Logout, revocation, stale views, missing/invalid footers and
resource-limit failures do not authorize a completed download. See
`docs/HOSTED_CUSTOMER_EXPORT.md` for exact selection, format and engineering
limits.

The export stores lifecycle/verification audit metadata without names, contacts
or export payloads. It creates no persistent server export file/table. The
browser clears interrupted buffers; a successful downloaded copy still needs
the owner's approved handling and disposal. A verified-client event confirms
reported content verification, not delivery to a customer or completion of a
legal request. Hashed internal references and IDs are not described as
anonymous.

Scoped actual evidence on October 28:

- The complete **121-case PostgreSQL18.6+TLS run**, ID
  `20261028T223546241790Z`, passed with zero skips/failures/errors, exact test
  coverage and stable source. It includes the current five export cases,
  32 MFA cases and prior hosted tests. All database data was in disposable
  fictional `ss_test_<uuid>` schemas on a loopback test fixture.
- The export pure/HTTP module passed **17 cases**. It includes multi-large-record
  byte-bounded pages, complete history beyond 100 entries, identity/purpose/
  recency checks, tenant/role/CSRF, interruption, revocation and audit failures.
- Actual Chrome passed **nine scoped cases**, with a fictional 275-entry file
  downloaded and its counts/hash verified. It rejected truncation, changed
  content and excessive buffer size, cleared across real portal logout/new
  sign-in, ignored delayed old refresh/search401 responses and released cursors.
  The report has matching before/after source hashes at
  `Testing/next-security/privacy-export/privacy-user-acceptance.json`.

These facts advance part of G3 (hosted access export) and parts of G5
(authentication). G3 is still open for identity correction, deletion and an
end-to-end rights process. No complete search of duplicate/unlinked accounts,
local workstations, scanner/printer files, previous downloads, support stores,
backups or external provider records is claimed. Incidental third-party free
text still requires owner review. G1 entity/notices/contracts, G2 Square
provenance/offboarding, G4 retention/copy disposal, G6 incident/restore and the
remaining provider/Marketplace/security gates stay open. Data monetization is
still outside the initial release. No legal compliance or commercial readiness
certification is made by this addendum.
