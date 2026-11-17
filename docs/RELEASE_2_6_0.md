# Stock & Sleeve 2.6.0 — Shared Pilot

Private pilot development snapshot, October 28, 2026. **Commercial release is
not approved.** The earlier Local Test ZIP is a separate, older artifact.

Authenticated password-change validation, November 17, 2026: hosted commit
`2aa667d57f2d61cb9e866223a0ba9d095dd5e1a4` passed all 480 PostgreSQL
cases across 32 modules and all 85 hosted units, with zero failures, errors or
skips. All prior 464 database IDs remain; all 16 new password-change cases pass.
The 20 actual payloads, 240 copied public files and 118 runner fingerprints were
independently reconciled. See private `PASSWORD_CHANGE_TEST_REPORT_20261117_r2.txt`
and https://github.com/thundergamescda/RocaSifter/actions/runs/44179698055.
The first run's one audit-query fixture error is preserved with its raw log,
diagnostics and exact one-statement correction; app code, IDs and assertions
were unchanged by that correction. Own-account password changes revoke all of
that staff member's sessions atomically and preserve active MFA/recovery material.
No password response changes cookies; the source-reviewed UI signs out locally,
avoiding a delayed logout response clearing a newer sign-in. Real browser HTTPS,
two-tab behavior and usability remain unrun. Password reset and all-factor-loss
recovery remain unfinished. This hosted run does not execute the entire private
597-file desktop source or clear Windows, devices, live Square, legal/tax/privacy
or commercial release gates. No service, installed app or Square data changed.

Bounded backlog validation, November 17, 2026: hosted CI commit
`58e8f6e1da189b61d2f25417d442b36f9b854865` passed all 464 PostgreSQL
cases across 31 modules and the unchanged 85 hosted units across five modules.
All 446 prior database IDs remain; 18 new cases passed on the first attempt.
Zero failures, errors or skips. All 20 payloads, the 237-file public image and
116 runner fingerprints were independently reconciled. See private
`CLEANUP_BACKLOG_TEST_REPORT_20261117_r1.txt` and
https://github.com/thundergamescda/RocaSifter/actions/runs/44152563236.
The lifetime receipt/fact stop is replaced by immutable batches of at most 100
receipts and a separate durable preparation frontier. Tests exceed 10,000
receipts and 100,000 facts, preserve positive history across later intervals,
exercise rollback/concurrency and admit a second store during ongoing work.
Generic worker steps admit one scope without folding, then perform one due step.
The exact prior schema12 fixture upgrades to schema13 twice without rewriting
old work/history; versioned receipt bases prevent old/new proof substitution.
Preparation never advances accepted-read acknowledgment or proves fresh coverage.
The global gate and schedulers remain inactive. Portal controls have source and
HTTP evidence, not browser usability acceptance. These results cover the public
hosted snapshot, not the entire 594-file private working copy or native app.
Windows, devices, actual providers, retention/capacity, legal/tax and commercial
release gates remain open. No service, installed app or Square data was changed.

Inventory observer validation, November 17, 2026: hosted CI commit
`2aac9bccf9989807c386e2db70ec5dfacd08c847` passed all 446 PostgreSQL
cases across 29 modules and 85 hosted units across five complete modules, with
zero failures, errors or skips. The prior 411 database IDs remain; 35 new cases
cover consent, immutable history, partial coverage, stale authority, leases,
rollback and the authenticated HTTP routes. All 20 delivered payloads passed
length/hash verification. See private `CLEANUP_OBSERVER_TEST_REPORT_20261117_r2.txt`
and https://github.com/thundergamescda/RocaSifter/actions/runs/44142032901.
The first run's 16 HTTP fixture setup errors remain preserved; the correction
inserts a new fictional session without weakening the production identity guard.
The observer stores bounded, minimal evidence and rejects changed original
generation/configuration/receipt scope, permission, session or lease. Missing
counts stay unknown and positive partial evidence survives later failures.
Global collection defaults off; per-generation owner/background consent is
separate. No worker or renewal scheduler was activated. Portal controls are
source-reviewed; browser visuals/usability remain unverified. This hosted fixture
run covers its 233-file public image, not the entire private working copy or
native app. No eligibility/deletion or continuous-zero proof is enabled. Windows,
device, live-provider, tax/legal and commercial release gates remain open.

OAuth renewal validation, November 16, 2026: commit
`0c1b5c1ad7f3c0db58e995fc20455f9674800598` passed all 411 PostgreSQL
cases across 26 modules and 41 hosted units across four full modules, with
zero failures, errors or skips. The prior 392 database IDs remain; 19 cases
exercise renewal, revocation/reconnect races, lease takeover, frozen context,
legacy migration, fair retries and the real HTTP status route. All 18 payloads
and source pins were independently reconciled. The failed first attempt and
its fixture-only helper-name correction remain preserved. See private
`SQUARE_RENEWAL_TEST_REPORT_20261116_r2.txt` and
https://github.com/thundergamescda/RocaSifter/actions/runs/43973126349.
Renewal no longer holds database locks during provider I/O. Local age/app
binding remains unknown for legacy records until genuine renewal. The batch
interface defaults off; no scheduler is active or proven. Portal visuals and
the two private desktop mismatch-compatibility edits remain runtime-unverified.
No service/app deployment or live Square action was performed. All broader
Windows, device, provider, tax and commercial release gates remain open.

Inventory-history validation update, November 16, 2026: commit
`cd75270d2e790ef56c961200935e22f47d0156dd` passed all 392 PostgreSQL cases
across 25 modules and the same five identity API units, with zero adverse
outcomes. The full prior 370-case selection remained; 22 cases cover new signed
inventory receipts, immutable ordered facts, original-generation replay, inert
refresh intents, race/rollback/migration checks and the actual HTTP route.
All 12 payloads, the 223-file image and exact published source were independently
reconciled. See private `CLEANUP_EVENTS_TEST_REPORT_20261116_r1.txt` and
https://github.com/thundergamescda/RocaSifter/actions/runs/43954328174.
This does not prove complete ingress or continuous-zero history. The queue has
no worker, collector, eligibility, deletion or provider-write path. Shared
collection/projection, outage recovery and a fenced worker still need review
and acceptance. No installed app or hosted service was deployed by this run;
Windows signing/device, live-provider, tax and commercial gates remain open.

Hosted validation update, November 16, 2026: public CI commit
`4c9dc4860a2edeb9cf1d00d1cba6f198815472ec` passed the complete 370-case
PostgreSQL selection and five store-identity API unit cases, with zero failures,
errors or skips. The disposable Linux/PostgreSQL 18.6 fixture used fictional
records and mocked Square resources. All 12 delivered evidence payloads passed
length/hash verification; independent reconciliation reported no discrepancies.
The first run's three stale identity-test failures remain preserved. The fix
aligned tests with the intentional server-derived MFA status; authentication
and permission enforcement were unchanged. See the private planning report
`HOSTED_CI_TEST_REPORT_20261116_r2.txt` and
https://github.com/thundergamescda/RocaSifter/actions/runs/43949732618.
This result covers that hosted snapshot only. It does not clear Windows SSL,
signing, desktop/browser, device, live Square, tax or commercial release gates.
No deployed service, main branch or installed app was updated by this run.

Working-copy validation update, November 16, 2026: the new server-only cleanup
read collector passed all 31 fictional-streamed tests and 81 subtests with zero
failures, errors or skips; its two Python syntax checks passed. The run preserved
573 source files, the original 4,492 runtime files and caches. Raw and readable
evidence is recorded in `CLEANUP_READS_TEST_REPORT_20261116_r2.txt` under the
private planning directory. The first attempt stopped before cases because its
exact import list omitted a required helper; its failed logs remain preserved.
The corrected run received fresh manual admission with that one reviewed leaf.

The collector retains inactive locations, checks complete catalog parent/child
relationships, follows every bounded page and keeps missing counts unknown.
Its result explicitly denies atomic-snapshot, continuous-zero and deletion
claims. It is not wired to a production route, worker or observation journal.
Shared PostgreSQL history, signed inventory-event processing, outage recovery,
outstanding-order/channel coverage and real provider acceptance remain required.
No live SKU deletion or new Square permission was enabled. Full application,
desktop/browser, hardware and Windows signing/installation gates remain open.

Working-copy validation update, November 15, 2026: the focused scanner, pricing,
cleanup and receiving-boundary run passed all 119 cases and 369 subtests with
zero failures, errors or skips. Ten Python syntax checks also passed. The run
preserved its 570-file source snapshot, 4,492 original runtime files and caches.
Its private evidence record is `FOCUSED_FIXES_RECONCILIATION_20261115_r3.json`.
That release-note update followed its frozen run; all other inputs matched its
snapshot at that time. The November 16 collector additions have separate evidence
above. The November 15 results cover data helpers, real in-memory SQLite and fictional
receiving transport. App integration inputs were syntax-checked only. Desktop,
PostgreSQL, live Square, hardware, signing and installer acceptance remain open.
Historical frozen-build results below apply to their separate snapshots.

Cleanup preview keeps damaged timestamp rows ineligible and allows valid rows
to remain visible. A fresh trusted observation restarts damaged history at
zero elapsed days; readable prior timestamps still reject older observations.
No production caller currently feeds Square snapshots to `Cleanup.observe`.
Trusted complete snapshot ingestion and real location/channel/order coverage
remain release dependencies, and live SKU deletion remains disabled.

## Implemented in this snapshot

- Standalone Windows program, approved logo, Desktop/Start Menu pilot shortcuts,
  matching Stop shortcut and in-app Save & quit. Pilot profiles and ports are
  isolated. Build identity prevents reopening a different same-version binary.
- Reviewed scanner/CSV intake with column mapping, explicit delivery versus stock
  quantity selection, preview, cancellation and duplicate-delivery protection.
  Pokémon, Magic, Riftbound, One Piece, Union Arena, Gundam, sports and named
  additional games retain their identity.
- Manual cards/slabs and sports details, explicit missing-price/zero gates,
  research coverage and exact-game marketplace search aids. Search links are
  not verified pricing evidence or commercial feed rights.
- Price-specific Square variation logic preserves stocked older prices.
  Cleanup remains an observation/preview workflow; live deletion is disabled.
- Online staff sign-in, shared customer history/corrections, encrypted session
  and pending-action journals, role/store/session checks and restart recovery.
- Hosted receipt-derived trade credit. An owner/manager reviews a saved hosted
  offer and confirms customer agreement and physical receipt of selected cards.
  The server issues one ledger entry. A lost response/restart recovers the same
  action; it does not create a second local wallet entry.
- Owner setup/invitation/staff-disable portal controls, tenant isolation, Square
  OAuth foundation, immutable reconciliation evidence and a disabled worker.
- Compact receipt-list caches avoid rereading every card on each history poll.
  Original receipts, financial entries and receiving journals remain authority.

## Evidence and its limits

The frozen application passed 392 isolated Windows unit/loopback tests.
Actual Chrome acceptance passed shared trades (4 cases), CSV mapping (5),
intake (3), hosted onboarding, authentication-transition/cookie checks and
packaged start/reopen/research/save/cancel/quit. Logs record source hashes.

Hosted commit b59f537990fc4d23a5581d86c3d767d4d62a2c24 passed 84 actual
private-network PostgreSQL/TLS cases, including 33 accepted-trade cases, and a
two-worker Gunicorn check. Those tests use isolated fictional records and mocked
Square resources. The desktop shared-credit/trade walkthroughs use a fictional
hosted transport; two actual store PCs signed into the real service remain a gate.

The package build ID is
4ebd3876ad60bee093f240cea287e8f8d2287af15e3b6f9e0a0ed99769e486b0.
Executable and archive hashes are recorded separately. Hashes are not publisher
signing or proof of commercial readiness.

## Before any customer launch

Square developer configuration, consent/refresh/revoke, actual checkout device,
refund/report behavior and written tax treatment still need acceptance. Credit
origins remain unreviewed; automatic tracked-discount redemption and worker
execution stay disabled. No real Square write was used for these tests.

Complete MFA/recovery, data-rights/offboarding/retention, owner notices/contracts,
backup restoration, signing, real scanner/printer tests, broader Windows
installation/update/rollback and representative cashier acceptance. See
Testing/RELEASE_ACCEPTANCE_MATRIX.md and the Planning privacy register.

Existing accepted local buys are not converted to shared credit. Existing
unclassified balances are not backfilled into approved tax origins. Do not point
older pre-bridge binaries at a profile containing hosted-linked purchases.

Main and existing store PC update paths remain unchanged. Hosted deployments are
manual from the preview branch; the pilot is not a rollout to other stores.
