"""Independent two-step receiving probes in disposable PostgreSQL schemas.

All catalog/stock effects use the injected fictional provider; HTTP is forbidden.
"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import unittest
import uuid
from unittest.mock import patch
import psycopg

import test_hosted_receiving_pipeline_postgres as pipeline_fixtures
import test_hosted_settlement_postgres as postgres_fixtures
from hosted.receiving_plan import ReceivingError,fingerprint
from hosted.security import ServiceError
from test_hosted_receiving import plan_data


class PostgreSQLCatalogPipelineAdversarialTests(postgres_fixtures._PostgreSQLSettlementFixture):
    setUp=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.setUp
    approve=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.approve
    terms=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.terms
    wallet=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.wallet
    trade=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.trade
    accept_trade=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.accept_trade
    commit=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.commit
    recover=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.recover
    new_plan=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.new_plan
    authorize=pipeline_fixtures.PostgreSQLReceivingPipelineAcceptance.authorize

    def extra_delivery(self):
        return self.claims.create(self.actor,{'request_key':'delivery-'+uuid.uuid4().hex,
            'physical_reference':'fictional-separate-stack-'+uuid.uuid4().hex,'source_manifest':self.manifest,
            'physical_delivery_verified':True,'additional_delivery_of':self.claim['physical_intake_id'],
            'additional_delivery_confirmed':True,'additional_delivery_reason':'Fictional separate physical stack delivered later'})

    def operation(self,plan):
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',(self.tenant,plan['receiving_id'])).fetchone()
            steps=db.execute('SELECT ordinal,body_hash,encrypted_body,provider_key FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s ORDER BY ordinal',
                             (self.tenant,plan['receiving_id'])).fetchall()
        return row,steps

    def expire_lease(self,plan):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(self.tenant,plan['receiving_id']))

    def test_source_mapping_revocation_blocks_reuse_of_its_generated_price_authority(self):
        first=self.new_plan();self.commit(first)
        self.assertEqual(self.authorize(first)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))
        self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'revoke-family-source','version':1,
            'reason':'Fictional owner revoked the physical identity source mapping'})
        claim=self.extra_delivery();data=plan_data(claim);data['lines'][0]['selling_cents']=1250
        with self.assertRaises(ReceivingError):self.service.create_plan(self.actor,data)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_partial_catalog_effect_never_proves_inventory_and_keeps_original_journal(self):
        plan=self.new_plan(prices=(1250,1500))
        def partial(access,body_bytes,additions):
            self.provider.catalog_calls.append(bytes(body_bytes));body=json.loads(body_bytes)
            parent=deepcopy(body['batches'][0]['objects'][0]);parent['version']+=1;kept=[]
            for child in parent['item_data']['variations']:
                if child['id'].startswith('#'):
                    if any(row['id'].startswith('partial-created') for row in kept):continue
                    child['id']='partial-created-variation';child['version']=parent['version']
                kept.append(child);self.provider.objects[child['id']]=deepcopy(child)
            parent['item_data']['variations']=kept;self.provider.objects[parent['id']]=parent
            raise ReceivingError('Fictional incomplete catalog acknowledgement')
        with patch.object(self.provider,'dispatch_catalog',side_effect=partial):unknown=self.commit(plan)
        self.assertEqual((unknown['state'],unknown['catalog_confirmed'],unknown['inventory_confirmed'],unknown['receipt']),('outcome_unknown',False,False,None))
        row,steps=self.operation(plan);self.assertEqual([step['ordinal'] for step in steps],[0]);original=deepcopy(steps)
        self.assertIsNotNone(row['first_admitted_at'])
        for _ in range(2):
            result=self.recover(plan)
            self.assertEqual((result['state'],result['catalog_confirmed'],result['inventory_confirmed']),('outcome_unknown',False,False))
        self.assertEqual(self.operation(plan)[1],original)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertFalse(self.authorize(plan)['inventory_confirmed'])
        self.assertEqual(len(self.provider.calls),0)

    def test_request_history_and_replayed_catalog_response_never_claim_inventory_early(self):
        plan=self.new_plan();body=self.terms(plan,confirmed=True,excluded_not_received_ack=True)
        first=self.commit(plan,body=body)
        self.assertEqual((first['state'],first['inventory_confirmed'],first['receipt']),('catalog_confirmed',False,None))
        self.assertEqual(self.service.request_status(self.actor,body['request_key'])['result'],first)
        self.assertEqual(self.commit(plan,body=body),first)
        self.assertEqual([step['ordinal'] for step in self.operation(plan)[1]],[0])
        final=self.authorize(plan);self.assertTrue(final['inventory_confirmed'])
        historical=self.service.request_status(self.actor,body['request_key'])['result']
        self.assertEqual(historical,first)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_old_sibling_parent_tax_category_and_version_drift_block_inventory_after_catalog(self):
        sibling=deepcopy(self.provider.objects['fixture-variation']);sibling['id']='old-second-sibling'
        sibling['item_variation_data'].update(sku='OLD-STOCK-SKU',price_money={'amount':500,'currency':'USD'})
        self.provider.objects[sibling['id']]=sibling
        self.provider.objects['fixture-parent']['item_data']['variations'].append(deepcopy(sibling))
        plan=self.new_plan();self.commit(plan);baseline=deepcopy(self.provider.objects)
        mutations=[lambda objects:objects['fixture-parent']['item_data'].update(is_archived=True),
                   lambda objects:objects['fixture-tax']['tax_data'].update(percentage='9'),
                   lambda objects:objects['fixture-parent']['item_data'].update(categories=[{'id':'different-category'}]),
                   lambda objects:objects['fixture-parent']['item_data']['variations'][1]['item_variation_data'].update(price_money={'amount':501,'currency':'USD'}),
                   lambda objects:objects['fixture-parent']['item_data']['variations'].pop(1),
                   lambda objects:objects['fixture-parent'].update(version=1),
                   lambda objects:objects['fixture-parent']['item_data'].update(description='Altered old business description')]
        for index,mutate in enumerate(mutations):
            self.provider.objects=deepcopy(baseline);mutate(self.provider.objects)
            with self.subTest(index=index),self.assertRaises(ReceivingError):self.authorize(plan)
            self.assertEqual(len(self.provider.calls),0)
            self.assertEqual([step['ordinal'] for step in self.operation(plan)[1]],[0])
        self.provider.objects=baseline
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_stale_catalog_ack_after_read_recovery_does_not_admit_inventory(self):
        plan=self.new_plan();entered=threading.Event();release=threading.Event()
        def hold():
            entered.set()
            if not release.wait(10):raise AssertionError('Fictional catalog response release timed out')
        self.provider.after_catalog=hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.commit,plan)
            try:
                self.assertTrue(entered.wait(5));self.expire_lease(plan)
                recovered=self.recover(plan)
                self.assertEqual((recovered['state'],recovered['inventory_confirmed']),('catalog_confirmed',False))
            finally:release.set()
            late=pending.result(5)
        self.assertFalse(late['inventory_confirmed']);self.assertIsNone(late['receipt'])
        self.assertEqual([step['ordinal'] for step in self.operation(plan)[1]],[0])
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual(self.authorize(plan)['state'],'received')

    def test_current_session_revocation_during_second_step_preflight_admits_no_stock(self):
        plan=self.new_plan();self.commit(plan);manager=self.actors['manager'];original=self.provider.catalog
        def revoke_after_read(access,ids):
            result=original(access,ids)
            with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,manager['staff_id']))
            return result
        with patch.object(self.provider,'catalog',side_effect=revoke_after_read),self.assertRaises(ServiceError):self.authorize(plan,actor=manager)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual([step['ordinal'] for step in self.operation(plan)[1]],[0])
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'catalog_confirmed')

    def test_changed_principal_or_disabled_flags_between_steps_never_admit_inventory(self):
        plan=self.new_plan();self.commit(plan);original=deepcopy(self.provider.proof)
        for field,value in (('merchant_id','changed-merchant'),('environment','production'),('application_id','changed-application')):
            self.provider.proof=original|{field:value}
            with self.subTest(field=field),self.assertRaises(ReceivingError):self.authorize(plan)
        self.provider.proof=original
        for field in ('enabled','new_price_enabled'):
            with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET '+field+'=FALSE WHERE tenant_id=%s',(self.tenant,))
            with self.subTest(field=field),self.assertRaises(ReceivingError):self.authorize(plan)
            with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET '+field+'=TRUE WHERE tenant_id=%s',(self.tenant,))
        self.service.writes_enabled=False
        with self.assertRaises(ReceivingError):self.authorize(plan)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual([step['ordinal'] for step in self.operation(plan)[1]],[0])

    def test_crash_after_catalog_admission_keeps_financial_sql_guard_and_no_stock(self):
        trade=self.trade();plan=self.new_plan()
        class Interrupted(BaseException):pass
        with patch.object(self.provider,'dispatch_catalog',side_effect=Interrupted),self.assertRaises(Interrupted):self.commit(plan)
        row,steps=self.operation(plan);self.assertEqual(row['state'],'catalog_pending');self.assertIsNotNone(row['first_admitted_at'])
        self.assertEqual([step['ordinal'] for step in steps],[0])
        for state in ('accepted_cash','accepted_credit'):
            with self.subTest(state=state),self.assertRaisesRegex(psycopg.errors.RaiseException,'permanently blocks'):
                with self.database.transaction() as db:db.execute('UPDATE ss_trade_receipts SET state=%s WHERE tenant_id=%s AND id=%s',(state,self.tenant,trade['trade_id']))
        self.expire_lease(plan);self.assertEqual(self.recover(plan)['state'],'outcome_unknown')
        self.assertEqual(self.operation(plan)[1],steps)
        self.assertEqual((self.provider.catalog_calls,self.provider.calls,self.wallet()),([],[],(0,0)))

    def test_catalog_database_finalization_failure_recovers_without_catalog_reissue(self):
        from hosted.database import audit
        plan=self.new_plan()
        def fail(db,tenant,staff,action,*args,**kwargs):
            if action=='receiving-catalog-confirmed':raise RuntimeError('Fictional catalog outcome database failure')
            return audit(db,tenant,staff,action,*args,**kwargs)
        with patch('hosted.receiving_pipeline.audit',side_effect=fail),self.assertRaises(RuntimeError):self.commit(plan)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.expire_lease(plan)
        recovered=self.recover(plan)
        self.assertEqual((recovered['state'],recovered['inventory_confirmed']),('catalog_confirmed',False))
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_mismatched_adapter_request_hash_retains_unknown_without_generated_authority(self):
        plan=self.new_plan();original=self.provider.dispatch_catalog
        def wrong(access,body,additions):
            result=original(access,body,additions);result['proof']['request_hash']='0'*64;return result
        with patch.object(self.provider,'dispatch_catalog',side_effect=wrong):unknown=self.commit(plan)
        self.assertEqual((unknown['state'],unknown['catalog_confirmed'],unknown['inventory_confirmed']),('outcome_unknown',False,False))
        with self.database.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) AS n FROM ss_receiving_bindings WHERE tenant_id=%s AND approval_source='price_template'",(self.tenant,)).fetchone()['n'],0)
        self.assertEqual(self.recover(plan)['state'],'catalog_confirmed')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))

    def test_another_physical_claim_cannot_take_seller_lane_between_catalog_and_stock(self):
        first=self.new_plan();self.commit(first)
        data=plan_data(self.extra_delivery());data['lines'][0]['selling_cents']=1500
        second=self.service.create_plan(self.actor,data)
        with self.assertRaises(ReceivingError) as error:self.commit(second)
        self.assertEqual(error.exception.code,'SELLER_OPERATION_PENDING')
        self.assertIsNone(self.operation(second)[0]['first_admitted_at'])
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual(self.authorize(first)['state'],'received')
        self.assertEqual(self.commit(second)['state'],'catalog_confirmed')
        self.assertEqual(self.authorize(second)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(2,2))

    def test_same_id_category_definition_deletion_or_missing_reference_invalidates_owner_policy(self):
        from test_hosted_receiving_templates import approval
        self.service.templates.revoke(self.actor,self.price_template['template_id'],{'request_key':'replace-category-free-template','version':1,
            'reason':'Fictional owner will review the explicit category settings'})
        self.provider.objects['fixture-parent']['item_data']['categories']=[{'id':'fixture-category'}]
        self.provider.objects['fixture-category']={'id':'fixture-category','type':'CATEGORY','version':17,
            'category_data':{'name':'Original reviewed category'}}
        preview=self.service.templates.preview(self.actor,{'binding_id':self.binding['binding_id'],'variation_name_prefix':'Reviewed category price'})
        self.price_template=self.service.templates.approve(self.actor,preview['preview_id'],approval(preview))
        baseline=deepcopy(self.provider.objects)
        def mutate(objects,mode):
            if mode=='name':objects['fixture-category']['category_data']['name']='A different category meaning'
            elif mode=='deleted':objects['fixture-category']['is_deleted']=True
            else:objects.pop('fixture-category')
        for mode in ('name','deleted','missing'):
            self.provider.objects=deepcopy(baseline);mutate(self.provider.objects,mode)
            with self.subTest(stage='plan',mode=mode),self.assertRaises(ReceivingError):self.new_plan()
        self.provider.objects=baseline;plan=self.new_plan();self.commit(plan);created=deepcopy(self.provider.objects)
        for mode in ('name','deleted','missing'):
            self.provider.objects=deepcopy(created);mutate(self.provider.objects,mode)
            with self.subTest(stage='inventory',mode=mode),self.assertRaises(ReceivingError):self.authorize(plan)
            self.assertEqual(len(self.provider.calls),0)
        self.provider.objects=created;self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_source_revocation_during_catalog_http_records_proof_without_active_delegated_authority(self):
        plan=self.new_plan()
        self.provider.after_catalog=lambda:self.service.revoke_binding(self.actor,self.binding['binding_id'],{
            'request_key':'revoke-source-inflight','version':1,'reason':'Fictional owner revoked the family while provider request was in flight'})
        result=self.commit(plan)
        self.assertTrue(result['catalog_confirmed']);self.assertFalse(result['inventory_confirmed']);self.assertIsNone(result['receipt'])
        with self.database.transaction() as db:
            bindings=db.execute("SELECT revoked_at FROM ss_receiving_bindings WHERE tenant_id=%s AND approval_source='price_template'",(self.tenant,)).fetchall()
            template=db.execute('SELECT revoked_at FROM ss_receiving_price_templates WHERE tenant_id=%s AND id=%s',(self.tenant,self.price_template['template_id'])).fetchone()
        self.assertEqual(len(bindings),1);self.assertIsNotNone(bindings[0]['revoked_at']);self.assertIsNotNone(template['revoked_at'])
        with self.assertRaises(ReceivingError):self.authorize(plan)
        data=plan_data(self.extra_delivery());data['lines'][0]['selling_cents']=1250
        with self.assertRaises(ReceivingError):self.service.create_plan(self.actor,data)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))

    def test_retirement_during_plan_preflight_blocks_every_late_creation_and_preserves_wire_proof(self):
        body=plan_data(self.claim);body['lines'][0]['selling_cents']=1250
        entered=threading.Event();release=threading.Event();original=self.provider.catalog;held=[False]
        def hold_first(access,ids):
            result=original(access,ids)
            if not held[0]:
                held[0]=True;entered.set()
                if not release.wait(10):raise AssertionError('Fictional plan read release timed out')
            return result
        with patch.object(self.provider,'catalog',side_effect=hold_first),ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.service.create_plan,self.actor,body)
            try:
                self.assertTrue(entered.wait(5))
                proof=self.service.retire_plan_request(self.actor,body['request_key'],{'original_plan':body,'retire_uncreated_request':True})
                self.assertEqual(proof,{'request_key':body['request_key'],'retired':True,'state':'retired_before_creation',
                                        'receiving_id':None,'original_plan_sha256':fingerprint(body),'retirement_basis':'registered_original_wire'})
            finally:release.set()
            with self.assertRaises(ReceivingError) as error:pending.result(5)
            self.assertEqual(error.exception.code,'PLAN_REQUEST_RETIRED')
        self.assertEqual(self.service.request_status(self.actor,body['request_key']),{'found':True,'result':proof})
        self.assertEqual(self.service.retire_plan_request(self.actor,body['request_key'],{'original_plan':body,'retire_uncreated_request':True}),proof)
        for saved in (body,body|{'unknown_future_input':True}):
            with self.subTest(saved=saved),self.assertRaises(ReceivingError) as error:self.service.create_plan(self.actor,saved)
            self.assertEqual(error.exception.code,'PLAN_REQUEST_RETIRED')
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_operations WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],0)
        correction=deepcopy(body);correction['request_key']='corrected-'+uuid.uuid4().hex
        self.assertEqual(self.service.create_plan(self.actor,correction)['state'],'ready')
        self.assertEqual((self.provider.catalog_calls,self.provider.calls,self.wallet()),([],[],(0,0)))

    def test_retirement_cannot_replace_created_operation_changed_wire_or_another_action(self):
        body=plan_data(self.claim);body['lines'][0]['selling_cents']=1250
        plan=self.service.create_plan(self.actor,body)
        with self.assertRaises(ReceivingError) as error:
            self.service.retire_plan_request(self.actor,body['request_key'],{'original_plan':body,'retire_uncreated_request':True})
        self.assertEqual(error.exception.code,'CLAIM_WORKFLOW_CONFLICT');self.assertEqual(error.exception.receiving_id,plan['receiving_id'])
        changed=deepcopy(body);changed['lines'][0]['selling_cents']=1500
        with self.assertRaises(ReceivingError) as error:
            self.service.retire_plan_request(self.actor,body['request_key'],{'original_plan':changed,'retire_uncreated_request':True})
        self.assertEqual(error.exception.code,'PLAN_CHANGED')
        other=deepcopy(body);other['request_key']='approve-price-family'
        with self.assertRaises(ReceivingError) as error:
            self.service.retire_plan_request(self.actor,other['request_key'],{'original_plan':other,'retire_uncreated_request':True})
        self.assertEqual(error.exception.code,'PLAN_CHANGED')
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'ready')
        self.assertEqual((self.provider.catalog_calls,self.provider.calls),([],[]))

    def test_new_plan_pins_currency_names_without_changing_existing_sibling_name_or_price(self):
        original=deepcopy(self.provider.objects['fixture-variation']);plan=self.new_plan()
        row,_=self.operation(plan)
        self.assertEqual(row['plan']['catalog_additions'][0]['template']['name_format_version'],2)
        catalog=self.commit(plan)
        self.assertEqual((catalog['state'],catalog['inventory_confirmed']),('catalog_confirmed',False))
        created=self.provider.objects[catalog['catalog_resolutions'][0]['variation_id']]
        self.assertEqual(created['item_variation_data']['name'],'Sticker price · $12.50')
        self.assertEqual(created['item_variation_data']['price_money'],{'amount':1250,'currency':'USD'})
        self.assertEqual(json.loads(created['item_variation_data']['user_data'])['name_format'],2)
        self.assertEqual(self.provider.objects['fixture-variation'],original)
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))


if __name__=='__main__':unittest.main(verbosity=2)
