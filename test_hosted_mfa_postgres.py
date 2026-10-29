"""Real PostgreSQL MFA acceptance in a unique ss_test_<uuid> namespace only.

No merchant data, Square, email, live staff accounts, or production migration.
Skipped without DATABASE_URL; a skip is not private-network acceptance evidence.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import os
import threading
import unittest
from unittest.mock import patch
import uuid
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
from hosted.auth import Auth
from hosted.app import create_app
from hosted.database import Database
from hosted.mfa import secret_bytes, totp
from hosted.security import ServiceError, digest, password_hash


class PostgreSQLMFAAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('DATABASE_URL required for isolated private-network PostgreSQL MFA acceptance.')
        cls.schema = 'ss_test_' + uuid.uuid4().hex
        with psycopg.connect(cls.url, sslmode='require') as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        try:
            cls.database = Database(cls.url, schema=cls.schema)
            cls.database.migrate()
            cls.password = 'fictional-owner-password'
            cls.hashed_password = password_hash(cls.password)
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'database'):
            cls.database.close()
        if hasattr(cls, 'schema'):
            with psycopg.connect(cls.url, sslmode='require') as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.tenant, self.staff = uuid.uuid4(), uuid.uuid4()
        self.slug = 'mfa-fixture-' + self.tenant.hex
        self.address = 'fictional-address-' + self.tenant.hex
        self.now = datetime.now(timezone.utc)
        self.key = Fernet.generate_key().decode()
        self.auth = Auth(self.database, encryption_keys=self.key)
        self.auth.mfa.clock = lambda: self.now
        self.credentials = {'store_slug': self.slug, 'email': 'owner@example.invalid',
                            'password': self.password, 'mfa_supported': True}
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',
                       (self.tenant, self.slug, 'Fictional MFA store'))
            db.execute('''INSERT INTO ss_staff(id,tenant_id,email,password_hash,role)
                          VALUES (%s,%s,'owner@example.invalid',%s,'owner')''',
                       (self.staff, self.tenant, self.hashed_password))

    def login(self):
        return self.auth.login(self.credentials, self.address)

    def setup_pending(self):
        token, _ = self.login()
        self.actor = self.auth.resolve(token)
        self.session_token = token
        self.setup = self.auth.mfa.begin(self.actor, {'password': self.password}, self.address)
        return self.setup

    def code(self):
        return totp(secret_bytes(self.setup['secret']), self.now.timestamp())

    def enroll(self):
        self.setup_pending()
        self.confirmed = self.auth.mfa.confirm(self.actor, {
            'enrollment_token': self.setup['enrollment_token'], 'code': self.code()}, self.address)
        self.codes = self.confirmed['recovery_codes']
        self.now += timedelta(seconds=30)
        return self.confirmed

    def challenge(self):
        token, result = self.login()
        self.assertIsNone(token)
        self.assertTrue(result['mfa_required'])
        self.assertNotIn('access_token', result)
        return result['challenge_token']

    def verify(self, challenge, code=None):
        return self.auth.mfa.verify_challenge({'challenge_token': challenge, 'code': self.code() if code is None else code}, self.address)

    def reject(self, function, status=401):
        with self.assertRaises(ServiceError) as caught:
            function()
        self.assertEqual(caught.exception.status, status)

    def session_count(self):
        with self.database.transaction() as db:
            return db.execute('SELECT count(*) AS n FROM ss_sessions WHERE tenant_id=%s', (self.tenant,)).fetchone()['n']

    def legacy_insert(self, db=None):
        token='legacy-fixture-'+uuid.uuid4().hex
        params=(digest(token),self.tenant,self.staff)
        statement="INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at) VALUES (%s,%s,%s,now()+interval '8 hours')"
        if db is None:
            with self.database.transaction() as transaction:transaction.execute(statement,params)
        else:db.execute(statement,params)
        return token

    def test_schema4_does_not_enroll_existing_staff(self):
        self.assertTrue(self.database.ready())
        with self.database.transaction() as db:
            self.assertIsNone(db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s', (self.tenant,)).fetchone())

    def test_unenrolled_login_remains_compatible_without_capability(self):
        token, result = self.auth.login({k: v for k, v in self.credentials.items() if k != 'mfa_supported'}, self.address)
        self.assertTrue(token)
        self.assertEqual(result['role'], 'owner')
        self.assertFalse(self.auth.resolve(token)['mfa_enabled'])

    def test_enrollment_requires_current_password(self):
        token, _ = self.login()
        self.reject(lambda: self.auth.mfa.begin(self.auth.resolve(token), {'password': 'wrong'}, self.address), 403)

    def test_pending_seed_is_encrypted_and_status_never_returns_it(self):
        self.setup_pending()
        with self.database.transaction() as db:
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s', (self.tenant,)).fetchone()
        self.assertNotIn(self.setup['secret'], state['pending_seed'])
        self.assertNotEqual(state['pending_token_hash'], self.setup['enrollment_token'])
        status = self.auth.mfa.status(self.actor)
        self.assertEqual(status, {'enabled': False, 'recovery_codes_remaining': 0, 'enrollment_pending': True})

    def test_confirmation_is_bound_to_exact_session(self):
        self.setup_pending()
        second, _ = self.login()
        self.reject(lambda: self.auth.mfa.confirm(self.auth.resolve(second), {
            'enrollment_token': self.setup['enrollment_token'], 'code': self.code()}, self.address), 409)

    def test_pending_expiration_and_replacement_invalidate_old_proof(self):
        self.setup_pending()
        old = dict(self.setup)
        self.setup = self.auth.mfa.begin(self.actor, {'password': self.password}, self.address)
        self.reject(lambda: self.auth.mfa.confirm(self.actor, {'enrollment_token': old['enrollment_token'],
                    'code': totp(secret_bytes(old['secret']), self.now.timestamp())}, self.address), 409)
        self.now += timedelta(seconds=601)
        self.reject(lambda: self.auth.mfa.confirm(self.actor, {'enrollment_token': self.setup['enrollment_token'],
                    'code': self.code()}, self.address), 409)

    def test_cancel_pending_setup_removes_encrypted_seed_and_token(self):
        self.setup_pending()
        self.assertEqual(self.auth.mfa.cancel_enrollment(self.actor), {'cancelled': True})
        self.reject(lambda: self.auth.mfa.confirm(self.actor, {'enrollment_token': self.setup['enrollment_token'],
                    'code': self.code()}, self.address), 409)
        with self.database.transaction() as db:
            state=db.execute('SELECT pending_seed,pending_token_hash FROM ss_staff_mfa WHERE tenant_id=%s', (self.tenant,)).fetchone()
        self.assertEqual(state, {'pending_seed': None, 'pending_token_hash': None})

    def test_password_change_invalidates_pending_enrollment(self):
        self.setup_pending()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_staff SET password_hash='changed-fixture' WHERE tenant_id=%s", (self.tenant,))
        self.reject(lambda: self.auth.mfa.confirm(self.actor, {'enrollment_token': self.setup['enrollment_token'],
                    'code': self.code()}, self.address), 409)

    def test_confirm_returns_recovery_once_clears_pending_and_revokes_other_sessions(self):
        other, _ = self.login()
        self.enroll()
        self.reject(lambda: self.auth.resolve(other))
        self.assertEqual(self.session_count(), 1)
        self.assertTrue(self.auth.resolve(self.session_token)['mfa_authenticated'])
        self.reject(lambda: self.auth.mfa.confirm(self.actor, {'enrollment_token': self.setup['enrollment_token'],
                    'code': self.code()}, self.address), 409)
        with self.database.transaction() as db:
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s', (self.tenant,)).fetchone()
            codes = db.execute('SELECT code_hash FROM ss_mfa_recovery_codes WHERE tenant_id=%s', (self.tenant,)).fetchall()
        self.assertIsNone(state['pending_seed'])
        self.assertIsNone(state['pending_token_hash'])
        self.assertEqual(len(codes), 10)
        self.assertTrue(all(item['code_hash'] not in self.codes for item in codes))

    def test_enrolled_old_client_requires_upgrade_without_issuing_session(self):
        self.enroll()
        before = self.session_count()
        self.reject(lambda: self.auth.login(self.credentials | {'mfa_supported': False}, self.address), 426)
        self.assertEqual(self.session_count(), before)

    def test_challenge_does_not_issue_full_session_before_proof(self):
        self.enroll()
        self.auth.logout(self.session_token)
        self.challenge()
        self.assertEqual(self.session_count(), 0)

    def test_totp_verification_rechecks_current_role_and_issues_valid_session(self):
        self.enroll()
        challenge = self.challenge()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s", (self.tenant, self.staff))
        token, result = self.verify(challenge)
        self.assertEqual(result['role'], 'manager')
        self.assertTrue(self.auth.resolve(token)['mfa_authenticated'])
        self.reject(lambda: self.verify(challenge))

    def test_totp_step_cannot_replay_across_new_challenges(self):
        self.enroll()
        self.verify(self.challenge())
        self.reject(lambda: self.verify(self.challenge()))
        self.now += timedelta(seconds=30)
        self.verify(self.challenge())

    def test_recovery_code_is_case_tolerant_and_one_use(self):
        self.enroll()
        self.verify(self.challenge(), ' ' + self.codes[0].upper() + ' ')
        self.reject(lambda: self.verify(self.challenge(), self.codes[0]))
        self.assertEqual(self.auth.mfa.status(self.actor)['recovery_codes_remaining'], 9)

    def test_concurrent_recovery_verification_issues_exactly_one_session(self):
        self.enroll()
        challenge = self.challenge()
        before = self.session_count()
        def attempt(_):
            try: return self.verify(challenge, self.codes[0])[0]
            except ServiceError: return None
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(attempt, range(2)))
        self.assertEqual(sum(bool(value) for value in results), 1)
        self.assertEqual(self.session_count(), before + 1)

    def test_cancel_before_verify_prevents_session(self):
        self.enroll()
        challenge = self.challenge()
        self.auth.mfa.cancel_challenge({'challenge_token': challenge}, self.address)
        self.reject(lambda: self.verify(challenge, self.codes[0]))

    def test_cancel_after_lost_verify_response_revokes_only_exact_issued_session(self):
        self.enroll()
        first = self.challenge()
        first_token, _ = self.verify(first, self.codes[0])
        second_token, _ = self.verify(self.challenge(), self.codes[1])
        self.auth.mfa.cancel_challenge({'challenge_token': first}, self.address)
        self.reject(lambda: self.auth.resolve(first_token))
        self.assertTrue(self.auth.resolve(second_token)['mfa_authenticated'])
        self.assertTrue(self.auth.resolve(self.session_token)['mfa_authenticated'])

    def test_concurrent_cancel_and_verify_leaves_no_challenge_session(self):
        self.enroll()
        challenge = self.challenge()
        before = self.session_count()
        def verify():
            try: self.verify(challenge, self.codes[0])
            except ServiceError: pass
        with ThreadPoolExecutor(max_workers=2) as workers:
            a = workers.submit(verify)
            b = workers.submit(self.auth.mfa.cancel_challenge, {'challenge_token': challenge}, self.address)
            a.result(); b.result()
        self.assertEqual(self.session_count(), before)

    def test_expired_challenge_and_wrong_client_cannot_consume_recovery(self):
        self.enroll()
        challenge = self.challenge()
        self.reject(lambda: self.auth.mfa.verify_challenge({'challenge_token': challenge, 'code': self.codes[0]}, 'other-address'))
        self.reject(lambda: self.auth.mfa.cancel_challenge({'challenge_token':challenge},'other-address'))
        self.now += timedelta(seconds=301)
        self.reject(lambda: self.verify(challenge, self.codes[0]))
        self.assertEqual(self.auth.mfa.status(self.actor)['recovery_codes_remaining'], 10)

    def test_disabled_staff_password_change_and_mfa_version_invalidate_challenge(self):
        for changed in ('enabled=FALSE', "password_hash='changed-fixture'", None):
            with self.subTest(changed=changed):
                challenge = self.challenge() if hasattr(self, 'setup') else None
                if challenge is None:
                    self.enroll(); challenge = self.challenge()
                with self.database.transaction() as db:
                    if changed:
                        db.execute('UPDATE ss_staff SET ' + changed + ' WHERE tenant_id=%s AND id=%s', (self.tenant, self.staff))
                    else:
                        db.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s', (self.tenant, self.staff))
                self.reject(lambda: self.verify(challenge, self.codes[0]))
                with self.database.transaction() as db:
                    db.execute('UPDATE ss_staff SET enabled=TRUE,password_hash=%s WHERE tenant_id=%s AND id=%s',
                               (self.hashed_password, self.tenant, self.staff))

    def test_per_challenge_maximum_and_new_challenges_share_proof_budget(self):
        self.enroll()
        challenge = self.challenge()
        for _ in range(5):
            self.reject(lambda: self.verify(challenge, 'not-a-code'))
        self.reject(lambda: self.verify(challenge, self.codes[0]), 429)
        for _ in range(4):
            challenge = self.challenge()
            self.reject(lambda: self.verify(challenge, 'not-a-code'))
            self.auth.mfa.cancel_challenge({'challenge_token': challenge}, self.address)
        challenge = self.challenge()
        self.reject(lambda: self.verify(challenge, self.codes[0]), 429)

    def test_successful_password_reset_cannot_bypass_challenge_issuance_budget(self):
        self.enroll()
        for _ in range(10):
            challenge = self.challenge()
            self.auth.mfa.cancel_challenge({'challenge_token': challenge}, self.address)
        self.reject(self.challenge, 429)

    def test_seed_substitution_between_tenants_fails_closed(self):
        self.enroll()
        challenge = self.challenge()
        with self.database.transaction() as db:
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s', (self.tenant,)).fetchone()
            forged = self.auth.mfa.cipher.encrypt(self.setup['secret'], uuid.uuid4(), self.staff, 'active', state['version'])
            db.execute('UPDATE ss_staff_mfa SET encrypted_seed=%s WHERE tenant_id=%s', (forged, self.tenant))
        self.reject(lambda: self.verify(challenge), 503)

    def test_wrong_tenant_or_revoked_session_cannot_manage_mfa(self):
        self.setup_pending()
        self.reject(lambda: self.auth.mfa.status(self.actor | {'tenant_id': uuid.uuid4()}))
        self.auth.logout(self.session_token)
        self.reject(lambda: self.auth.mfa.confirm(self.actor, {'enrollment_token': self.setup['enrollment_token'], 'code': self.code()}, self.address))

    def test_disable_requires_password_and_unused_proof_then_revokes_all_sessions(self):
        self.enroll()
        self.verify(self.challenge(), self.codes[0])
        self.reject(lambda: self.auth.mfa.change(self.actor, {'password': 'wrong', 'code': self.codes[1]}, self.address, disable=True), 403)
        self.reject(lambda: self.auth.mfa.change(self.actor, {'password': self.password, 'code': self.codes[0]}, self.address, disable=True), 403)
        result = self.auth.mfa.change(self.actor, {'password': self.password, 'code': self.codes[1]}, self.address, disable=True)
        self.assertTrue(result['reauthentication_required'])
        self.assertEqual(self.session_count(), 0)
        token, info = self.auth.login({k: v for k, v in self.credentials.items() if k != 'mfa_supported'}, self.address)
        self.assertTrue(token)
        self.assertFalse(info['mfa_authenticated'])

    def test_recovery_regeneration_invalidates_old_codes_and_exposes_new_codes_once(self):
        self.enroll()
        result = self.auth.mfa.change(self.actor, {'password': self.password, 'code': self.codes[0]}, self.address)
        self.assertEqual(len(result['recovery_codes']), 10)
        self.reject(lambda: self.verify(self.challenge(), self.codes[1]))
        self.verify(self.challenge(), result['recovery_codes'][0])
        self.assertNotIn('recovery_codes', self.auth.mfa.status(self.actor))

    def test_real_routes_cookie_csrf_enrollment_and_mfa_login_end_to_end(self):
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://mfa.fixture.invalid','APP_ENCRYPTION_KEYS':self.key}, database=self.database)
        app.extensions['ss_auth'].mfa.clock=lambda:self.now
        client=app.test_client()
        def post(path,data,csrf=None):
            return client.post(path,json=data,base_url='https://mfa.fixture.invalid',
                               headers={'X-SS-CSRF':csrf} if csrf else {},environ_overrides={'REMOTE_ADDR':self.address})
        response=post('/api/auth/login',self.credentials)
        self.assertEqual(response.status_code,200)
        csrf=response.json['csrf_token']
        self.assertEqual(post('/api/auth/mfa/enroll',{'password':self.password}).status_code,403)
        response=post('/api/auth/mfa/enroll',{'password':self.password},csrf)
        self.assertEqual(response.status_code,200)
        self.setup=response.json
        response=post('/api/auth/mfa/confirm',{'enrollment_token':self.setup['enrollment_token'],'code':self.code()},csrf)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json['recovery_codes']),10)
        self.now+=timedelta(seconds=30)
        self.assertEqual(post('/api/auth/logout',{},csrf).status_code,200)
        response=post('/api/auth/login',self.credentials)
        self.assertTrue(response.json['mfa_required'])
        self.assertNotIn('access_token',response.json)
        self.assertIsNone(client.get_cookie('ss_session',domain='mfa.fixture.invalid'))
        response=post('/api/auth/mfa/challenge',{'challenge_token':response.json['challenge_token'],'code':self.code()})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['mfa_authenticated'])
        me=client.get('/api/me',base_url='https://mfa.fixture.invalid')
        self.assertTrue(me.json['mfa_enabled'] and me.json['mfa_authenticated'])

    def test_legacy_session_insert_before_enrollment_is_valid_then_revoked(self):
        legacy=self.legacy_insert()
        self.assertFalse(self.auth.resolve(legacy)['mfa_enabled'])
        self.enroll()
        self.reject(lambda:self.auth.resolve(legacy))
        self.assertTrue(self.auth.resolve(self.session_token)['mfa_authenticated'])

    def test_guard_rejects_legacy_insert_and_missing_or_wrong_session_proof(self):
        self.enroll()
        with self.assertRaises(psycopg.errors.CheckViolation):self.legacy_insert()
        for change in ('mfa_version=NULL','mfa_authenticated_at=NULL','mfa_version=999'):
            with self.subTest(change=change),self.assertRaises(psycopg.errors.CheckViolation):
                with self.database.transaction() as db:
                    db.execute('UPDATE ss_sessions SET '+change+' WHERE token_hash=%s',(digest(self.session_token),))
        verified,_=self.verify(self.challenge(),self.codes[0])
        self.assertTrue(self.auth.resolve(verified)['mfa_authenticated'])

    def test_legacy_insert_racing_active_enrollment_fails_closed_without_deadlock(self):
        self.setup_pending()
        reached,release=threading.Event(),threading.Event()
        original=self.auth.mfa._save_recovery
        def pause(*args):
            original(*args);reached.set()
            if not release.wait(10):raise AssertionError('Fixture enrollment release timed out')
        with ThreadPoolExecutor(max_workers=1) as workers,patch.object(self.auth.mfa,'_save_recovery',side_effect=pause):
            future=workers.submit(self.auth.mfa.confirm,self.actor,
                                  {'enrollment_token':self.setup['enrollment_token'],'code':self.code()},self.address)
            try:
                self.assertTrue(reached.wait(5),'Enrollment did not reach its locked transaction')
                with self.assertRaises(psycopg.errors.SerializationFailure):self.legacy_insert()
            finally:release.set()
            self.assertTrue(future.result(timeout=10)['enabled'])
        with self.assertRaises(psycopg.errors.CheckViolation):self.legacy_insert()

    def test_legacy_insert_committing_before_waiting_enrollment_cannot_survive_activation(self):
        self.setup_pending()
        started=threading.Event()
        def confirm():
            started.set()
            return self.auth.mfa.confirm(self.actor,{'enrollment_token':self.setup['enrollment_token'],'code':self.code()},self.address)
        with ThreadPoolExecutor(max_workers=1) as workers:
            with self.database.transaction() as db:
                legacy=self.legacy_insert(db)
                future=workers.submit(confirm)
                self.assertTrue(started.wait(5))
            self.assertTrue(future.result(timeout=10)['enabled'])
        self.reject(lambda:self.auth.resolve(legacy))

    def test_session_update_contention_fails_promptly_and_migration_reapply_preserves_guard(self):
        self.enroll()
        def update():
            with self.database.transaction() as db:
                db.execute('UPDATE ss_sessions SET expires_at=expires_at WHERE token_hash=%s',(digest(self.session_token),))
        with ThreadPoolExecutor(max_workers=1) as workers:
            with self.database.transaction() as db:
                db.execute('SELECT id FROM ss_staff WHERE tenant_id=%s AND id=%s FOR UPDATE',(self.tenant,self.staff))
                future=workers.submit(update)
                with self.assertRaises(psycopg.errors.SerializationFailure):future.result(timeout=5)
        self.database.migrate()
        self.assertTrue(self.auth.resolve(self.session_token)['mfa_authenticated'])
        with self.assertRaises(psycopg.errors.CheckViolation):self.legacy_insert()


if __name__ == '__main__':
    unittest.main()
