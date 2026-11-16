"""Actual method guards with signed fictional events; PostgreSQL proof is separate."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64, hashlib, hmac, json, unittest, uuid
from unittest.mock import MagicMock, Mock, patch

from hosted.security import ServiceError, digest
from hosted.square import Square, VerifiedSquareAccess
from hosted.receiving_square import ReceivingSquare
from hosted.reconciliation import Reconciler, AuthorizationPending
import test_hosted_square_authority as authority_fixture


class RecordingDatabase:
    def __init__(self, rows=(), all_rows=()):
        self.cursor=MagicMock();self.calls=[];self.transactions=[];self.depth=0
        self.cursor.execute.side_effect=self.execute
        self.cursor.fetchone.side_effect=list(rows)
        self.cursor.fetchall.side_effect=list(all_rows)

    def execute(self,sql,args=None):
        self.calls.append((str(sql),args));return self.cursor

    @contextmanager
    def transaction(self):
        self.transactions.append(len(self.calls));self.depth+=1
        try:yield self.cursor
        finally:self.depth-=1


class SquareRevocationGuardTests(unittest.TestCase):
    def setUp(self):
        guard=patch('requests.sessions.Session.request',side_effect=AssertionError('No live HTTP permitted.'))
        guard.start();self.addCleanup(guard.stop)
        self.tenant=uuid.uuid4();self.staff=uuid.uuid4();self.generation=uuid.uuid4()
        self.config={'PUBLIC_BASE_URL':'https://fictional.invalid','SQUARE_ENVIRONMENT':'production',
            'SQUARE_APPLICATION_ID':'fictional-app','SQUARE_APPLICATION_SECRET':'fictional-secret',
            'SQUARE_WEBHOOK_SIGNATURE_KEY':'fictional-hmac-key','APP_ENCRYPTION_KEYS':'fictional-encryption-key-at-least-32-chars'}
        self.payload={'event_id':'fictional-event','merchant_id':'fictional-merchant','type':'oauth.authorization.revoked',
            'created_at':'2020-08-14T15:51:04.246373287Z','data':{'type':'revocation','id':'not-an-authorization-id',
            'object':{'revocation':{'revoked_at':'2020-08-14T15:51:00.246373287Z','revoker_type':'MERCHANT'}}}}

    def square(self,rows=(),all_rows=()):
        db=RecordingDatabase(rows,all_rows);transport=Mock()
        return Square(db,self.config,transport),db,transport

    def signed(self,payload=None):
        raw=json.dumps(self.payload if payload is None else payload,separators=(',',':')).encode()
        signature=base64.b64encode(hmac.new(self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'].encode(),
            (self.config['PUBLIC_BASE_URL']+'/webhooks/square').encode()+raw,hashlib.sha256).digest()).decode()
        return raw,signature

    def test_first_notice_preserves_tokens_jobs_and_credit(self):
        square,db,transport=self.square([None,{'tenant_id':self.tenant},
            {'tenant_id':self.tenant,'environment':'production'},{'notice_cursor':7}])
        result=square.ingest(*self.signed())
        self.assertEqual(result,{'accepted':True,'revocation_pending':True,'duplicate':False})
        sql='\n'.join(q for q,_ in db.calls)
        self.assertNotIn('encrypted_tokens=',sql);self.assertNotIn('UPDATE ss_square_jobs',sql)
        self.assertNotIn('ss_entries',sql);self.assertNotIn('ss_reservations',sql)
        self.assertIn('pending_notice_cursor=%s',sql);self.assertNotIn('revoked',result)
        self.assertEqual(db.calls[0][0],'SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        self.assertIn('square-notices:fictional-app:production',str(db.calls[1][1]))
        transport.post.assert_not_called();transport.get.assert_not_called()

    def test_same_signed_event_replay_has_no_repeated_effect(self):
        raw,signature=self.signed();outcome={'accepted':True,'revocation_pending':True}
        square,db,_=self.square([{'body_hash':hashlib.sha256(raw).hexdigest(),'outcome':outcome}])
        self.assertEqual(square.ingest(raw,signature),outcome|{'duplicate':True})
        self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q,_ in db.calls))
        self.assertFalse(any('ss_square_connections' in q for q,_ in db.calls))

    def test_conflicting_event_id_denies_even_unknown_or_already_blocked(self):
        for outcome in ({'accepted':True,'ignored':True},{'accepted':True,'revocation_pending':True}):
            square,db,_=self.square([{'body_hash':'0'*64,'outcome':outcome}])
            with self.subTest(outcome=outcome),self.assertRaises(ServiceError) as caught:square.ingest(*self.signed())
            self.assertEqual(caught.exception.status,409)
            self.assertFalse(any(q.startswith(('UPDATE','INSERT')) for q,_ in db.calls))

    def test_unknown_merchant_notice_is_durable_without_inventing_a_connection(self):
        square,db,_=self.square([None,None,None,{'notice_cursor':8}])
        self.assertEqual(square.ingest(*self.signed()),{'accepted':True,'ignored':True,'duplicate':False})
        self.assertTrue(any('INSERT INTO ss_square_revocation_receipts' in q for q,_ in db.calls))
        self.assertFalse(any('UPDATE ss_square_connections' in q for q,_ in db.calls))

    def test_other_environment_connection_is_not_held_by_this_notice(self):
        square,db,_=self.square([None,{'tenant_id':self.tenant},
            {'tenant_id':self.tenant,'environment':'sandbox'},{'notice_cursor':8}])
        self.assertEqual(square.ingest(*self.signed()),{'accepted':True,'ignored':True,'duplicate':False})
        self.assertFalse(any('UPDATE ss_square_connections' in q for q,_ in db.calls))

    def test_insert_conflict_reads_and_compares_the_winning_receipt(self):
        raw,signature=self.signed();hashed=hashlib.sha256(raw).hexdigest()
        for winner_hash in (hashed,'f'*64):
            square,db,_=self.square([None,None,None,None,{'body_hash':winner_hash,'outcome':{'accepted':True,'ignored':True}}])
            if winner_hash==hashed:self.assertTrue(square.ingest(raw,signature)['duplicate'])
            else:
                with self.assertRaises(ServiceError) as caught:square.ingest(raw,signature)
                self.assertEqual(caught.exception.status,409)
            self.assertFalse(any('UPDATE ss_square_connections' in q for q,_ in db.calls))

    def test_resource_events_arriving_during_hold_are_queued_unchanged(self):
        square,db,transport=self.square([{'tenant_id':self.tenant,'revoked_at':datetime.now(timezone.utc),'pending_notice_cursor':7},None])
        payload={'event_id':'fictional-payment-event','merchant_id':'fictional-merchant','type':'payment.updated','data':{'id':'fictional-payment'}}
        raw,signature=self.signed(payload)
        self.assertEqual(square.ingest(raw,signature),{'accepted':True,'duplicate':False})
        insertion=next(args for sql,args in db.calls if 'INSERT INTO ss_square_jobs' in sql)
        self.assertEqual(insertion,('fictional-payment-event',self.tenant,'payment.updated','fictional-payment',hashlib.sha256(raw).hexdigest()))
        transport.get.assert_not_called();transport.post.assert_not_called()

    def test_pending_status_does_not_claim_current_provider_revocation(self):
        square,_,_=self.square([{'merchant_id':'fictional-merchant','environment':'production','revoked_at':datetime.now(timezone.utc),
            'expires_at':datetime.now(timezone.utc)+timedelta(days=10),'requested_profile':'standard','pending_notice_cursor':7}])
        result=square.status({'tenant_id':self.tenant})
        self.assertEqual(result['connection_status'],'revocation_pending');self.assertFalse(result['connected'])
        self.assertIn('preserved',result['message']);self.assertFalse(result['write_checkout_enabled'])

    def test_invalid_signature_does_not_open_a_transaction(self):
        square,db,transport=self.square()
        with self.assertRaises(ServiceError) as caught:square.ingest(self.signed()[0],'A'*44)
        self.assertEqual(caught.exception.status,403);self.assertEqual(db.transactions,[])
        transport.post.assert_not_called()

    def test_missing_or_blank_application_identity_fails_controlled_before_database(self):
        wire=self.signed()
        for application in (None,'','   ',False):
            square,db,transport=self.square()
            square.config=dict(self.config)
            if application is None:square.config.pop('SQUARE_APPLICATION_ID')
            else:square.config['SQUARE_APPLICATION_ID']=application
            with self.subTest(application=application),self.assertRaises(ServiceError) as caught:square.ingest(*wire)
            self.assertEqual(caught.exception.status,503);self.assertIn('application identity',str(caught.exception))
            self.assertEqual(db.transactions,[]);self.assertEqual(db.calls,[])
            transport.post.assert_not_called();transport.get.assert_not_called()

    def test_pending_blocks_token_use_without_refresh_or_provider_calls(self):
        square,db,transport=self.square([{'revoked_at':None,'environment':'production','pending_notice_cursor':9}])
        with self.assertRaises(ServiceError) as caught:square.access_token(self.tenant)
        self.assertEqual(caught.exception.status,409)
        transport.post.assert_not_called();transport.get.assert_not_called()

    def standard_owner_rows(self):
        # Exercise the real authority helper with an unenrolled, live owner.
        return [{'role':'owner','enabled':True},None,
            {'tenant_id':self.tenant,'staff_id':self.staff,'token_hash':'a'*64,
             'expires_at':datetime.now(timezone.utc)+timedelta(hours=1),
             'mfa_version':None,'mfa_authenticated_at':None,'checked_at':datetime.now(timezone.utc)}]

    def test_begin_pins_committed_cursor_and_current_generation_before_returning_url(self):
        square,db,transport=self.square(self.standard_owner_rows()+[{'notice_cursor':7},{'authorization_generation':self.generation}])
        square.begin({'tenant_id':self.tenant,'staff_id':self.staff,'role':'owner','session_hash':'a'*64})
        self.assertEqual(db.calls[0][0],'SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        args=next(args for sql,args in db.calls if 'INSERT INTO ss_oauth_states' in sql)
        self.assertEqual(args[-2:],(7,str(self.generation)))
        transport.post.assert_not_called()

    def test_introspection_cannot_return_authority_after_generation_only_change(self):
        fixture=authority_fixture.SquareAuthorityTests('test_exact_identity_actual_grants_and_server_only_token')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.cursor.execute.return_value.fetchone.side_effect=[fixture.row|{'authorization_generation':self.generation},
            fixture.row|{'authorization_generation':uuid.uuid4()}]
        fixture.respond()
        with self.assertRaises(ServiceError) as caught:fixture.square.verified_access(fixture.tenant,fixture.required)
        self.assertEqual(caught.exception.status,409)

    def test_receiving_final_guard_checks_internal_generation_and_pending(self):
        square,db,_=self.square()
        sealed=square.cipher.encrypt(b'{"access_token":"fictional-token"}').decode()
        row={'revoked_at':None,'pending_notice_cursor':None,'merchant_id':'fictional-merchant','environment':'production',
            'encrypted_tokens':sealed,'authorization_generation':self.generation}
        proof={'merchant_id':'fictional-merchant','environment':'production','application_id':'fictional-app',
            'expires_at':(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()}
        access=VerifiedSquareAccess('fictional-token',proof,str(self.generation))
        db.cursor.fetchone.side_effect=[row,row|{'authorization_generation':uuid.uuid4()},row|{'pending_notice_cursor':7}]
        adapter=ReceivingSquare(square);adapter.assert_current(db.cursor,self.tenant,access)
        for _ in range(2):
            with self.assertRaises(ServiceError):adapter.assert_current(db.cursor,self.tenant,access)
        self.assertNotIn('generation',proof);self.assertNotIn('fictional-token',repr(access))

    def callback(self,*,cursor=10,merchant_cursor=10,expected_generation=None,current=None):
        state='fictional-state';expiry=datetime.now(timezone.utc)+timedelta(days=30)
        actor={'tenant_id':self.tenant,'staff_id':self.staff,'role':'owner','session_hash':'a'*64}
        row={'tenant_id':self.tenant,'session_hash':actor['session_hash'],'expires_at':expiry,'state_hash':digest(state),
            'requested_profile':'standard','application_id':'fictional-app','environment':'production','requested_scopes':list(Square.SCOPES),
            'notice_cursor':cursor,'expected_connection_generation':expected_generation}
        status={'merchant_id':'fictional-merchant','environment':'production','revoked_at':None,'pending_notice_cursor':None,
            'expires_at':expiry,'requested_profile':'standard'}
        square,db,transport=self.square([{'requested_profile':'standard'}]+self.standard_owner_rows()+
            [row,{'notice_cursor':10}]+self.standard_owner_rows()+[{'notice_cursor':merchant_cursor},status],
            [[] if current is None else [current]])
        def exchange(_,*,configuration):
            self.assertEqual(configuration.application_id,'fictional-app')
            self.assertIs(configuration.transport,transport)
            self.assertEqual(db.depth,0)
            return {'access_token':'fictional-token','refresh_token':'fictional-refresh','merchant_id':'fictional-merchant'},expiry
        square._token=Mock(side_effect=exchange)
        return square,db,actor,state

    def test_initial_absent_generation_is_legitimate_only_with_captured_cursor(self):
        square,db,actor,state=self.callback()
        self.assertTrue(square.complete(actor,state,'fictional-code')['connected'])
        for start in db.transactions[:2]:self.assertEqual(db.calls[start][0],'SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        self.assertTrue(any('authorization_generation=EXCLUDED.authorization_generation' in sql for sql,_ in db.calls))
        self.assertTrue(any('pending_notice_cursor=NULL' in sql for sql,_ in db.calls))

    def test_notice_after_flow_began_rejects_late_callback_without_storing_tokens(self):
        square,db,actor,state=self.callback(merchant_cursor=11)
        with self.assertRaises(ServiceError) as caught:square.complete(actor,state,'fictional-code')
        self.assertEqual(caught.exception.status,409);self.assertEqual(square._token.call_count,1)
        self.assertFalse(any('INSERT INTO ss_square_connections' in sql for sql,_ in db.calls))

    def test_legacy_missing_cursor_rejects_before_token_exchange(self):
        square,db,actor,state=self.callback(cursor=None)
        with self.assertRaises(ServiceError) as caught:square.complete(actor,state,'fictional-code')
        self.assertEqual(caught.exception.status,409);square._token.assert_not_called()

    def test_other_completed_flow_invalidates_generation_without_notice(self):
        square,db,actor,state=self.callback(expected_generation=self.generation,current={'tenant_id':self.tenant,
            'merchant_id':'fictional-merchant','environment':'production','authorization_generation':uuid.uuid4()})
        with self.assertRaises(ServiceError) as caught:square.complete(actor,state,'fictional-code')
        self.assertEqual(caught.exception.status,409)
        self.assertFalse(any('INSERT INTO ss_square_connections' in sql for sql,_ in db.calls))


class ReconciliationHoldGuardTests(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid.uuid4();self.gen=uuid.uuid4()
        self.job={'event_id':'fictional-event','tenant_id':self.tenant,'lease_token':uuid.uuid4(),
            'attempts':8,'event_type':'payment.updated','resource_id':'fictional-payment',
            '_authorization_generation':str(self.gen)}

    def finish(self,connection):
        db=RecordingDatabase([{'event_id':self.job['event_id']},connection])
        worker=Reconciler(db,Mock(),enabled=True)
        with patch('hosted.reconciliation.record_exception') as exception:
            result=worker._finish(self.job,'exception','retry_budget_exhausted')
            exception.assert_not_called()
        self.assertEqual(result['status'],'authorization_pending')
        self.assertTrue(any('attempts=GREATEST(attempts-1,0)' in sql for sql,_ in db.calls))
        update=next(args for sql,args in db.calls if sql.startswith('UPDATE ss_square_jobs'))
        self.assertEqual(update,(self.job['event_id'],self.tenant,self.job['lease_token']))

    def test_hold_does_not_permanently_exception_or_exhaust_job(self):
        self.finish({'authorization_generation':self.gen,'pending_notice_cursor':7})

    def test_reconnect_before_finish_does_not_misclassify_old_authority_failure(self):
        self.finish({'authorization_generation':uuid.uuid4(),'pending_notice_cursor':None})

    def test_heartbeat_takes_job_lock_before_connection_and_preserves_hold(self):
        db=RecordingDatabase([{'event_id':self.job['event_id']},{'authorization_generation':self.gen,'pending_notice_cursor':7}])
        worker=Reconciler(db,Mock(),enabled=True)
        with self.assertRaises(AuthorizationPending):worker._heartbeat(self.job)
        self.assertTrue(db.calls[0][0].startswith('UPDATE ss_square_jobs'))
        self.assertIn('FOR SHARE',db.calls[1][0])


if __name__=='__main__':unittest.main(verbosity=2)
