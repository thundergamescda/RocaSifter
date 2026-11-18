"""Live authority for manual credit corrections, never provider permission.

Call after the request-key advisory lock and before customer/financial locks.
SHARE locks remain held through commit: staff -> MFA -> exact session. A final
call after financial/audit work rejects expiry or proof aging during blocking.
Exact committed-result recovery requires a current actor/session, not new MFA
freshness. No actor-supplied MFA boolean or authentication timestamp is trusted.
"""
import re
import uuid

from .security import ServiceError


class CreditAuthorityError(ServiceError):
    def __init__(self, message, status=403, *, code='CREDIT_AUTHORITY_CHANGED', sign_in_required=True):
        super().__init__(message, status)
        self.code = code
        self.sign_in_required = sign_in_required


def require_credit_authority(db, actor, *, fresh_mfa=True):
    """Require this live owner/manager identity; return no credential or DTO.

    Revocation that commits first is observed here. Revocation that meets these
    row locks waits for this financial transaction; it cannot retroactively
    cancel an authorized commit. Wall-clock expiry and the 15-minute MFA limit
    are checked again by the caller immediately before recording a new result.
    """
    if type(fresh_mfa) is not bool:
        raise ValueError('Credit freshness policy must be explicit.')
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
        raise CreditAuthorityError('Your current session is required for this credit action.', 401,
                                   code='CREDIT_SESSION_CHANGED') from None
    staff = db.execute('SELECT role,enabled FROM ss_staff WHERE tenant_id=%s AND id=%s FOR SHARE',
                       (tenant, staff_id)).fetchone()
    if not staff or not staff['enabled'] or staff['role'] not in ('owner', 'manager') or staff['role'] != role:
        raise CreditAuthorityError('Your current store role cannot perform this credit action.')
    mfa = db.execute('SELECT enabled,version FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR SHARE',
                     (tenant, staff_id)).fetchone()
    session = db.execute('''SELECT expires_at,mfa_version,mfa_authenticated_at FROM ss_sessions
                            WHERE tenant_id=%s AND staff_id=%s AND token_hash=%s FOR SHARE''',
                         (tenant, staff_id, session_hash)).fetchone()
    # Sample only after acquiring every authority lock, including a blocked
    # session lock. Transaction-start now() and a pre-lock sample are unsuitable.
    checked_at = db.execute('SELECT clock_timestamp() AS checked_at').fetchone()['checked_at']
    if not session or session['expires_at'] <= checked_at:
        raise CreditAuthorityError('Your session expired or changed. Sign in again.', 401,
                                   code='CREDIT_SESSION_CHANGED')
    if mfa and mfa['enabled'] and (session['mfa_version'] != mfa['version'] or not session['mfa_authenticated_at']):
        raise CreditAuthorityError('Your account security changed. Sign in again.', 401,
                                   code='CREDIT_SESSION_CHANGED')
    if fresh_mfa:
        at = session['mfa_authenticated_at']
        if (not mfa or not mfa['enabled'] or session['mfa_version'] != mfa['version'] or not at or
                not 0 <= (checked_at - at).total_seconds() <= 900):
            raise CreditAuthorityError('Enroll MFA and sign in with a fresh second-factor proof from the last 15 minutes before changing credit.',
                                       code='MFA_STEP_UP_REQUIRED')
