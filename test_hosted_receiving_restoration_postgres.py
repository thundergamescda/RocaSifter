"""Compact restoration-source API against disposable TLS PostgreSQL only."""
from copy import deepcopy
import hashlib
import json
import time
import uuid
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.types.json import Jsonb
from hosted.app import create_app
from hosted.receiving import RESTORATION_STATE_DOMAIN,RESTORATION_SOURCE_DOMAIN
from hosted.receiving_plan import canonical,fingerprint,ReceivingError,normalize_plan
from hosted.security import ServiceError,digest
from intake_manifest import capture_manifest
import test_hosted_receiving_postgres as receiving_fixture
import test_hosted_receiving_pipeline_postgres as pipeline_fixture
from test_hosted_receiving import plan_data
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import draft_data,acceptance_data
from test_intake_fixtures import fresh_mfa_actor


class NoProvider:
    def __getattr__(self,name):raise AssertionError('Restoration read must not access any provider method: '+name)


class PostgreSQLReceivingRestorationAcceptance(_PostgreSQLSettlementFixture):
    setUp=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.setUp
    approve=receiving_fixture.PostgreSQLReceivingAcceptance.approve
    plan=receiving_fixture.PostgreSQLReceivingAcceptance.plan
    terms=receiving_fixture.PostgreSQLReceivingAcceptance.terms
    commit=receiving_fixture.PostgreSQLReceivingAcceptance.commit
    recover=receiving_fixture.PostgreSQLReceivingAcceptance.recover
    wallet=receiving_fixture.PostgreSQLReceivingAcceptance.wallet
    trade=receiving_fixture.PostgreSQLReceivingAcceptance.trade
    accept_trade=receiving_fixture.PostgreSQLReceivingAcceptance.accept_trade
    new_plan=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.new_plan
    authorize=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.authorize

    def snapshot(self):
        with self.database.transaction() as db:
            names=db.execute("SELECT table_name FROM information_schema.columns WHERE table_schema=%s AND column_name='tenant_id' ORDER BY table_name",(self.schema,)).fetchall()
            payload={row['table_name']:db.execute(sql.SQL('SELECT to_jsonb(t) AS value FROM {} t WHERE tenant_id=%s ORDER BY to_jsonb(t)::text').format(sql.Identifier(row['table_name'])),(self.tenant,)).fetchall() for row in names}
        return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()

    def provider_snapshot(self):
        return deepcopy((self.provider.calls,self.provider.catalog_calls,self.provider.access_calls,self.provider.history_rows,self.provider.objects))

    def read_source(self,plan,actor=None):
        before=self.snapshot();provider=self.provider_snapshot()
        try:return self.service.restoration_source(actor or self.actor,plan['receiving_id'])
        finally:
            self.assertEqual(self.snapshot(),before,'Source read must not alter any tenant table')
            self.assertEqual(self.provider_snapshot(),provider,'Source read must not call or change provider')

    def http(self,plan,role='owner'):
        actor=self.actors[role];token='fictional-restoration-session-'+uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                          SELECT %s,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at FROM ss_sessions WHERE token_hash=%s''',
                       (digest(token),actor['session_hash']))
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://restoration.fixture.invalid',
                        'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.database)
        app.extensions['ss_receiving'].provider=NoProvider()
        client=app.test_client();path='/api/receiving/'+plan['receiving_id']+'/restoration-source'
        return client,path,{'Authorization':'Bearer '+token},token

    def check_hashes(self,source,operation):
        self.assertEqual(set(source),{'version','receivingId','serverRevision','planHash','sourceSnapshotHash','physicalIntakeId',
            'physicalManifestHash','workflowKind','acceptedTradeId','acceptedReceiptHash','normalizedPlan','admission','stateBasisHash','restorationSourceHash'})
        self.assertEqual(source['stateBasisHash'],fingerprint({'domain':RESTORATION_STATE_DOMAIN,'operation':operation,'admission':source['admission']}))
        body=dict(source);body.pop('restorationSourceHash')
        self.assertEqual(source['restorationSourceHash'],fingerprint({'domain':RESTORATION_SOURCE_DOMAIN,'source':body}))
        self.assertEqual(canonical(source['normalizedPlan']),canonical(normalize_plan(source['normalizedPlan'])))
        self.assertNotIn('request_key',source['normalizedPlan'])
        forbidden={'encrypted_body','provider_key','session_hash','authority_hash','token','access_token','request_key','parent_snapshots','catalog_additions'}
        def walk(value):
            if isinstance(value,dict):
                self.assertFalse(set(value)&forbidden)
                for child in value.values():walk(child)
            elif isinstance(value,list):
                for child in value:walk(child)
        walk(source)

    def test_ready_source_exact_normalized_facts_hashes_and_read_only_owner_manager_api(self):
        plan=self.plan();source=self.read_source(plan)
        self.check_hashes(source,plan)
        self.assertEqual(source['admission'],{'everAdmitted':False,'catalogStepPresent':False,'inventoryStepPresent':False,'leaseActive':False})
        self.assertIsNone(source['acceptedTradeId']);self.assertIsNone(source['acceptedReceiptHash'])
        for role in ('owner','manager'):
            client,path,headers,_=self.http(plan,role);before=self.snapshot();provider=self.provider_snapshot()
            result=client.get(path,base_url='https://restoration.fixture.invalid',headers=headers)
            self.assertEqual(result.status_code,200);self.assertEqual(result.data,canonical(source))
            self.assertEqual(result.headers['Cache-Control'],'no-store');self.assertLessEqual(len(result.data),2_000_000)
            self.assertEqual(self.snapshot(),before);self.assertEqual(self.provider_snapshot(),provider)

    def test_accepted_trade_source_original_ids_review_zero_facts_not_fresh_price_inference(self):
        trade=self.trade();self.accept_trade(trade);accepted=self.trades.get(self.actor,trade['trade_id'])
        body=plan_data(self.claim);body.pop('external_ownership_confirmed');body['lines'][0].pop('identity')
        body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['trade_id'],accepted_receipt_hash=accepted['receipt_hash'])
        body['lines'][0]['accepted_line_id']=accepted['accepted_receipt']['lines'][0]['id']
        plan=self.plan(body);before=self.wallet();source=self.read_source(plan)
        self.check_hashes(source,plan);self.assertEqual(source['acceptedTradeId'],trade['trade_id'])
        self.assertEqual(source['acceptedReceiptHash'],accepted['receipt_hash']);self.assertEqual(self.wallet(),before)
        self.assertEqual(source['normalizedPlan']['lines'][0]['reviewed'],True)
        self.assertIs(source['normalizedPlan']['lines'][0]['zero_price_confirmed'],False)

    def test_historical_cancelled_inventory_source_survives_later_trade_without_reviving(self):
        plan=self.plan();cancelled=self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Fictional unadmitted cancellation retained for historical source'))
        self.accept_trade(self.trade());before=self.wallet();source=self.read_source(plan)
        self.check_hashes(source,cancelled);self.assertEqual(source['workflowKind'],'inventory_only')
        self.assertIsNone(source['acceptedTradeId']);self.assertFalse(source['admission']['everAdmitted']);self.assertEqual(self.wallet(),before)

    def test_inventory_pending_active_and_expired_lease_are_durable_read_facts(self):
        plan=self.plan();seen=[]
        def pending():
            source=self.read_source(plan);seen.append(source)
            self.assertEqual(source['admission'],{'everAdmitted':True,'catalogStepPresent':False,'inventoryStepPresent':True,'leaseActive':True})
            with self.database.transaction() as db:db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(self.tenant,plan['receiving_id']))
            expired=self.read_source(plan);self.assertFalse(expired['admission']['leaseActive'])
        self.provider.after_dispatch=pending;final=self.commit(plan)
        self.assertEqual(final['state'],'received');self.assertEqual(len(seen),1)

    def test_catalog_pending_catalog_confirmed_and_terminal_receipt_source_hashes(self):
        plan=self.new_plan();seen=[]
        def pending():
            source=self.read_source(plan);seen.append(source)
            self.assertEqual(source['admission'],{'everAdmitted':True,'catalogStepPresent':True,'inventoryStepPresent':False,'leaseActive':True})
        self.provider.after_catalog=pending;catalog=self.commit(plan)
        self.assertEqual(catalog['state'],'catalog_confirmed');source=self.read_source(plan);self.check_hashes(source,catalog)
        self.assertTrue(source['admission']['catalogStepPresent']);self.assertFalse(source['admission']['inventoryStepPresent'])
        final=self.authorize(plan);terminal=self.read_source(plan);self.check_hashes(terminal,final)
        self.assertTrue(terminal['admission']['inventoryStepPresent']);self.assertNotEqual(source['stateBasisHash'],terminal['stateBasisHash'])
        self.assertEqual(source['normalizedPlan'],terminal['normalizedPlan']);self.assertEqual(len(seen),1)

    def test_catalog_and_inventory_unknown_outcomes_keep_distinct_stage_facts(self):
        plan=self.new_plan();self.provider.catalog_behavior='lost_response';unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown');source=self.read_source(plan);self.check_hashes(source,unknown)
        self.assertTrue(source['admission']['catalogStepPresent']);self.assertFalse(source['admission']['inventoryStepPresent'])
        self.assertFalse(source['admission']['leaseActive'])
        self.assertEqual(self.recover(plan)['state'],'catalog_confirmed')
        self.provider.behavior='lost_response';inventory_unknown=self.authorize(plan)
        self.assertEqual(inventory_unknown['state'],'outcome_unknown');inventory_source=self.read_source(plan)
        self.check_hashes(inventory_source,inventory_unknown)
        self.assertTrue(inventory_source['admission']['catalogStepPresent']);self.assertTrue(inventory_source['admission']['inventoryStepPresent'])
        self.assertFalse(inventory_source['admission']['leaseActive'])

    def test_retained_inventory_proof_needs_review_source_is_not_terminal_success(self):
        plan=self.plan()
        def changed():self.provider.objects['fixture-variation']['item_variation_data']['price_money']['amount']=999
        self.provider.after_dispatch=changed;value=self.commit(plan)
        self.assertEqual(value['state'],'needs_review');source=self.read_source(plan);self.check_hashes(source,value)
        self.assertTrue(source['admission']['inventoryStepPresent']);self.assertTrue(value['inventory_confirmed']);self.assertIsNone(value['receipt'])

    def test_actual_api_buyer_cashier_revoked_disabled_and_changed_role_are_denied(self):
        plan=self.plan()
        for role in ('buyer','cashier'):
            client,path,headers,_=self.http(plan,role);before=self.snapshot()
            result=client.get(path,base_url='https://restoration.fixture.invalid',headers=headers)
            self.assertEqual(result.status_code,403);self.assertEqual(self.snapshot(),before)
        for change in ('revoke','disable','role'):
            client,path,headers,token=self.http(plan)
            with self.database.transaction() as db:
                if change=='revoke':db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(digest(token),))
                elif change=='disable':db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,self.actor['staff_id']))
                else:db.execute("UPDATE ss_staff SET role='buyer' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
            result=client.get(path,base_url='https://restoration.fixture.invalid',headers=headers)
            self.assertEqual(result.status_code,401 if change in ('revoke','disable') else 403)
            with self.database.transaction() as db:db.execute("UPDATE ss_staff SET enabled=TRUE,role='owner' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))

    def test_missing_and_cross_tenant_operations_are_404_not_empty_sources(self):
        plan=self.plan();tenant=uuid.uuid4();actor={'tenant_id':tenant,'staff_id':uuid.uuid4(),'role':'owner'}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional unrelated source store')",(tenant,'source-'+tenant.hex))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'source@example.invalid','fictional-invalid-password','owner')",(actor['staff_id'],tenant))
            actor=fresh_mfa_actor(db,actor)
        for actual,ident in ((self.actor,str(uuid.uuid4())),(actor,plan['receiving_id'])):
            with self.assertRaises(ServiceError) as caught:self.service.restoration_source(actual,ident)
            self.assertEqual(caught.exception.status,404)
        self.actors['foreign']=actor;client,path,headers,_=self.http(plan,'foreign');before=self.snapshot()
        result=client.get(path,base_url='https://restoration.fixture.invalid',headers=headers)
        self.assertEqual(result.status_code,404);self.assertEqual(self.snapshot(),before)

    def test_stale_write_mfa_is_valid_read_authority_but_still_cannot_admit(self):
        plan=self.plan()
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        self.assertEqual(self.read_source(plan)['receivingId'],plan['receiving_id'])
        with self.assertRaises(ServiceError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])

    def test_session_expiring_during_bounded_serialization_fails_final_authority(self):
        plan=self.plan()
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()+interval '1 second' WHERE token_hash=%s",(self.actor['session_hash'],))
        from hosted.receiving import restoration_source_bytes
        def delayed(value):result=restoration_source_bytes(value);time.sleep(1.1);return result
        with patch('hosted.receiving.restoration_source_bytes',side_effect=delayed):
            with self.assertRaises(ServiceError) as caught:self.read_source(plan)
        self.assertEqual(caught.exception.status,401)

    def test_plan_fingerprint_normalized_boolean_target_and_source_tamper_fail_closed(self):
        plan=self.plan()
        with self.database.transaction() as db:original=db.execute('SELECT plan,plan_hash FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',(self.tenant,plan['receiving_id'])).fetchone()
        for change in ('hash','review','target','source'):
            corrupted=deepcopy(original['plan'])
            if change=='review':corrupted['lines'][0]['reviewed']=False
            elif change=='target':corrupted['targets'][0]['quantity']+=1
            elif change=='source':corrupted['physical_manifest_hash']='0'*64
            with self.database.transaction() as db:db.execute('UPDATE ss_receiving_operations SET plan=%s,plan_hash=%s WHERE tenant_id=%s AND id=%s',
                (Jsonb(corrupted),'0'*64 if change=='hash' else fingerprint(corrupted),self.tenant,plan['receiving_id']))
            with self.assertRaises(ReceivingError) as caught:self.read_source(plan)
            self.assertEqual(caught.exception.code,'RESTORATION_SOURCE_INCONSISTENT')
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_operations SET plan=%s,plan_hash=%s WHERE tenant_id=%s AND id=%s',(Jsonb(original['plan']),original['plan_hash'],self.tenant,plan['receiving_id']))

    def test_forged_terminal_receipt_is_not_hashed_into_trusted_source(self):
        plan=self.plan();self.provider.behavior='before_effect';unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown')
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_operations SET state='received',inventory_proof=%s,receipt=%s WHERE tenant_id=%s AND id=%s",
                       (Jsonb({'kind':'batch_create_ack','request_hash':'0'*64,'response_hash':'1'*64}),Jsonb({'received_at':'2026-01-01T00:00:00+00:00','receipt_hash':'f'*64}),self.tenant,plan['receiving_id']))
        with self.assertRaises(ReceivingError) as caught:self.read_source(plan)
        self.assertEqual(caught.exception.code,'RESTORATION_SOURCE_INCONSISTENT')

    def test_unknown_or_review_without_durable_admission_is_inconsistent(self):
        plan=self.plan()
        for state in ('outcome_unknown','needs_review'):
            with self.database.transaction() as db:
                db.execute('UPDATE ss_receiving_operations SET state=%s WHERE tenant_id=%s AND id=%s',(state,self.tenant,plan['receiving_id']))
            with self.assertRaises(ReceivingError) as caught:self.read_source(plan)
            self.assertEqual(caught.exception.code,'RESTORATION_SOURCE_INCONSISTENT')

    def test_compact_cap_failure_is_read_only_without_changing_existing_8mib(self):
        plan=self.plan()
        with patch('hosted.receiving.RESTORATION_SOURCE_BYTES',32):
            with self.assertRaises(ReceivingError) as caught:self.read_source(plan)
        self.assertEqual(caught.exception.code,'RESTORATION_SOURCE_LIMIT')
        from hosted.receiving_plan import PUBLIC_RESPONSE_BYTES
        self.assertEqual(PUBLIC_RESPONSE_BYTES,8*1024*1024)

    def test_maximum_1000_legal_accepted_rows_fit_compact_actual_api_after_terminal(self):
        original=self.original;manifest=capture_manifest('manual',[{'identity':original['identity'],'quantity':1000}])
        claim=self.claims.create(self.actor,{'request_key':'max-source-claim','physical_reference':'fictional-max-source','source_manifest':manifest,'physical_delivery_verified':True})
        body=draft_data(customer_id=self.customer,request_key='max-source-trade');body['source_reference']='fictional-max-source-trade'
        body.update(physical_intake_id=claim['physical_intake_id'],physical_manifest_hash=claim['manifest_hash'])
        body['lines'][0].update(quantity=1000,source_line_key=plan_data(claim)['lines'][0]['source_line_key'])
        draft=self.trades.create(self.actor,body);accept=acceptance_data(self.customer,request_key='max-source-accept',version=draft['version'],cents=500000);accept['claim_reviewed']=True
        self.trades.accept(self.actor,draft['trade_id'],accept);trade=self.trades.get(self.actor,draft['trade_id'])
        body=plan_data(claim);body.pop('external_ownership_confirmed');base=body['lines'][0];base.pop('identity')
        base.update(quantity=1,accepted_line_id=trade['accepted_receipt']['lines'][0]['id'])
        body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['id'],accepted_receipt_hash=trade['receipt_hash'],
                    lines=[dict(base,review_line_id=str(uuid.uuid4())) for _ in range(1000)])
        self.assertLess(len(canonical(body)),512*1024)
        plan=self.plan(body);final=self.commit(plan);self.assertEqual(final['state'],'received')
        client,path,headers,_=self.http(plan);before=self.snapshot();provider=self.provider_snapshot()
        result=client.get(path,base_url='https://restoration.fixture.invalid',headers=headers)
        self.assertEqual(result.status_code,200);source=result.json;self.check_hashes(source,final)
        self.assertEqual(len(source['normalizedPlan']['lines']),1000);self.assertLess(len(result.data),2_000_000)
        self.assertTrue(source['admission']['inventoryStepPresent']);self.assertEqual(self.snapshot(),before);self.assertEqual(self.provider_snapshot(),provider)
        print('Max legal accepted rows: 1000; compact source UTF-8 bytes: '+str(len(result.data)))


if __name__=='__main__':unittest.main()
