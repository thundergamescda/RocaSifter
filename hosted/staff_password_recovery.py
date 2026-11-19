"""Owner-mediated non-owner password recovery; existing MFA is never removed.

Lock order is issuer owner -> target non-owner -> recovery. Both validation
passes use real authority and wall-clock time after locks; PBKDF2 runs outside
SQL. Token redemption creates no session and must not change browser cookies.
"""
import re
import secrets
import uuid

from .database import audit
from .security import ServiceError, digest, email_address, password_hash
from .staff_authority import require_staff_authority


INVALID = 'This recovery is unavailable, expired or already used. Ask your store owner for a new private link.'


class StaffPasswordRecovery:
    def __init__(self, auth):
        self.auth, self.database = auth, auth.database

    @staticmethod
    def _target(db, tenant, target):
        # Exclude owners before locking, preventing owner-to-owner lock cycles.
        row = db.execute("""SELECT id,email,password_hash,role FROM ss_staff
            WHERE tenant_id=%s AND id=%s AND enabled=TRUE AND role<>'owner' FOR UPDATE""",
            (tenant, target)).fetchone()
        if not row:
            raise ServiceError(INVALID, 403)
        state = db.execute('SELECT enabled,version FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR UPDATE',
                           (tenant, target)).fetchone()
        return row, state

    @staticmethod
    def _security_binding(staff, mfa):
        # No password hash, factor secret or recovery code is copied to the token
        # record. Role/email/factor changes invalidate previously issued tokens.
        return digest('\0'.join((staff['email'], staff['role'], staff['password_hash'],
            str(bool(mfa and mfa['enabled'])), str(mfa['version'] if mfa else 0))))

    def issue(self, actor, staff_id, data, address=''):
        if type(data) is not dict or data != {'staff_identity_confirmed': True} or data['staff_identity_confirmed'] is not True:
            raise ServiceError('Confirm the intended staff member’s identity before issuing password recovery.')
        try:
            target = uuid.UUID(str(staff_id))
        except (TypeError, ValueError, AttributeError):
            raise ServiceError('Choose an enabled non-owner staff member in your store.') from None
        with self.database.transaction() as db:
            require_staff_authority(db, actor, write=True)
        # Persistent independently committed IP + issuer budgets remain spent
        # even if target lookup, audit, a later authority check or commit fails.
        try:
            self.auth.mfa._budget('staff-password-recovery-issue', address, actor['staff_id'])
        except ServiceError as error:
            if error.status == 429:
                raise ServiceError('Too many password-recovery attempts. Try again later.', 429) from None
            raise
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            require_staff_authority(db, actor, write=True)
            staff, mfa = self._target(db, actor['tenant_id'], target)
            # The target lock serializes all issuers. A newer issue invalidates
            # every previous unredeemed link, including one currently hashing.
            db.execute('''UPDATE ss_staff_password_recoveries SET cancelled_at=clock_timestamp()
                WHERE tenant_id=%s AND staff_id=%s AND used_at IS NULL AND cancelled_at IS NULL''',
                (actor['tenant_id'], target))
            db.execute('''INSERT INTO ss_staff_password_recoveries
                (token_hash,tenant_id,staff_id,security_binding,created_by,issuer_session_hash,expires_at)
                VALUES (%s,%s,%s,%s,%s,%s,statement_timestamp()+interval '15 minutes')''',
                (digest(token), actor['tenant_id'], target, self._security_binding(staff, mfa),
                 actor['staff_id'], actor['session_hash']))
            audit(db, actor['tenant_id'], actor['staff_id'], 'staff-password-recovery-issued', target,
                  {'expires_in': 900, 'mfa_preserved': True})
            require_staff_authority(db, actor, write=True)
        return {'recovery_token': token, 'expires_in': 900,
                'delivery': 'Share privately with this staff member after confirming their identity. Stay signed in; no email was sent. Existing MFA is still required.'}

    def _validated(self, db, token_hash, email):
        # Initial read gives lock routing only. Never lock token before issuer
        # or target; issue/cancel must use the identical lock order.
        route = db.execute('SELECT tenant_id,staff_id,created_by,issuer_session_hash FROM ss_staff_password_recoveries WHERE token_hash=%s',
                           (token_hash,)).fetchone()
        if not route:
            raise ServiceError(INVALID, 403)
        issuer = {'tenant_id': route['tenant_id'], 'staff_id': route['created_by'],
                  'role': 'owner', 'session_hash': route['issuer_session_hash']}
        try:
            require_staff_authority(db, issuer, write=True)
        except ServiceError:
            raise ServiceError(INVALID, 403) from None
        staff, mfa = self._target(db, route['tenant_id'], route['staff_id'])
        row = db.execute('SELECT * FROM ss_staff_password_recoveries WHERE token_hash=%s FOR UPDATE',
                         (token_hash,)).fetchone()
        checked_at = db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        if not row or row['used_at'] or row['cancelled_at'] or row['expires_at'] <= checked_at:
            # Email lives only on staff; tokens are never transferable to another
            # tenant/account, and no browser-provided tenant/role is accepted.
            raise ServiceError(INVALID, 403)
        if (staff['email'] != email or row['security_binding'] != self._security_binding(staff, mfa)):
            raise ServiceError(INVALID, 403)
        return row, issuer

    def redeem(self, data, address=''):
        if type(data) is not dict or set(data) != {'recovery_token', 'email', 'new_password'}:
            raise ServiceError('Use the private recovery code, staff email and new password.')
        token, new = data['recovery_token'], data['new_password']
        if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
            raise ServiceError(INVALID, 403)
        if type(new) is not str or not 15 <= len(new) <= 128:
            raise ServiceError('Use a new password of 15 to 128 characters.')
        try:
            new.encode('utf-8')
        except UnicodeError:
            raise ServiceError('Use a new password of 15 to 128 Unicode characters.') from None
        email, token_hash = email_address(data['email']), digest(token)
        # Independent committed invite-style budget includes unknown/replayed
        # tokens and every later rollback, before any expensive password hash.
        try:
            self.auth._invite_attempt('staff-password-recovery:' + str(address))
        except ServiceError as error:
            if error.status == 429:
                raise ServiceError('Too many password-recovery attempts. Try again later.', 429) from None
            raise
        with self.database.transaction() as db:
            self._validated(db, token_hash, email)
        replacement = password_hash(new)
        with self.database.transaction() as db:
            row, issuer = self._validated(db, token_hash, email)
            tenant, target = row['tenant_id'], row['staff_id']
            db.execute('UPDATE ss_staff SET password_hash=%s WHERE tenant_id=%s AND id=%s',
                       (replacement, tenant, target))
            db.execute('''UPDATE ss_staff_mfa SET pending_seed=NULL,pending_token_hash=NULL,pending_session_hash=NULL,
                pending_password_fingerprint=NULL,pending_expires_at=NULL,pending_attempts=0,updated_at=clock_timestamp()
                WHERE tenant_id=%s AND staff_id=%s AND pending_seed IS NOT NULL''', (tenant, target))
            db.execute('''UPDATE ss_mfa_challenges SET cancelled_at=clock_timestamp() WHERE tenant_id=%s AND staff_id=%s
                AND used_at IS NULL AND cancelled_at IS NULL''', (tenant, target))
            db.execute('DELETE FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s', (tenant, target))
            db.execute('UPDATE ss_staff_password_recoveries SET used_at=clock_timestamp() WHERE token_hash=%s', (token_hash,))
            audit(db, tenant, issuer['staff_id'], 'staff-password-recovered', target,
                  {'all_sessions_revoked': True, 'mfa_preserved': True})
            # Reject/roll back if owner proof or token expired during work/audit.
            try:
                require_staff_authority(db, issuer, write=True)
            except ServiceError:
                raise ServiceError(INVALID, 403) from None
            at = db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            if row['expires_at'] <= at:
                raise ServiceError(INVALID, 403)
        return {'password_changed': True, 'sign_in_required': True, 'all_sessions_revoked': True,
                'mfa_preserved': True}


def register_staff_password_recovery(app, auth, authenticated, body, require_db):
    """Narrow registration keeps account-recovery behavior out of shared app.py."""
    from flask import g, jsonify, render_template, request
    # Portal/health/readiness also start without a configured database. The
    # existing require_db/authenticated guards reject API use before this value.
    recovery = StaffPasswordRecovery(auth) if auth is not None else None

    @app.get('/staff-password-recovery')
    def staff_password_recovery_page():
        return render_template('staff_password_recovery.html')

    @app.post('/api/staff/<uuid:staff_id>/password-recovery')
    @authenticated
    def issue_staff_password_recovery(staff_id):
        return jsonify(recovery.issue(g.actor, staff_id, body(), request.remote_addr or '')), 201

    @app.post('/api/staff/password-recovery/redeem')
    def redeem_staff_password_recovery():
        require_db()
        # Never consume a token in a signed-in browser. In particular an owner
        # following their own link cannot lose/replace their working session.
        live_cookie = False
        cookie = request.cookies.get('ss_session')
        if cookie:
            try:
                auth.resolve(cookie)
                live_cookie = True
            except ServiceError as error:
                # Revoked/expired HttpOnly cookies can outlive their SQL row.
                # Permit recovery with no current session, without clearing a
                # cookie that another tab could have replaced in the meantime.
                if error.status != 401:
                    raise
        if live_cookie or request.headers.get('Authorization'):
            raise ServiceError('Use a signed-out browser for staff password recovery. Your current session has not changed.', 409)
        if request.content_length is None or request.content_length > 4096:
            raise ServiceError('Send a small password-recovery request.', 413)
        try:
            return jsonify(recovery.redeem(body(), request.remote_addr or ''))
        except ServiceError:
            raise
        except Exception as error:
            app.logger.error('staff_password_recovery_unconfirmed type=%s', type(error).__name__)
            return jsonify(error='The result could not be confirmed. Try signing in with your new password first, then your previous password before requesting a new recovery link.'), 500
