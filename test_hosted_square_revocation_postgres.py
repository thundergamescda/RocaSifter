"""Authored actual-PG revocation acceptance; requires a separately approved run.

All signatures/OAuth responses are fictional. No Square request is permitted.
Module aliases avoid rediscovering inherited fixture TestCase classes.
"""
from concurrent.futures import ThreadPoolExecutor
import base64, hashlib, hmac, json, threading, uuid
from pathlib import Path
from unittest.mock import Mock, patch
import psycopg
from psycopg import IsolationLevel
from psycopg import sql

from hosted.database import Database
from hosted.security import ServiceError
from hosted.square import Square
from hosted.reconciliation import Reconciler
import test_hosted_settlement_postgres as settlement_fixture
import test_hosted_receiving_oauth_postgres as oauth_fixture


class PostgreSQLSquareRevocationAcceptance(settlement_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        oauth_fixture.PostgreSQLReceivingOAuthAcceptance.setUp(self)
        self.config['SQUARE_WEBHOOK_SIGNATURE_KEY']='fictional-revocation-signing-key'

    def begin(self):return oauth_fixture.PostgreSQLReceivingOAuthAcceptance.begin(self)
    def connection(self):return oauth_fixture.PostgreSQLReceivingOAuthAcceptance.connection(self)

    def connect(self):
        state,_=self.begin()
        self.square.complete(self.actor,state,'fictional-authorization-code')
        return self.connection()

    def event(self,event=None,*,merchant=None,kind='oauth.authorization.revoked',extra=None):
        body={'event_id':event or 'fictional-revocation-'+uuid.uuid4().hex,
            'merchant_id':merchant or self.tokens['merchant_id'],'type':kind,
            'created_at':'2020-08-14T15:51:04.246373287Z',
            'data':{'type':'revocation','id':'not-an-authorization-generation',
                'object':{'revocation':{'revoked_at':'2020-08-14T15:51:00.246373287Z','revoker_type':'MERCHANT'}}}}
        if extra:body.update(extra)
        raw=json.dumps(body,separators=(',',':')).encode()
        signature=base64.b64encode(hmac.new(self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'].encode(),
            self.square.webhook_url.encode()+raw,hashlib.sha256).digest()).decode()
        return raw,signature

    def receipt(self,event):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_square_revocation_receipts WHERE application_id=%s AND environment=%s AND event_id=%s',
                (self.config['SQUARE_APPLICATION_ID'],'production',event)).fetchone()

    def jobs(self):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s ORDER BY event_id',(self.tenant,)).fetchall()

    def add_job(self,*,attempts=0):
        event='fictional-payment-'+uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash,attempts)
                          VALUES (%s,%s,'payment.updated','fictional-payment',%s,%s)''',(event,self.tenant,'0'*64,attempts))
        return event

    def financial(self):
        with self.database.transaction() as db:
            return {table:db.execute('SELECT count(*) AS n FROM '+table+' WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']
                for table in ('ss_credit_entries','ss_reservations','ss_trade_receipts','ss_intake_claims')}

    def test_exact_replay_after_reconnect_does_not_hold_new_generation_or_jobs(self):
        old=self.connect();event='fictional-exact-replay-'+uuid.uuid4().hex;wire=self.event(event)
        self.square.ingest(*wire);self.add_job();current=self.connect()
        self.assertNotEqual(old['authorization_generation'],current['authorization_generation'])
        jobs=self.jobs();financial=self.financial();receipt=self.receipt(event)
        self.assertTrue(self.square.ingest(*wire)['duplicate'])
        self.assertEqual(self.connection(),current);self.assertEqual(self.jobs(),jobs)
        self.assertEqual(self.financial(),financial);self.assertEqual(self.receipt(event),receipt)

    def test_concurrent_duplicate_has_one_receipt_and_one_effect_audit(self):
        before=self.connect();event='fictional-concurrent-'+uuid.uuid4().hex;wire=self.event(event)
        self.add_job();jobs=self.jobs();barrier=threading.Barrier(2)
        def deliver():
            barrier.wait(timeout=10);return self.square.ingest(*wire)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(deliver) for _ in range(2)]
            results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(r['duplicate'] for r in results),[False,True])
        with self.database.transaction() as db:
            count=db.execute("SELECT count(*) AS n FROM ss_audit WHERE tenant_id=%s AND action='square-authorization-notice-pending'",(self.tenant,)).fetchone()['n']
        self.assertEqual(count,1);self.assertEqual(self.jobs(),jobs)
        self.assertEqual(self.connection()['encrypted_tokens'],before['encrypted_tokens'])

    def test_conflicting_event_body_is_rejected_after_hold_and_after_reconnect(self):
        self.connect();event='fictional-conflict-'+uuid.uuid4().hex;self.square.ingest(*self.event(event))
        for reconnect in (False,True):
            if reconnect:self.connect()
            before=self.connection();receipt=self.receipt(event)
            with self.assertRaises(ServiceError) as caught:self.square.ingest(*self.event(event,extra={'different':True}))
            self.assertEqual(caught.exception.status,409);self.assertEqual(self.connection(),before)
            self.assertEqual(self.receipt(event),receipt)

    def test_unknown_merchant_receipt_remains_after_first_connection(self):
        event='fictional-before-connect-'+uuid.uuid4().hex;wire=self.event(event)
        self.assertTrue(self.square.ingest(*wire)['ignored']);self.assertIsNotNone(self.receipt(event))
        before=self.connect();self.assertTrue(self.square.ingest(*wire)['duplicate'])
        self.assertEqual(self.connection(),before)

    def test_delayed_first_notice_holds_current_tokens_and_preserves_all_work(self):
        before=self.connect();self.add_job(attempts=8);jobs=self.jobs();financial=self.financial()
        self.assertTrue(self.square.ingest(*self.event())['revocation_pending'])
        after=self.connection()
        self.assertEqual(after['encrypted_tokens'],before['encrypted_tokens'])
        self.assertEqual(after['authorization_generation'],before['authorization_generation'])
        self.assertIsNotNone(after['pending_notice_cursor']);self.assertIsNotNone(after['revoked_at'])
        self.assertEqual(self.jobs(),jobs);self.assertEqual(self.financial(),financial)
        with self.assertRaises(ServiceError):self.square.access_token(self.tenant)

    def test_new_event_still_holds_post_notice_reconnected_generation(self):
        self.connect();self.square.ingest(*self.event());self.connect()
        wire=self.event();self.assertFalse(self.square.ingest(*wire)['duplicate'])
        self.assertIsNotNone(self.connection()['pending_notice_cursor'])

    def test_notice_during_token_exchange_rejects_late_callback(self):
        before=self.connect();state,_=self.begin()
        def exchange(_,*,configuration):
            self.assertEqual(configuration.application_id,self.config['SQUARE_APPLICATION_ID'])
            self.assertEqual(configuration.secret,self.config['SQUARE_APPLICATION_SECRET'])
            self.square.ingest(*self.event());return self.tokens,self.expiry
        self.square._token.side_effect=exchange
        with self.assertRaises(ServiceError) as caught:self.square.complete(self.actor,state,'fictional-code')
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(self.connection()['encrypted_tokens'],before['encrypted_tokens'])
        self.assertIsNotNone(self.connection()['pending_notice_cursor'])

    def test_unknown_merchant_notice_during_first_flow_cannot_be_cleared(self):
        state,_=self.begin()
        self.square.ingest(*self.event())
        with self.assertRaises(ServiceError) as caught:self.square.complete(self.actor,state,'fictional-code')
        self.assertEqual(caught.exception.status,409);self.assertIsNone(self.connection())

    def test_two_owner_flows_cannot_overwrite_the_first_completed_generation(self):
        self.connect();a,_=self.begin();b,_=self.begin()
        self.square.complete(self.actor,a,'fictional-code-a');current=self.connection()
        with self.assertRaises(ServiceError) as caught:self.square.complete(self.actor,b,'fictional-code-b')
        self.assertEqual(caught.exception.status,409);self.assertEqual(self.connection(),current)

    def test_notice_transaction_failure_rolls_back_receipt_hold_and_audit(self):
        before=self.connect();wire=self.event();event=json.loads(wire[0])['event_id']
        with patch('hosted.square.audit',side_effect=RuntimeError('fictional-safe-fault')):
            with self.assertRaises(RuntimeError):self.square.ingest(*wire)
        self.assertIsNone(self.receipt(event));self.assertEqual(self.connection(),before)
        self.assertFalse(self.square.ingest(*wire)['duplicate']);self.assertIsNotNone(self.receipt(event))

    def test_notice_for_other_merchant_does_not_reject_current_owner_flow(self):
        before=self.connect();state,_=self.begin()
        self.square.ingest(*self.event(merchant='fictional-other-'+uuid.uuid4().hex))
        self.square.complete(self.actor,state,'fictional-code')
        self.assertIsNone(self.connection()['pending_notice_cursor'])
        self.assertNotEqual(self.connection()['authorization_generation'],before['authorization_generation'])

    def test_pending_jobs_are_not_claimed_or_budget_exhausted(self):
        self.connect();self.add_job(attempts=8);self.square.ingest(*self.event());jobs=self.jobs()
        worker=Reconciler(self.database,self.square,enabled=True)
        self.assertEqual(worker.run_once(tenant_id=self.tenant)['status'],'idle')
        self.assertEqual(self.jobs(),jobs)
        self.square.transport.post.assert_not_called();self.square.transport.get.assert_not_called()

    def test_claim_hold_race_defers_exact_job_without_exception_or_budget_loss(self):
        self.connect();event=self.add_job(attempts=8);worker=Reconciler(self.database,self.square,enabled=True)
        claimed=worker._claim(self.tenant);self.assertEqual(claimed['event_id'],event)
        self.square.ingest(*self.event())
        result=worker._finish(claimed,'exception','retry_budget_exhausted')
        self.assertEqual(result['status'],'authorization_pending')
        job=self.jobs()[0];self.assertEqual((job['state'],job['attempts'],job['lease_token']),('pending',8,None))
        with self.database.transaction() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM ss_reconciliation_exceptions WHERE tenant_id=%s',(self.tenant,)).fetchone())

    def test_begin_overrides_repeatable_read_default_before_cursor_snapshot(self):
        isolated=Database(self.url,schema=self.schema,max_size=1)
        self.addCleanup(isolated.close)
        with isolated.pool.connection() as connection:connection.isolation_level=IsolationLevel.REPEATABLE_READ
        square=Square(isolated,self.config,self.transport);modes=[];original=square._notice_cursor
        def cursor(db,merchant=None):
            modes.append(db.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'])
            return original(db,merchant)
        with patch.object(square,'_notice_cursor',side_effect=cursor):square.begin(self.actor)
        self.assertEqual(modes,['read committed'])

    def test_schema7_is_unready_until_real_migration8_preserves_legacy_without_receipts(self):
        legacy=Path(__file__).resolve().parent/'tests/fixtures/schema7.sql'
        raw=legacy.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'c5a10046808fe3a3b8ba7c668dafd4b01af3165e4b523143f219b23dfe82e903')
        namespace='ss_test_'+uuid.uuid4().hex;created=False;upgraded=None
        try:
            with psycopg.connect(self.url,sslmode='require') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(namespace,)).fetchone())
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            created=True
            upgraded=Database(self.url,schema=namespace,max_size=1)
            with upgraded.transaction() as db:
                db.execute(raw.decode('utf-8'),prepare=False)
                self.assertIsNotNone(db.execute('SELECT 1 FROM ss_schema_versions WHERE version=7').fetchone())
                self.assertIsNone(db.execute('SELECT 1 FROM ss_schema_versions WHERE version=8').fetchone())
            self.assertFalse(upgraded.ready())
            tenant=uuid.uuid4();sealed=self.square.cipher.encrypt(b'{"access_token":"fictional-legacy-token"}').decode()
            with upgraded.transaction() as db:
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional upgrade store')",(tenant,'upgrade-'+tenant.hex))
                db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                              VALUES (%s,%s,'production',%s,%s)''',(tenant,'legacy-'+tenant.hex,sealed,self.expiry))
                db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash)
                              VALUES (%s,%s,'payment.updated','fictional-legacy-payment',%s)''',('legacy-event-'+tenant.hex,tenant,'0'*64))
                old_connection=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
                old_job=db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s',(tenant,)).fetchone()
            upgraded.migrate();self.assertTrue(upgraded.ready())
            with upgraded.transaction() as db:
                connection=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
                self.assertEqual({key:connection[key] for key in old_connection},old_connection)
                self.assertIsNotNone(connection['authorization_generation']);self.assertIsNone(connection['pending_notice_cursor'])
                self.assertEqual(db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s',(tenant,)).fetchone(),old_job)
                self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_square_revocation_receipts').fetchone()['n'],0)
                primary=db.execute("""SELECT array_agg(a.attname ORDER BY u.position) AS columns
                    FROM pg_constraint c CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS u(attribute,position)
                    JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=u.attribute
                    WHERE c.conrelid='ss_square_revocation_receipts'::regclass AND c.contype='p'""").fetchone()
                self.assertEqual(primary['columns'],['application_id','environment','event_id'])
                trigger=db.execute("SELECT tgenabled FROM pg_trigger WHERE tgrelid='ss_square_revocation_receipts'::regclass AND tgname='ss_square_revocation_receipt_immutable'").fetchone()
                self.assertEqual(trigger['tgenabled'],'O')
                self.assertIsNotNone(db.execute("SELECT 1 FROM pg_constraint WHERE conrelid='ss_square_revocation_receipts'::regclass AND contype='c'").fetchone())
        finally:
            if upgraded is not None:upgraded.close()
            if created:
                with psycopg.connect(self.url,sslmode='require') as db:
                    db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
                with psycopg.connect(self.url,sslmode='require') as db:
                    self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(namespace,)).fetchone())
