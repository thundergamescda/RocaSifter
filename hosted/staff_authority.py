"""Live owner authority for staff management, independent of financial policy.

Reads lock the acting owner SHARE. Mutations lock that owner UPDATE immediately,
then MFA and exact session SHARE, matching the account-security lock order and
avoiding any later owner-lock upgrade. The caller must acquire no target lock
first. Disable may subsequently lock only a same-tenant non-owner target; its
SQL predicate must exclude owners before row locking. No provider calls occur.
"""
import re
import uuid

from .security import ServiceError


class StaffAuthorityError(ServiceError):
    def __init__(self, message, status=403, *, code='STAFF_AUTHORITY_CHANGED', sign_in_required=True):
        super().__init__(message, status)
        self.code = code
        self.sign_in_required = sign_in_required


def require_staff_authority(db, actor, *, write=False):
    """Check stored owner/session identity, and fresh enrolled MFA for writes.

    Repeat after potentially blocking reads, target changes and audit work,
    immediately before returning/committing. Time is sampled after every
    authority lock, rather than at transaction start or before a lock wait.
    Revocation committed first denies; later conflicting revocation waits for
    these locks and cannot retroactively cancel an authorized transaction.
    """
    if type(write) is not bool:
        raise ValueError('Staff authority policy must be explicit.')
    try:
        if not isinstance(actor, dict):
            raise ValueError()
        tenant = uuid.UUID(str(actor['tenant_id']))
        staff_id = uuid.UUID(str(actor['staff_id']))
        role = actor.get('role')
        session_hash = actor.get('session_hash')
        if not isinstance(session_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', session_hash):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise StaffAuthorityError('Your current session is required to manage staff.', 401,
                                  code='STAFF_SESSION_CHANGED') from None
    staff = db.execute('SELECT role,enabled FROM ss_staff WHERE tenant_id=%s AND id=%s FOR ' +
                       ('UPDATE' if write else 'SHARE'), (tenant, staff_id)).fetchone()
    if not staff or not staff['enabled'] or staff['role'] != 'owner' or role != staff['role']:
        raise StaffAuthorityError('Only your current enabled store-owner account can manage staff.')
    mfa = db.execute('SELECT enabled,version FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR SHARE',
                     (tenant, staff_id)).fetchone()
    session = db.execute('''SELECT expires_at,mfa_version,mfa_authenticated_at FROM ss_sessions
                            WHERE tenant_id=%s AND staff_id=%s AND token_hash=%s FOR SHARE''',
                         (tenant, staff_id, session_hash)).fetchone()
    checked_at = db.execute('SELECT clock_timestamp() AS checked_at').fetchone()['checked_at']
    if not session or session['expires_at'] <= checked_at:
        raise StaffAuthorityError('Your staff-management session expired or changed. Sign in again.', 401,
                                  code='STAFF_SESSION_CHANGED')
    if mfa and mfa['enabled'] and (session['mfa_version'] != mfa['version'] or not session['mfa_authenticated_at']):
        raise StaffAuthorityError('Your account security changed. Sign in again before managing staff.', 401,
                                  code='STAFF_SESSION_CHANGED')
    if write:
        at = session['mfa_authenticated_at']
        if (not mfa or not mfa['enabled'] or session['mfa_version'] != mfa['version'] or not at or
                not 0 <= (checked_at - at).total_seconds() <= 900):
            raise StaffAuthorityError('Enroll MFA and sign in with a fresh second-factor proof from the last 15 minutes before changing staff access.',
                                      code='STAFF_MFA_STEP_UP_REQUIRED')
