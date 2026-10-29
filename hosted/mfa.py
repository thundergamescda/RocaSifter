"""Optional TOTP with bound encrypted seeds and atomic one-use proof.

RFC 6238/4226 primitives use standard-library HMAC. Authenticator setup is
explicit; no historical staff accounts are enrolled by migration.
"""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
import struct
from urllib.parse import quote, urlencode
from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from .database import audit
from .security import ServiceError, digest, password_valid


INVALID = 'The sign-in verification is invalid, expired or already used. Start sign-in again if needed.'


def hotp(secret, counter, *, digits=6, algorithm='sha1'):
    if not isinstance(secret, bytes) or not 16 <= len(secret) <= 128:
        raise ValueError('Invalid OTP secret.')
    if isinstance(counter, bool) or not isinstance(counter, int) or not 0 <= counter < 2**64:
        raise ValueError('Invalid OTP counter.')
    if digits not in (6, 8) or algorithm not in ('sha1', 'sha256', 'sha512'):
        raise ValueError('Invalid OTP parameters.')
    value = hmac.new(secret, struct.pack('>Q', counter), algorithm).digest()
    offset = value[-1] & 15
    number = struct.unpack('>I', value[offset:offset + 4])[0] & 0x7fffffff
    return str(number % 10**digits).zfill(digits)


def totp(secret, timestamp, *, digits=6, algorithm='sha1'):
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not 0 <= timestamp < 2**64 * 30:
        raise ValueError('Invalid OTP time.')
    return hotp(secret, int(timestamp // 30), digits=digits, algorithm=algorithm)


def secret_bytes(seed):
    if not isinstance(seed, str) or not re.fullmatch(r'[A-Z2-7]{32}', seed):
        raise ValueError('Invalid authenticator seed.')
    return base64.b32decode(seed)


def matching_step(seed, code, timestamp, last_step=-1):
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}', code):
        return None
    secret = secret_bytes(seed)
    step = int(timestamp // 30)
    matched = None
    for candidate in (step - 1, step, step + 1):
        if candidate >= 0 and hmac.compare_digest(hotp(secret, candidate), code) and candidate > last_step:
            matched = candidate
    return matched


def recovery_normalized(code):
    if not isinstance(code, str) or len(code) > 64:
        return None
    value = code.strip().replace('-', '').lower()
    return value if re.fullmatch(r'[a-f0-9]{32}', value) else None


def recovery_hash(tenant, staff, code):
    value = recovery_normalized(code)
    return digest('ss:mfa:recovery:v1:' + str(tenant) + ':' + str(staff) + ':' + value) if value else None


def new_recovery_codes():
    return ['-'.join(value[i:i + 4] for i in range(0, 32, 4))
            for value in (secrets.token_hex(16) for _ in range(10))]


class SeedCipher:
    def __init__(self, keys):
        values = [key.strip() for key in str(keys).split(',') if key.strip()]
        try:
            if any(len(value) < 32 for value in values):
                raise ValueError()
            # Domain separation keeps MFA seed encryption separate from Square tokens.
            self.cipher = MultiFernet([Fernet(base64.urlsafe_b64encode(hashlib.sha256(
                b'stock-and-sleeve:mfa-seeds:v1:' + key.encode()).digest())) for key in values]) if values else None
        except (ValueError, TypeError):
            raise ServiceError('Server MFA encryption configuration is invalid.', 503) from None

    def encrypt(self, seed, tenant, staff, purpose, binding):
        if not self.cipher:
            raise ServiceError('Server MFA encryption configuration is required.', 503)
        secret_bytes(seed)
        payload = {'v': 1, 'tenant': str(tenant), 'staff': str(staff),
                   'purpose': purpose, 'binding': str(binding), 'seed': seed}
        return self.cipher.encrypt(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).decode()

    def decrypt(self, ciphertext, tenant, staff, purpose, binding):
        try:
            if not self.cipher:
                raise ValueError()
            payload = json.loads(self.cipher.decrypt(ciphertext.encode()))
            expected = {'v': 1, 'tenant': str(tenant), 'staff': str(staff),
                        'purpose': purpose, 'binding': str(binding)}
            if any(payload.get(key) != value for key, value in expected.items()):
                raise ValueError()
            secret_bytes(payload['seed'])
            return payload['seed']
        except (InvalidToken, ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError('Authenticator verification is unavailable. Contact the service administrator.', 503) from None


def issue_session(db, staff, *, mfa_version=None):
    token = secrets.token_urlsafe(32)
    db.execute('DELETE FROM ss_sessions WHERE expires_at<now()')
    db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                  VALUES (%s,%s,%s,now()+interval '8 hours',%s,CASE WHEN %s::BIGINT IS NULL THEN NULL ELSE now() END)''',
               (digest(token), staff['tenant_id'], staff['id'], mfa_version, mfa_version))
    audit(db, staff['tenant_id'], staff['id'], 'staff-login', staff['id'], {'mfa': mfa_version is not None})
    return token, {'store': staff['slug'], 'store_name': staff.get('name', staff['slug']),
                   'role': staff['role'], 'expires_in': 28800, 'mfa_authenticated': mfa_version is not None}


class MFA:
    def __init__(self, database, keys='', *, clock=None):
        self.database, self.cipher = database, SeedCipher(keys)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _budget(self, namespace, address, staff=None):
        keys = [(digest(namespace + ':ip:' + str(address)), 50)]
        if staff is not None:
            keys.append((digest(namespace + ':staff:' + str(staff)), 10))
        limited = False
        with self.database.transaction() as db:
            db.execute("DELETE FROM ss_login_attempts WHERE window_start<now()-interval '1 day'")
            for key, maximum in keys:
                db.execute('INSERT INTO ss_login_attempts(key_hash) VALUES (%s) ON CONFLICT DO NOTHING', (key,))
                row = db.execute('SELECT * FROM ss_login_attempts WHERE key_hash=%s FOR UPDATE', (key,)).fetchone()
                if row['window_start'] < self.clock() - timedelta(minutes=15):
                    db.execute('UPDATE ss_login_attempts SET failures=1,window_start=now() WHERE key_hash=%s', (key,))
                elif row['failures'] >= maximum:
                    limited = True
                else:
                    db.execute('UPDATE ss_login_attempts SET failures=failures+1 WHERE key_hash=%s', (key,))
        if limited:
            raise ServiceError('Too many authenticator attempts. Try again later.', 429)

    @staticmethod
    def _locked_staff(db, tenant, staff_id):
        row = db.execute('''SELECT s.*,t.slug,t.name FROM ss_staff s JOIN ss_tenants t ON t.id=s.tenant_id
                            WHERE s.tenant_id=%s AND s.id=%s FOR UPDATE OF s''', (tenant, staff_id)).fetchone()
        if not row or not row['enabled']:
            raise ServiceError(INVALID, 401)
        return row

    def _actor(self, db, actor):
        staff = self._locked_staff(db, actor['tenant_id'], actor['staff_id'])
        session = db.execute('''SELECT token_hash FROM ss_sessions WHERE token_hash=%s AND tenant_id=%s
                                AND staff_id=%s AND expires_at>now() FOR SHARE''',
                             (actor.get('session_hash', ''), staff['tenant_id'], staff['id'])).fetchone()
        if not session:
            raise ServiceError('Your session expired. Sign in again.', 401)
        return staff

    def _reauthenticate(self, actor, password, address):
        self._budget('mfa-management', address, actor['staff_id'])
        with self.database.transaction() as db:
            row = db.execute('SELECT password_hash FROM ss_staff WHERE tenant_id=%s AND id=%s AND enabled=TRUE',
                             (actor['tenant_id'], actor['staff_id'])).fetchone()
        if not row or not password_valid(password, row['password_hash']):
            raise ServiceError('Confirm your current password to change authenticator settings.', 403)
        return row['password_hash']

    def begin_challenge(self, db, staff, address):
        row = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR UPDATE',
                         (staff['tenant_id'], staff['id'])).fetchone()
        if not row or not row['enabled']:
            return issue_session(db, staff)
        if not self.cipher.cipher:
            raise ServiceError('Server MFA encryption configuration is required.', 503)
        token = secrets.token_urlsafe(32)
        db.execute('''UPDATE ss_mfa_challenges SET cancelled_at=now() WHERE tenant_id=%s AND staff_id=%s
                      AND used_at IS NULL AND cancelled_at IS NULL''', (staff['tenant_id'], staff['id']))
        db.execute('''INSERT INTO ss_mfa_challenges(token_hash,tenant_id,staff_id,mfa_version,password_fingerprint,client_hash,expires_at)
                      VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                   (digest(token), staff['tenant_id'], staff['id'], row['version'], digest(staff['password_hash']),
                    digest(str(address)), self.clock() + timedelta(minutes=5)))
        return None, {'mfa_required': True, 'challenge_token': token, 'expires_in': 300,
                      'methods': ['totp', 'recovery_code'], 'store': staff['slug']}

    def _proof(self, db, staff, state, code):
        seed = self.cipher.decrypt(state['encrypted_seed'], staff['tenant_id'], staff['id'], 'active', state['version'])
        step = matching_step(seed, code, self.clock().timestamp(), state['last_totp_step'])
        if step is not None:
            db.execute('UPDATE ss_staff_mfa SET last_totp_step=%s,updated_at=now() WHERE tenant_id=%s AND staff_id=%s',
                       (step, staff['tenant_id'], staff['id']))
            return True
        hashed = recovery_hash(staff['tenant_id'], staff['id'], code)
        if hashed:
            row = db.execute('''UPDATE ss_mfa_recovery_codes SET used_at=now() WHERE tenant_id=%s AND staff_id=%s
                                AND mfa_version=%s AND code_hash=%s AND used_at IS NULL RETURNING code_hash''',
                             (staff['tenant_id'], staff['id'], state['version'], hashed)).fetchone()
            return bool(row)
        return False

    def verify_challenge(self, data, address):
        token = data.get('challenge_token', '')
        self._budget('mfa-verify-ip', address)
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise ServiceError(INVALID, 401)
        with self.database.transaction() as db:
            found = db.execute('SELECT tenant_id,staff_id FROM ss_mfa_challenges WHERE token_hash=%s', (digest(token),)).fetchone()
        if not found:
            raise ServiceError(INVALID, 401)
        self._budget('mfa-verify-staff', address, found['staff_id'])
        failure, result = None, None
        with self.database.transaction() as db:
            staff = self._locked_staff(db, found['tenant_id'], found['staff_id'])
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR UPDATE',
                               (staff['tenant_id'], staff['id'])).fetchone()
            challenge = db.execute('SELECT * FROM ss_mfa_challenges WHERE token_hash=%s FOR UPDATE', (digest(token),)).fetchone()
            if (not challenge or not state or not state['enabled'] or challenge['mfa_version'] != state['version']
                    or challenge['password_fingerprint'] != digest(staff['password_hash'])
                    or challenge['client_hash'] != digest(str(address)) or challenge['expires_at'] <= self.clock()
                    or challenge['used_at'] or challenge['cancelled_at']):
                failure = ServiceError(INVALID, 401)
            elif challenge['attempts'] >= 5:
                failure = ServiceError('Too many attempts. Start sign-in again.', 429)
            elif not self._proof(db, staff, state, data.get('code')):
                db.execute('UPDATE ss_mfa_challenges SET attempts=attempts+1 WHERE token_hash=%s', (digest(token),))
                failure = ServiceError(INVALID, 401)
            else:
                result = issue_session(db, staff, mfa_version=state['version'])
                db.execute('''UPDATE ss_mfa_challenges SET used_at=now(),issued_session_hash=%s WHERE token_hash=%s''',
                           (digest(result[0]), digest(token)))
        if failure:
            raise failure
        return result

    def cancel_challenge(self, data, address):
        token = data.get('challenge_token', '')
        self._budget('mfa-cancel', address)
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            return {'cancelled': True}
        with self.database.transaction() as db:
            found = db.execute('SELECT tenant_id,staff_id FROM ss_mfa_challenges WHERE token_hash=%s', (digest(token),)).fetchone()
        if found:
            with self.database.transaction() as db:
                # Same lock order as verification; cancellation can revoke a
                # committed session even if its verification response was lost.
                db.execute('SELECT id FROM ss_staff WHERE tenant_id=%s AND id=%s FOR UPDATE',
                           (found['tenant_id'], found['staff_id']))
                challenge = db.execute('SELECT * FROM ss_mfa_challenges WHERE token_hash=%s FOR UPDATE', (digest(token),)).fetchone()
                if challenge and challenge['client_hash'] != digest(str(address)):
                    raise ServiceError(INVALID, 401)
                if challenge and challenge['client_hash'] == digest(str(address)):
                    if challenge['issued_session_hash']:
                        db.execute('DELETE FROM ss_sessions WHERE token_hash=%s AND tenant_id=%s AND staff_id=%s',
                                   (challenge['issued_session_hash'], found['tenant_id'], found['staff_id']))
                    db.execute('UPDATE ss_mfa_challenges SET cancelled_at=COALESCE(cancelled_at,now()) WHERE token_hash=%s', (digest(token),))
        return {'cancelled': True}

    def status(self, actor):
        with self.database.transaction() as db:
            staff = self._actor(db, actor)
            state = db.execute('SELECT enabled,pending_expires_at FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',
                               (staff['tenant_id'], staff['id'])).fetchone()
            count = db.execute('''SELECT count(*) AS count FROM ss_mfa_recovery_codes
                                  WHERE tenant_id=%s AND staff_id=%s AND used_at IS NULL''',
                               (staff['tenant_id'], staff['id'])).fetchone()['count']
        return {'enabled': bool(state and state['enabled']), 'recovery_codes_remaining': count,
                'enrollment_pending': bool(state and state['pending_expires_at'] and state['pending_expires_at'] > self.clock())}

    def begin(self, actor, data, address):
        verified_hash = self._reauthenticate(actor, data.get('password'), address)
        seed = base64.b32encode(secrets.token_bytes(20)).decode()
        token = secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            staff = self._actor(db, actor)
            if staff['password_hash'] != verified_hash:
                raise ServiceError('Your password changed. Sign in again.', 401)
            db.execute('INSERT INTO ss_staff_mfa(tenant_id,staff_id) VALUES (%s,%s) ON CONFLICT DO NOTHING',
                       (staff['tenant_id'], staff['id']))
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR UPDATE',
                               (staff['tenant_id'], staff['id'])).fetchone()
            if state['enabled']:
                raise ServiceError('An authenticator is already enabled for this account.', 409)
            encrypted = self.cipher.encrypt(seed, staff['tenant_id'], staff['id'], 'pending', digest(token))
            db.execute('''UPDATE ss_staff_mfa SET pending_seed=%s,pending_token_hash=%s,pending_session_hash=%s,pending_password_fingerprint=%s,
                          pending_expires_at=%s,pending_attempts=0,updated_at=now() WHERE tenant_id=%s AND staff_id=%s''',
                       (encrypted, digest(token), actor['session_hash'], digest(staff['password_hash']), self.clock() + timedelta(minutes=10), staff['tenant_id'], staff['id']))
            audit(db, staff['tenant_id'], staff['id'], 'mfa-enrollment-started', staff['id'])
        label = 'Stock & Sleeve:' + staff['slug'] + ':' + staff['email']
        uri = 'otpauth://totp/' + quote(label, safe='') + '?' + urlencode({
            'secret': seed, 'issuer': 'Stock & Sleeve', 'algorithm': 'SHA1', 'digits': 6, 'period': 30})
        return {'enrollment_token': token, 'secret': seed, 'provisioning_uri': uri, 'expires_in': 600}

    def _save_recovery(self, db, staff, version, codes):
        db.execute('DELETE FROM ss_mfa_recovery_codes WHERE tenant_id=%s AND staff_id=%s', (staff['tenant_id'], staff['id']))
        for code in codes:
            db.execute('INSERT INTO ss_mfa_recovery_codes(tenant_id,staff_id,mfa_version,code_hash) VALUES (%s,%s,%s,%s)',
                       (staff['tenant_id'], staff['id'], version, recovery_hash(staff['tenant_id'], staff['id'], code)))

    def cancel_enrollment(self, actor):
        with self.database.transaction() as db:
            staff = self._actor(db, actor)
            db.execute('''UPDATE ss_staff_mfa SET pending_seed=NULL,pending_token_hash=NULL,pending_session_hash=NULL,
                          pending_password_fingerprint=NULL,pending_expires_at=NULL,pending_attempts=0,updated_at=now()
                          WHERE tenant_id=%s AND staff_id=%s AND enabled=FALSE''', (staff['tenant_id'], staff['id']))
        return {'cancelled': True}

    def confirm(self, actor, data, address):
        self._budget('mfa-confirm', address, actor['staff_id'])
        token = data.get('enrollment_token', '')
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
            raise ServiceError('Authenticator setup expired. Start setup again.', 409)
        failure = None
        codes = new_recovery_codes()
        with self.database.transaction() as db:
            staff = self._actor(db, actor)
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR UPDATE',
                               (staff['tenant_id'], staff['id'])).fetchone()
            if (not state or state['enabled'] or state['pending_token_hash'] != digest(token)
                    or state['pending_session_hash'] != actor['session_hash'] or not state['pending_expires_at']
                    or state['pending_password_fingerprint'] != digest(staff['password_hash'])
                    or state['pending_expires_at'] <= self.clock()):
                failure = ServiceError('Authenticator setup expired. Start setup again.', 409)
            elif state['pending_attempts'] >= 5:
                failure = ServiceError('Too many setup attempts. Start setup again.', 429)
            else:
                seed = self.cipher.decrypt(state['pending_seed'], staff['tenant_id'], staff['id'], 'pending', digest(token))
                step = matching_step(seed, data.get('code'), self.clock().timestamp())
                if step is None:
                    db.execute('UPDATE ss_staff_mfa SET pending_attempts=pending_attempts+1 WHERE tenant_id=%s AND staff_id=%s',
                               (staff['tenant_id'], staff['id']))
                    failure = ServiceError('The authenticator code is incorrect. Check its clock and try again.', 400)
                else:
                    version = state['version'] + 1
                    encrypted = self.cipher.encrypt(seed, staff['tenant_id'], staff['id'], 'active', version)
                    db.execute('''UPDATE ss_staff_mfa SET enabled=TRUE,version=%s,encrypted_seed=%s,last_totp_step=%s,
                                  pending_seed=NULL,pending_token_hash=NULL,pending_session_hash=NULL,pending_password_fingerprint=NULL,pending_expires_at=NULL,
                                  pending_attempts=0,enabled_at=now(),updated_at=now() WHERE tenant_id=%s AND staff_id=%s''',
                               (version, encrypted, step, staff['tenant_id'], staff['id']))
                    self._save_recovery(db, staff, version, codes)
                    db.execute('DELETE FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s AND token_hash<>%s',
                               (staff['tenant_id'], staff['id'], actor['session_hash']))
                    db.execute('UPDATE ss_sessions SET mfa_version=%s,mfa_authenticated_at=now() WHERE token_hash=%s',
                               (version, actor['session_hash']))
                    db.execute('UPDATE ss_mfa_challenges SET cancelled_at=COALESCE(cancelled_at,now()) WHERE tenant_id=%s AND staff_id=%s',
                               (staff['tenant_id'], staff['id']))
                    audit(db, staff['tenant_id'], staff['id'], 'mfa-enabled', staff['id'])
        if failure:
            raise failure
        return {'enabled': True, 'recovery_codes': codes, 'other_sessions_revoked': True}

    def change(self, actor, data, address, *, disable=False):
        verified_hash = self._reauthenticate(actor, data.get('password'), address)
        failure = None
        codes = new_recovery_codes() if not disable else None
        with self.database.transaction() as db:
            staff = self._actor(db, actor)
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR UPDATE',
                               (staff['tenant_id'], staff['id'])).fetchone()
            if staff['password_hash'] != verified_hash or not state or not state['enabled']:
                raise ServiceError('Sign in again before changing authenticator settings.', 401)
            if not self._proof(db, staff, state, data.get('code')):
                failure = ServiceError('Use a fresh authenticator code or an unused recovery code.', 403)
            elif disable:
                db.execute('''UPDATE ss_staff_mfa SET enabled=FALSE,version=version+1,encrypted_seed=NULL,last_totp_step=-1,
                              pending_seed=NULL,pending_token_hash=NULL,pending_session_hash=NULL,pending_password_fingerprint=NULL,pending_expires_at=NULL,
                              pending_attempts=0,updated_at=now() WHERE tenant_id=%s AND staff_id=%s''',
                           (staff['tenant_id'], staff['id']))
                db.execute('DELETE FROM ss_mfa_recovery_codes WHERE tenant_id=%s AND staff_id=%s', (staff['tenant_id'], staff['id']))
                db.execute('DELETE FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s', (staff['tenant_id'], staff['id']))
                db.execute('UPDATE ss_mfa_challenges SET cancelled_at=COALESCE(cancelled_at,now()) WHERE tenant_id=%s AND staff_id=%s',
                           (staff['tenant_id'], staff['id']))
                audit(db, staff['tenant_id'], staff['id'], 'mfa-disabled', staff['id'])
            else:
                self._save_recovery(db, staff, state['version'], codes)
                audit(db, staff['tenant_id'], staff['id'], 'mfa-recovery-codes-replaced', staff['id'])
        if failure:
            raise failure
        return {'enabled': False, 'reauthentication_required': True} if disable else {'enabled': True, 'recovery_codes': codes}
