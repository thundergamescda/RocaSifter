# Stock & Sleeve 2.6.0 — Shared Pilot

Private pilot development snapshot, October 28, 2026. **Commercial release is
not approved.** The earlier Local Test ZIP is a separate, older artifact.

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
