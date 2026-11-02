"""Receiving saga transactions in disposable ss_test_<uuid> schemas only.

Injected fictional provider, no HTTP, merchant data or production migration.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
import uuid
from unittest.mock import patch
import psycopg
from cryptography.fernet import Fernet
from intake_manifest import capture_manifest
from hosted.intake import IntakeClaims
from hosted.receiving import Receiving
from hosted.receiving_plan import ReceivingError, fingerprint
from hosted.security import ServiceError
from hosted.trades import Trades
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import card_line, draft_data, acceptance_data
from test_hosted_receiving import plan_data
from test_intake_fixtures import fresh_mfa_actor
from test_receiving_fixtures import FictionalReceivingProvider


class PostgreSQLReceivingAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        self.tenant,self.customer=uuid.uuid4(),uuid.uuid4()
        self.actors={role:{'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':role} for role in ('owner','manager','buyer','cashier')}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional receiving store')",(self.tenant,'fixture-'+self.tenant.hex))
            for role,actor in self.actors.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',(actor['staff_id'],self.tenant,role+'@example.invalid','fictional-invalid-password',role))
                self.actors[role]=fresh_mfa_actor(db,actor)
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Fictional receiving customer','fixture-only')",(self.customer,self.tenant))
        self.actor=self.actors['owner'];self.provider=FictionalReceivingProvider(self.database,self.tenant)
        self.cipher=Fernet(Fernet.generate_key());self.service=Receiving(self.database,self.provider,self.cipher,writes_enabled=True)
        self.claims=IntakeClaims(self.database);self.trades=Trades(self.database)
        self.original=card_line();self.manifest=capture_manifest('manual',[{'identity':self.original['identity'],'quantity':2}])
        self.claim=self.claims.create(self.actor,{'request_key':'claim-base','physical_reference':'fictional-batch-base',
             'source_manifest':self.manifest,'physical_delivery_verified':True})
        self.approve()
        with self.database.transaction() as db:db.execute('INSERT INTO ss_receiving_settings(tenant_id,enabled) VALUES (%s,TRUE)',(self.tenant,))
        no_http=patch('requests.sessions.Session.request',side_effect=AssertionError('External HTTP forbidden in saga fixtures.'))
        no_http.start();self.addCleanup(no_http.stop)

    def approve(self):
        self.preview=self.service.binding_preview(self.actor,{'identity':self.original['identity'],'variation_id':'fixture-variation','location_id':'fixture-location'})
        self.binding=self.service.approve_binding(self.actor,self.preview['preview_id'],{'request_key':'mapping-'+uuid.uuid4().hex,
            'preview_hash':self.preview['preview_hash'],'identity_confirmed':True,'reason':'Fictional owner reviewed the exact card identity'})

    def plan(self, body=None):
        return self.service.create_plan(self.actor,body or plan_data(self.claim))

    def terms(self, plan, **extra):
        return {'request_key':'request-'+uuid.uuid4().hex,'revision':plan['revision'],'plan_hash':plan['plan_hash']}|extra

    def commit(self, plan, *, actor=None, body=None):
        return self.service.commit(actor or self.actor,plan['receiving_id'],body or self.terms(plan,confirmed=True,excluded_not_received_ack=True))

    def recover(self, plan, *, actor=None):return self.service.recover(actor or self.actor,plan['receiving_id'],self.terms(plan))

    def wallet(self):
        with self.database.transaction() as db:
            balance=db.execute('SELECT balance_cents FROM ss_customers WHERE tenant_id=%s AND id=%s',(self.tenant,self.customer)).fetchone()['balance_cents']
            entries=db.execute('SELECT count(*) AS n FROM ss_credit_entries WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']
        return balance,entries

    def trade(self):
        body=draft_data(customer_id=self.customer,request_key='trade-'+uuid.uuid4().hex)
        body['source_reference']='pc-receipt-'+uuid.uuid4().hex
        body.update(physical_intake_id=self.claim['physical_intake_id'],physical_manifest_hash=self.claim['manifest_hash'])
        body['lines'][0]['source_line_key']=plan_data(self.claim)['lines'][0]['source_line_key']
        return self.trades.create(self.actor,body)

    def accept_trade(self, trade):
        body=acceptance_data(self.customer,request_key='trade-accept-'+uuid.uuid4().hex,version=trade['version']);body['claim_reviewed']=True
        return self.trades.accept(self.actor,trade['trade_id'],body)

    def test_owner_mapping_approval_is_explicit_idempotent_and_preserves_catalog(self):
        before=deepcopy(self.provider.objects)
        body={'request_key':'approval-repeat','preview_hash':self.preview['preview_hash'],'identity_confirmed':True,'reason':'Fictional explicitly reviewed duplicate approval'}
        first=self.service.approve_binding(self.actor,self.preview['preview_id'],body)
        self.assertEqual(self.service.approve_binding(self.actor,self.preview['preview_id'],body),first)
        self.assertEqual(first['binding_id'],self.binding['binding_id']);self.assertEqual(self.provider.objects,before)
        self.assertEqual(self.provider.calls,[])
        self.assertNotIn('token',json.dumps(first))

    def test_global_and_tenant_write_switches_default_off_before_provider_admission(self):
        plan=self.plan();self.service.writes_enabled=False;access_before=len(self.provider.access_calls)
        with self.assertRaises(ReceivingError) as caught:self.commit(plan)
        self.assertEqual(caught.exception.code,'RECEIVING_DISABLED')
        self.assertEqual(len(self.provider.access_calls),access_before)
        self.service.writes_enabled=True
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET enabled=FALSE WHERE tenant_id=%s',(self.tenant,))
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])
        with self.database.transaction() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_steps WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],0)

    def test_plan_create_lost_response_and_different_pc_id_recover_permanent_operation(self):
        body=plan_data(self.claim);first=self.plan(body)
        self.assertEqual(self.plan(body),first)
        copied=deepcopy(body);copied['request_key']='other-PC';copied['lines'][0]['review_line_id']=str(uuid.uuid4())
        with self.assertRaises(ReceivingError) as caught:self.plan(copied)
        self.assertEqual(caught.exception.receiving_id,first['receiving_id'])
        self.assertEqual(self.service.request_status(self.actor,body['request_key']),{'found':True,'result':first})
        self.assertEqual(self.service.get(self.actor,first['receiving_id']),first)
        self.assertEqual(self.provider.calls,[])

    def test_successful_inventory_retains_old_sku_price_catalog_and_exact_receipt(self):
        before=deepcopy(self.provider.objects);plan=self.plan();terms=self.terms(plan,confirmed=True,excluded_not_received_ack=True)
        result=self.commit(plan,body=terms)
        self.assertEqual((result['state'],result['inventory_confirmed']),('received',True))
        receipt=result['receipt'];hashed=receipt.pop('receipt_hash');self.assertEqual(fingerprint(receipt),hashed);receipt['receipt_hash']=hashed
        self.assertEqual(receipt['confirmed_targets'][0]['target_sku'],'000000001234')
        self.assertEqual(receipt['confirmed_targets'][0]['confirmed_quantity'],2)
        self.assertFalse(receipt['catalog_write_performed']);self.assertEqual(self.provider.objects,before)
        self.assertEqual(self.commit(plan,body=terms),result);self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.service.request_status(self.actor,terms['request_key'])['result'],result)
        self.assertEqual(self.wallet(),(0,0))

    def test_lost_response_holds_same_encrypted_key_and_recovers_by_history_without_reissue(self):
        self.provider.behavior='lost_response';plan=self.plan();unknown=self.commit(plan)
        self.assertEqual((unknown['state'],unknown['inventory_confirmed']),('outcome_unknown',False));self.assertEqual(len(self.provider.calls),1)
        with self.database.transaction() as db:
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s',(self.tenant,plan['receiving_id'])).fetchone()
            lane=db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE tenant_id=%s',(self.tenant,)).fetchone()
        self.assertEqual(lane['state'],'unknown');self.assertNotIn('fictional-server-token',step['encrypted_body'])
        self.assertEqual(self.service._open(self.tenant,plan['receiving_id'],step),json.loads(self.provider.calls[0]))
        result=self.recover(plan)
        self.assertEqual(result['state'],'received');self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(result['receipt']['confirmed_targets'][0]['confirmed_quantity'],2)

    def test_admission_before_effect_blocks_new_trade_even_when_request_was_lost(self):
        trade=self.trade();self.provider.behavior='before_effect';plan=self.plan();unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown');self.assertEqual(self.provider.history_rows,[])
        with self.assertRaises(ServiceError):self.accept_trade(trade)
        self.assertEqual(self.wallet(),(0,0))
        self.assertEqual(self.claims.get(self.actor,self.claim['physical_intake_id'])['state'],'open')
        self.assertEqual(self.recover(plan)['state'],'outcome_unknown');self.assertEqual(len(self.provider.calls),1)
        with self.assertRaises(ReceivingError):self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Must not cancel uncertain provider admission'))

    def test_first_admission_financial_guard_serializes_during_inflight_http(self):
        trade=self.trade();plan=self.plan();entered=threading.Event();release=threading.Event()
        def hold():entered.set();self.assertTrue(release.wait(5))
        self.provider.after_dispatch=hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.commit,plan)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ServiceError):self.accept_trade(trade)
                self.assertEqual(self.wallet(),(0,0))
            finally:release.set()
            self.assertEqual(pending.result(5)['state'],'received')

    def test_trade_first_receiving_uses_server_accepted_identity_quantity_and_copied_line_ids(self):
        trade=self.trade();self.accept_trade(trade);receipt=self.trades.get(self.actor,trade['trade_id'])['accepted_receipt']
        body=plan_data(self.claim);body.pop('external_ownership_confirmed');body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['trade_id'],accepted_receipt_hash=fingerprint({}))
        body['accepted_receipt_hash']=self.trades.get(self.actor,trade['trade_id'])['receipt_hash']
        row=body['lines'][0];row.pop('identity');row['accepted_line_id']=receipt['lines'][0]['id'];row['review_line_id']=str(uuid.uuid4())
        result=self.commit(self.plan(body))
        self.assertEqual(result['state'],'received');self.assertEqual(result['receipt']['accepted_trade_id'],trade['trade_id'])
        self.assertEqual(self.wallet(),(1000,1))
        bad=deepcopy(body);bad['request_key']='forged-identity';bad['lines'][0]['identity']={'name':'forged'}
        with self.assertRaises(ServiceError):self.plan(bad)

    def test_current_mfa_role_session_and_mapping_revocation_gate_new_steps(self):
        plan=self.plan()
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])
        manager=self.actors['manager']
        with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,manager['staff_id']))
        with self.assertRaises(ServiceError):self.commit(plan,actor=manager)
        with self.database.transaction() as db:db.execute('UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp() WHERE token_hash=%s',(self.actor['session_hash'],))
        self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'revoke-mapping','version':1,'reason':'Fictional owner revoked before admission'})
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])

    def test_unknown_lane_survives_lease_expiry_and_blocks_different_claim(self):
        self.provider.behavior='lost_response';first=self.plan();self.commit(first)
        second_claim=self.claims.create(self.actor,{'request_key':'separate-physical-delivery','physical_reference':'another-actual-stack','source_manifest':self.manifest,
            'physical_delivery_verified':True,'additional_delivery_of':self.claim['physical_intake_id'],'additional_delivery_confirmed':True,
            'additional_delivery_reason':'Fictional separate physical stack arrived later'})
        second=self.plan(plan_data(second_claim))
        with self.database.transaction() as db:db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 day' WHERE tenant_id=%s AND id=%s",(self.tenant,first['receiving_id']))
        with self.assertRaises(ReceivingError) as caught:self.commit(second)
        self.assertEqual(caught.exception.code,'SELLER_OPERATION_PENDING');self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.recover(first)['state'],'received')
        self.provider.behavior='success';self.assertEqual(self.commit(second)['state'],'received')

    def test_postwrite_catalog_change_preserves_proven_stock_but_blocks_final_labels(self):
        plan=self.plan()
        def changed():self.provider.objects['fixture-variation']['item_variation_data']['price_money']['amount']=999
        self.provider.after_dispatch=changed
        result=self.commit(plan)
        self.assertEqual((result['state'],result['inventory_confirmed'],result['receipt']),('needs_review',True,None))
        self.provider.objects['fixture-variation']['item_variation_data']['price_money']['amount']=1000
        self.assertEqual(self.recover(plan)['state'],'received');self.assertEqual(len(self.provider.calls),1)

    def test_prewrite_cancel_revision_preserves_same_operation_and_old_plan_history(self):
        body=plan_data(self.claim);plan=self.plan(body)
        self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Fictional receipt review before any request'))
        revised_body=deepcopy(body);revised_body.update(request_key='revise-before-admission',revision=0);revised_body['lines'][0]['quantity']=1
        revised=self.service.revise(self.actor,plan['receiving_id'],revised_body)
        self.assertEqual((revised['receiving_id'],revised['revision'],revised['excluded_quantities'][body['lines'][0]['source_line_key']]),(plan['receiving_id'],1,1))
        self.assertEqual(self.commit(revised)['state'],'received')
        with self.database.transaction() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_plan_revisions WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],2)
        with self.assertRaises(ReceivingError):self.service.revise(self.actor,plan['receiving_id'],revised_body|{'request_key':'bad-late-edit','revision':1})

    def test_concurrent_commits_never_repeat_provider_write(self):
        plan=self.plan()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.commit(plan),range(2)))
        self.assertEqual(len(self.provider.calls),1)
        self.assertTrue(any(result['state']=='received' for result in results))
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'received')

    def test_final_audit_failure_recovers_exact_provider_history_without_second_call(self):
        plan=self.plan()
        from hosted.database import audit
        def failing(db,tenant,staff,action,*args,**kwargs):
            if action=='receiving-completed':raise RuntimeError('Fictional outcome database failure')
            return audit(db,tenant,staff,action,*args,**kwargs)
        with patch('hosted.receiving.audit',side_effect=failing):
            with self.assertRaises(RuntimeError):self.commit(plan)
        self.assertEqual(len(self.provider.calls),1)
        with self.database.transaction() as db:db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(self.tenant,plan['receiving_id']))
        self.assertEqual(self.recover(plan)['state'],'received');self.assertEqual(len(self.provider.calls),1)

    def test_tenant_cashier_buyer_request_key_and_immutable_journal_boundaries(self):
        plan=self.plan()
        for role in ('buyer','cashier'):
            with self.subTest(role=role),self.assertRaises(ServiceError):self.service.get(self.actors[role],plan['receiving_id'])
            with self.assertRaises(ServiceError):self.service.request_status(self.actors[role],'anything')
        with self.assertRaises(ServiceError):self.service.get(self.actor|{'tenant_id':uuid.uuid4()},plan['receiving_id'])
        self.commit(plan)
        for statement in ("DELETE FROM ss_receiving_steps WHERE tenant_id=%s", "UPDATE ss_receiving_steps SET api_version='2024-09-19' WHERE tenant_id=%s", "UPDATE ss_receiving_operations SET plan_hash=repeat('0',64) WHERE tenant_id=%s"):
            with self.subTest(statement=statement),self.assertRaises(psycopg.errors.RaiseException):
                with self.database.transaction() as db:db.execute(statement,(self.tenant,))
        self.database.migrate();self.assertTrue(self.database.ready());self.assertEqual(len(self.provider.calls),1)
