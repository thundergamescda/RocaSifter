"""Independent enrollment races in disposable private PostgreSQL schemas only."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from hosted.owner_onboarding import OwnerOnboarding,EnrollmentError
from hosted.security import password_hash
import test_hosted_settlement_postgres as postgres_fixture
from test_owner_enrollment_fixtures import issue_fixture,accept_fixture,operator_fixture


class PostgreSQLOwnerOnboardingAdversarialTests(postgres_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        self.service=OwnerOnboarding(self.database);self.address='fictional-owner-'+uuid.uuid4().hex
        no_http=patch('requests.sessions.Session.request',side_effect=AssertionError('External HTTP is forbidden in owner-enrollment fixtures.'))
        no_http.start();self.addCleanup(no_http.stop)

    def owner_counts(self):
        with self.database.transaction() as db:
            return tuple(db.execute('SELECT count(*) AS n FROM '+table).fetchone()['n']
                for table in ('ss_tenants','ss_staff','ss_sessions','ss_customers','ss_credit_entries'))

    def test_expiry_while_waiting_for_final_row_lock_is_unavailable_not_sql_error_or_grant(self):
        invitation,_=issue_fixture(self.service,expires_seconds=2);terms=accept_fixture(invitation);before=self.owner_counts()
        original=self.service._row;started=threading.Event();worker_pid=[]
        def row(db,token,*,lock=False):
            if lock:
                worker_pid.append(db.execute('SELECT pg_backend_pid() AS id').fetchone()['id']);started.set()
            return original(db,token,lock=lock)
        def accept():
            try:return self.service.accept(terms,self.address)
            except Exception as error:return error
        with ThreadPoolExecutor(max_workers=1) as pool,patch.object(self.service,'_row',side_effect=row):
            with self.database.transaction() as holding:
                held=holding.execute('SELECT * FROM ss_owner_enrollments WHERE id=%s FOR UPDATE',(invitation['invitation_id'],)).fetchone()
                future=pool.submit(accept);self.assertTrue(started.wait(5),'Acceptance never reached final row validation')
                deadline=time.monotonic()+1;blocked=False
                while time.monotonic()<deadline:
                    holding.execute('SELECT pg_stat_clear_snapshot()')
                    observed=holding.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s',(worker_pid[0],)).fetchone()
                    if observed and observed['wait_event_type']=='Lock':blocked=True;break
                    time.sleep(.01)
                self.assertTrue(blocked,'The controlled final row-lock wait was not observed')
                remaining=(held['expires_at']-datetime.now(timezone.utc)).total_seconds()
                if remaining>0:time.sleep(remaining+.10)
            outcome=future.result(timeout=5)
        self.assertEqual(self.owner_counts(),before,'An expired invitation created a partial store or owner')
        self.assertIsInstance(outcome,EnrollmentError,
            'Late expiry must produce the generic invitation-unavailable error, not '+type(outcome).__name__)
        self.assertEqual((outcome.status,outcome.code),(403,'INVITATION_UNAVAILABLE'))

    def test_different_concurrent_terms_consume_once_and_only_original_request_recovers(self):
        invitation,_=issue_fixture(self.service);one=accept_fixture(invitation);two=accept_fixture(invitation)
        two['password']='Another fictional password only 2026!';before=self.owner_counts();barrier=threading.Barrier(2)
        def hash_at_once(password):
            barrier.wait(timeout=5);return password_hash(password)
        def consume(terms,address):
            try:return self.service.accept(terms,address)
            except EnrollmentError as error:return error
        with patch('hosted.owner_onboarding.password_hash',side_effect=hash_at_once),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(consume,body,self.address+str(i)) for i,body in enumerate((one,two))]
            results=[future.result(timeout=8) for future in futures]
        winners=[i for i,result in enumerate(results) if isinstance(result,dict)]
        self.assertEqual(len(winners),1);winner=winners[0];loser=1-winner
        self.assertEqual((results[loser].status,results[loser].code),(409,'ENROLLMENT_ALREADY_COMPLETED'))
        after=self.owner_counts();self.assertEqual(tuple(a-b for a,b in zip(after,before)),(1,1,0,0,0))
        winning_body=(one,two)[winner];losing_body=(one,two)[loser]
        self.assertEqual(self.service.status({k:winning_body[k] for k in ('invitation_token','email','request_key')},self.address),
            {'found':True,'state':'completed','result':results[winner]})
        with self.assertRaises(EnrollmentError):self.service.status({k:losing_body[k] for k in ('invitation_token','email','request_key')},self.address)

    def test_last_platform_audit_failure_rolls_back_consume_business_audit_and_new_owner(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation);before=self.owner_counts()
        original=self.service._platform_audit
        def fail_after_business_audit(db,ident,operator,action):
            if action=='consumed':raise RuntimeError('Fictional final platform-audit failure')
            return original(db,ident,operator,action)
        with self.database.transaction() as db:
            audits_before=db.execute('SELECT count(*) AS n FROM ss_audit').fetchone()['n']
        with patch.object(self.service,'_platform_audit',side_effect=fail_after_business_audit),self.assertRaises(RuntimeError):
            self.service.accept(terms,self.address)
        self.assertEqual(self.owner_counts(),before)
        with self.database.transaction() as db:
            row=db.execute('SELECT state,accepted_key,consumed_at,result FROM ss_owner_enrollments WHERE id=%s',(invitation['invitation_id'],)).fetchone()
            self.assertEqual(tuple(row.values()),('issued',None,None,None))
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_audit').fetchone()['n'],audits_before)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM ss_owner_enrollment_audit WHERE enrollment_id=%s AND action='consumed'",(invitation['invitation_id'],)).fetchone()['n'],0)
        self.assertTrue(self.service.accept(terms,self.address)['owner_created'])

    def test_slug_claimed_during_hash_never_attaches_owner_to_existing_store(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation);before=self.owner_counts();other=uuid.uuid4()
        def collide(password):
            with self.database.transaction() as db:
                db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',
                    (other,invitation['store']['slug'],'Fictional independently created store'))
            return password_hash(password)
        with patch('hosted.owner_onboarding.password_hash',side_effect=collide),self.assertRaises(EnrollmentError) as caught:
            self.service.accept(terms,self.address)
        self.assertEqual(caught.exception.code,'STORE_ALREADY_EXISTS')
        self.assertEqual(tuple(a-b for a,b in zip(self.owner_counts(),before)),(1,0,0,0,0))
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_staff WHERE tenant_id=%s',(other,)).fetchone()['n'],0)
            self.assertEqual(db.execute('SELECT state FROM ss_owner_enrollments WHERE id=%s',(invitation['invitation_id'],)).fetchone()['state'],'issued')

    def test_password_hash_holds_no_database_row_lock_and_revocation_wins(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation);before=self.owner_counts()
        hashing=threading.Event();release=threading.Event()
        def held_hash(password):
            hashing.set()
            if not release.wait(5):raise AssertionError('Fictional hash release timeout')
            return password_hash(password)
        def accept():
            try:return self.service.accept(terms,self.address)
            except EnrollmentError as error:return error
        with patch('hosted.owner_onboarding.password_hash',side_effect=held_hash),ThreadPoolExecutor(max_workers=2) as pool:
            future=pool.submit(accept);self.assertTrue(hashing.wait(5))
            try:
                revoked=pool.submit(self.service.revoke,
                    operator_fixture(self.database),
                    invitation['invitation_id'],'Fictional issuer withdrew the invitation during password hashing').result(timeout=2)
                self.assertTrue(revoked['revoked'])
            finally:release.set()
            outcome=future.result(timeout=5)
        self.assertIsInstance(outcome,EnrollmentError);self.assertEqual(outcome.status,403)
        self.assertEqual(self.owner_counts(),before)

    def test_new_tenant_has_no_provider_financial_receiving_or_session_state(self):
        invitation,_=issue_fixture(self.service);terms=accept_fixture(invitation)
        result=self.service.accept(terms,self.address)
        self.assertEqual((result['square_connected'],result['receiving_enabled'],result['checkout_enabled']),(False,False,False))
        self.assertTrue(result['mfa_enrollment_required_for_trade_and_square_writes'])
        self.assertNotIn('mfa_enrollment_required_for_financial_actions',result)
        with self.database.transaction() as db:
            tenant=db.execute('SELECT * FROM ss_tenants WHERE slug=%s',(invitation['store']['slug'],)).fetchone()
            self.assertFalse(tenant['credit_checkout_enabled']);self.assertIsNone(tenant['discount_id']);self.assertIsNone(tenant['tax_policy_reference'])
            staff=db.execute('SELECT role,enabled FROM ss_staff WHERE tenant_id=%s',(tenant['id'],)).fetchall()
            self.assertEqual(staff,[{'role':'owner','enabled':True}])
            for table in ('ss_sessions','ss_customers','ss_credit_entries','ss_credit_lots','ss_reservations','ss_square_connections',
                'ss_receiving_settings','ss_receiving_bindings','ss_receiving_operations','ss_trade_receipts','ss_intake_claims'):
                with self.subTest(table=table):self.assertEqual(db.execute('SELECT count(*) AS n FROM '+table+' WHERE tenant_id=%s',(tenant['id'],)).fetchone()['n'],0)


if __name__=='__main__':unittest.main()
