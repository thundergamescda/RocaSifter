"""Bounded receipt-chain acceptance with fictional input and real isolated SQL.

These definitions are authored only. Direct fixture receipt seeds do not claim
webhook authentication, physical stock continuity or provider acceptance.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import uuid

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from hosted import cleanup_receipt_batches as batches
from hosted.cleanup_observer import CleanupObserver
from hosted.database import Database
from hosted.square import Square
from test_intake_fixtures import fresh_mfa_actor
import test_hosted_cleanup_observer_postgres as fixture
import test_hosted_cleanup_backlog_adversarial_postgres as bulk_fixture


class PostgreSQLCleanupBacklogAcceptance(fixture._CleanupObserverFixture):
    def _read_receipt_nodes(self):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE tenant_id=%s ORDER BY high_inclusive,id',(self.tenant,)).fetchall()

    def test_v2_basis_is_distinct_from_literal_manifest_and_copied_into_immutable_history(self):
        self.owner_configure()
        for _ in range(3):self.deliver_inventory()
        work=self.enqueue_manual()
        self.assertEqual((work['receipt_basis_version'],work['work_kind'],len(work['receipt_manifest'])),(2,'collection',3))
        self.assertEqual(work['manifest_hash'],batches.digest(work['receipt_manifest']))
        self.assertNotEqual(work['manifest_hash'],work['receipt_basis_hash'])
        claim=self.observer.claim(work['id']); self.assertIsNotNone(claim)
        self.observer.finish(claim,deepcopy(self.reader.result),self.reader.access(self.tenant))
        collection=self.read_collections()[0]
        for field in batches.BASIS_FIELDS:
            self.assertEqual(claim.attempt[field],work[field]); self.assertEqual(collection[field],work[field])
        self.assertEqual(self.read_projection()['acknowledged_batch_id'],work['receipt_head_batch_id'])
        for table in ('ss_cleanup_receipt_batches','ss_cleanup_collection_work'):
            with self.subTest(table=table),self.assertRaises(psycopg.Error):
                with self.database.transaction() as db:
                    db.execute(sql.SQL('UPDATE {} SET receipt_manifest=%s WHERE id=%s').format(sql.Identifier(table)),
                        (Jsonb([]),work['receipt_head_batch_id'] if table.endswith('batches') else work['id']))
        # An old default-v1 attempt cannot borrow a v2 work header by omitting
        # nullable basis fields: the separate nonnull version FK rejects it.
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.database.transaction() as db:
                db.execute('''INSERT INTO ss_cleanup_collection_attempts
                    (id,work_id,attempt,tenant_id,application_id,environment,merchant_id,authorization_generation,
                     config_version,receipt_hwm,manifest_hash,notice_cursor,access_digest,lease_until)
                    SELECT %s,work_id,2,tenant_id,application_id,environment,merchant_id,authorization_generation,
                        config_version,receipt_hwm,manifest_hash,notice_cursor,access_digest,lease_until
                    FROM ss_cleanup_collection_attempts WHERE id=%s''',(uuid.uuid4(),claim.attempt['id']))

    def test_noncontiguous_global_cursors_keep_exact_full_scope_membership(self):
        self.owner_configure()
        own=[]
        for _ in range(3):
            own.extend(bulk_fixture.seed_backlog(self,scope=self.scope_row(),receipts=1,first_quantity='0'))
            other=self.scope_row()|{'merchant_id':'fictional-unrelated-merchant','authorization_generation':uuid.uuid4()}
            bulk_fixture.seed_backlog(self,scope=other,receipts=1)
        work=self.enqueue_manual()
        self.assertEqual(work['receipt_manifest'],[{'receipt_cursor':row['receipt_cursor'],'body_hash':row['body_hash']} for row in own])
        self.assertGreater(own[-1]['receipt_cursor']-own[0]['receipt_cursor'],len(own)-1)
        self.assertEqual(self.observer.run_once(work_id=work['id'])['status'],'complete')
        node=self._read_receipt_nodes()[-1]
        self.assertEqual((node['prefix_counts']['receipts'],node['prefix_counts']['positive']),(3,0))
        self.assertTrue(self.observer.status(self.actor)['coverage_current'])

    def test_prefix_counts_preserve_mixed_facts_but_next_interval_can_clear(self):
        self.owner_configure()
        item=deepcopy(fixture.complete_attempt()['counts'][0]); item.update(catalog_object_type='ITEM',quantity='-2',state='RESERVED_FOR_SALE',is_estimated=True)
        positive=deepcopy(fixture.complete_attempt()['counts'][0]); positive['quantity']='7'
        self.deliver_inventory([fixture.complete_attempt()['counts'][0],positive,item])
        work=self.enqueue_manual(); self.observer.run_once(work_id=work['id'])
        first=self.read_collections()[0]; prefix=self._read_receipt_nodes()[-1]['prefix_counts']
        self.assertEqual({name:prefix[name] for name in ('receipts','facts','positive','negative','estimated','item','other_state')},
            {'receipts':1,'facts':3,'positive':1,'negative':1,'estimated':1,'item':1,'other_state':1})
        self.assertTrue({'notification_positive','notification_negative','notification_estimated','notification_item_count','notification_other_state'}<=set(first['receipt_flags']))
        second=self.enqueue_manual(); self.observer.run_once(work_id=second['id'])
        self.assertEqual(self.read_collections()[0],first); self.assertEqual(self.read_collections()[-1]['receipt_flags'],[])
        self.assertEqual(self._read_receipt_nodes()[-1]['prefix_counts'],prefix)

    def test_unprocessed_raw_tail_fences_finish_without_losing_the_new_positive(self):
        self.owner_configure(); work=self.enqueue_manual(); claim=self.observer.claim(work['id'])
        before=self.read_projection(); positive=deepcopy(fixture.complete_attempt()['counts'][0]); positive['quantity']='7'
        self.deliver_inventory([positive])
        result=self.observer.finish(claim,deepcopy(self.reader.result),self.reader.access(self.tenant))
        self.assertEqual(result['status'],'fenced'); self.assertEqual(self.read_projection(),before)
        self.assertIn('notification_advanced',self.read_collections()[0]['reason_codes'])
        status=self.observer.status(self.actor)
        self.assertEqual((status['pending_notification_count'],status['receipt_processing_pending_count']),(1,1))
        self.assertFalse(status['coverage_current'])
        retry=self.enqueue_manual(); self.observer.run_once(work_id=retry['id'])
        self.assertIn('notification_positive',self.read_collections()[-1]['receipt_flags'])

    def test_preparation_new_tail_creates_same_principal_child_without_second_fold(self):
        self.owner_configure()
        initial=bulk_fixture.seed_backlog(self,scope=self.scope_row(),receipts=150)
        work=self.enqueue_manual(); self.assertEqual(work['work_kind'],'receipt_prepare')
        tail=bulk_fixture.seed_backlog(self,scope=self.scope_row(),receipts=120,first_quantity='0')
        before_calls=(len(self.reader.calls),self.reader.access_calls)
        result=self.observer.prepare_once(work['id'])
        self.assertEqual(result['status'],'receipt_progress'); self.assertFalse(result['ready'])
        self.assertEqual((len(self.reader.calls),self.reader.access_calls),before_calls)
        with self.database.transaction() as db:
            head=db.execute('SELECT * FROM ss_cleanup_receipt_progress WHERE tenant_id=%s',(self.tenant,)).fetchone()
            child=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE parent_work_id=%s',(work['id'],)).fetchone()
        self.assertEqual(head['processed_hwm'],initial[-1]['receipt_cursor'])
        self.assertEqual(child['receipt_hwm'],tail[-1]['receipt_cursor']); self.assertEqual(child['work_kind'],'receipt_prepare')
        for name in ('tenant_id','application_id','environment','merchant_id','authorization_generation','config_version','principal_kind','staff_id','session_hash','requested_role'):
            self.assertEqual(child[name],work[name])
        self.assertEqual(self.read_work(work['id'])['receipt_hwm'],initial[-1]['receipt_cursor'])
        self.assertEqual(self.read_projection()['acknowledged_hwm'],0)
        self.assertEqual(self.observer.prepare_once(work['id'])['status'],'idle')
        self.assertEqual(self.read_work()['id'],child['id'])

    def test_ready_preparation_check_never_equates_processed_receipts_with_fresh_coverage(self):
        self.owner_configure(); original=self.enqueue_manual(); self.observer.run_once(work_id=original['id'])
        old=self.read_projection(); self.deliver_inventory(); work=self.enqueue_manual()
        counts=(len(self.reader.calls),self.reader.access_calls)
        self.assertTrue(self.observer.prepare_once(work['id'])['ready'])
        self.assertEqual((len(self.reader.calls),self.reader.access_calls),counts)
        status=self.observer.status(self.actor)
        self.assertEqual((status['pending_notification_count'],status['receipt_processing_pending_count']),(1,0))
        self.assertTrue(status['pending_notification_count_exact']); self.assertTrue(status['receipt_processing_pending_count_exact'])
        self.assertFalse(status['coverage_current']); self.assertEqual(self.read_projection(),old)
        self.assertEqual(status['last_complete']['finished_at'],self.read_collections()[0]['finished_at'].isoformat())

    def test_exact_schema12_upgrade_preserves_v1_history_and_recovers_without_invented_prefix(self):
        raw=(Path(__file__).resolve().parent/'tests/fixtures/schema12.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'c4cb1e6139798d0d193f03a4cf15799dfe6f2e75566b22db087d8d933354aab4')
        namespace='ss_test_'+uuid.uuid4().hex; database=None; created=False
        try:
            with psycopg.connect(self.url,sslmode='require') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(namespace,)).fetchone())
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            created=True; database=Database(self.url,schema=namespace)
            tenant,staff,generation,work_id,lease,collection_id,queued_id=[uuid.uuid4() for _ in range(7)]
            saved_conn=self.connection_row(); app=saved_conn['oauth_application_id']; merchant='fictional-schema12-'+tenant.hex
            with database.transaction() as db:
                db.execute(raw.decode('utf-8'),prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional schema12 store')",(tenant,'legacy-'+tenant.hex))
                db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,'fictional-invalid','owner')",
                    (staff,tenant,tenant.hex+'@example.invalid'))
                actor=fresh_mfa_actor(db,{'tenant_id':tenant,'staff_id':staff,'role':'owner'})
                db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at,
                    authorization_generation,oauth_application_id,token_obtained_at) VALUES (%s,%s,'production',%s,%s,%s,%s,%s)''',
                    (tenant,merchant,saved_conn['encrypted_tokens'],saved_conn['expires_at'],generation,app,saved_conn['token_obtained_at']))
                audit=db.execute("INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id) VALUES (%s,%s,'fictional-schema12',%s) RETURNING id",(tenant,staff,str(generation))).fetchone()['id']
                db.execute('''INSERT INTO ss_cleanup_observer_config(tenant_id,application_id,environment,merchant_id,authorization_generation,
                    config_version,enabled,background_enabled,cadence_seconds,stale_after_seconds,owner_staff_id,audit_id,request_key,request_hash)
                    VALUES (%s,%s,'production',%s,%s,1,TRUE,TRUE,3600,7200,%s,%s,'fictional-schema12-config',%s)''',
                    (tenant,app,merchant,generation,staff,audit,'a'*64))
                receipt=db.execute('''INSERT INTO ss_cleanup_inventory_receipts(application_id,environment,event_id,merchant_id,event_type,
                    body_hash,provider_created_at,resource_id,payload_status,fact_count,tenant_id,authorization_generation,binding_status,refresh_queued)
                    VALUES (%s,'production','fictional-schema12-event',%s,'inventory.count.updated',%s,'2020-08-14T15:51:00Z',
                        'fictional-schema12-counts','counts_present',1,%s,%s,'active',TRUE) RETURNING receipt_cursor''',
                    (app,merchant,'b'*64,tenant,generation)).fetchone()['receipt_cursor']
                db.execute('''INSERT INTO ss_cleanup_inventory_facts(receipt_cursor,ordinal,catalog_object_id,catalog_object_type,location_id,state,quantity,provider_calculated_at)
                    VALUES (%s,0,'fictional-variation','ITEM_VARIATION','fictional-location','IN_STOCK','7','2020-08-14T15:51:00Z')''',(receipt,))
                db.execute('''INSERT INTO ss_cleanup_refresh_queue(receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,binding_status)
                    VALUES (%s,%s,'production',%s,%s,%s,'active')''',(receipt,app,merchant,tenant,generation))
                manifest=[{'receipt_cursor':receipt,'body_hash':'b'*64}]; digest=batches.digest(manifest)
                for identity,state in ((work_id,'done'),(queued_id,'queued')):
                    db.execute('''INSERT INTO ss_cleanup_collection_work(id,tenant_id,application_id,environment,merchant_id,authorization_generation,
                        config_version,receipt_hwm,receipt_manifest,manifest_hash,trigger_kind,principal_kind,state)
                        VALUES (%s,%s,%s,'production',%s,%s,1,%s,%s,%s,'notification','platform',%s)''',
                        (identity,tenant,app,merchant,generation,receipt,Jsonb(manifest),digest,state))
                db.execute('''INSERT INTO ss_cleanup_collection_attempts(id,work_id,attempt,tenant_id,application_id,environment,merchant_id,
                    authorization_generation,config_version,receipt_hwm,manifest_hash,notice_cursor,access_digest,lease_until)
                    VALUES (%s,%s,1,%s,%s,'production',%s,%s,1,%s,%s,0,%s,clock_timestamp()+interval '2 minutes')''',
                    (lease,work_id,tenant,app,merchant,generation,receipt,digest,'c'*64))
                db.execute('''INSERT INTO ss_cleanup_collections(id,attempt_id,work_id,tenant_id,application_id,environment,merchant_id,authorization_generation,
                    config_version,receipt_hwm,manifest_hash,result_hash,outcome,started_at,coverage_complete,pagination_complete,context_unchanged,
                    bracketed,reason_codes,context,missing,missing_count,count_rows,nonzero_count,receipt_flags)
                    VALUES (%s,%s,%s,%s,%s,'production',%s,%s,1,%s,%s,%s,'complete',clock_timestamp(),TRUE,TRUE,TRUE,TRUE,'[]','{}','[]',0,0,0,'[]')''',
                    (collection_id,lease,work_id,tenant,app,merchant,generation,receipt,digest,'d'*64))
                db.execute('''INSERT INTO ss_cleanup_current_projection(tenant_id,application_id,environment,merchant_id,authorization_generation,
                    config_version,latest_collection_id,last_complete_id,acknowledged_hwm,health)
                    VALUES (%s,%s,'production',%s,%s,1,%s,%s,%s,'complete')''',
                    (tenant,app,merchant,generation,collection_id,collection_id,receipt))
                old={table:db.execute(sql.SQL('SELECT * FROM {} ORDER BY {}').format(sql.Identifier(table),sql.Identifier(order))).fetchall()
                    for table,order in (('ss_cleanup_collection_work','id'),('ss_cleanup_collection_attempts','id'),
                        ('ss_cleanup_collections','id'),('ss_cleanup_current_projection','tenant_id'))}
            self.assertFalse(database.ready()); database.migrate(); database.migrate(); self.assertTrue(database.ready())
            with database.transaction() as db:
                for table,rows in old.items():
                    current=db.execute(sql.SQL('SELECT * FROM {} ORDER BY {}').format(sql.Identifier(table),sql.Identifier('tenant_id' if table.endswith('projection') else 'id'))).fetchall()
                    self.assertEqual(len(current),len(rows))
                    self.assertEqual([{name:row[name] for name in original} for row,original in zip(current,rows)],rows)
                self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_cleanup_receipt_batches').fetchone()['n'],0)
                self.assertIsNone(db.execute('SELECT acknowledged_batch_id FROM ss_cleanup_current_projection').fetchone()['acknowledged_batch_id'])
            square=Square(database,self.config,self.transport); reader=fixture.FictionalCleanupReader(database,square)
            observer=CleanupObserver(database,square,reader,enabled=True)
            self.assertFalse(observer.status(actor)['coverage_current'])
            observer.request_manual(actor,{'request_key':'fictional-v13-recovery','expected_generation':str(generation),'expected_config_version':1})
            with database.transaction() as db:
                retired=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s',(queued_id,)).fetchone()
                new=db.execute("SELECT * FROM ss_cleanup_collection_work WHERE state='queued'").fetchone()
            self.assertEqual(retired['state'],'superseded'); self.assertEqual(retired['receipt_basis_version'],1)
            self.assertEqual(new['receipt_basis_version'],2); self.assertEqual(new['receipt_manifest'],manifest)
            self.assertEqual(observer.run_once(work_id=new['id'])['status'],'complete')
            self.assertTrue(observer.status(actor)['coverage_current'])
            with database.transaction() as db:
                historical=db.execute('SELECT * FROM ss_cleanup_collections WHERE id=%s',(collection_id,)).fetchone()
                recovered=db.execute('SELECT * FROM ss_cleanup_collections WHERE id<>%s',(collection_id,)).fetchone()
            self.assertEqual({name:historical[name] for name in old['ss_cleanup_collections'][0]},old['ss_cleanup_collections'][0])
            self.assertIn('notification_positive',recovered['receipt_flags'])
        finally:
            if database is not None:database.close()
            if created:
                with psycopg.connect(self.url,sslmode='require') as db:db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
