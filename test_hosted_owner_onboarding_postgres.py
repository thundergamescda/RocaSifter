"""Independent owner enrollment transactions in disposable PostgreSQL only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import time
import uuid
import unittest
from pathlib import Path
import hashlib
from unittest.mock import patch
import psycopg
from psycopg import sql
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.auth import Auth
from hosted.database import Database
from hosted.owner_onboarding import OwnerOnboarding,EnrollmentError
from hosted.security import ServiceError,password_hash
import test_hosted_settlement_postgres as postgres_fixture
from test_owner_enrollment_fixtures import operator_fixture,issue_fixture,accept_fixture


class PostgreSQLOwnerOnboarding(postgres_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        self.service=OwnerOnboarding(self.database);self.operator=operator_fixture(self.database);self.address='fixture-'+uuid.uuid4().hex
        no_http=patch('requests.sessions.Session.request',side_effect=AssertionError('External HTTP forbidden in owner fixtures.'))
        no_http.start();self.addCleanup(no_http.stop)

    def counts(self):
        with self.database.transaction() as db:
            return {name:db.execute('SELECT count(*) AS n FROM '+name).fetchone()['n'] for name in
                ('ss_tenants','ss_staff','ss_sessions','ss_customers','ss_credit_entries','ss_credit_lots','ss_square_connections','ss_receiving_settings','ss_owner_enrollments')}

    def test_independent_second_and_third_owners_enroll_without_reusing_global_bootstrap(self):
        auth=Auth(self.database,'fictional-initial-bootstrap',encryption_keys=Fernet.generate_key().decode())
        suffix=uuid.uuid4().hex
        auth.bootstrap({'bootstrap_token':'fictional-initial-bootstrap','store_slug':'initial-'+suffix,'store_name':'Fictional initial store',
                        'email':'initial@example.invalid','password':'Fictional initial password only!'})
        before=self.counts();results=[]
        for i in range(2):
            invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation)
            results.append(self.service.accept(terms,self.address+'-'+str(i)))
        after=self.counts();self.assertEqual(after['ss_tenants']-before['ss_tenants'],2);self.assertEqual(after['ss_staff']-before['ss_staff'],2)
        for table in ('ss_sessions','ss_customers','ss_credit_entries','ss_credit_lots','ss_square_connections','ss_receiving_settings'):self.assertEqual(after[table],before[table])
        with self.database.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM ss_bootstrap WHERE key='initial-owner'").fetchone()['n'],1)
            for result in results:
                row=db.execute('SELECT credit_checkout_enabled,discount_id,tax_policy_reference FROM ss_tenants WHERE slug=%s',(result['store']['slug'],)).fetchone()
                self.assertEqual(tuple(row.values()),(False,None,None));self.assertEqual(result['next_action'],'sign_in')

    def test_email_bound_validation_precedes_expensive_hash_and_allows_normalized_email(self):
        invitation,_=issue_fixture(self.service);before=self.counts();terms=accept_fixture(invitation,email='wrong@example.invalid')
        with patch('hosted.owner_onboarding.password_hash') as expensive,self.assertRaises(EnrollmentError):self.service.accept(terms,self.address)
        expensive.assert_not_called();self.assertEqual(self.counts(),before)
        terms['email']=' '+invitation['email'].upper()+' '
        result=self.service.accept(terms,self.address);self.assertEqual(result['owner_email'],invitation['email'])

    def test_unicode_invitation_issue_and_proof_reject_without_alternate_mailbox_creation(self):
        before=self.counts()
        with self.assertRaises(ServiceError):issue_fixture(self.service,email='straße@example.invalid')
        self.assertEqual(self.counts(),before)
        invitation,_=issue_fixture(self.service,email='strasse@example.invalid');terms=accept_fixture(invitation,email='straße@example.invalid')
        with patch('hosted.owner_onboarding.password_hash') as expensive,self.assertRaises(EnrollmentError):self.service.accept(terms,self.address)
        expensive.assert_not_called();self.assertEqual(self.counts()['ss_tenants'],before['ss_tenants'])

    def test_exact_lost_response_replay_status_and_concurrent_accept_create_one_owner(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation);before=self.counts()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda i:self.service.accept(terms,self.address+str(i)),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.service.accept(terms,self.address),results[0])
        status=self.service.status({key:terms[key] for key in ('invitation_token','email','request_key')},self.address)
        self.assertEqual(status,{'found':True,'state':'completed','result':results[0]})
        after=self.counts();self.assertEqual(after['ss_tenants']-before['ss_tenants'],1);self.assertEqual(after['ss_staff']-before['ss_staff'],1);self.assertEqual(after['ss_sessions'],before['ss_sessions'])
        with self.assertRaises(EnrollmentError) as changed:self.service.accept(dict(terms,request_key='changed-key'),self.address)
        self.assertEqual(changed.exception.code,'ENROLLMENT_ALREADY_COMPLETED')

    def test_revocation_during_hash_rechecks_before_any_tenant_or_staff_commit(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation);before=self.counts()
        def revoke(password):
            self.service.revoke(self.operator,invitation['invitation_id'],'Fictional issuer revoked while password hash was pending')
            return password_hash(password)
        with patch('hosted.owner_onboarding.password_hash',side_effect=revoke),self.assertRaises(EnrollmentError):self.service.accept(terms,self.address)
        self.assertEqual(self.counts(),before)

    def test_audit_failure_rolls_back_owner_tenant_and_invite_consume(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation);before=self.counts()
        with patch('hosted.owner_onboarding.audit',side_effect=RuntimeError('Fictional audit failure')),self.assertRaises(RuntimeError):self.service.accept(terms,self.address)
        self.assertEqual(self.counts(),before)
        self.assertEqual(self.service.inspect({key:terms[key] for key in ('invitation_token','email')},self.address)['invitation_id'],invitation['invitation_id'])
        self.assertTrue(self.service.accept(terms,self.address)['owner_created'])

    def test_expired_and_revoked_invites_never_hash_or_create_accounts(self):
        invitation,_=issue_fixture(self.service,expires_seconds=1);time.sleep(1.05)
        with patch('hosted.owner_onboarding.password_hash') as expensive,self.assertRaises(EnrollmentError):self.service.accept(accept_fixture(invitation),self.address)
        expensive.assert_not_called()
        invitation,_=issue_fixture(self.service);self.service.revoke(self.operator,invitation['invitation_id'],'Fictional issuer withdrew the unused invitation')
        with self.assertRaises(EnrollmentError):self.service.inspect({'invitation_token':invitation['invitation_token'],'email':invitation['email']},self.address)

    def test_issuance_replay_never_recovers_raw_token_and_scope_and_audit_are_immutable(self):
        invitation,body=issue_fixture(self.service);replay=self.service.issue(self.operator,body)
        self.assertIsNone(replay['invitation_token']);self.assertFalse(replay['token_available']);self.assertEqual(replay['invitation_id'],invitation['invitation_id'])
        for update in ("UPDATE ss_owner_enrollments SET email='changed@example.invalid' WHERE id=%s","DELETE FROM ss_owner_enrollments WHERE id=%s"):
            with self.assertRaises(psycopg.errors.RaiseException):
                with self.database.transaction() as db:db.execute(update,(invitation['invitation_id'],))
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_owner_enrollments WHERE id=%s',(invitation['invitation_id'],)).fetchone()
        self.assertNotIn(invitation['invitation_token'],json.dumps(dict(row),default=str));self.assertNotIn('password',json.dumps(dict(row),default=str))

    def test_token_budget_cannot_reset_by_changing_ip_or_repeating_inspect(self):
        invitation,_=issue_fixture(self.service);data={'invitation_token':invitation['invitation_token'],'email':'wrong@example.invalid'}
        for i in range(10):
            with self.assertRaises(EnrollmentError) as caught:self.service.inspect(data,self.address+str(i))
            self.assertEqual(caught.exception.status,403)
        with self.assertRaises(EnrollmentError) as caught:self.service.inspect(dict(data,email=invitation['email']),self.address+'last')
        self.assertEqual(caught.exception.status,429)

    def test_real_http_accept_issues_no_cookie_then_normal_login_uses_new_store(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation)
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://owner-pg.fixture.invalid','APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.database)
        client=app.test_client();base='https://owner-pg.fixture.invalid';kwargs={'base_url':base,'environ_base':{'REMOTE_ADDR':self.address}}
        inspect=client.post('/api/owner-invitations/inspect',json={key:terms[key] for key in ('invitation_token','email')},**kwargs)
        self.assertEqual(inspect.status_code,200)
        response=client.post('/api/owner-invitations/accept',json=terms,**kwargs)
        self.assertEqual(response.status_code,201);self.assertNotIn('Set-Cookie',response.headers)
        credentials={'store_slug':response.json['store']['slug'],'email':terms['email'],'password':terms['password'],'mfa_supported':True}
        login=client.post('/api/auth/login',json=credentials,**kwargs);self.assertEqual(login.status_code,200);self.assertIn('Set-Cookie',login.headers)
        me=client.get('/api/me',base_url=base);self.assertEqual(me.status_code,200);self.assertEqual(me.json['store'],credentials['store_slug']);self.assertEqual(me.json['role'],'owner')
        other,_=issue_fixture(self.service)
        response=client.post('/api/owner-invitations/accept',json=accept_fixture(other),**kwargs)
        self.assertEqual((response.status_code,response.json['code']),(409,'SIGN_OUT_REQUIRED'));self.assertNotIn('Set-Cookie',response.headers)

    def test_consumed_exact_key_metadata_recovers_after_expiry_without_new_session(self):
        invitation,_=issue_fixture(self.service,expires_seconds=1);terms=accept_fixture(invitation)
        result=self.service.accept(terms,self.address);before=self.counts();time.sleep(1.05)
        data={key:terms[key] for key in ('invitation_token','email','request_key')}
        self.assertEqual(self.service.status(data,self.address),{'found':True,'state':'completed','result':result})
        self.assertEqual(self.service.accept(terms,self.address),result);self.assertEqual(self.counts(),before)
        with self.assertRaises(EnrollmentError):self.service.inspect({key:terms[key] for key in ('invitation_token','email')},self.address)

    def test_verified_schema6_to7_adds_no_enrollment_or_financial_backfill(self):
        base=Path(__file__).resolve().parent;current=(base/'hosted/schema.sql').read_bytes()
        payload,marker,_=current.partition(b'\n-- Version 7: independent owner enrollment')
        self.assertTrue(marker)
        self.assertEqual(hashlib.sha256(payload).hexdigest(),'7d2b637f854c31f02d9548d0e6b40a5fc8ef6b60b80d21014eee8e6c4b2e4745')
        schema='ss_test_'+uuid.uuid4().hex;database=None
        with psycopg.connect(self.url,sslmode='require') as db:db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            database=Database(self.url,schema=schema);tenant,owner,customer=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
            ciphertext=Fernet(Fernet.generate_key()).encrypt(b'fictional legacy token bytes').decode()
            with database.transaction() as db:
                self.assertEqual(db.execute('SELECT current_schema() AS name').fetchone()['name'],schema)
                db.execute(payload.decode(),prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,'legacy-fixture','Fictional legacy tenant')",(tenant,))
                db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'legacy@example.invalid','fictional-invalid-password-hash','owner')",(owner,tenant))
                db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis,balance_cents) VALUES (%s,%s,'Fictional legacy balance','fixture-only',1234)",(customer,tenant))
                db.execute("INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at) VALUES (%s,'fictional-legacy-merchant','sandbox',%s,now()+interval '1 day')",(tenant,ciphertext))
                before=db.execute('SELECT * FROM ss_tenants WHERE id=%s',(tenant,)).fetchone()
            database.migrate();database.migrate();self.assertTrue(database.ready())
            with database.transaction() as db:
                self.assertEqual(db.execute('SELECT * FROM ss_tenants WHERE id=%s',(tenant,)).fetchone(),before)
                self.assertEqual(db.execute('SELECT balance_cents FROM ss_customers WHERE id=%s',(customer,)).fetchone()['balance_cents'],1234)
                self.assertEqual(db.execute('SELECT encrypted_tokens FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()['encrypted_tokens'],ciphertext)
                for table in ('ss_owner_enrollments','ss_credit_lots','ss_intake_claims','ss_receiving_operations','ss_receiving_settings'):
                    self.assertEqual(db.execute('SELECT count(*) AS n FROM '+table).fetchone()['n'],0)
        finally:
            if database:database.close()
            with psycopg.connect(self.url,sslmode='require') as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
