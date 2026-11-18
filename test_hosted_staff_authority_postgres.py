"""Staff-owner authority on real disposable PostgreSQL and HTTP; draft UNRUN.

The helper-only base uses actual Auth/password/MFA enrollment and sessions. No
production TESTING exception, guard disable, injected cookie or authentication
result is used. Explicit negative clock/role mutations are fictional SQL facts.
Provider HTTP is denied by the inherited fixture. Secret-bearing comparisons
use fixed-message booleans so assertion output does not render tokens/proofs.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hmac
import re
import threading
from unittest.mock import patch
import uuid

from hosted.app import create_app
from hosted.database import audit
from hosted.security import ServiceError, digest
from hosted.staff_authority import StaffAuthorityError, require_staff_authority
import test_hosted_credit_authority_postgres as credit_fixture


class PostgreSQLStaffAuthorityAcceptance(credit_fixture._CreditAuthorityFixture):
    origin = 'https://staff-authority.fixture.invalid'
    cookie_domain = 'staff-authority.fixture.invalid'

    def _invite_staff(self, actor=None, *, email='new-staff@example.invalid', role='buyer'):
        return self.auth.invite(self.actors['owner'] if actor is None else actor, {'email': email, 'role': role})

    def _management_rows(self):
        with self.database.transaction() as db:
            return {'invites': db.execute('SELECT * FROM ss_invites WHERE tenant_id=%s ORDER BY token_hash',
                                         (self.tenant,)).fetchall(),
                    'audits': db.execute("SELECT * FROM ss_audit WHERE tenant_id=%s AND action IN ('staff-invited','staff-disabled') ORDER BY id",
                                         (self.tenant,)).fetchall()}

    def _staff_rows(self):
        rows = self._management_rows()
        with self.database.transaction() as db:
            rows['staff'] = db.execute('SELECT id,email,role,enabled,created_at FROM ss_staff WHERE tenant_id=%s ORDER BY id',
                                       (self.tenant,)).fetchall()
            rows['sessions'] = db.execute('SELECT * FROM ss_sessions WHERE tenant_id=%s ORDER BY token_hash',
                                          (self.tenant,)).fetchall()
        return rows

    def _target_rows(self, staff_id):
        with self.database.transaction() as db:
            return {'staff': db.execute('SELECT id,role,enabled FROM ss_staff WHERE tenant_id=%s AND id=%s',
                                        (self.tenant, staff_id)).fetchone(),
                    'sessions': db.execute('SELECT * FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s ORDER BY token_hash',
                                           (self.tenant, staff_id)).fetchall()}

    def _reject_staff(self, function, *, status=403, code='STAFF_MFA_STEP_UP_REQUIRED', sign_in_required=True):
        with self.assertRaises(StaffAuthorityError) as caught:
            function()
        self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.sign_in_required),
                         (status, code, sign_in_required))

    def _http_sign_in(self, role='owner'):
        app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': self.origin,
            'APP_ENCRYPTION_KEYS': self.key, 'BOOTSTRAP_TOKEN': '',
            'SQUARE_APPLICATION_ID': 'fictional-staff-app',
            'SQUARE_APPLICATION_SECRET': 'fictional-staff-secret'}, database=self.database)
        client = app.test_client()
        response = client.post('/api/auth/login', base_url=self.origin,
            headers={'Origin': self.origin}, environ_overrides={'REMOTE_ADDR': self.address},
            json={'store_slug': self.slug, 'email': self.accounts[role]['email'],
                  'password': self.password, 'mfa_supported': True})
        self.assertEqual(response.status_code, 200)
        info = response.get_json()
        self.assertTrue(isinstance(info, dict), 'Login must return its actual typed envelope.')
        if info.get('mfa_required') is True:
            challenge = info.get('challenge_token')
            self.assertTrue(isinstance(challenge, str) and len(challenge) == 43 and not info.get('access_token'),
                            'Password-only challenge must have no full session authority.')
            response = client.post('/api/auth/mfa/challenge', base_url=self.origin,
                headers={'Origin': self.origin}, environ_overrides={'REMOTE_ADDR': self.address},
                json={'challenge_token': challenge, 'code': self.recovery_codes[role].pop()})
            self.assertEqual(response.status_code, 200)
            info = response.get_json()
            self.assertTrue(isinstance(info, dict) and info.get('mfa_authenticated') is True,
                            'Actual MFA proof must issue the enrolled session.')
        token, csrf = info.get('access_token'), info.get('csrf_token')
        self.assertTrue(isinstance(token, str) and 1 <= len(token) <= 256 and
                        isinstance(csrf, str) and len(csrf) == 64,
                        'Sign-in must issue bounded actual session/CSRF proofs.')
        cookie = client.get_cookie('ss_session', domain=self.cookie_domain)
        self.assertTrue(cookie is not None and hmac.compare_digest(cookie.value, token),
                        'Cookie must come from actual sign-in, never fixture injection.')
        me = client.get('/api/me', base_url=self.origin)
        self.assertEqual(me.status_code, 200)
        value = me.get_json()
        self.assertTrue(isinstance(value, dict) and value.get('role') == role and value.get('csrf_token') == csrf,
                        'Actual cookie must resolve the expected role and CSRF proof.')
        return app, client, token, csrf

    def _staff_http(self, client, path, *, csrf=None, body=None):
        if body is None:
            response = client.get(path, base_url=self.origin)
        else:
            headers = {'Origin': self.origin}
            if csrf is not None:
                headers['X-SS-CSRF'] = csrf
            response = client.post(path, base_url=self.origin, headers=headers,
                                   environ_overrides={'REMOTE_ADDR': self.address}, json=body)
        self.assertFalse('Set-Cookie' in response.headers, 'Staff responses must not change browser cookies.')
        return response

    def _assert_staff_http_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        value = response.get_json()
        self.assertTrue(isinstance(value, dict) and set(value) == {'error', 'code', 'sign_in_required'} and
                        value.get('code') == code and value.get('sign_in_required') is True,
                        'Staff denial must expose only its safe typed authority fields.')

    def test_live_owner_roster_allows_aged_and_unenrolled_sessions_without_new_freshness(self):
        original = self.auth.list_staff(self.actors['owner'])
        self.assertTrue(all(set(row) == {'id', 'email', 'role', 'enabled', 'created_at'} for row in original),
                        'Roster must keep its existing credential-free fields.')
        self._set_proof_time("clock_timestamp()-interval '20 minutes'")
        before = self._staff_rows()
        self.assertTrue(self.auth.list_staff(self.actors['owner']) == original, 'Aged proof must still allow an authorized roster read.')
        self.assertTrue(self._staff_rows() == before, 'Roster read changed staff state.')
        self.auth.mfa.change(self.actors['owner'], {'password': self.password,
            'code': self.recovery_codes['owner'].pop()}, self.address, disable=True)
        self._sign_in('owner')
        before = self._staff_rows()
        self.assertTrue(self.auth.list_staff(self.actors['owner']) == original, 'Unenrolled current owner must still be able to read staff.')
        self.assertTrue(self._staff_rows() == before, 'Unenrolled roster read changed staff state.')
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()+interval '5 seconds' WHERE token_hash=%s",
                       (self.actors['owner']['session_hash'],))
        expiring_before = self._staff_rows()
        checks = []
        def age_during_read(db, actor, **kwargs):
            checks.append(kwargs.get('write', False))
            result = require_staff_authority(db, actor, **kwargs)
            if len(checks) == 1:
                db.execute('''SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (expires_at-clock_timestamp())))+0.05)
                              FROM ss_sessions WHERE token_hash=%s''', (actor['session_hash'],))
            return result
        with patch('hosted.auth.require_staff_authority', side_effect=age_during_read):
            self._reject_staff(lambda: self.auth.list_staff(self.actors['owner']), status=401, code='STAFF_SESSION_CHANGED')
        self.assertTrue(checks == [False, False] and self._staff_rows() == expiring_before,
                        'Roster must recheck expiry after the read and return no stale rows.')

    def test_stored_owner_role_tenant_staff_and_session_binding_guard_every_operation(self):
        owner = self.actors['owner']
        manager = self._sign_in('manager')
        buyer = self._sign_in('buyer')
        cashier = self._sign_in('cashier')
        before = self._staff_rows()
        cases = ((manager, 403, 'STAFF_AUTHORITY_CHANGED'), (buyer, 403, 'STAFF_AUTHORITY_CHANGED'),
                 (cashier, 403, 'STAFF_AUTHORITY_CHANGED'),
                 (manager | {'role': 'owner'}, 403, 'STAFF_AUTHORITY_CHANGED'),
                 (owner | {'tenant_id': uuid.uuid4()}, 403, 'STAFF_AUTHORITY_CHANGED'),
                 (owner | {'staff_id': self.accounts['manager']['id']}, 403, 'STAFF_AUTHORITY_CHANGED'),
                 (owner | {'session_hash': manager['session_hash']}, 401, 'STAFF_SESSION_CHANGED'),
                 (owner | {'session_hash': None}, 401, 'STAFF_SESSION_CHANGED'))
        for index, (actor, status, code) in enumerate(cases):
            operations = (lambda: self.auth.list_staff(actor), lambda: self._invite_staff(actor),
                          lambda: self.auth.disable_staff(actor, self.accounts['buyer']['id']))
            for operation, call in enumerate(operations):
                with self.subTest(identity=index, operation=operation):
                    self._reject_staff(call, status=status, code=code)
        self.assertTrue(self._staff_rows() == before, 'Rejected authority changed staff data.')

    def test_mutations_reject_stale_future_unenrolled_and_changed_mfa_despite_actor_flags(self):
        before = self._management_rows()
        for label, expression in (('stale', "clock_timestamp()-interval '16 minutes'"),
                                  ('future', "clock_timestamp()+interval '1 hour'")):
            self._set_proof_time(expression)
            actor = self.actors['owner'] | {'mfa_enabled': True, 'mfa_authenticated': True, 'mfa_fresh': True}
            with self.subTest(proof=label):
                self._reject_staff(lambda: self._invite_staff(actor))
                self._reject_staff(lambda: self.auth.disable_staff(actor, self.accounts['manager']['id']))
        self.auth.mfa.change(self.actors['owner'], {'password': self.password,
            'code': self.recovery_codes['owner'].pop()}, self.address, disable=True)
        self._sign_in('owner')
        actor = self.actors['owner'] | {'mfa_enabled': True, 'mfa_authenticated': True, 'mfa_fresh': True}
        self._reject_staff(lambda: self._invite_staff(actor))
        self._reject_staff(lambda: self.auth.disable_staff(actor, self.accounts['manager']['id']))
        self._enroll_role('owner')
        with self.database.transaction() as db:
            db.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',
                       (self.tenant, self.accounts['owner']['id']))
        actor = self.actors['owner']
        for call in (lambda: self.auth.list_staff(actor), lambda: self._invite_staff(actor),
                     lambda: self.auth.disable_staff(actor, self.accounts['manager']['id'])):
            self._reject_staff(call, status=401, code='STAFF_SESSION_CHANGED')
        self.assertTrue(self._management_rows() == before, 'Failed MFA authority changed staff mutations.')

    def test_invitation_keeps_hash_only_fixed_roles_twenty_four_hour_ttl_and_single_use(self):
        before_invalid = self._management_rows()
        with self.assertRaises(ServiceError) as caught:
            self._invite_staff(role='owner')
        self.assertEqual(caught.exception.status, 400)
        self.assertTrue(self._management_rows() == before_invalid, 'Invitation cannot grant an owner role.')
        for role in ('manager', 'buyer', 'cashier'):
            email = 'new-' + role + '@example.invalid'
            with self.database.transaction() as db:
                before = db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            result = self._invite_staff(email=email, role=role)
            self.assertTrue(set(result) == {'invite_token', 'expires_in', 'delivery'} and result['expires_in'] == 86400,
                            'Invitation result shape and TTL must remain unchanged.')
            token = result['invite_token']
            self.assertTrue(isinstance(token, str) and re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is not None,
                            'Invitation must retain its opaque random token format.')
            with self.database.transaction() as db:
                row = db.execute('SELECT *,clock_timestamp() AS checked_at FROM ss_invites WHERE token_hash=%s',
                                 (digest(token),)).fetchone()
            self.assertTrue(row is not None and row['token_hash'] == digest(token) and row['role'] == role and
                            before + timedelta(hours=24) <= row['expires_at'] <= row['checked_at'] + timedelta(hours=24),
                            'Stored invitation must retain its exact role, hash and bounded24h expiry.')
            self.assertTrue(token not in str(self._management_rows()), 'Raw invitation token appeared in database evidence.')
            accepted = self.auth.accept_invite({'invite_token': token, 'email': email,
                'password': self.password, 'role': 'owner'}, self.address)
            self.assertTrue(accepted == {'staff_created': True}, 'Invite acceptance result changed.')
            with self.assertRaises(ServiceError):
                self.auth.accept_invite({'invite_token': token, 'email': email, 'password': self.password}, self.address)
            with self.database.transaction() as db:
                staff = db.execute('SELECT id,role,enabled FROM ss_staff WHERE tenant_id=%s AND email=%s',
                                   (self.tenant, email)).fetchone()
                sessions = db.execute('SELECT count(*) AS n FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s',
                                      (self.tenant, staff['id'])).fetchone()['n']
            self.assertTrue(staff['role'] == role and staff['enabled'] and sessions == 0,
                            'Acceptance must preserve the invited role and create no signed-in session.')

    def test_disable_revokes_all_target_sessions_and_preserves_other_accounts_and_factors(self):
        self._enroll_role('manager')
        first = self.session_tokens['manager']
        self._sign_in('manager')
        second = self.session_tokens['manager']
        self._sign_in('buyer')
        target = self.accounts['manager']['id']
        with self.database.transaction() as db:
            factors = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s', (self.tenant, target)).fetchone()
            recovery = db.execute('SELECT * FROM ss_mfa_recovery_codes WHERE tenant_id=%s AND staff_id=%s ORDER BY code_hash',
                                  (self.tenant, target)).fetchall()
        owner_before = self._target_rows(self.accounts['owner']['id'])
        buyer_before = self._target_rows(self.accounts['buyer']['id'])
        self.assertGreaterEqual(len(self._target_rows(target)['sessions']), 2)
        self.assertTrue(self.auth.disable_staff(self.actors['owner'], target) is None,
                        'Disable must keep its existing service return shape.')
        after = self._target_rows(target)
        self.assertTrue(after['staff']['enabled'] is False and not after['sessions'], 'Target access was not fully revoked.')
        for token in (first, second):
            with self.assertRaises(ServiceError):
                self.auth.resolve(token)
        self.assertTrue(self._target_rows(self.accounts['owner']['id']) == owner_before and
                        self._target_rows(self.accounts['buyer']['id']) == buyer_before,
                        'Disable affected an unrelated account or session.')
        with self.database.transaction() as db:
            self.assertTrue(db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',
                            (self.tenant, target)).fetchone() == factors, 'Disable rewrote active MFA material.')
            self.assertTrue(db.execute('SELECT * FROM ss_mfa_recovery_codes WHERE tenant_id=%s AND staff_id=%s ORDER BY code_hash',
                            (self.tenant, target)).fetchall() == recovery, 'Disable rewrote recovery material.')
        self.assertEqual(len(self._management_rows()['audits']), 1)

    def test_self_foreign_owner_and_invalid_targets_are_preserved_without_cross_owner_deadlock(self):
        self._enroll_role('manager')
        with self.database.transaction() as db:
            db.execute("UPDATE ss_staff SET role='owner' WHERE tenant_id=%s AND id=%s",
                       (self.tenant, self.accounts['manager']['id']))
            foreign, foreign_staff = uuid.uuid4(), uuid.uuid4()
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional foreign staff store')",
                       (foreign, 'foreign-staff-' + foreign.hex))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'foreign@example.invalid',%s,'buyer')",
                       (foreign_staff, foreign, self.saved_password_hash))
        other_owner = self.auth.resolve(self.session_tokens['manager'])
        before = self._staff_rows()
        for target in (self.accounts['owner']['id'], self.accounts['manager']['id'], foreign_staff, uuid.uuid4(), None, 'invalid-target'):
            self._reject_staff(lambda: self.auth.disable_staff(self.actors['owner'], target), status=400,
                               code='STAFF_TARGET_INVALID', sign_in_required=False)
        def cross_disable(actor, target):
            try:
                self.auth.disable_staff(actor, target)
            except StaffAuthorityError as error:
                return error.status, error.code, error.sign_in_required
            return None
        both_owners_locked = threading.Barrier(2)
        def hold_each_owner(*args, **kwargs):
            result = require_staff_authority(*args, **kwargs)
            both_owners_locked.wait(timeout=5)
            return result
        with ThreadPoolExecutor(max_workers=2) as pool, patch('hosted.auth.require_staff_authority', side_effect=hold_each_owner):
            futures = [pool.submit(cross_disable, self.actors['owner'], self.accounts['manager']['id']),
                       pool.submit(cross_disable, other_owner, self.accounts['owner']['id'])]
            results = [future.result(timeout=10) for future in futures]
        self.assertTrue(results == [(400, 'STAFF_TARGET_INVALID', False)] * 2,
                        'Cross-owner targets must fail promptly without locking each other.')
        self.assertTrue(self._staff_rows() == before, 'Protected target rejection changed local staff state.')
        with self.database.transaction() as db:
            self.assertTrue(db.execute('SELECT enabled FROM ss_staff WHERE tenant_id=%s AND id=%s',
                                      (foreign, foreign_staff)).fetchone()['enabled'], 'Foreign staff was disabled.')
        # A non-owner becoming an owner while its row is locked must be
        # re-evaluated by the target predicate, never disabled from stale facts.
        target = self.accounts['buyer']['id']
        admitted = threading.Event()
        management = self._management_rows()
        def owner_checked(*args, **kwargs):
            result = require_staff_authority(*args, **kwargs)
            admitted.set()
            return result
        with ThreadPoolExecutor(max_workers=1) as pool, patch('hosted.auth.require_staff_authority', side_effect=owner_checked):
            with self.database.transaction() as blocker:
                blocker.execute('SELECT id FROM ss_staff WHERE tenant_id=%s AND id=%s FOR UPDATE', (self.tenant, target))
                future = pool.submit(self.auth.disable_staff, self.actors['owner'], target)
                self.assertTrue(admitted.wait(5))
                blocker.execute("UPDATE ss_staff SET role='owner' WHERE tenant_id=%s AND id=%s", (self.tenant, target))
            with self.assertRaises(StaffAuthorityError) as caught:
                future.result(timeout=10)
        self.assertEqual((caught.exception.status, caught.exception.code), (400, 'STAFF_TARGET_INVALID'))
        self.assertTrue(self._target_rows(target)['staff']['role'] == 'owner' and
                        self._target_rows(target)['staff']['enabled'] is True and self._management_rows() == management,
                        'A newly promoted owner was disabled or generated a false disable audit.')

    def test_earlier_logout_disablement_demotion_and_mfa_change_deny_after_owner_lock_wait(self):
        self._sign_in('buyer')
        target = self.accounts['buyer']['id']
        for label in ('logout', 'disable', 'demote', 'mfa-version'):
            with self.subTest(change=label):
                captured = self.actors['owner']
                management, target_before = self._management_rows(), self._target_rows(target)
                waiting = threading.Event()
                def checked(*args, **kwargs):
                    waiting.set()
                    return require_staff_authority(*args, **kwargs)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.database.transaction() as blocker:
                        blocker.execute('SELECT id FROM ss_staff WHERE tenant_id=%s AND id=%s FOR UPDATE',
                                        (self.tenant, self.accounts['owner']['id']))
                        with patch('hosted.auth.require_staff_authority', side_effect=checked):
                            future = pool.submit(self._invite_staff, captured, email='wait-' + label + '@example.invalid')
                            self.assertTrue(waiting.wait(5))
                            if label == 'logout':
                                self.auth.logout(self.session_tokens['owner'])
                            elif label == 'disable':
                                blocker.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',
                                                (self.tenant, self.accounts['owner']['id']))
                            elif label == 'demote':
                                blocker.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",
                                                (self.tenant, self.accounts['owner']['id']))
                            else:
                                blocker.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',
                                                (self.tenant, self.accounts['owner']['id']))
                    with self.assertRaises(StaffAuthorityError):
                        future.result(timeout=10)
                self.assertTrue(self._management_rows() == management and self._target_rows(target) == target_before,
                                'Earlier authority revocation allowed a staff mutation.')
                if label != 'mfa-version':
                    with self.database.transaction() as db:
                        db.execute("UPDATE ss_staff SET enabled=TRUE,role='owner' WHERE tenant_id=%s AND id=%s",
                                   (self.tenant, self.accounts['owner']['id']))
                    self._sign_in('owner')

    def test_authorized_invite_linearizes_before_later_owner_logout(self):
        locked, release, revoking, revoked = (threading.Event() for _ in range(4))
        calls = []
        def checked(*args, **kwargs):
            result = require_staff_authority(*args, **kwargs)
            calls.append(kwargs.get('write', False))
            if len(calls) == 1:
                locked.set()
                if not release.wait(5):
                    raise RuntimeError('Fictional staff authority barrier expired.')
            return result
        def revoke():
            revoking.set()
            self.auth.logout(self.session_tokens['owner'])
            revoked.set()
        with ThreadPoolExecutor(max_workers=2) as pool, patch('hosted.auth.require_staff_authority', side_effect=checked):
            creating = pool.submit(self._invite_staff)
            try:
                self.assertTrue(locked.wait(5))
                logout = pool.submit(revoke)
                self.assertTrue(revoking.wait(5))
                self.assertFalse(revoked.wait(.1), 'Logout bypassed the authorized transaction session lock.')
            finally:
                release.set()
            result = creating.result(timeout=10)
            self.assertTrue(isinstance(result, dict) and result.get('expires_in') == 86400,
                            'The authorized invitation must commit before the waiting logout.')
            logout.result(timeout=10)
        self.assertTrue(calls == [True, True] and revoked.is_set(), 'Both live write checks and later revocation must complete.')
        self._reject_staff(lambda: self._invite_staff(email='after-logout@example.invalid'), status=401, code='STAFF_SESSION_CHANGED')
        self._reject_staff(lambda: self.auth.list_staff(self.actors['owner']), status=401, code='STAFF_SESSION_CHANGED')
        self.assertEqual(len(self._management_rows()['invites']), 1)
        self.assertEqual(len(self._management_rows()['audits']), 1)

    def test_final_session_expiry_during_target_wait_rolls_back_disable_sessions_and_audit(self):
        self._sign_in('manager')
        self._sign_in('manager')
        target = self.accounts['manager']['id']
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()+interval '5 seconds' WHERE token_hash=%s",
                       (self.actors['owner']['session_hash'],))
        before = self._staff_rows()
        authorized, audited = threading.Event(), threading.Event()
        def checked(*args, **kwargs):
            result = require_staff_authority(*args, **kwargs)
            authorized.set()
            return result
        def recorded(*args, **kwargs):
            audit(*args, **kwargs)
            audited.set()
        with ThreadPoolExecutor(max_workers=1) as pool, patch('hosted.auth.require_staff_authority', side_effect=checked), patch('hosted.auth.audit', side_effect=recorded):
            with self.database.transaction() as blocker:
                blocker.execute('SELECT id FROM ss_staff WHERE tenant_id=%s AND id=%s FOR UPDATE', (self.tenant, target))
                future = pool.submit(self.auth.disable_staff, self.actors['owner'], target)
                self.assertTrue(authorized.wait(5))
                blocker.execute('''SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (expires_at-clock_timestamp())))+0.05)
                                   FROM ss_sessions WHERE token_hash=%s''', (self.actors['owner']['session_hash'],))
                self.assertTrue(blocker.execute('SELECT expires_at<=clock_timestamp() AS expired FROM ss_sessions WHERE token_hash=%s',
                                               (self.actors['owner']['session_hash'],)).fetchone()['expired'])
            with self.assertRaises(StaffAuthorityError) as caught:
                future.result(timeout=10)
        self.assertEqual((caught.exception.status, caught.exception.code), (401, 'STAFF_SESSION_CHANGED'))
        self.assertTrue(audited.is_set(), 'Expiry must be rejected after the attempted target/session/audit work.')
        self.assertTrue(self._staff_rows() == before, 'Final expiry failed to roll back target sessions, status or audit.')

    def test_final_mfa_age_after_invite_audit_rolls_back_token_and_requires_new_proof(self):
        self._set_proof_time("clock_timestamp()-interval '895 seconds'")
        before = self._staff_rows()
        audited = threading.Event()
        def age_after_audit(db, *args, **kwargs):
            audit(db, *args, **kwargs)
            audited.set()
            db.execute('''SELECT pg_sleep(GREATEST(0,900-EXTRACT(EPOCH FROM (clock_timestamp()-mfa_authenticated_at)))+0.05)
                          FROM ss_sessions WHERE token_hash=%s''', (self.actors['owner']['session_hash'],))
        with patch('hosted.auth.audit', side_effect=age_after_audit):
            self._reject_staff(lambda: self._invite_staff())
        self.assertTrue(audited.is_set() and self._staff_rows() == before,
                        'Final proof-age rejection must roll back the hashed token and audit.')
        self._sign_in('owner')
        result = self._invite_staff()
        self.assertTrue(isinstance(result.get('invite_token'), str), 'A new actual proof must allow the legitimate invitation.')
        self.assertEqual(len(self._management_rows()['invites']), 1)

    def test_audit_failure_rolls_back_invite_and_disable_without_partial_access_changes(self):
        self._sign_in('manager')
        self._sign_in('manager')
        before = self._staff_rows()
        calls = (lambda: self._invite_staff(),
                 lambda: self.auth.disable_staff(self.actors['owner'], self.accounts['manager']['id']))
        for operation, call in enumerate(calls):
            with self.subTest(operation=operation), patch('hosted.auth.audit', side_effect=RuntimeError('Fictional staff audit failure')):
                with self.assertRaises(RuntimeError):
                    call()
            self.assertTrue(self._staff_rows() == before, 'Audit failure left a token, disabled account or missing session.')
        self._invite_staff()
        self.auth.disable_staff(self.actors['owner'], self.accounts['manager']['id'])
        self.assertEqual(len(self._management_rows()['invites']), 1)
        self.assertEqual(len(self._management_rows()['audits']), 2)

    def test_real_http_cookie_csrf_role_and_fresh_mfa_boundaries_keep_safe_errors(self):
        _, manager_client, _, manager_csrf = self._http_sign_in('manager')
        before = self._staff_rows()
        self._assert_staff_http_error(self._staff_http(manager_client, '/api/staff'), 403, 'STAFF_AUTHORITY_CHANGED')
        self._assert_staff_http_error(self._staff_http(manager_client, '/api/staff/invites', csrf=manager_csrf,
            body={'email': 'forbidden@example.invalid', 'role': 'buyer'}), 403, 'STAFF_AUTHORITY_CHANGED')
        self.assertTrue(self._staff_rows() == before, 'Non-owner HTTP request changed staff data.')
        _, client, token, csrf = self._http_sign_in()
        response = self._staff_http(client, '/api/staff')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(isinstance(response.get_json(), dict) and isinstance(response.get_json().get('staff'), list),
                        'Authorized roster must retain its existing HTTP envelope.')
        allowed = self._staff_http(client, '/api/staff/invites', csrf=csrf,
            body={'email': 'http-allowed@example.invalid', 'role': 'cashier'})
        self.assertEqual(allowed.status_code, 201)
        self.assertTrue(isinstance(allowed.get_json(), dict) and
                        set(allowed.get_json()) == {'invite_token', 'expires_in', 'delivery'},
                        'Authorized HTTP mutation must retain the original invitation result.')
        before = self._staff_rows()
        missing_csrf = self._staff_http(client, '/api/staff/invites',
            body={'email': 'missing-csrf@example.invalid', 'role': 'buyer'})
        self.assertEqual(missing_csrf.status_code, 403)
        self.assertTrue(self._staff_rows() == before, 'Missing CSRF changed staff state.')
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",
                       (digest(token),))
        before = self._staff_rows()
        self.assertEqual(self._staff_http(client, '/api/staff').status_code, 200)
        self._assert_staff_http_error(self._staff_http(client, '/api/staff/invites', csrf=csrf,
            body={'email': 'http-aged@example.invalid', 'role': 'buyer'}), 403, 'STAFF_MFA_STEP_UP_REQUIRED')
        self._assert_staff_http_error(self._staff_http(client,
            '/api/staff/' + str(self.accounts['manager']['id']) + '/disable', csrf=csrf, body={}),
            403, 'STAFF_MFA_STEP_UP_REQUIRED')
        self.assertTrue(self._staff_rows() == before, 'Aged HTTP mutation changed staff data or cookie context.')

    def test_real_http_logout_after_resolution_denies_roster_invite_and_disable_without_cookie_change(self):
        self._sign_in('manager')
        target = self.accounts['manager']['id']
        operations = (('list_staff', '/api/staff', None),
                      ('invite', '/api/staff/invites', {'email': 'http-race@example.invalid', 'role': 'buyer'}),
                      ('disable_staff', '/api/staff/' + str(target) + '/disable', {}))
        for method, path, body in operations:
            with self.subTest(operation=method):
                app, client, token, csrf = self._http_sign_in()
                service = app.extensions['ss_auth']
                original = getattr(service, method)
                before, target_before = self._management_rows(), self._target_rows(target)
                revocations = []
                def revoke_before_operation(actor, *args, **kwargs):
                    self.assertTrue(actor.get('session_hash') == digest(token), 'Race must target the actual resolved HTTP session.')
                    service.logout(token)
                    revocations.append(True)
                    return original(actor, *args, **kwargs)
                with patch.object(service, method, side_effect=revoke_before_operation):
                    response = self._staff_http(client, path, csrf=csrf, body=body)
                self.assertEqual(len(revocations), 1)
                self._assert_staff_http_error(response, 401, 'STAFF_SESSION_CHANGED')
                cookie = client.get_cookie('ss_session', domain=self.cookie_domain)
                self.assertTrue(cookie is not None and hmac.compare_digest(cookie.value, token),
                                'Staff denial must not erase a newer browser cookie through an ambient logout response.')
                self.assertTrue(self._management_rows() == before and self._target_rows(target) == target_before,
                                'Resolve/logout race changed staff evidence or target sessions.')
