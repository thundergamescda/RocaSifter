"""DRAFT UNRUN: password recovery on real isolated PostgreSQL and Flask HTTP.

Intended Linux hosted CI only after separate review/admission. Actual Auth,
password hashing, MFA proof, session guards and transaction behavior remain in
place. Fault/ordering hooks wrap real operations; no required import, authority
result, cookie or encryption primitive is replaced. No provider HTTP is allowed.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
import threading
from unittest.mock import patch
import uuid

import psycopg
from psycopg import sql

from hosted.app import create_app
from hosted.auth import Auth
from hosted.database import Database, audit
from hosted.security import ServiceError, digest, password_hash, password_valid
from hosted.staff_password_recovery import INVALID, StaffPasswordRecovery
import test_hosted_credit_authority_postgres as fixture


class PostgreSQLStaffPasswordRecoveryAcceptance(fixture._CreditAuthorityFixture):
    origin = 'https://staff-recovery.fixture.invalid'
    new_password = 'fictional-replacement-password-only'

    def setUp(self):
        super().setUp()
        self.recovery = StaffPasswordRecovery(self.auth)

    def _issue(self, role='buyer', actor=None):
        return self.recovery.issue(self.actors['owner'] if actor is None else actor,
            self.accounts[role]['id'], {'staff_identity_confirmed': True}, self.address)

    def _redeem(self, token, role='buyer', **updates):
        return self.recovery.redeem({'recovery_token': token, 'email': self.accounts[role]['email'],
            'new_password': self.new_password} | updates, self.address)

    def _rows(self):
        with self.database.transaction() as db:
            return {table: db.execute('SELECT * FROM ' + table + ' WHERE tenant_id=%s ORDER BY ' + order,
                                     (self.tenant,)).fetchall()
                    for table, order in (('ss_staff', 'id'), ('ss_sessions', 'token_hash'),
                        ('ss_staff_mfa', 'staff_id'), ('ss_mfa_recovery_codes', 'staff_id,code_hash'),
                        ('ss_mfa_challenges', 'token_hash'), ('ss_staff_password_recoveries', 'token_hash'),
                        ('ss_audit', 'id'), ('ss_invites', 'token_hash'))}

    def _invalid(self, function):
        with self.assertRaises(ServiceError) as caught:
            function()
        self.assertTrue(caught.exception.status == 403 and str(caught.exception) == INVALID,
                        'Invalid recovery must have the same generic rejection.')

    def _http(self):
        app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': self.origin,
            'APP_ENCRYPTION_KEYS': self.key, 'BOOTSTRAP_TOKEN': ''}, database=self.database)
        return app, app.test_client()

    def _post(self, client, path, body, headers=None):
        response = client.post(path, base_url=self.origin, json=body,
            headers={'Origin': self.origin} | (headers or {}),
            environ_overrides={'REMOTE_ADDR': self.address})
        self.assertFalse('Set-Cookie' in response.headers, 'Recovery responses must not alter cookies.')
        return response

    def _http_owner(self, client):
        response = client.post('/api/auth/login', base_url=self.origin, headers={'Origin': self.origin},
            environ_overrides={'REMOTE_ADDR': self.address}, json={'store_slug': self.slug,
                'email': self.accounts['owner']['email'], 'password': self.password, 'mfa_supported': True})
        self.assertEqual(response.status_code, 200)
        challenge = response.get_json()
        self.assertTrue(challenge.get('mfa_required') is True and not challenge.get('access_token'),
                        'Actual owner login must first require its actual enrolled MFA.')
        response = client.post('/api/auth/mfa/challenge', base_url=self.origin, headers={'Origin': self.origin},
            environ_overrides={'REMOTE_ADDR': self.address}, json={'challenge_token': challenge['challenge_token'],
                'code': self.recovery_codes['owner'].pop()})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result.get('mfa_authenticated') is True, 'Actual MFA must issue the owner cookie.')
        return result['access_token'], result['csrf_token']

    def test_recovery_changes_password_once_revokes_target_sessions_and_preserves_owner_and_other_staff(self):
        self._sign_in('buyer'); first = self.session_tokens['buyer']
        self._sign_in('buyer'); second = self.session_tokens['buyer']
        self._sign_in('cashier')
        before = self._rows()
        issued = self._issue()
        self.assertTrue(set(issued) == {'recovery_token', 'expires_in', 'delivery'} and issued['expires_in'] == 900)
        token = issued['recovery_token']
        stored = self._rows()
        row = stored['ss_staff_password_recoveries'][0]
        self.assertTrue(row['token_hash'] == digest(token) and row['expires_at'] - row['created_at'] == timedelta(minutes=15),
                        'Recovery must persist only the token hash with exactly bounded15m lifetime.')
        self.assertTrue(token not in str(stored) and self.new_password not in str(stored),
                        'Raw token or new password appeared in persistent evidence.')
        result = self._redeem(token)
        self.assertTrue(result == {'password_changed': True, 'sign_in_required': True,
            'all_sessions_revoked': True, 'mfa_preserved': True})
        after = self._rows(); target = self.accounts['buyer']['id']
        self.assertTrue([r for r in after['ss_staff'] if r['id'] != target] ==
                        [r for r in before['ss_staff'] if r['id'] != target], 'Other account state changed.')
        self.assertTrue(after['ss_sessions'] == [r for r in before['ss_sessions'] if r['staff_id'] != target],
                        'Only all target sessions may be revoked; no session may be issued.')
        self.assertTrue(self.auth.resolve(self.session_tokens['owner'])['role'] == 'owner')
        for old in (first, second):
            with self.assertRaises(ServiceError): self.auth.resolve(old)
        self._invalid(lambda: self._redeem(token))
        current = next(r for r in after['ss_staff'] if r['id'] == target)
        self.assertTrue(password_valid(self.new_password, current['password_hash']) and
                        not password_valid(self.password, current['password_hash']), 'Only the new password must authenticate.')
        audits = [r for r in after['ss_audit'] if r['action'] == 'staff-password-recovered']
        self.assertTrue(len(audits) == 1 and audits[0]['staff_id'] == self.accounts['owner']['id'] and
                        audits[0]['entity_id'] == str(target) and token not in str(after) and self.new_password not in str(after),
                        'Exactly one issuer-attributed credential-free recovery audit is required.')

    def test_enrolled_target_keeps_factors_codes_and_requires_actual_mfa_after_reset(self):
        self._enroll_role('buyer')
        issued = self._issue()
        token, challenge = self.auth.login({'store_slug': self.slug, 'email': self.accounts['buyer']['email'],
            'password': self.password, 'mfa_supported': True}, self.address)
        self.assertIsNone(token)
        before = self._rows()
        self._redeem(issued['recovery_token'])
        after = self._rows()
        self.assertTrue(after['ss_staff_mfa'] == before['ss_staff_mfa'] and
                        after['ss_mfa_recovery_codes'] == before['ss_mfa_recovery_codes'], 'Existing factors/codes changed.')
        with self.assertRaises(ServiceError):
            self.auth.mfa.verify_challenge({'challenge_token': challenge['challenge_token'],
                'code': self.recovery_codes['buyer'].pop()}, self.address)
        token, challenge = self.auth.login({'store_slug': self.slug, 'email': self.accounts['buyer']['email'],
            'password': self.new_password, 'mfa_supported': True}, self.address)
        self.assertTrue(token is None and challenge.get('mfa_required') is True,
                        'Recovery must never bypass enrolled MFA or issue a signed-in session.')
        token, _ = self.auth.mfa.verify_challenge({'challenge_token': challenge['challenge_token'],
            'code': self.recovery_codes['buyer'].pop()}, self.address)
        self.assertTrue(self.auth.resolve(token)['mfa_authenticated'] is True)

    def test_pending_factor_enrollment_cannot_complete_after_password_recovery(self):
        self._sign_in('buyer')
        enrollment = self.auth.mfa.begin(self.actors['buyer'], {'password': self.password}, self.address)
        self._redeem(self._issue()['recovery_token'])
        with self.database.transaction() as db:
            state = db.execute('SELECT * FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',
                               (self.tenant, self.accounts['buyer']['id'])).fetchone()
        self.assertTrue(not state['enabled'] and state['pending_seed'] is None and state['pending_token_hash'] is None and
                        state['pending_session_hash'] is None and state['pending_password_fingerprint'] is None,
                        'Pending enrollment must be fully cancelled without enabling MFA.')
        self.assertTrue(isinstance(enrollment['enrollment_token'], str))

    def test_owner_self_other_owner_disabled_foreign_and_unknown_targets_are_rejected_without_changes(self):
        foreign = uuid.uuid4(); foreign_staff = uuid.uuid4(); other_owner = uuid.uuid4()
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional other shop')", (foreign, 'other-' + foreign.hex))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'other@example.invalid',%s,'buyer')",
                       (foreign_staff, foreign, self.saved_password_hash))
            db.execute("UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s", (self.tenant, self.accounts['cashier']['id']))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'other-owner@example.invalid',%s,'owner')",
                       (other_owner, self.tenant, self.saved_password_hash))
        before = self._rows()
        for target in (self.accounts['owner']['id'], other_owner, self.accounts['cashier']['id'], foreign_staff, uuid.uuid4()):
            with self.subTest(target_kind=str(target) == str(foreign_staff)):
                self._invalid(lambda: self.recovery.issue(self.actors['owner'], target,
                    {'staff_identity_confirmed': True}, self.address))
        self.assertTrue(self._rows() == before, 'Invalid target changed account, audit or recovery state.')

    def test_manager_forged_role_missing_session_and_aged_owner_cannot_issue(self):
        manager = self._enroll_role('manager')
        actors = [manager, manager | {'role': 'owner'}, self.actors['owner'] | {'session_hash': 'f' * 64}]
        for actor in actors:
            with self.assertRaises(ServiceError): self._issue(actor=actor)
        self._set_proof_time("clock_timestamp()-interval '16 minutes'")
        before = self._rows()
        with self.assertRaises(ServiceError): self._issue()
        self.assertTrue(self._rows() == before, 'Rejected issuer changed recovery/account state.')

    def test_unknown_expired_used_wrong_email_and_superseded_tokens_have_generic_denial(self):
        first = self._issue()['recovery_token']; second = self._issue()['recovery_token']
        before = self._rows()
        for token in ('x' * 43, first): self._invalid(lambda: self._redeem(token))
        self._invalid(lambda: self._redeem(second, email='unknown@example.invalid'))
        self.assertTrue(self._rows() == before)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_staff_password_recoveries SET created_at=statement_timestamp()-interval '16 minutes',expires_at=statement_timestamp()-interval '1 minute' WHERE token_hash=%s", (digest(second),))
        before = self._rows()
        self._invalid(lambda: self._redeem(second))
        self.assertTrue(self._rows() == before)

    def test_owner_logout_after_issue_cancels_token_without_touching_target(self):
        token = self._issue()['recovery_token']
        self.auth.logout(self.session_tokens['owner'])
        before = self._rows()
        self._invalid(lambda: self._redeem(token))
        self.assertTrue(self._rows() == before)

    def test_security_and_authority_changes_during_hash_deny_and_preserve_the_newer_state(self):
        for kind in ('target-password', 'target-role', 'target-disabled', 'owner-proof', 'owner-logout', 'superseded', 'token-expiry'):
            if kind != 'target-password':
                # Restore only explicit fictional facts, and perform a fresh real
                # owner sign-in. No target factor/session or guard is fabricated.
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_staff SET enabled=TRUE,role='buyer',password_hash=%s WHERE tenant_id=%s AND id=%s",
                               (self.saved_password_hash, self.tenant, self.accounts['buyer']['id']))
                self._sign_in('owner')
            token = self._issue()['recovery_token']; changed = []
            def hash_after_change(password):
                if kind == 'owner-proof': self._set_proof_time("clock_timestamp()-interval '16 minutes'")
                elif kind == 'owner-logout': self.auth.logout(self.session_tokens['owner'])
                elif kind == 'superseded': self._issue()
                elif kind == 'token-expiry':
                    with self.database.transaction() as db:
                        db.execute("UPDATE ss_staff_password_recoveries SET created_at=statement_timestamp()-interval '16 minutes',expires_at=statement_timestamp()-interval '1 minute' WHERE token_hash=%s", (digest(token),))
                else:
                    assignment, value = {'target-password': ('password_hash', password_hash('a-different-fictional-password')),
                        'target-role': ('role', 'manager'), 'target-disabled': ('enabled', False)}[kind]
                    with self.database.transaction() as db:
                        db.execute('UPDATE ss_staff SET ' + assignment + '=%s WHERE tenant_id=%s AND id=%s',
                                   (value, self.tenant, self.accounts['buyer']['id']))
                changed.append(self._rows())
                return password_hash(password)
            with self.subTest(change=kind), patch('hosted.staff_password_recovery.password_hash', side_effect=hash_after_change):
                self._invalid(lambda: self._redeem(token))
            self.assertTrue(len(changed) == 1 and self._rows() == changed[0], 'Failed recovery overwrote newer state.')

    def test_target_factor_enrollment_and_owner_factor_version_changes_invalidate_issued_tokens(self):
        for kind in ('target-enrollment', 'owner-version'):
            token = self._issue()['recovery_token']; changed = []
            def hash_after_factor_change(password):
                if kind == 'target-enrollment': self._enroll_role('buyer')
                else:
                    with self.database.transaction() as db:
                        db.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',
                                   (self.tenant, self.accounts['owner']['id']))
                changed.append(self._rows())
                return password_hash(password)
            with self.subTest(change=kind), patch('hosted.staff_password_recovery.password_hash', side_effect=hash_after_factor_change):
                self._invalid(lambda: self._redeem(token))
            self.assertTrue(len(changed) == 1 and self._rows() == changed[0])

    def test_two_concurrent_redemptions_hash_outside_locks_and_only_one_can_commit(self):
        token = self._issue()['recovery_token']; rendezvous = threading.Barrier(2)
        def synchronized_hash(password):
            rendezvous.wait(timeout=20)
            return password_hash(password)
        def redeem():
            try: self._redeem(token); return 'changed'
            except ServiceError as error:
                if error.status == 403 and str(error) == INVALID: return 'rejected'
                raise
        with patch('hosted.staff_password_recovery.password_hash', side_effect=synchronized_hash), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(redeem) for _ in range(2)]
            results = [future.result(timeout=30) for future in futures]
        self.assertTrue(sorted(results) == ['changed', 'rejected'])
        self.assertTrue(len([r for r in self._rows()['ss_audit'] if r['action'] == 'staff-password-recovered']) == 1)

    def test_audit_failure_rolls_back_password_revocations_and_consumption_but_not_budget(self):
        self._sign_in('buyer'); token = self._issue()['recovery_token']; before = self._rows()
        with patch('hosted.staff_password_recovery.audit', side_effect=RuntimeError('fictional audit outage')):
            with self.assertRaises(RuntimeError): self._redeem(token)
        self.assertTrue(self._rows() == before, 'Audit failure must roll back the entire account mutation.')
        self._redeem(token)
        with self.database.transaction() as db:
            key = digest('invite-accept:staff-password-recovery:' + self.address)
            counter = db.execute('SELECT failures FROM ss_login_attempts WHERE key_hash=%s', (key,)).fetchone()
        self.assertTrue(counter['failures'] == 2, 'Rollback must not refund the persistent redemption budget.')

    def test_owner_proof_and_token_expiry_are_rechecked_after_audit_and_roll_back_every_mutation(self):
        for kind in ('owner-proof', 'token-expiry'):
            token = self._issue()['recovery_token']; before = self._rows()
            audit_entered = []
            def hash_then_prepare_deadline(password):
                replacement = password_hash(password)
                # Prepare a real short deadline before final validation captures
                # and locks the row. Hashing time cannot consume this headroom.
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_staff_password_recoveries SET expires_at=statement_timestamp()+interval '5 seconds' WHERE token_hash=%s",
                               (digest(token),))
                before.clear(); before.update(self._rows())
                return replacement
            def audit_then_expire(db, tenant, staff, action, entity, details=None):
                audit(db, tenant, staff, action, entity, details)
                audit_entered.append(True)
                if kind == 'owner-proof':
                    db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",
                               (self.actors['owner']['session_hash'],))
                else:
                    # Let the captured expiry really pass; never change the
                    # locked expiry or assert against a different row model.
                    db.execute('''SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM
                        (expires_at-clock_timestamp())))+0.05) FROM ss_staff_password_recoveries
                        WHERE token_hash=%s''', (digest(token),))
            hash_operation = hash_then_prepare_deadline if kind == 'token-expiry' else password_hash
            with self.subTest(expiry=kind), patch('hosted.staff_password_recovery.password_hash', side_effect=hash_operation), patch('hosted.staff_password_recovery.audit', side_effect=audit_then_expire):
                self._invalid(lambda: self._redeem(token))
            self.assertTrue(audit_entered == [True], 'The test must reach audit before the final expiry/authority recheck.')
            self.assertTrue(self._rows() == before, 'Late expiry must roll back password, revocations, token consumption and audit.')

    def test_issuance_and_unknown_redemption_have_persistent_separate_budgets(self):
        for _ in range(10): self._issue()
        before = self._rows()
        with self.assertRaises(ServiceError) as caught: self._issue()
        self.assertEqual(caught.exception.status, 429)
        self.assertTrue(self._rows() == before)
        for _ in range(30): self._invalid(lambda: self._redeem('x' * 43))
        with self.assertRaises(ServiceError) as caught: self._redeem('x' * 43)
        self.assertEqual(caught.exception.status, 429)
        self.assertTrue(self._rows() == before, 'Rate denial must not issue/consume a recovery or alter account data.')

    def test_http_actual_owner_csrf_signed_out_redemption_and_cookie_preservation(self):
        _, client = self._http(); owner_token, csrf = self._http_owner(client)
        path = '/api/staff/' + str(self.accounts['buyer']['id']) + '/password-recovery'
        denied = self._post(client, path, {'staff_identity_confirmed': True})
        self.assertEqual(denied.status_code, 403)
        issued = self._post(client, path, {'staff_identity_confirmed': True}, {'X-SS-CSRF': csrf})
        self.assertEqual(issued.status_code, 201)
        token = issued.get_json()['recovery_token']
        payload = {'recovery_token': token, 'email': self.accounts['buyer']['email'], 'new_password': self.new_password}
        before = self._rows()
        blocked = self._post(client, '/api/staff/password-recovery/redeem', payload)
        self.assertEqual(blocked.status_code, 409)
        self.assertTrue(self._rows() == before and self.auth.resolve(owner_token)['role'] == 'owner',
                        'Opening/redeeming an owner link must preserve the current owner session and token.')
        _, separate = self._http()
        result = self._post(separate, '/api/staff/password-recovery/redeem', payload)
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json().get('mfa_preserved') is True)
        replay = self._post(separate, '/api/staff/password-recovery/redeem', payload)
        self.assertTrue(replay.status_code == 403 and replay.get_json() == {'error': INVALID})
        self.assertTrue(self.auth.resolve(owner_token)['role'] == 'owner')
        page = client.get('/staff-password-recovery', base_url=self.origin)
        self.assertEqual(page.status_code, 200)
        self.assertFalse('Set-Cookie' in page.headers)
        self.assertTrue(page.headers.get('Cache-Control') == 'no-store' and page.headers.get('Referrer-Policy') == 'no-referrer')

    def test_http_cross_origin_tenant_override_malformed_payload_and_bearer_are_rejected(self):
        _, client = self._http(); token = self._issue()['recovery_token']
        payload = {'recovery_token': token, 'email': self.accounts['buyer']['email'], 'new_password': self.new_password}
        cases = [(payload, {'Origin': 'https://other.invalid'}, 403),
                 (payload | {'tenant_id': str(self.tenant)}, {}, 400),
                 (payload | {'role': 'owner'}, {}, 400),
                 (payload, {'Authorization': 'Bearer ' + self.session_tokens['owner']}, 409),
                 (payload | {'new_password': 'a' * 5000}, {}, 413)]
        before = self._rows()
        for body, headers, status in cases:
            response = self._post(client, '/api/staff/password-recovery/redeem', body, headers)
            self.assertEqual(response.status_code, status)
        self.assertTrue(self._rows() == before)

    def test_http_revoked_cookie_allows_recovery_without_clearing_or_replacing_it(self):
        _, client = self._http()
        response = client.post('/api/auth/login', base_url=self.origin, headers={'Origin': self.origin},
            environ_overrides={'REMOTE_ADDR': self.address}, json={'store_slug': self.slug,
                'email': self.accounts['buyer']['email'], 'password': self.password, 'mfa_supported': True})
        self.assertEqual(response.status_code, 200)
        old = response.get_json()['access_token']; self.auth.logout(old)
        self.assertEqual(client.get('/api/me', base_url=self.origin).status_code, 401)
        token = self._issue()['recovery_token']
        response = self._post(client, '/api/staff/password-recovery/redeem',
            {'recovery_token': token, 'email': self.accounts['buyer']['email'], 'new_password': self.new_password})
        self.assertEqual(response.status_code, 200)
        cookie = client.get_cookie('ss_session', domain='staff-recovery.fixture.invalid')
        self.assertTrue(cookie is not None and cookie.value == old, 'Recovery must leave even a stale browser cookie untouched.')
        self.assertTrue(self.auth.resolve(self.session_tokens['owner'])['role'] == 'owner')

    def test_migration_from_real_v14_is_additive_and_repeated_migration_preserves_existing_accounts(self):
        # Separate owned fixture schema starts with the exact pre-v15 SQL. Its
        # real owner, session and invitation must survive the upgrade unchanged.
        schema = 'ss_test_' + uuid.uuid4().hex; database = None
        with psycopg.connect(self.url, sslmode='require') as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            database = Database(self.url, schema=schema)
            old_sql = (Path(__file__).parent / 'hosted' / 'schema.sql').read_text().split('-- Version 15:')[0]
            with database.transaction() as db: db.execute(old_sql, prepare=False)
            self.assertFalse(database.ready(), 'v14 must not report recovery-schema readiness.')
            tenant, owner = uuid.uuid4(), uuid.uuid4(); slug = 'legacy-' + tenant.hex
            with database.transaction() as db:
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional legacy shop')", (tenant, slug))
                db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'legacy@example.invalid',%s,'owner')",
                           (owner, tenant, self.saved_password_hash))
                db.execute("INSERT INTO ss_invites(token_hash,tenant_id,email,role,created_by,expires_at) VALUES (%s,%s,'pending@example.invalid','buyer',%s,now()+interval '24 hours')",
                           (digest('fictional-old-invitation'), tenant, owner))
            auth = Auth(database, encryption_keys=self.key)
            token, _ = auth.login({'store_slug': slug, 'email': 'legacy@example.invalid', 'password': self.password}, self.address)
            def legacy_rows():
                with database.transaction() as db:
                    return [db.execute('SELECT * FROM ' + table + ' ORDER BY ' + ordering).fetchall()
                            for table, ordering in (('ss_tenants','id'), ('ss_staff','id'), ('ss_sessions','token_hash'),
                                ('ss_invites','token_hash'), ('ss_audit','id'), ('ss_staff_mfa','staff_id'))]
            before = legacy_rows()
            database.migrate(); database.migrate()
            self.assertTrue(database.ready() and legacy_rows() == before and auth.resolve(token)['role'] == 'owner',
                            'Additive migration must preserve prior identities, sessions, invites and audit.')
            with database.transaction() as db:
                versions = [row['version'] for row in db.execute('SELECT version FROM ss_schema_versions ORDER BY version').fetchall()]
                count = db.execute('SELECT count(*) AS n FROM ss_staff_password_recoveries').fetchone()['n']
            self.assertTrue(versions == list(range(1, 16)) and count == 0, 'Migration must preserve all versions and create no recovery credentials.')
        finally:
            if database is not None: database.close()
            with psycopg.connect(self.url, sslmode='require') as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
