# Independent-store invitation contract — first slice

This isolated development slice creates a new tenant and its first owner only
from a platform-issued invitation. It does not enable public signup, send email,
verify a legal identity, accept final legal terms or finish the full commercial
account lifecycle. Receiving source and existing stores remain untouched.

## Platform boundary

`OwnerOnboarding.issue(OperatorAuthority(reference), data)` is internal. There is
no HTTP issuance/revocation route and a store-owner session is not platform
authority. The CLI requires a server service account's private database access,
`SS_OWNER_INVITATIONS_OPERATOR=1`, and an explicit operator confirmation. The
environment/confirmation guards prevent accidental use; actual privilege comes
from access to the trusted platform process and its database credentials.

Issuance fixes email, slug, display name, purpose, reserved tenant/owner IDs and
expiry. The opaque 256-bit token is shown once to the privileged operator and
only its domain-separated hash is stored. A repeated identical issuance key
returns metadata without recovering the secret. An unused invitation can be
explicitly revoked and replaced under a new issuance key; it is never retargeted.

New invitations require a supported ASCII email address before normalization.
Unicode/internationalized mailbox input is rejected rather than case-folded into
another mailbox. Existing Auth/accounts/sign-in are unchanged. Internationalized
email support and actual delivery/verification remain separate engineering and
provider gates; this is an explicit first-slice limit, not a permanent store
restriction or evidence that typed email proves mailbox ownership.

Protect privileged console output as a credential. `InvitationDelivery` is the
neutral delivery interface; `NoDelivery` explicitly reports not configured and
sends nothing. Actual email/domain verification, a durable delivery/outbox adapter,
sender approval and delivery monitoring are later engineering/provider gates.

Private links use `/owner-invite#owner_invite=<token>&invitation_id=<uuid>`.
Fragments must be removed from browser history immediately. The public UUID is
not authority; the server token/email binding governs every operation. The
browser stores only the nonsecret invitation UUID and stable acceptance request
key, never the token, email, password or resulting owner credentials.

## Public typed endpoints

All are fixed-origin POST JSON, retain the existing 512 KiB request cap and have
no automatic cookie creation/deletion or access-token response.

- `/api/owner-invitations/inspect`: `{invitation_token,email}`. A live match returns
  protocol 1, invitation ID, fixed store slug/name, email, expiry, sign-out
  requirement and false public-signup/delivery capability flags.
- `/api/owner-invitations/accept`: `{invitation_token,email,password,request_key,
  store_details_confirmed:true}`. No client tenant, role, owner ID or store edits.
  A currently valid cookie or bearer session requires explicit sign-out first.
  Expired/invalid credentials do not become enrollment authority.
- `/api/owner-invitations/status`: `{invitation_token,email,request_key}`. Exact
  committed-key recovery returns `{found:true,state:completed,result}`. A valid
  issued invitation returns `found:false,state:not_completed,result:null`.

Accepted result includes fixed store slug/name, owner email, `owner_created:true`,
`next_action:sign_in`, `mfa_enrollment_required_for_trade_and_square_writes:true`,
and false Square-connected/receiving/checkout flags. It contains no tenant/staff
IDs, session secret or automatic sign-in. Normal login and existing portal MFA/
Square setup follow. Opening/correction step-up consistency is a separate gate;
this flag does not claim every financial action currently requires MFA.

Wrong token/email, expiry or revocation use generic HTTP 403
`INVITATION_UNAVAILABLE`; rate limits use HTTP 429. A changed consumed acceptance
uses HTTP 409 `ENROLLMENT_ALREADY_COMPLETED`; active-session acceptance uses HTTP
409 `SIGN_OUT_REQUIRED`. No error echoes token, password or raw database details.

## Atomicity, expiry and recovery

Validation and independently committed IP/token budgets precede expensive password
hashing. New tokens do not reset the IP budget, and changing IP does not reset a
known token's proof budget. Hashing occurs outside a held database connection or
row lock. The final transaction rechecks exact invitation/email/expiry/revocation,
creates tenant/owner, consumes once and writes both audits atomically. Slug
conflict or audit failure creates no partial account. Existing same-email owners
in other tenants are not attached or reused.

New stores inherit no customer, credit, origin approval, OAuth connection,
receiving settings, staff session or platform bootstrap authority. Standard
tenant defaults keep checkout off; no new policy or provider permission is
approved by accepting an invitation. Global initial-owner bootstrap remains a
separate unchanged privileged setup/recovery path.

After consumption, inspect is unavailable. The original exact request key can
recover the minimal completed result through status or exact acceptance replay,
including after issuance expiry. That is read-only result recovery, not reuse to
create another account or reset its password. The token/result retention and
account-rights policy still needs review before production.
The result describes the original creation snapshot; it is not a current setup
progress read after a store later configures MFA or connects Square.

## Authenticated own-password change

An existing signed-in staff member of any role can change only their own
password through `POST /api/auth/password/change`. Its exact JSON body is
`{current_password,new_password}`, limited to 4096 UTF8 bytes. Duplicate keys,
unknown fields and malformed/nonfinite JSON are rejected. The server derives the
account from the current session; the client cannot supply a target account.

The current password is required. The new password accepts 15–128 Unicode
codepoints, preserves whitespace and must differ from the current password.
Enrolled MFA requires fresh proof. Unenrolled accounts still require a current
session and current password. Independently committed staff/IP guess limits
precede password hashing, which runs outside held database locks. Final state
checks fence a changed hash, staff, session or active MFA enrollment.

One transaction replaces the hash, revokes all sessions of that staff account,
cancels unused active MFA challenges, clears pending enrollment and writes a
minimal credential-free audit. Active MFA/version/recovery codes, other accounts,
invitations and business data remain intact. Success returns exactly
`{password_changed:true,sign_in_required:true,all_sessions_revoked:true}` and
requires normal sign-in. Typed errors carry a code and sign-in requirement.

No outcome of this endpoint sets or deletes a cookie. A delayed old reply must
not clear another tab's newer sign-in. The UI clears entered secrets and performs
guarded local sign-out without calling ambient logout. It stores no password or
replay journal; an uncertain outcome offers manual sign-in. Browser HTTPS,
two-tab callback behavior, visual/accessibility and usability acceptance remain
separate from the passing 16 PostgreSQL cases. See private
`PASSWORD_CHANGE_TEST_REPORT_20261117_r2.txt` for the exact hosted source/results.

## Still required

Public signup/delivery/domain/terms approval, forgotten-password delivery/reset, verified
lost-all-factor recovery, owner transfer/replacement, tenant suspension/closure,
controlled support access, complete store export/offboarding/retention and actual
customer rollout remain separate unfinished work. Invitation-first is a concrete
onboarding step, not a claim that commercial onboarding is complete.
