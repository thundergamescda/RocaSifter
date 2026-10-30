# Optional staff authenticator security — development snapshot

This implementation is in `Development/next-security`. It does not modify the
verified pilot ZIP or enroll an existing account. Schema 4 is additive; existing
unenrolled staff keep password sign-in. No production migration, deployment,
configuration change or actual customer/staff enrollment is authorized by tests.

## Behavior and evidence boundaries

TOTP uses HMAC-SHA1, six ASCII digits and a 30-second step, accepting the current
step and one adjacent step in either direction. A previously accepted step cannot
be used again for that staff account. The primitives are checked against all
18 SHA1/SHA256/SHA512 vectors in [RFC 6238 Appendix B](https://www.rfc-editor.org/rfc/rfc6238)
and the counter vectors in [RFC 4226 Appendix D](https://www.rfc-editor.org/rfc/rfc4226).
This is an implementation choice, not a certification or legal conclusion.

Each account gets an independently random 160-bit seed. The server encrypts it
using domain-separated MultiFernet keys derived from `APP_ENCRYPTION_KEYS`. The
authenticated envelope binds tenant, staff, active/pending purpose, and MFA version
or pending token hash. Moving ciphertext between accounts, stores, versions or
purposes fails closed. Old keys must remain available while old ciphertext exists;
new encryption uses the first configured key. No plaintext seed is stored in SQL.

Enrollment requires a valid session, browser CSRF and current password. Its
opaque token is bound to the exact session and password fingerprint and expires
in ten minutes. Confirming a fresh code activates MFA, clears pending ciphertext,
revokes other sessions and upgrades the confirming session. The seed is returned
only when starting setup, never by status or after confirmation. Cancel clears the
server's pending setup. Restarting setup replaces an earlier pending setup.

Confirmation returns ten independent 128-bit recovery codes once. They are shown
as eight groups of four hexadecimal characters; case and hyphens are optional on
input. SQL stores only tenant/staff-bound SHA256 hashes. Each code is consumed
atomically. Replacement requires current password plus a fresh TOTP or unused
recovery code and invalidates the previous set. Disable requires the same proof,
deletes active/pending seeds and recovery hashes, increments the security version
and revokes every session, including the caller.

## Desktop and portal sign-in contract

- `POST /api/auth/login`: existing credentials plus `mfa_supported:true`.
  Unenrolled accounts return the usual full session. An enrolled account on an
  older client receives HTTP 426 with an update instruction and no new session.
- Enrolled, capable clients receive `{mfa_required:true,challenge_token,
  expires_in:300,methods:["totp","recovery_code"],store}`. There is no bearer,
  CSRF token, role authorization or new full session at this stage. The portal's
  previous cookie is cleared.
- `POST /api/auth/mfa/challenge` with `{challenge_token,code}` returns the normal
  bearer/cookie/CSRF result and `mfa_authenticated:true` only after validation.
  The final transaction rechecks current staff enablement, role, password
  fingerprint and MFA version. A challenge is one-use and client-address bound.
- `POST /api/auth/mfa/cancel` with `{challenge_token}` invalidates that challenge.
  If verification already committed but its response was lost, it revokes only
  the session issued by that exact challenge. Cancellation and verification share
  a staff-first lock order. A wrong client address receives HTTP 401; the client
  must not claim that cancellation was confirmed in that case.
- Invalid/replayed/expired sign-in proofs return HTTP 401. Five wrong proofs
  exhaust a challenge; a further attempt returns HTTP 429. Independent committed
  15-minute budgets cap issuance and proof attempts across fresh challenges and
  cancellation: ten per staff and fifty per client address per namespace.

Management routes are `GET /api/auth/mfa`, `POST /api/auth/mfa/enroll`,
`POST /api/auth/mfa/enroll/cancel`, `POST /api/auth/mfa/confirm`,
`POST /api/auth/mfa/recovery-codes`, and `POST /api/auth/mfa/disable`.
Browser writes require the current CSRF token; bearer clients retain the existing
API authentication boundary. Request bodies cannot supply tenant scope.

The browser holds challenge/enrollment tokens only in memory. It removes setup,
password, proof and recovery-code values when their flow ends or sign-out occurs;
this is reference/DOM clearing, not a claim to overwrite browser memory. Login,
MFA verification and logout are serialized until authoritative session resolution.
The only cross-tab storage is a fresh random `ss-auth-epoch` nonce; the
`ss-auth-transition` broadcast carries `{changed:true}` and no credentials or PII.
Privacy export listeners use those events to discard stale buffers.

## Verification and remaining gates

Run pure/HTTP tests with `python -m unittest -v test_hosted_mfa`. Run actual SQL
acceptance with `DATABASE_URL` supplied to the process and
`python -m unittest -v test_hosted_mfa_postgres`; that runner creates and removes
only a fresh `ss_test_<uuid>` schema. A skipped PG class is not SQL evidence.
Run real Chrome fixture UX with `python tools/test_hosted_mfa.py`; this uses
fictional responses, no real enrollment, Square or email. Reports include source
hashes and must be rerun when those inputs change. Deployment also requires all
existing trade, settlement, reconciliation, export and WSGI acceptance to pass.

Before offering a commercial service, decide account-recovery/support procedure
when password or all second factors are lost, owner enforcement policy, clock
monitoring, encrypted-backup/key rotation and operational cleanup of expired
encrypted setup/challenge metadata. No administrative MFA bypass, legal mandate,
regulatory certification, phishing-resistant claim, automatic enrollment or data
monetization is implied. Real provider/device/tax/marketplace release gates remain.

## Rollback safety

Do not roll an enrolled store back to a schema-3-era hosted authentication server.
That code ignores the new MFA state and could issue a password-only session.
Schema 4 additionally rejects ordinary session inserts/updates for an enrolled
account unless the current MFA version and proof timestamp are present. Its
staff-row guard uses `NOWAIT` and a retryable serialization error on contention;
an update trigger must not wait for staff while holding a session row in the
reverse order. Actual PostgreSQL tests cover legacy insert before/after/racing
enrollment, missing proof, update contention, valid new sessions and migration
reapplication. This defense does not make an older application a supported
rollback target. After the first
enrollment, rollback must preserve equivalent MFA enforcement, or authentication
routes must remain offline until a compatible server is restored. Validate that
procedure before enrollment or deployment; do not promise transparent rollback.
An older desktop client receiving HTTP 426 is a separate compatibility behavior
and does not authorize an older hosted server to bypass MFA.

Operational release checks must include synchronized/monitored server time,
retention and cleanup of expired encrypted pending seeds/challenge metadata,
key availability and rotation, protected backups, verified account-recovery
procedures and a tested fail-closed response when verification/key services fail.

The development evidence currently includes 20 pure/RFC/HTTP MFA tests and 32
actual PostgreSQL 18.6 TLS MFA cases with exact case coverage, zero skips/failures
and stable source (run `Testing/next-security/postgres-runs/20261028T222403842118Z`).
Real Chrome fixture MFA UX has seven checkpoints, plus the existing onboarding
and held cookie-transition regressions and eight deterministic Node VM races.
These are isolated engineering results; production deployment, real staff
enrollment, disaster recovery and the broader commercial release gates remain.
