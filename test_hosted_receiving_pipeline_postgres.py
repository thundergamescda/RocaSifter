"""Catalog/stock pipeline with an injected fictional provider in private schemas."""
from copy import deepcopy
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest
from hosted.receiving_pipeline import CatalogReceiving
from hosted.receiving_plan import ReceivingError, fingerprint
from hosted.security import ServiceError
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
import test_hosted_receiving_postgres as receiving_fixture
from test_hosted_receiving import plan_data


class PostgreSQLReceivingPipelineAcceptance(_PostgreSQLSettlementFixture):
    approve=receiving_fixture.PostgreSQLReceivingAcceptance.approve
    terms=receiving_fixture.PostgreSQLReceivingAcceptance.terms
    wallet=receiving_fixture.PostgreSQLReceivingAcceptance.wallet
    trade=receiving_fixture.PostgreSQLReceivingAcceptance.trade
    accept_trade=receiving_fixture.PostgreSQLReceivingAcceptance.accept_trade
    commit=receiving_fixture.PostgreSQLReceivingAcceptance.commit
    recover=receiving_fixture.PostgreSQLReceivingAcceptance.recover
    _physical_mapping=receiving_fixture.PostgreSQLReceivingAcceptance._physical_mapping
    _retire_original_mapping=receiving_fixture.PostgreSQLReceivingAcceptance._retire_original_mapping
    _approve_physical_mapping=receiving_fixture.PostgreSQLReceivingAcceptance._approve_physical_mapping
    _physical_plan=receiving_fixture.PostgreSQLReceivingAcceptance._physical_plan

    def setUp(self):
        receiving_fixture.PostgreSQLReceivingAcceptance.setUp(self)
        self.service=CatalogReceiving(self.database,self.provider,self.cipher,writes_enabled=True)
        preview=self.service.templates.preview(self.actor,{'binding_id':self.binding['binding_id'],'variation_name_prefix':'Sticker price'})
        self.price_template=self.service.templates.approve(self.actor,preview['preview_id'],{'request_key':'approve-price-family',
            'preview_hash':preview['preview_hash'],'preserve_existing_tax_configuration_confirmed':True,
            'preserve_existing_siblings_confirmed':True,'source_identity_confirmed':True,
            'approval_reason':'Fictional owner reviewed the current existing tax and catalog settings','policy_reference':preview['facts']['policy_reference']})
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET new_price_enabled=TRUE WHERE tenant_id=%s',(self.tenant,))

    def new_plan(self,*,prices=(1250,)):
        body=plan_data(self.claim);base=body['lines'][0];body['lines']=[]
        for cents in prices:
            row=deepcopy(base);row.update(review_line_id=str(uuid.uuid4()),selling_cents=cents,quantity=2 if len(prices)==1 else 1)
            body['lines'].append(row)
        return self.service.create_plan(self.actor,body)

    def authorize(self,plan,actor=None):
        return self.service.authorize(actor or self.actor,plan['receiving_id'],self.terms(plan,resume_same_operation=True))

    def arbitrary_source(self,sku):
        self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'replace-fixture-base','version':1,'reason':'Fictional fixture begins with another store source SKU'})
        self.provider.objects['fixture-variation']['item_variation_data']['sku']=sku
        self.provider.objects['fixture-parent']['item_data']['variations'][0]['item_variation_data']['sku']=sku
        self.approve()
        preview=self.service.templates.preview(self.actor,{'binding_id':self.binding['binding_id'],'variation_name_prefix':'Sticker price'})
        self.assertEqual(preview['facts']['base_sku'],sku)
        self.price_template=self.service.templates.approve(self.actor,preview['preview_id'],{'request_key':'arbitrary-source-price-family',
            'preview_hash':preview['preview_hash'],'preserve_existing_tax_configuration_confirmed':True,
            'preserve_existing_siblings_confirmed':True,'source_identity_confirmed':True,
            'approval_reason':'Fictional owner reviewed the exact arbitrary source SKU and settings','policy_reference':preview['facts']['policy_reference']})

    def _approve_physical_template(self):
        preview=self.service.templates.preview(self.actor,{'binding_id':self.binding['binding_id'],'variation_name_prefix':'Sticker price'})
        self.price_template=self.service.templates.approve(self.actor,preview['preview_id'],{
            'request_key':'physical-price-family-'+uuid.uuid4().hex,'preview_hash':preview['preview_hash'],
            'preserve_existing_tax_configuration_confirmed':True,'preserve_existing_siblings_confirmed':True,
            'source_identity_confirmed':True,'approval_reason':'Fictional owner reviewed the full physical source and preserved catalog policy',
            'policy_reference':preview['facts']['policy_reference']})
        return preview

    def _physical_family(self):
        _,preview,_,binding=self._approve_physical_mapping()
        self.binding=binding;self._approve_physical_template()
        return deepcopy(preview['facts']['physical_ownership'])

    def _new_physical_plan(self,ownership,*,prices=(1250,)):
        body=self._physical_plan(ownership);base=body['lines'][0];body['lines']=[]
        for cents in prices:
            row=deepcopy(base);row.update(review_line_id=str(uuid.uuid4()),selling_cents=cents,quantity=2 if len(prices)==1 else 1)
            body['lines'].append(row)
        return self.service.create_plan(self.actor,body)

    def test_new_price_creates_preserved_sibling_then_freshly_admits_exact_stock(self):
        original=deepcopy(self.provider.objects);plan=self.new_plan()
        self.assertEqual(plan['targets'][0]['action'],'create_price');self.assertIsNone(plan['targets'][0]['variation_id'])
        catalog=self.commit(plan)
        self.assertEqual((catalog['state'],catalog['catalog_confirmed'],catalog['inventory_confirmed']),('catalog_confirmed',True,False))
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        final=self.authorize(plan)
        self.assertEqual((final['state'],final['inventory_confirmed']),('received',True))
        self.assertTrue(final['receipt']['catalog_write_performed'])
        target=final['receipt']['confirmed_targets'][0]
        self.assertEqual(target['source_sku'],'000000001234');self.assertEqual(target['target_sku'],plan['targets'][0]['target_sku'])
        self.assertNotEqual(target['binding_id'],target['source_binding_id']);self.assertFalse(target['variation_id'].startswith('#'))
        self.assertEqual(self.provider.objects['fixture-variation'],original['fixture-variation'])
        self.assertEqual(self.provider.objects['fixture-parent']['item_data']['tax_ids'],original['fixture-parent']['item_data']['tax_ids'])
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls),self.wallet()),(1,1,(0,0)))
        created=self.provider.objects[target['variation_id']]['item_variation_data']
        self.assertEqual(created['name'],'Sticker price · $12.50');self.assertEqual(json.loads(created['user_data'])['name_format'],2)

    def test_lost_catalog_ack_is_read_recovered_without_stock_or_duplicate_sibling(self):
        self.provider.catalog_behavior='lost_response';plan=self.new_plan();unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown');self.assertEqual(len(self.provider.calls),0)
        recovered=self.recover(plan)
        self.assertEqual(recovered['state'],'catalog_confirmed');self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual(self.authorize(plan)['state'],'received');self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_legacy_addition_without_name_pin_recovers_original_body_after_upgrade(self):
        original_prepare=self.service._prepare
        def legacy_prepare(*args):
            prepared,access=original_prepare(*args)
            for addition in prepared['catalog_additions']:addition['template'].pop('name_format_version',None)
            return prepared,access
        with patch.object(self.service,'_prepare',side_effect=legacy_prepare):plan=self.new_plan()
        self.provider.catalog_behavior='lost_response';self.commit(plan)
        with self.database.transaction() as db:
            before=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(self.tenant,plan['receiving_id'])).fetchone()
        body=self.service._open(self.tenant,plan['receiving_id'],before)
        appended=body['batches'][0]['objects'][0]['item_data']['variations'][-1]['item_variation_data']
        self.assertEqual(appended['name'],'Sticker price 1250');self.assertNotIn('name_format',json.loads(appended['user_data']))
        self.assertEqual(self.recover(plan)['state'],'catalog_confirmed');self.assertEqual(self.authorize(plan)['state'],'received')
        with self.database.transaction() as db:
            after=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(self.tenant,plan['receiving_id'])).fetchone()
        self.assertEqual((after['encrypted_body'],after['body_hash'],after['provider_key']),(before['encrypted_body'],before['body_hash'],before['provider_key']))
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def _check_split_counts(self,prices):
        plan=self.new_plan(prices=prices);self.commit(plan);final=self.authorize(plan)
        targets=final['receipt']['confirmed_targets'];self.assertEqual([row['confirmed_quantity'] for row in targets],[1,1])
        self.assertEqual(len({row['variation_id'] for row in targets}),len(set(prices)))
        self.assertEqual(len(json.loads(self.provider.calls[0])['changes']),len(set(prices)))

    def test_same_new_price_coalesces_catalog_but_preserves_both_review_rows(self):self._check_split_counts((1250,1250))

    def test_existing_and_new_price_counts_are_separate(self):self._check_split_counts((1000,1250))

    def test_two_new_prices_create_distinct_preserved_siblings_and_inventory_changes(self):self._check_split_counts((1250,1500))

    def test_new_price_flag_template_revocation_tax_change_and_sku_collision_stop_before_write(self):
        plan=self.new_plan()
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET new_price_enabled=FALSE WHERE tenant_id=%s',(self.tenant,))
        with self.assertRaises(ReceivingError):self.commit(plan)
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET new_price_enabled=TRUE WHERE tenant_id=%s',(self.tenant,))
        self.provider.objects['fixture-tax']['tax_data']['percentage']='7'
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.provider.objects['fixture-tax']['tax_data']['percentage']='6'
        duplicate=deepcopy(self.provider.objects['fixture-variation']);duplicate['id']='foreign-candidate';duplicate['item_variation_data']['sku']=plan['targets'][0]['target_sku'];self.provider.objects[duplicate['id']]=duplicate
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.provider.objects.pop(duplicate['id'])
        self.service.templates.revoke(self.actor,self.price_template['template_id'],{'request_key':'revoke-price-template','version':1,'reason':'Fictional policy revoked before any new catalog admission'})
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual((self.provider.catalog_calls,self.provider.calls),([],[]))

    def test_stale_mfa_or_template_revocation_between_steps_preserves_catalog_no_stock(self):
        plan=self.new_plan();self.commit(plan)
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.authorize(plan)
        self.assertEqual(len(self.provider.calls),0)
        with self.database.transaction() as db:db.execute('UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp() WHERE token_hash=%s',(self.actor['session_hash'],))
        self.service.templates.revoke(self.actor,self.price_template['template_id'],{'request_key':'revoke-after-catalog','version':1,'reason':'Fictional owner revoked the family before stock admission'})
        with self.assertRaises(ReceivingError):self.authorize(plan)
        self.assertEqual(len(self.provider.calls),0)
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'catalog_confirmed')

    def test_catalog_admission_blocks_new_credit_even_before_any_stock_call(self):
        trade=self.trade();self.provider.catalog_behavior='before_effect';plan=self.new_plan();self.commit(plan)
        with self.assertRaises(ServiceError):self.accept_trade(trade)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls),self.wallet()),(1,0,(0,0)))
        self.assertEqual(self.recover(plan)['state'],'outcome_unknown')

    def test_inventory_lost_response_recovers_matching_new_variation_without_catalog_rewrite(self):
        plan=self.new_plan();self.commit(plan);self.provider.behavior='lost_response'
        self.assertEqual(self.authorize(plan)['state'],'outcome_unknown')
        final=self.recover(plan)
        self.assertEqual(final['state'],'received');self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))
        self.assertEqual(final['receipt']['confirmed_targets'][0]['confirmed_quantity'],2)

    def test_concurrent_catalog_commit_and_authorize_admit_each_step_once(self):
        plan=self.new_plan()
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:self.commit(plan),range(2)))
        with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:self.authorize(plan),range(2)))
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'received')

    def test_distinct_owner_and_manager_sessions_authorize_one_stock_key_then_manager_recovers(self):
        plan=self.new_plan();self.commit(plan);self.provider.behavior='lost_response'
        owner,manager=self.actor,self.actors['manager']
        self.assertNotEqual(owner['staff_id'],manager['staff_id']);self.assertNotEqual(owner['session_hash'],manager['session_hash'])
        requests=[self.terms(plan,resume_same_operation=True),self.terms(plan,resume_same_operation=True)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.service.authorize,actor,plan['receiving_id'],body) for actor,body in zip((owner,manager),requests)]
            results=[future.result(5) for future in futures]
        self.assertTrue(all(result['state'] in ('inventory_pending','outcome_unknown') for result in results))
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))
        with self.database.transaction() as db:
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(self.tenant,plan['receiving_id'])).fetchone()
            self.assertIn(step['authorized_by'],(owner['staff_id'],manager['staff_id']))
            self.assertIn(step['session_hash'],(owner['session_hash'],manager['session_hash']))
            db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(owner['session_hash'],))
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '20 minutes' WHERE token_hash=%s",(manager['session_hash'],))
            db.execute('UPDATE ss_receiving_settings SET enabled=FALSE,new_price_enabled=FALSE WHERE tenant_id=%s',(self.tenant,))
        saved_body=self.service._open(self.tenant,plan['receiving_id'],step)
        self.assertEqual(json.loads(self.provider.calls[0]),saved_body);self.assertEqual(saved_body['idempotency_key'],str(step['provider_key']))
        self.service.writes_enabled=False
        final=self.service.recover(manager,plan['receiving_id'],self.terms(plan))
        self.assertEqual(final['state'],'received');self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))
        self.assertEqual(final['receipt']['confirmed_targets'][0]['confirmed_quantity'],2)
        self.assertEqual(final['receipt']['client_label_profile'],plan['client_label_profile'])
        with self.database.transaction() as db:
            after=db.execute('SELECT body_hash,provider_key FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(self.tenant,plan['receiving_id'])).fetchone()
        self.assertEqual((after['body_hash'],after['provider_key']),(step['body_hash'],step['provider_key']))

    def test_arbitrary_source_sku_spaces_and_punctuation_are_preserved_exactly(self):
        from square_variations import variant_sku
        sku=' SPORTS / 07-25 !#$ '
        self.arbitrary_source(sku);original=deepcopy(self.provider.objects['fixture-variation'])
        plan=self.new_plan();self.assertEqual(plan['targets'][0]['source_sku'],sku)
        self.assertEqual(plan['targets'][0]['target_sku'],variant_sku(sku,1250))
        self.commit(plan);final=self.authorize(plan);target=final['receipt']['confirmed_targets'][0]
        self.assertEqual((target['source_sku'],target['target_sku']),(sku,variant_sku(sku,1250)))
        self.assertRegex(target['target_sku'],r'^[0-9]{12}$');self.assertEqual(self.provider.objects['fixture-variation'],original)

    def test_unicode_source_sku_uses_exact_legacy_hash_bytes_without_changing_source(self):
        from square_variations import variant_sku
        sku='Édition / カード'
        self.arbitrary_source(sku);original=deepcopy(self.provider.objects['fixture-variation'])
        plan=self.new_plan();self.commit(plan);target=self.authorize(plan)['receipt']['confirmed_targets'][0]
        self.assertEqual((target['source_sku'],target['target_sku']),(sku,variant_sku(sku,1250)))
        self.assertEqual(self.provider.objects['fixture-variation'],original)

    def test_arbitrary_source_generated_collision_is_review_only_without_catalog_or_stock(self):
        from square_variations import variant_sku
        sku='STORE-A / CUSTOM-99'
        self.arbitrary_source(sku)
        foreign=deepcopy(self.provider.objects['fixture-variation']);foreign['id']='conflicting-existing-variation'
        foreign['item_variation_data']['sku']=variant_sku(sku,1250);self.provider.objects[foreign['id']]=foreign
        with self.assertRaises(ReceivingError) as caught:self.new_plan()
        self.assertEqual(caught.exception.code,'SKU_COLLISION_REVIEW_REQUIRED')
        self.assertEqual((self.provider.catalog_calls,self.provider.calls),([],[]))

    def test_v2_catalog_then_separate_inventory_preserves_owner_derived_binding_receipt_and_old_siblings(self):
        original=deepcopy(self.provider.objects);ownership=self._physical_family();plan=self._new_physical_plan(ownership)
        self.assertEqual(plan['physical_ownership_version'],2)
        self.assertEqual(plan['targets'][0]['physical_ownership'],ownership)
        self.assertNotIn('identity',plan['targets'][0])
        catalog=self.commit(plan)
        self.assertEqual((catalog['state'],catalog['inventory_confirmed']),('catalog_confirmed',False))
        self.assertIsNone(catalog['receipt']);self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        resolution=catalog['catalog_resolutions'][0]
        self.assertEqual(resolution['physical_ownership'],ownership)
        created=self.provider.objects[resolution['variation_id']]['item_variation_data'];marker=json.loads(created['user_data'])
        self.assertEqual(marker,{'ss_price':3,'i':plan['targets'][0]['identity_hash'],'k':ownership['key'],
            'o':plan['receiving_id'],'r':plan['targets'][0]['review_line_id'],'n':2})
        self.assertEqual(len(created['user_data'].encode('ascii')),248)
        self.assertEqual(self.provider.objects['fixture-variation'],original['fixture-variation'])
        parent=deepcopy(self.provider.objects['fixture-parent']);parent['version']=original['fixture-parent']['version']
        appended=parent['item_data']['variations'].pop()
        self.assertEqual(appended['id'],resolution['variation_id']);self.assertEqual(parent,original['fixture-parent'])
        with self.database.transaction() as db:
            binding=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,resolution['binding_id'])).fetchone()
        self.assertEqual(binding['physical_ownership'],ownership);self.assertEqual(binding['approval_source'],'price_template')
        self.assertEqual(str(binding['template_id']),self.price_template['template_id']);self.assertIsNone(binding['revoked_at'])
        self.assertEqual((str(binding['id']),binding['version'],binding['evidence_hash']),
            (resolution['binding_id'],resolution['binding_version'],resolution['binding_evidence_hash']))
        self.assertNotEqual(str(binding['id']),self.binding['binding_id'])
        final=self.authorize(plan);receipt=final['receipt'];target=receipt['confirmed_targets'][0]
        self.assertEqual((final['state'],final['inventory_confirmed'],receipt['physical_ownership_version']),('received',True,2))
        self.assertEqual(target['physical_ownership'],ownership);self.assertEqual(target['source_binding_id'],self.binding['binding_id'])
        self.assertEqual((target['binding_id'],target['binding_version']),(str(binding['id']),binding['version']))
        self.assertEqual(receipt['catalog_resolutions'][0]['physical_ownership'],ownership)
        self.assertEqual(receipt['receipt_hash'],fingerprint({key:value for key,value in receipt.items() if key!='receipt_hash'}))
        change=json.loads(self.provider.calls[0])['changes'][0]['adjustment']
        self.assertEqual((change['catalog_object_id'],change['quantity']),(target['variation_id'],'2'))
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls),self.wallet()),(1,1,(0,0)))
        self.assertEqual(self.service.get(self.actor,plan['receiving_id']),final)

    def test_v2_lost_ack_recovers_original_encrypted_body_key_hash_and_owned_resolution(self):
        ownership=self._physical_family();plan=self._new_physical_plan(ownership)
        self.provider.catalog_behavior='lost_response';unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown');self.assertEqual(len(self.provider.calls),0)
        with self.database.transaction() as db:
            before=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(self.tenant,plan['receiving_id'])).fetchone()
        body=self.service._open(self.tenant,plan['receiving_id'],before)
        self.assertEqual(json.loads(self.provider.catalog_calls[0]),body)
        self.assertEqual((body['idempotency_key'],fingerprint(body)),(str(before['provider_key']),before['body_hash']))
        marker=json.loads(body['batches'][0]['objects'][0]['item_data']['variations'][-1]['item_variation_data']['user_data'])
        self.assertEqual((marker['ss_price'],marker['k']),(3,ownership['key']))
        recovered=self.recover(plan)
        self.assertEqual((recovered['state'],recovered['catalog_proof']['kind']),('catalog_confirmed','catalog_state_match'))
        self.assertEqual(recovered['catalog_resolutions'][0]['physical_ownership'],ownership)
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual(len(self.provider.objects['fixture-parent']['item_data']['variations']),2)
        with self.database.transaction() as db:
            after=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=0',(self.tenant,plan['receiving_id'])).fetchone()
        for field in ('encrypted_body','provider_key','body_hash','api_version'):
            self.assertEqual(after[field],before[field])
        final=self.authorize(plan)
        self.assertEqual(final['receipt']['confirmed_targets'][0]['physical_ownership'],ownership)
        self.assertEqual((final['state'],len(self.provider.catalog_calls),len(self.provider.calls)),('received',1,1))

    def test_v2_same_price_aliases_preserve_all_review_ids_and_one_owned_stock_target(self):
        ownership=self._physical_family();plan=self._new_physical_plan(ownership,prices=(1250,1250))
        reviews={target['review_line_id'] for target in plan['targets']};self.assertEqual(len(reviews),2)
        catalog=self.commit(plan);self.assertEqual(len(catalog['catalog_resolutions']),1)
        resolution=catalog['catalog_resolutions'][0]
        self.assertEqual(set(resolution['review_line_ids']),reviews);self.assertIn(resolution['review_line_id'],reviews)
        self.assertEqual(resolution['physical_ownership'],ownership)
        self.assertEqual(catalog['catalog_resolutions_hash'],fingerprint(catalog['catalog_resolutions']))
        self.assertTrue(all(target['physical_ownership']==ownership for target in catalog['targets']))
        final=self.authorize(plan);targets=final['receipt']['confirmed_targets']
        self.assertEqual({target['review_line_id'] for target in targets},reviews)
        self.assertEqual([target['confirmed_quantity'] for target in targets],[1,1])
        self.assertEqual({target['variation_id'] for target in targets},{resolution['variation_id']})
        self.assertEqual({target['binding_id'] for target in targets},{resolution['binding_id']})
        self.assertTrue(all(target['physical_ownership']==ownership for target in targets))
        changes=json.loads(self.provider.calls[0])['changes']
        self.assertEqual(len(changes),1);self.assertEqual(changes[0]['adjustment']['quantity'],'2')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_v2_cannot_inherit_legacy_template_authority_without_fresh_physical_source_and_template(self):
        _,preview,_=self._physical_mapping();ownership=preview['facts']['physical_ownership']
        with self.assertRaises(ReceivingError) as caught:self._new_physical_plan(ownership)
        self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED')
        _,fresh,_,binding=self._approve_physical_mapping();self.binding=binding
        with self.assertRaises(ReceivingError) as caught:self._new_physical_plan(fresh['facts']['physical_ownership'])
        self.assertEqual(caught.exception.code,'CATALOG_MAPPING_REVIEW_REQUIRED')
        self.assertEqual((self.provider.catalog_calls,self.provider.calls),([],[]))
        physical=self._approve_physical_template()
        self.assertEqual(physical['facts']['physical_ownership'],ownership)
        plan=self._new_physical_plan(ownership)
        self.assertEqual((plan['physical_ownership_version'],plan['state']),(2,'ready'))
        self.assertEqual(plan['targets'][0]['template_id'],self.price_template['template_id'])
        self.assertEqual((self.provider.catalog_calls,self.provider.calls),([],[]))

    def test_v2_late_ack_after_template_retirement_allocates_only_revoked_owned_child_without_stock(self):
        ownership=self._physical_family();plan=self._new_physical_plan(ownership)
        def retire_after_provider_effect():
            self.service.templates.revoke(self.actor,self.price_template['template_id'],{
                'request_key':'retire-v2-before-late-ack','version':self.price_template['version'],
                'reason':'Fictional owner retires this physical family before acknowledgement allocation'})
        self.provider.after_catalog=retire_after_provider_effect
        catalog=self.commit(plan)
        self.assertEqual((catalog['state'],catalog['catalog_confirmed'],catalog['inventory_confirmed']),('catalog_confirmed',True,False))
        resolution=catalog['catalog_resolutions'][0]
        with self.database.transaction() as db:
            template=db.execute('SELECT * FROM ss_receiving_price_templates WHERE tenant_id=%s AND id=%s',(self.tenant,self.price_template['template_id'])).fetchone()
            children=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND template_id=%s',(self.tenant,self.price_template['template_id'])).fetchall()
        self.assertIsNotNone(template['revoked_at']);self.assertEqual(len(children),1)
        child=children[0];self.assertEqual(str(child['id']),resolution['binding_id'])
        self.assertEqual(child['physical_ownership'],ownership);self.assertEqual(resolution['physical_ownership'],ownership)
        self.assertEqual((child['revoked_at'],child['revoked_by'],child['revoke_reason']),
            (template['revoked_at'],template['revoked_by'],template['revoke_reason']))
        with self.assertRaises(ReceivingError):self.authorize(plan)
        with self.database.transaction() as db:
            stock=db.execute('SELECT 1 FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1',(self.tenant,plan['receiving_id'])).fetchone()
        self.assertIsNone(stock);self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        self.assertEqual(self.recover(plan)['catalog_resolutions'],catalog['catalog_resolutions'])
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls),self.wallet()),(1,0,(0,0)))
