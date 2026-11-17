"""Fictional reader + real isolated PostgreSQL observer acceptance; UNRUN.

The reusable fixture base contains no test methods. It invokes OAuth setup but
does not inherit an acceptance class or shadow its instance attribute names.
"""
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
from pathlib import Path
import uuid
from unittest.mock import patch

import psycopg
from psycopg import IsolationLevel,sql

from hosted.cleanup_observer import CleanupObserver, CleanupObserverError
from hosted.database import Database
from hosted.receiving_plan import READ_SCOPES
from hosted.receiving_square import ReceivingSquare
from hosted.security import ServiceError
from hosted.square import Square,VerifiedSquareAccess
from test_intake_fixtures import fresh_mfa_actor
import test_hosted_receiving_oauth_postgres as oauth_fixture
import test_hosted_settlement_postgres as settlement_fixture


def complete_attempt():
    return {'coverage_complete':True,'pagination_complete':True,'context_unchanged':True,'bracketed':True,
        'reason_codes':[],'before_context_hash':'a'*64,'after_context_hash':'a'*64,
        'locations':[{'id':'fictional-location','status':'ACTIVE','currency':'USD',
            'name':'Fictional private label','address':{'address_line_1':'Never persist this address'}}],
        'objects':{
            'fictional-item':{'id':'fictional-item','type':'ITEM','version':1,'item_data':{
                'name':'Never persist this item name','description':'Never persist this note',
                'variations':[{'id':'fictional-variation','type':'ITEM_VARIATION','version':2}]}},
            'fictional-variation':{'id':'fictional-variation','type':'ITEM_VARIATION','version':2,
                'item_variation_data':{'item_id':'fictional-item','name':'Never persist variation name'}}},
        'counts':[{'catalog_object_id':'fictional-variation','catalog_object_type':'ITEM_VARIATION',
            'location_id':'fictional-location','state':'IN_STOCK','quantity':'0','calculated_at':'2020-08-14T15:51:00.246373287Z'}],
        'missing':[],'atomic_snapshot':False,'continuous_zero_verified':False,'deletion_enabled':False}


class FictionalCleanupReader:
    def __init__(self,database,square):
        self.database,self.square=database,square
        self.result=complete_attempt(); self.calls=[]; self.access_calls=0
        self.on_collect=None; self.on_access=None

    def access(self,tenant):
        self.access_calls+=1
        if self.on_access:self.on_access(tenant,self.access_calls)
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
        if not row or row['revoked_at'] is not None or row['pending_notice_cursor'] is not None:raise ServiceError('Fictional authority unavailable.',409)
        tokens=json.loads(self.square.cipher.decrypt(row['encrypted_tokens'].encode()))
        proof={'merchant_id':row['merchant_id'],'application_id':self.square.config['SQUARE_APPLICATION_ID'],
            'environment':row['environment'],'scopes':sorted(READ_SCOPES),'required_scopes':sorted(READ_SCOPES),
            'expires_at':row['expires_at'].isoformat(),'checked_at':datetime.now(timezone.utc).isoformat(),
            'square_version':self.square.API_VERSION,'method':'oauth-token-status','grants_verified':True}
        return VerifiedSquareAccess(tokens['access_token'],proof,str(row['authorization_generation']))

    def assert_current(self,db,tenant,access):
        return ReceivingSquare(self.square).assert_current(db,tenant,access)

    def collect_attempt(self,access):
        self.calls.append(('collect',access.generation))
        if self.on_collect:self.on_collect(access)
        return deepcopy(self.result)


class _CleanupObserverFixture(settlement_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        oauth_fixture.PostgreSQLReceivingOAuthAcceptance.setUp(self)
        self.config['SQUARE_WEBHOOK_SIGNATURE_KEY']='fictional-observer-notice-key'
        self.reconnect_owner()
        self.reader=FictionalCleanupReader(self.database,self.square)
        self.observer=CleanupObserver(self.database,self.square,self.reader,enabled=True)

    def reconnect_owner(self):
        state,_=oauth_fixture.PostgreSQLReceivingOAuthAcceptance.begin(self)
        self.square.complete(self.actor,state,'fictional-observer-owner-code')

    def connection_row(self):
        return oauth_fixture.PostgreSQLReceivingOAuthAcceptance.connection(self)

    def read_saved_tokens(self):
        return json.loads(self.square.cipher.decrypt(self.connection_row()['encrypted_tokens'].encode()))

    def scope_row(self):
        row=self.connection_row()
        return {'tenant_id':self.tenant,'application_id':row['oauth_application_id'],'environment':row['environment'],
            'merchant_id':row['merchant_id'],'authorization_generation':row['authorization_generation']}

    def owner_configure(self,*,enabled=True,background=False,expected_version=None,request_key=None):
        current=self.observer.status(self.actor)
        body={'request_key':request_key or 'fictional-config-'+uuid.uuid4().hex,'expected_generation':current['generation'],
            'expected_config_version':current['config_version'] if expected_version is None else expected_version,
            'enabled':enabled,'background_enabled':background,'cadence_seconds':3600,'stale_after_seconds':7200}
        self.observer.configure(self.actor,body)
        return self.observer.status(self.actor)

    def enqueue_manual(self,actor=None,*,request_key=None):
        actor=actor or self.actor; current=self.observer.status(actor)
        self.observer.request_manual(actor,{'request_key':request_key or 'fictional-read-'+uuid.uuid4().hex,
            'expected_generation':current['generation'],'expected_config_version':current['config_version']})
        return self.read_work()

    def read_work(self,work_id=None):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_cleanup_collection_work WHERE '+('id=%s' if work_id else 'tenant_id=%s ORDER BY created_at DESC,id DESC LIMIT 1'),
                (work_id or self.tenant,)).fetchone()

    def read_attempt(self,attempt_id=None):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_cleanup_collection_attempts WHERE '+('id=%s' if attempt_id else 'tenant_id=%s ORDER BY started_at DESC,id DESC LIMIT 1'),
                (attempt_id or self.tenant,)).fetchone()

    def read_projection(self,scope=None):
        scope=scope or self.scope_row()
        with self.database.transaction() as db:
            return db.execute('''SELECT * FROM ss_cleanup_current_projection WHERE tenant_id=%s AND application_id=%s
                AND environment=%s AND merchant_id=%s AND authorization_generation=%s''',tuple(scope[key] for key in
                ('tenant_id','application_id','environment','merchant_id','authorization_generation'))).fetchone()

    def read_collections(self):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_cleanup_collections WHERE tenant_id=%s ORDER BY finished_at,id',(self.tenant,)).fetchall()

    def deliver_inventory(self,facts=None):
        facts=facts if facts is not None else complete_attempt()['counts']
        value={'event_id':'fictional-observer-event-'+uuid.uuid4().hex,'merchant_id':self.connection_row()['merchant_id'],
            'type':'inventory.count.updated','created_at':'2020-08-14T15:51:04.246373287Z',
            'data':{'type':'inventory_counts','id':'fictional-observer-counts','object':{'inventory_counts':facts}}}
        raw=json.dumps(value,separators=(',',':')).encode()
        signature=base64.b64encode(hmac.new(self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'].encode(),
            self.square.webhook_url.encode()+raw,hashlib.sha256).digest()).decode()
        return self.square.ingest(raw,signature)

    def add_actor(self,role):
        actor={'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':role}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,'fictional-invalid',%s)",
                (actor['staff_id'],self.tenant,role+'-'+uuid.uuid4().hex+'@example.invalid',role))
            return fresh_mfa_actor(db,actor)


class PostgreSQLCleanupObserverAcceptance(_CleanupObserverFixture):
    def test_default_off_and_first_sql_barrier_do_not_create_authority_or_schedule(self):
        disabled=CleanupObserver(self.database,self.square,self.reader)
        with patch.object(self.database,'transaction',side_effect=AssertionError('Disabled worker opened SQL')):
            self.assertEqual(disabled.run_once()['status'],'disabled')
            self.assertEqual(disabled.schedule_notifications()['scheduled'],0)
        current=self.observer.status(self.actor)
        self.assertEqual(current['config_version'],0); self.assertFalse(current['enabled']); self.assertFalse(current['background_enabled'])
        self.assertFalse(current['coverage_current']); self.assertIsNone(current['last_attempt'])
        isolated=Database(self.url,schema=self.schema,max_size=1); self.addCleanup(isolated.close)
        with isolated.pool.connection() as connection:connection.isolation_level=IsolationLevel.REPEATABLE_READ
        square=Square(isolated,self.config,self.transport); reader=FictionalCleanupReader(isolated,square)
        observer=CleanupObserver(isolated,square,reader); modes=[]; original=square._notice_barrier
        def barrier(db,*,configuration=None):
            original(db,configuration=configuration)
            modes.append(db.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'])
        with patch.object(square,'_notice_barrier',side_effect=barrier):observer.status(self.actor)
        self.assertEqual(modes,['read committed']); self.assertEqual(reader.access_calls,0); self.assertEqual(reader.calls,[])

    def test_owner_settings_cas_replay_and_global_off_withdrawal_need_live_fresh_authority(self):
        current=self.observer.status(self.actor)
        body={'request_key':'fictional-consent-'+uuid.uuid4().hex,'expected_generation':current['generation'],'expected_config_version':0,
            'enabled':True,'background_enabled':True,'cadence_seconds':3600,'stale_after_seconds':7200}
        self.assertFalse(self.observer.configure(self.actor,body)['duplicate'])
        self.assertTrue(self.observer.configure(self.actor,body)['duplicate'])
        with self.assertRaises(CleanupObserverError) as conflict:self.observer.configure(self.actor,body|{'cadence_seconds':4000})
        self.assertEqual(conflict.exception.code,'OBSERVER_REQUEST_CONFLICT')
        with self.assertRaises(CleanupObserverError) as stale:self.observer.configure(self.actor,body|{'request_key':'fictional-stale-'+uuid.uuid4().hex})
        self.assertEqual(stale.exception.code,'OBSERVER_CONFIG_CHANGED')
        with self.assertRaises(CleanupObserverError) as contradictory:
            self.observer.configure(self.actor,body|{'enabled':False,'background_enabled':True})
        self.assertEqual((contradictory.exception.status,contradictory.exception.code),(400,'OBSERVER_INPUT_INVALID'))
        manager=self.add_actor('manager')
        with self.assertRaises(CleanupObserverError) as denied:self.observer.configure(manager,body)
        self.assertEqual(denied.exception.status,403)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(CleanupObserverError) as expired:self.observer.configure(self.actor,body)
        self.assertEqual(expired.exception.code,'MFA_STEP_UP_REQUIRED')
        with self.database.transaction() as db:
            db.execute('UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp() WHERE token_hash=%s',(self.actor['session_hash'],))
        off=CleanupObserver(self.database,self.square,self.reader)
        self.config['SQUARE_APPLICATION_SECRET']=''
        withdrawn=body|{'request_key':'fictional-withdraw-'+uuid.uuid4().hex,'expected_config_version':1,'enabled':False,'background_enabled':False}
        self.assertTrue(off.configure(self.actor,withdrawn)['accepted'])
        self.assertFalse(off.status(self.actor)['enabled']); self.assertFalse(off.status(self.actor)['background_enabled'])
        self.assertTrue(off.configure(self.actor,body)['duplicate'])
        self.assertFalse(off.status(self.actor)['enabled'])
        self.assertEqual(self.reader.access_calls,0); self.assertEqual(self.reader.calls,[])

    def test_complete_read_keeps_only_scoped_minimal_history_and_no_provider_write(self):
        self.owner_configure(); work=self.enqueue_manual(); before=self.connection_row()
        result=self.observer.run_once(work_id=work['id'])
        self.assertEqual(result['status'],'complete')
        collection=self.read_collections()[0]; projection=self.read_projection()
        self.assertTrue(collection['coverage_complete']); self.assertEqual(collection['count_rows'],1)
        self.assertEqual(projection['last_complete_id'],collection['id'])
        self.assertEqual(projection['acknowledged_hwm'],work['receipt_hwm'])
        self.assertEqual(self.read_work()['state'],'done')
        with self.database.transaction() as db:
            counts=db.execute('SELECT * FROM ss_cleanup_collection_counts WHERE collection_id=%s',(collection['id'],)).fetchall()
        self.assertEqual(counts[0]['quantity'],'0')
        self.assertEqual(counts[0]['calculated_at'],'2020-08-14T15:51:00.246373287Z')
        self.assertIsNone(counts[0]['is_estimated'])
        encoded=json.dumps(collection,default=str)
        for text in ('Never persist','Fictional private label','address_line_1','fictional-access-private','fictional-refresh-private'):
            self.assertNotIn(text,encoded)
        status=self.observer.status(self.actor)
        self.assertTrue(status['coverage_current']); self.assertEqual(status['state'],'complete')
        for name in ('atomic_snapshot','continuous_zero_verified','deletion_enabled'):self.assertIs(status[name],False)
        for name in ('lease_token','manifest_hash','before_context_hash','access_digest'):self.assertNotIn(name,status)
        self.assertEqual(self.connection_row(),before)
        self.transport.get.assert_not_called(); self.transport.post.assert_not_called()

    def test_partial_ordered_positive_negative_estimate_witnesses_survive_later_zero(self):
        self.owner_configure(); first=self.enqueue_manual(); self.observer.run_once(work_id=first['id'])
        initial=self.read_projection(); original_complete=initial['last_complete_id']
        notification=deepcopy(complete_attempt()['counts'][0]); notification['quantity']='7'
        self.deliver_inventory([notification,complete_attempt()['counts'][0]])
        partial=complete_attempt(); positive=deepcopy(partial['counts'][0]); positive['quantity']='+7.00000'; positive['is_estimated']=True
        negative=deepcopy(partial['counts'][0]); negative['quantity']='-1'; negative.pop('calculated_at')
        partial.update(coverage_complete=False,pagination_complete=False,bracketed=False,after_context_hash=None,reason_codes=['counts_incomplete'])
        partial['counts']=[partial['counts'][0],positive,negative]; self.reader.result=partial
        work=self.enqueue_manual(); self.assertEqual(self.observer.run_once(work_id=work['id'])['status'],'partial')
        saved=self.read_collections()[-1]; projection=self.read_projection()
        with self.database.transaction() as db:
            counts=db.execute('SELECT * FROM ss_cleanup_collection_counts WHERE collection_id=%s ORDER BY ordinal',(saved['id'],)).fetchall()
            facts=db.execute('''SELECT quantity FROM ss_cleanup_inventory_facts f JOIN ss_cleanup_inventory_receipts r
                ON r.receipt_cursor=f.receipt_cursor WHERE r.tenant_id=%s ORDER BY r.receipt_cursor,f.ordinal''',(self.tenant,)).fetchall()
        self.assertEqual([row['quantity'] for row in counts],['0','+7.00000','-1'])
        self.assertEqual([row['quantity'] for row in facts],['7','0'])
        self.assertIsNone(counts[2]['calculated_at']); self.assertIs(counts[1]['is_estimated'],True)
        self.assertIn('notification_positive',saved['receipt_flags'])
        self.assertEqual(projection['acknowledged_hwm'],work['receipt_hwm'])
        self.assertEqual(projection['last_complete_id'],original_complete)
        self.assertNotEqual(projection['diagnostic_epoch'],initial['diagnostic_epoch'])
        self.assertFalse(self.observer.status(self.actor)['coverage_current'])
        self.reader.result=complete_attempt(); retry=self.enqueue_manual(); self.observer.run_once(work_id=retry['id'])
        self.assertNotEqual(self.read_projection()['diagnostic_epoch'],projection['diagnostic_epoch'])
        with self.database.transaction() as db:
            after=db.execute('SELECT quantity FROM ss_cleanup_collection_counts WHERE collection_id=%s ORDER BY ordinal',(saved['id'],)).fetchall()
        self.assertEqual([row['quantity'] for row in after],['0','+7.00000','-1'])

    def test_inactive_location_missing_count_remains_unknown_and_never_becomes_zero(self):
        self.owner_configure(); self.reader.result['locations'].append({'id':'fictional-inactive','status':'INACTIVE','currency':'CAD'})
        self.reader.result.update(coverage_complete=False,reason_codes=['missing_counts'],missing=[['fictional-variation','fictional-inactive']])
        work=self.enqueue_manual(); self.assertEqual(self.observer.run_once(work_id=work['id'])['status'],'partial')
        collection=self.read_collections()[0]
        self.assertEqual(collection['count_rows'],1); self.assertEqual(collection['missing_count'],1)
        self.assertIn({'id':'fictional-inactive','status':'INACTIVE','currency':'CAD'},collection['context']['locations'])
        status=self.observer.status(self.actor)
        self.assertEqual(status['missing'],[{'object_id':'fictional-variation','location_id':'fictional-inactive'}]); self.assertFalse(status['coverage_current'])

    def test_notification_coalescing_freezes_manifest_and_preserves_all_original_intents(self):
        self.owner_configure(background=True)
        for _ in range(3):self.deliver_inventory()
        with self.database.transaction() as db:
            original=db.execute('SELECT * FROM ss_cleanup_refresh_queue WHERE tenant_id=%s ORDER BY receipt_cursor',(self.tenant,)).fetchall()
        self.assertEqual(self.observer.schedule_notifications()['scheduled'],1)
        old=self.read_work(); self.assertEqual(len(old['receipt_manifest']),3)
        self.assertEqual(self.observer.schedule_notifications()['scheduled'],0)
        self.deliver_inventory(); self.observer.schedule_notifications()
        current=self.read_work()
        self.assertNotEqual(current['id'],old['id']); self.assertGreater(current['receipt_hwm'],old['receipt_hwm'])
        saved_old=self.read_work(old['id'])
        self.assertEqual(saved_old['state'],'superseded')
        for name in ('authorization_generation','receipt_hwm','receipt_manifest','manifest_hash','principal_kind'):
            self.assertEqual(saved_old[name],old[name])
        with self.database.transaction() as db:
            preserved=db.execute('SELECT * FROM ss_cleanup_refresh_queue WHERE tenant_id=%s AND receipt_cursor=ANY(%s) ORDER BY receipt_cursor',
                (self.tenant,[row['receipt_cursor'] for row in original])).fetchall()
        self.assertEqual(preserved,original); self.assertTrue(all(row['state']=='queued_not_executed' for row in preserved))

    def test_platform_periodic_work_uses_consent_without_borrowing_an_owner_session(self):
        self.owner_configure(background=True); buyer=self.add_actor('buyer')
        with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
        self.assertEqual(self.observer.schedule_periodic()['scheduled'],1)
        work=self.read_work(); self.assertEqual(work['principal_kind'],'platform'); self.assertIsNone(work['staff_id']); self.assertIsNone(work['session_hash'])
        self.assertEqual(self.observer.run_once(work_id=work['id'])['status'],'complete')
        self.assertTrue(self.observer.status(buyer)['coverage_current'])
        with self.assertRaises(CleanupObserverError) as forbidden:self.enqueue_manual(buyer)
        self.assertEqual(forbidden.exception.status,403)
        cashier=self.add_actor('cashier')
        with self.assertRaises(CleanupObserverError):self.observer.status(cashier)

    def test_periodic_and_manual_work_never_follow_a_reconnected_generation(self):
        self.owner_configure(background=True); self.observer.schedule_periodic(); old=self.read_work(); scope=self.scope_row()
        self.reconnect_owner(); status=self.observer.status(self.actor)
        self.assertNotEqual(status['generation'],str(scope['authorization_generation']))
        self.assertEqual(status['config_version'],0); self.assertFalse(status['enabled']); self.assertFalse(status['background_enabled'])
        self.assertFalse(self.observer.run_once(work_id=old['id'])['processed'])
        self.assertEqual(self.read_work(old['id'])['authorization_generation'],scope['authorization_generation'])
        self.assertEqual(self.observer.schedule_periodic()['scheduled'],0)
        self.assertEqual(self.reader.access_calls,0); self.assertEqual(self.reader.calls,[])

    def test_latest_generation_returned_by_access_cannot_authorize_old_work_data_read(self):
        self.owner_configure(); work=self.enqueue_manual()
        def reconnect(tenant,index):
            if index==1:self.reconnect_owner()
        self.reader.on_access=reconnect
        self.assertFalse(self.observer.run_once(work_id=work['id'])['processed'])
        self.assertEqual(self.reader.calls,[]); self.assertEqual(self.read_collections(),[])
        self.assertEqual(self.read_work(work['id'])['authorization_generation'],work['authorization_generation'])
        self.assertEqual(self.observer.status(self.actor)['config_version'],0)

    def test_concurrent_token_change_is_a_diagnostic_gap_and_cannot_hide_behind_old_success(self):
        self.owner_configure(); initial=self.enqueue_manual(); self.observer.run_once(work_id=initial['id'])
        before=self.read_projection()
        def rotate(_):
            tokens=self.read_saved_tokens()|{'access_token':'fictional-concurrent-renewal-private'}
            with self.database.transaction() as db:
                db.execute('UPDATE ss_square_connections SET encrypted_tokens=%s WHERE tenant_id=%s',
                    (self.square.cipher.encrypt(json.dumps(tokens).encode()).decode(),self.tenant))
        self.reader.on_collect=rotate
        work=self.enqueue_manual(); self.assertEqual(self.observer.run_once(work_id=work['id'])['status'],'fenced')
        self.assertEqual(self.read_projection(),before)
        self.assertFalse(self.observer.status(self.actor)['coverage_current'])
        self.assertEqual(self.read_collections()[-1]['count_rows'],0)
        self.reader.on_collect=None
        retry=self.enqueue_manual(); self.assertEqual(self.observer.run_once(work_id=retry['id'])['status'],'complete')
        self.assertNotEqual(self.read_projection()['diagnostic_epoch'],before['diagnostic_epoch'])

    def test_unscheduled_receipt_backlog_invalidates_status_without_mutating_projection(self):
        self.owner_configure(); work=self.enqueue_manual(); self.observer.run_once(work_id=work['id'])
        projection=self.read_projection(); positive=deepcopy(complete_attempt()['counts'][0]); positive['quantity']='3'
        self.deliver_inventory([positive]); self.deliver_inventory()
        status=self.observer.status(self.actor)
        self.assertEqual(status['pending_notification_count'],2)
        self.assertEqual(status['state'],'stale'); self.assertFalse(status['coverage_current'])
        self.assertEqual(self.read_projection(),projection)
        self.assertFalse(status['continuous_zero_verified']); self.assertFalse(status['deletion_enabled'])

    def test_history_and_work_identity_are_immutable_and_projection_cannot_cross_generation(self):
        self.owner_configure(); work=self.enqueue_manual(); self.observer.run_once(work_id=work['id'])
        collection=self.read_collections()[0]; attempt=self.read_attempt()
        tables=(('ss_cleanup_observer_config','tenant_id',self.tenant),('ss_cleanup_manual_requests','tenant_id',self.tenant),
            ('ss_cleanup_collection_attempts','id',attempt['id']),('ss_cleanup_collections','id',collection['id']),
            ('ss_cleanup_collection_counts','collection_id',collection['id']))
        for table,column,value in tables:
            with self.subTest(table=table):
                with self.assertRaises(psycopg.Error):
                    with self.database.transaction() as db:
                        db.execute(sql.SQL('UPDATE {} SET {}={} WHERE {}=%s').format(sql.Identifier(table),sql.Identifier(column),sql.Identifier(column),sql.Identifier(column)),(value,))
        for field,value in (('authorization_generation',uuid.uuid4()),('receipt_hwm',work['receipt_hwm']+1),('principal_kind','platform')):
            with self.subTest(identity=field),self.assertRaises(psycopg.Error):
                with self.database.transaction() as db:
                    db.execute(sql.SQL('UPDATE ss_cleanup_collection_work SET {}=%s WHERE id=%s').format(sql.Identifier(field)),(value,work['id']))
        self.reconnect_owner(); self.owner_configure()
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.database.transaction() as db:
                db.execute('UPDATE ss_cleanup_current_projection SET latest_collection_id=%s WHERE tenant_id=%s AND authorization_generation=%s',
                    (collection['id'],self.tenant,self.connection_row()['authorization_generation']))

    def test_schema7_upgrade_to12_repeats_without_backfilled_consent_or_changed_legacy_work(self):
        raw=(Path(__file__).resolve().parent/'tests/fixtures/schema7.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'c5a10046808fe3a3b8ba7c668dafd4b01af3165e4b523143f219b23dfe82e903')
        namespace='ss_test_'+uuid.uuid4().hex; database=None; created=False
        try:
            with psycopg.connect(self.url,sslmode='require') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(namespace,)).fetchone())
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            created=True; database=Database(self.url,schema=namespace); tenant=uuid.uuid4()
            with database.transaction() as db:
                db.execute(raw.decode('utf-8'),prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional observer migration store')",(tenant,'legacy-'+tenant.hex))
                db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                    VALUES (%s,%s,'production','fictional-old-ciphertext',%s)''',(tenant,'legacy-'+tenant.hex,self.expiry))
                db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash)
                    VALUES (%s,%s,'payment.updated','fictional-old-payment',%s)''',('legacy-'+tenant.hex,tenant,'0'*64))
                old_conn=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
                old_job=db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s',(tenant,)).fetchone()
                db.execute('INSERT INTO ss_schema_versions(version) VALUES (11)')
            self.assertFalse(database.ready()); database.migrate(); self.assertTrue(database.ready()); database.migrate()
            with database.transaction() as db:
                current=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
                self.assertEqual({key:current[key] for key in old_conn},old_conn)
                self.assertEqual(db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s',(tenant,)).fetchone(),old_job)
                for table in ('ss_cleanup_observer_config','ss_cleanup_collection_work','ss_cleanup_collection_attempts','ss_cleanup_collections','ss_cleanup_current_projection'):
                    self.assertEqual(db.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n'],0)
        finally:
            if database is not None:database.close()
            if created:
                with psycopg.connect(self.url,sslmode='require') as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
