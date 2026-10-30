# Desktop staff verification

This work is isolated in Development/next-security. It does not change or
activate MFA in the verified pilot checkpoint, its ZIP, a deployed service or
any real staff account. Enrollment and recovery-code generation belong to the
hosted service and owner workflow, tested separately.

Unenrolled staff retain the existing pilot sign-in. Enrolled staff enter their
password, then a current authenticator code or one unused recovery code. The
desktop saves no staff session until the hosted service confirms valid MFA and
returns `mfa_authenticated: true`. Only then is the opaque staff session saved
in the dedicated encrypted OS vault. Browser state receives a local session ID,
not the bearer token or hosted CSRF token.

## Challenge and privacy boundaries

- The hosted challenge token stays in desktop process memory. The browser gets
  a separate random local nonce identifying this attempt, with a store label,
  supported methods and remaining lifetime. It gets no authenticated access.
- Passwords, verification proofs and recovery codes are sent for the requested
  action, cleared from fields immediately and never put in the staff-session
  vault, pending-credit journal, app logs, localStorage or sessionStorage.
  Authentication failures use fixed safe messages rather than reflecting
  submitted secrets from an upstream error.
- Challenges expire after at most five minutes. Wrong/replayed proofs stay
  signed out. Attempt exhaustion requires waiting and a fresh sign-in. The
  hosted service is the authority for TOTP replay and recovery-code consumption;
  the client never assumes a failed/lost response means a recovery code is unused.
- Cancel and sign-out clear the local challenge. Same-challenge cancellation can
  also revoke the exact server session issued before a lost verification
  response. A cancellation racing a successful verification clears that newly
  saved local session; a stale nonce cannot cancel another/newer sign-in.
- If server cancellation cannot be confirmed, the exact handle stays only in
  cancel-only RAM, with no authenticated access. Staff can retry cancellation
  after reconnecting. Address changes have a deliberate escape: **Clear this
  sign-in on this computer** discards that local handle without claiming server
  revocation. The UI explains that any issued server session expires within
  eight hours. This separate action permits a fresh sign-in without permanent
  lockout and never alters pending credit/trade journals.
- An uncertain proof response remains cancelable with the original challenge.
  Starting another password sign-in first requires confirming cancellation of
  the previous in-memory attempt. Explicit offline sign-out clears local access
  and reports when server revocation could not be confirmed.
- Unreadable or malformed successful responses are also uncertain, because the
  server may already have issued a session. Their cancellation handle survives
  local challenge expiry in a cancel-only state. An interruption after the local
  session file is written likewise stays cancelable; the matching local and
  server sessions are cleared together. Mixed session/challenge payloads are
  rejected before any session is saved.
- Restarting the process cannot restore an unfinished challenge. Existing
  encrypted pending customer/credit/trade actions remain separate and unchanged.

## API compatibility

The desktop includes `mfa_supported: true` with `POST /api/auth/login`.
Unenrolled login responses remain compatible with the previous token/store/role
contract. Enrolled login returns `mfa_required`, a memory-only `challenge_token`,
`expires_in`, `methods` and `store`, with no full staff-session token.

`POST /api/auth/mfa/challenge` accepts the hosted challenge token and proof.
`POST /api/auth/mfa/cancel` cancels the challenge or revokes its issued session.
The loopback relay exposes `POST /api/cloud/mfa-verify` and `mfa-cancel` using the
browser's local `challengeId`; it never accepts a browser-supplied hosted token.
These routes retain the local Host/Origin boundary and do not grant staff scope
before proof. Role, enabled-state, tenant and auth-version checks remain hosted
authority.

Older desktop clients omit the capability flag. The hosted service must return
HTTP 426 with an actionable app-upgrade message for enrolled staff, rather than
returning a challenge an old desktop cannot interpret. Enrollment should not be
offered to real pilot staff until their clients support verification.

## Evidence and limits

`test_station_mfa.py` covers durable encrypted sessions, memory-only challenges,
nonce binding, expiry/replay, one-use recovery, malformed responses, session
scope changes, cancellation races, error privacy, prior-client compatibility,
credit-journal preservation and real loopback Origin enforcement.

`tools/user_acceptance_mfa.py` runs eight real Chrome workflows against a strict
fictional hosted transport, with external browser/backend traffic and Square
writes blocked. It checks browser storage and temporary profile files for
fixture secrets. Results and screenshots are under Testing/security-mfa.
This does not establish live enrollment, a deployed MFA rollout, production
account recovery, tax/legal compliance or general consumer readiness.
