"""Manual-credit authority on real disposable PostgreSQL; draft UNRUN.

Real Auth/password/MFA enrollment supplies positive sessions. Negative clock and
security mutations are explicit fictional fixture data; no trigger is disabled.
There is no app TESTING exception, HTTP/provider call, checkout activation or
production-schema dependency. The reusable fixture inherits no test methods.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import threading
from unittest.mock import patch
import uuid

from cryptography.fernet import Fernet

from hosted.auth import Auth
from hosted.credit_authority import CreditAuthorityError, require_credit_authority
from hosted.database import audit
from hosted.ledger import Ledger
from hosted.mfa import secret_bytes, totp
from hosted.security import ServiceError, password_hash
import test_hosted_settlement_postgres as sql_fixture


class _CreditAuthorityFixture(sql_fixture._PostgreSQLSettlementFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.password = 'fictional-credit-authority-password'
            cls.saved_password_hash = password_hash(cls.password)
        except Exception:
            super().tearDownClass()
            raise

    def setUp(self):
        self.tenant, self.customer = uuid.uuid4(), uuid.uuid4()
        self.slug = 'credit-authority-' + self.tenant.hex
        self.address = 'fictional-credit-address-' + self.tenant.hex
        self.key = Fernet.generate_key().decode()
        self.mfa_now = datetime.now(timezone.utc)
        self.auth = Auth(self.database, encryption_keys=self.key)
        self.auth.mfa.clock = lambda: self.mfa_now
        self.ledger = Ledger(self.database)
        self.accounts = {role: {'id': uuid.uuid4(), 'email': role + '@example.invalid'}
                         for role in ('owner', 'manager', 'buyer', 'cashier')}
        self.actors, self.session_tokens, self.recovery_codes = {}, {}, {}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional credit-authority store')",
                       (self.tenant, self.slug))
            for role, account in self.accounts.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',
                           (account['id'], self.tenant, account['email'], self.saved_password_hash, role))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Fictional credit customer','fixture-only')",
                       (self.customer, self.tenant))
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Provider HTTP forbidden in credit-authority fixtures'))
        guard.start()
        self.addCleanup(guard.stop)
        self._sign_in('owner')
        self._enroll_role('owner')

    def _sign_in(self, role):
        token, info = self.auth.login({'store_slug': self.slug, 'email': self.accounts[role]['email'],
                                      'password': self.password, 'mfa_supported': True}, self.address)
        if info.get('mfa_required'):
            self.assertIsNone(token)
            token, info = self.auth.mfa.verify_challenge({'challenge_token': info['challenge_token'],
                'code': self.recovery_codes[role].pop()}, self.address)
        self.assertIsInstance(token, str)
        self.session_tokens[role] = token
        self.actors[role] = self.auth.resolve(token)
        return self.actors[role]

    def _enroll_role(self, role):
        if role not in self.actors:
            self._sign_in(role)
        setup = self.auth.mfa.begin(self.actors[role], {'password': self.password}, self.address)
        result = self.auth.mfa.confirm(self.actors[role], {'enrollment_token': setup['enrollment_token'],
            'code': totp(secret_bytes(setup['secret']), self.mfa_now.timestamp())}, self.address)
        self.recovery_codes[role] = list(result['recovery_codes'])
        self.actors[role] = self.auth.resolve(self.session_tokens[role])
        self.assertTrue(self.actors[role]['mfa_authenticated'])
        return self.actors[role]

    def _body(self, key='correction', *, kind='adjust_add', amount=100, version=0):
        return {'request_key': key, 'kind': kind, 'amount_cents': amount, 'version': version,
                'reason': 'Fictional manual correction', 'reference': 'FICTIONAL-CREDIT'}

    def _financial_rows(self):
        with self.database.transaction() as db:
            rows = {table: db.execute('SELECT * FROM ' + table + ' WHERE tenant_id=%s ORDER BY ' + ordering,
                                     (self.tenant,)).fetchall()
                    for table, ordering in (('ss_customers', 'id'), ('ss_credit_entries', 'id'),
                        ('ss_credit_lots', 'id'), ('ss_credit_lot_mutations', 'entry_id,lot_id'),
                        ('ss_reservations', 'id'), ('ss_requests', 'key'))}
            rows['audits'] = db.execute("SELECT * FROM ss_audit WHERE tenant_id=%s AND action='credit-adjusted' ORDER BY id",
                                       (self.tenant,)).fetchall()
        return rows

    def _reject(self, actor, body, *, status=403, code='MFA_STEP_UP_REQUIRED'):
        with self.assertRaises(CreditAuthorityError) as caught:
            self.ledger.adjust(actor, self.customer, body)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        self.assertIs(caught.exception.sign_in_required, True)

    def _set_proof_time(self, expression):
        # Expressions are fixed test literals, never request data. Keep the real
        # session trigger, identity and current MFA version in place.
        with self.database.transaction() as db:
            db.execute('UPDATE ss_sessions SET mfa_authenticated_at=' + expression + ' WHERE token_hash=%s',
                       (self.actors['owner']['session_hash'],))


class PostgreSQLCreditAuthorityAcceptance(_CreditAuthorityFixture):
    def test_real_auth_mfa_owner_and_manager_preserve_ledger_and_lot_semantics(self):
        owner = self.actors['owner']
        opening = self.ledger.adjust(owner, self.customer, self._body('opening', kind='opening', amount=1000))
        added = self.ledger.adjust(owner, self.customer, self._body('add', amount=100, version=1))
        removed = self.ledger.adjust(owner, self.customer, self._body('remove', kind='adjust_remove', amount=200, version=2))
        self.assertEqual((opening['balance_cents'], added['balance_cents'], removed['balance_cents']), (1000, 1100, 900))
        self.assertEqual(set(removed), {'entry_id', 'balance_cents', 'version'})
        rows = self._financial_rows()
        self.assertEqual((len(rows['ss_credit_entries']), len(rows['ss_requests']), len(rows['audits'])), (3, 3, 3))
        self.assertEqual(sum(row['amount_cents'] for row in rows['ss_credit_entries']), 900)
        self.assertEqual(sum(row['remaining_cents'] for row in rows['ss_credit_lots']), 900)
        self.assertTrue(all(row['origin_code'] == 'unknown' and row['policy_id'] is None for row in rows['ss_credit_lots']))
        manager = self._enroll_role('manager')
        self.assertEqual(self.ledger.adjust(manager, self.customer, self._body('manager-add', amount=50, version=3))['balance_cents'], 950)
        with self.database.transaction() as db:
            self.assertFalse(db.execute('SELECT credit_checkout_enabled FROM ss_tenants WHERE id=%s', (self.tenant,)).fetchone()['credit_checkout_enabled'])
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_square_connections WHERE tenant_id=%s', (self.tenant,)).fetchone()['n'], 0)

    def test_role_staff_tenant_and_session_bindings_are_checked_from_database(self):
        owner = self.actors['owner']
        manager = self._sign_in('manager')
        buyer = self._sign_in('buyer')
        cashier = self._sign_in('cashier')
        before = self._financial_rows()
        cases = ((buyer, 403, 'CREDIT_AUTHORITY_CHANGED'), (cashier, 403, 'CREDIT_AUTHORITY_CHANGED'),
                 (manager | {'role': 'owner'}, 403, 'CREDIT_AUTHORITY_CHANGED'),
                 (owner | {'tenant_id': uuid.uuid4()}, 403, 'CREDIT_AUTHORITY_CHANGED'),
                 (owner | {'staff_id': self.accounts['manager']['id']}, 403, 'CREDIT_AUTHORITY_CHANGED'),
                 (owner | {'session_hash': manager['session_hash']}, 401, 'CREDIT_SESSION_CHANGED'),
                 (owner | {'session_hash': 'f' * 64}, 401, 'CREDIT_SESSION_CHANGED'),
                 (owner | {'session_hash': None}, 401, 'CREDIT_SESSION_CHANGED'))
        for index, (actor, status, code) in enumerate(cases):
            with self.subTest(case=index):
                self._reject(actor, self._body('identity-' + str(index)), status=status, code=code)
        self.assertEqual(self._financial_rows(), before)

    def test_unenrolled_actor_cannot_authorize_credit_with_forged_mfa_fields(self):
        manager = self._sign_in('manager')
        before = self._financial_rows()
        forged = manager | {'mfa_enabled': True, 'mfa_authenticated': True, 'mfa_fresh': True,
                            'mfa_authenticated_at': datetime.now(timezone.utc)}
        for kind in ('opening', 'adjust_add', 'adjust_remove'):
            with self.subTest(kind=kind):
                self._reject(forged, self._body('unenrolled-' + kind, kind=kind))
        self.assertEqual(self._financial_rows(), before)

    def test_stale_future_and_changed_mfa_proof_reject_every_new_kind(self):
        actor = self.actors['owner'] | {'mfa_fresh': True}
        before = self._financial_rows()
        for label, expression in (('stale', "clock_timestamp()-interval '16 minutes'"),
                                  ('future', "clock_timestamp()+interval '1 hour'")):
            self._set_proof_time(expression)
            for kind in ('opening', 'adjust_add', 'adjust_remove'):
                with self.subTest(proof=label, kind=kind):
                    self._reject(actor, self._body(label + '-' + kind, kind=kind))
        with self.database.transaction() as db:
            db.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',
                       (self.tenant, self.accounts['owner']['id']))
        for kind in ('opening', 'adjust_add', 'adjust_remove'):
            with self.subTest(proof='changed-version', kind=kind):
                self._reject(actor, self._body('changed-' + kind, kind=kind), status=401, code='CREDIT_SESSION_CHANGED')
        self.assertEqual(self._financial_rows(), before)

    def test_exact_committed_replay_needs_live_session_but_not_new_mfa_freshness(self):
        body = self._body('original-opening', kind='opening', amount=1000)
        result = self.ledger.adjust(self.actors['owner'], self.customer, body)
        before = self._financial_rows()
        self._set_proof_time("clock_timestamp()-interval '20 minutes'")
        self.assertEqual(self.ledger.adjust(self.actors['owner'], self.customer, body | {'version': 999}), result)
        self._reject(self.actors['owner'], self._body('new-aged', version=1))
        self.auth.mfa.change(self.actors['owner'], {'password': self.password,
            'code': self.recovery_codes['owner'].pop()}, self.address, disable=True)
        self._sign_in('owner')
        self.assertEqual(self.ledger.adjust(self.actors['owner'], self.customer, body), result)
        self._reject(self.actors['owner'], self._body('new-unenrolled', version=1))
        self.assertEqual(self._financial_rows(), before)
        original_hash = hashlib.sha256(json.dumps({'customer': str(self.customer), 'amount': 1000,
            'kind': 'opening', 'reason': body['reason'], 'reference': body['reference']},
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(before['ss_requests'][0]['body_hash'], original_hash)
        self.assertEqual(before['ss_requests'][0]['result'], result)
        self.assertEqual(before['ss_requests'][0]['operation'], 'ledger')

    def test_revoked_expired_and_changed_authority_cannot_replay_a_committed_result(self):
        body = self._body('saved')
        self.ledger.adjust(self.actors['owner'], self.customer, body)
        before = self._financial_rows()
        captured = self.actors['owner']
        self.auth.logout(self.session_tokens['owner'])
        self._reject(captured, body, status=401, code='CREDIT_SESSION_CHANGED')
        self._sign_in('owner')
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",
                       (self.actors['owner']['session_hash'],))
        self._reject(self.actors['owner'], body, status=401, code='CREDIT_SESSION_CHANGED')
        self._reject(self.actors['owner'], self._body('expired-new', version=1), status=401, code='CREDIT_SESSION_CHANGED')
        self._sign_in('owner')
        for label, change in (('disabled', 'enabled=FALSE'), ('changed-role', "role='buyer'")):
            with self.subTest(change=label):
                with self.database.transaction() as db:
                    db.execute('UPDATE ss_staff SET ' + change + ' WHERE tenant_id=%s AND id=%s',
                               (self.tenant, self.accounts['owner']['id']))
                self._reject(self.actors['owner'], body, code='CREDIT_AUTHORITY_CHANGED')
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_staff SET enabled=TRUE,role='owner' WHERE tenant_id=%s AND id=%s",
                               (self.tenant, self.accounts['owner']['id']))
        with self.database.transaction() as db:
            db.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',
                       (self.tenant, self.accounts['owner']['id']))
        self._reject(self.actors['owner'], body, status=401, code='CREDIT_SESSION_CHANGED')
        self.assertEqual(self._financial_rows(), before)

    def test_concurrent_same_key_has_one_entry_and_identical_results(self):
        barrier = threading.Barrier(2)
        body = self._body('simultaneous')
        def submit():
            barrier.wait(timeout=5)
            return self.ledger.adjust(self.actors['owner'], self.customer, body)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(results[0], results[1])
        rows = self._financial_rows()
        self.assertEqual((rows['ss_customers'][0]['balance_cents'], len(rows['ss_credit_entries']),
                          len(rows['ss_requests']), len(rows['audits'])), (100, 1, 1, 1))

    def test_changed_body_stale_version_and_second_opening_preserve_financial_state(self):
        body = self._body('fixed-opening', kind='opening', amount=1000)
        self.ledger.adjust(self.actors['owner'], self.customer, body)
        before = self._financial_rows()
        for invalid in (body | {'amount_cents': 1001}, self._body('stale-version', version=0),
                        self._body('second-opening', kind='opening', version=1)):
            with self.subTest(key=invalid['request_key']), self.assertRaises(ServiceError):
                self.ledger.adjust(self.actors['owner'], self.customer, invalid)
        self.assertEqual(self._financial_rows(), before)

    def test_revocation_committed_during_request_key_wait_denies_new_write(self):
        self._enroll_role('manager')
        original_begin = self.ledger._begin_request
        for label in ('logout', 'disable', 'demote'):
            with self.subTest(revocation=label):
                captured = self.actors['manager']
                body = self._body('key-wait-' + label)
                before = self._financial_rows()
                waiting = threading.Event()
                def begin(*args, **kwargs):
                    waiting.set()
                    return original_begin(*args, **kwargs)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    with self.database.transaction() as blocker:
                        blocker.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                                        (str(self.tenant) + ':' + body['request_key'],))
                        with patch.object(self.ledger, '_begin_request', side_effect=begin):
                            future = pool.submit(self.ledger.adjust, captured, self.customer, body)
                            self.assertTrue(waiting.wait(5))
                            if label == 'logout':
                                self.auth.logout(self.session_tokens['manager'])
                            elif label == 'disable':
                                self.auth.disable_staff(self.actors['owner'], self.accounts['manager']['id'])
                            else:
                                with self.database.transaction() as db:
                                    db.execute("UPDATE ss_staff SET role='buyer' WHERE tenant_id=%s AND id=%s",
                                               (self.tenant, self.accounts['manager']['id']))
                    with self.assertRaises(CreditAuthorityError):
                        future.result(timeout=10)
                self.assertEqual(self._financial_rows(), before)
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_staff SET enabled=TRUE,role='manager' WHERE tenant_id=%s AND id=%s",
                               (self.tenant, self.accounts['manager']['id']))
                self._sign_in('manager')

    def test_authorized_write_linearizes_before_waiting_session_revocation(self):
        locked, release, revoking, revoked = (threading.Event() for _ in range(4))
        calls = []
        def checked(*args, **kwargs):
            result = require_credit_authority(*args, **kwargs)
            calls.append(kwargs.get('fresh_mfa', True))
            if len(calls) == 1:
                locked.set()
                if not release.wait(5):
                    raise RuntimeError('Fictional credit authority barrier expired.')
            return result
        def revoke():
            revoking.set()
            self.auth.logout(self.session_tokens['owner'])
            revoked.set()
        with ThreadPoolExecutor(max_workers=2) as pool, patch('hosted.ledger.require_credit_authority', side_effect=checked):
            write = pool.submit(self.ledger.adjust, self.actors['owner'], self.customer, self._body('wins-before-revoke'))
            try:
                self.assertTrue(locked.wait(5))
                revoke_future = pool.submit(revoke)
                self.assertTrue(revoking.wait(5))
                self.assertFalse(revoked.wait(.1), 'Revocation bypassed the actual session SHARE lock.')
            finally:
                release.set()
            self.assertEqual(write.result(timeout=10)['balance_cents'], 100)
            revoke_future.result(timeout=10)
        self.assertEqual(calls, [True, True])
        self.assertTrue(revoked.is_set())
        self._reject(self.actors['owner'], self._body('after-revoke', version=1), status=401, code='CREDIT_SESSION_CHANGED')
        self.assertEqual(len(self._financial_rows()['ss_credit_entries']), 1)

    def test_session_expiry_while_waiting_for_customer_rolls_back_financial_and_audit_rows(self):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()+interval '5 seconds' WHERE token_hash=%s",
                       (self.actors['owner']['session_hash'],))
        before = self._financial_rows()
        authorized, audited = threading.Event(), threading.Event()
        def checked(*args, **kwargs):
            result = require_credit_authority(*args, **kwargs)
            authorized.set()
            return result
        def recorded(*args, **kwargs):
            audit(*args, **kwargs)
            audited.set()
        with ThreadPoolExecutor(max_workers=1) as pool, patch('hosted.ledger.require_credit_authority', side_effect=checked), patch('hosted.ledger.audit', side_effect=recorded):
            with self.database.transaction() as blocker:
                blocker.execute('SELECT id FROM ss_customers WHERE tenant_id=%s AND id=%s FOR UPDATE',
                                (self.tenant, self.customer))
                future = pool.submit(self.ledger.adjust, self.actors['owner'], self.customer, self._body('expires-in-customer-wait'))
                self.assertTrue(authorized.wait(5))
                blocker.execute('''SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM (expires_at-clock_timestamp())))+0.05)
                                   FROM ss_sessions WHERE token_hash=%s''', (self.actors['owner']['session_hash'],))
                self.assertTrue(blocker.execute('SELECT expires_at<=clock_timestamp() AS expired FROM ss_sessions WHERE token_hash=%s',
                                               (self.actors['owner']['session_hash'],)).fetchone()['expired'])
            with self.assertRaises(CreditAuthorityError) as caught:
                future.result(timeout=10)
        self.assertEqual((caught.exception.status, caught.exception.code), (401, 'CREDIT_SESSION_CHANGED'))
        self.assertTrue(audited.is_set(), 'The final check must reject after the attempted financial/audit work.')
        self.assertEqual(self._financial_rows(), before)

    def test_mfa_age_crossing_limit_during_audit_rolls_back_and_retry_requires_new_proof(self):
        self._set_proof_time("clock_timestamp()-interval '895 seconds'")
        before = self._financial_rows()
        def age_after_audit(db, *args, **kwargs):
            audit(db, *args, **kwargs)
            db.execute('''SELECT pg_sleep(GREATEST(0,900-EXTRACT(EPOCH FROM (clock_timestamp()-mfa_authenticated_at)))+0.05)
                          FROM ss_sessions WHERE token_hash=%s''', (self.actors['owner']['session_hash'],))
        body = self._body('ages-during-audit')
        with patch('hosted.ledger.audit', side_effect=age_after_audit):
            self._reject(self.actors['owner'], body)
        self.assertEqual(self._financial_rows(), before)
        self._sign_in('owner')
        self.assertEqual(self.ledger.adjust(self.actors['owner'], self.customer, body)['balance_cents'], 100)

    def test_audit_failure_rolls_back_then_same_key_can_commit_without_duplicate_credit(self):
        body = self._body('audit-rollback')
        before = self._financial_rows()
        with patch('hosted.ledger.audit', side_effect=RuntimeError('Fictional audit failure')):
            with self.assertRaises(RuntimeError):
                self.ledger.adjust(self.actors['owner'], self.customer, body)
        self.assertEqual(self._financial_rows(), before)
        result = self.ledger.adjust(self.actors['owner'], self.customer, body)
        self.assertEqual(self.ledger.adjust(self.actors['owner'], self.customer, body), result)
        self.assertEqual(len(self._financial_rows()['ss_credit_entries']), 1)
