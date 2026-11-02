"""Public result budgets before admission, using only disposable PG fixtures."""
from copy import deepcopy
from unittest.mock import patch
import uuid
from intake_manifest import capture_manifest
from hosted.receiving_plan import ReceivingError,canonical,public_response_bytes
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
import test_hosted_receiving_pipeline_postgres as pipeline_fixture
import test_hosted_receiving_postgres as receiving_fixture
from test_hosted_receiving import plan_data


class PostgreSQLReceivingProjectionAcceptance(_PostgreSQLSettlementFixture):
    approve=receiving_fixture.PostgreSQLReceivingAcceptance.approve
    terms=receiving_fixture.PostgreSQLReceivingAcceptance.terms
    wallet=receiving_fixture.PostgreSQLReceivingAcceptance.wallet
    commit=receiving_fixture.PostgreSQLReceivingAcceptance.commit
    recover=receiving_fixture.PostgreSQLReceivingAcceptance.recover
    new_plan=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.new_plan
    authorize=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.authorize

    def setUp(self):pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.setUp(self)

    def counts(self):
        with self.database.transaction() as db:
            return tuple(db.execute('SELECT count(*) AS n FROM '+table+' WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']
                         for table in ('ss_receiving_operations','ss_receiving_steps','ss_receiving_plan_revisions'))

    def test_oversize_plan_has_no_operation_and_can_retire_or_retry_exact_wire(self):
        body=plan_data(self.claim)
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',1),self.assertRaises(ReceivingError) as caught:
            self.service.create_plan(self.actor,body)
        self.assertEqual(caught.exception.code,'RECEIVING_PROJECTION_LIMIT')
        self.assertEqual(self.counts(),(0,0,0));self.assertEqual((self.provider.calls,self.provider.catalog_calls),([],[]))
        self.assertFalse(self.service.request_status(self.actor,body['request_key'])['found'])
        proof=self.service.retire_plan_request(self.actor,body['request_key'],{'original_plan':body,'retire_uncreated_request':True})
        self.assertEqual(proof['retirement_basis'],'registered_original_wire')
        with self.assertRaises(ReceivingError) as retired:self.service.create_plan(self.actor,body)
        self.assertEqual(retired.exception.code,'PLAN_REQUEST_RETIRED')
        body['request_key']='reviewed-correction';plan=self.service.create_plan(self.actor,body)
        self.assertEqual(plan['state'],'ready');self.assertEqual(self.counts(),(1,0,1))

    def test_oversize_revision_cannot_replace_the_original_reviewed_plan(self):
        body=plan_data(self.claim);plan=self.service.create_plan(self.actor,body)
        body.update(request_key='revise-over-budget',revision=plan['revision'])
        body['client_label_profile']={'stock':'4x2','layout':'fold','dpi':300}
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',1),self.assertRaises(ReceivingError) as caught:
            self.service.revise(self.actor,plan['receiving_id'],body)
        self.assertEqual(caught.exception.code,'RECEIVING_PROJECTION_LIMIT')
        self.assertEqual(self.service.get(self.actor,plan['receiving_id']),plan)
        self.assertEqual(self.counts(),(1,0,1));self.assertEqual(self.provider.calls,[])

    def test_old_same_price_plan_is_rechecked_before_any_inventory_admission(self):
        plan=self.service.create_plan(self.actor,plan_data(self.claim))
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',1),self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.counts(),(1,0,1));self.assertEqual(self.provider.calls,[])
        with self.database.transaction() as db:
            self.assertIsNone(db.execute('SELECT first_admitted_at FROM ss_receiving_operations WHERE tenant_id=%s',(self.tenant,)).fetchone()['first_admitted_at'])
        self.assertEqual(self.commit(plan)['state'],'received')

    def test_old_new_price_plan_is_rechecked_before_catalog_and_before_stock(self):
        plan=self.new_plan()
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',1),self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.counts(),(1,0,1));self.assertEqual(self.provider.catalog_calls,[])
        self.assertEqual(self.commit(plan)['state'],'catalog_confirmed')
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',1),self.assertRaises(ReceivingError):self.authorize(plan)
        self.assertEqual(self.counts(),(1,1,1));self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'catalog_confirmed')
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_unbounded_or_extra_catalog_projection_evidence_holds_the_admitted_step(self):
        original=self.provider.dispatch_catalog
        def unbounded(*args):
            result=original(*args);result['resolutions'][0]['variation_id']='v'*256
            return result
        self.provider.dispatch_catalog=unbounded
        plan=self.new_plan();result=self.commit(plan)
        self.assertEqual(result['state'],'outcome_unknown');self.assertIsNone(result['receipt'])
        self.assertFalse(result['catalog_confirmed']);self.assertEqual(self.provider.calls,[])
        self.provider.dispatch_catalog=original
        self.assertEqual(self.recover(plan)['state'],'catalog_confirmed')
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_raw_catalog_fields_cannot_be_projected_through_a_proof(self):
        original=self.provider.dispatch_catalog
        def extra(*args):
            result=original(*args);result['proof']['raw_provider_body']={'private':'fictional raw body never public'}
            return result
        self.provider.dispatch_catalog=extra
        plan=self.new_plan();result=self.commit(plan)
        self.assertEqual(result['state'],'outcome_unknown');self.assertIsNone(result['catalog_proof'])
        self.assertNotIn(b'raw_provider_body',canonical(result));self.assertEqual(self.provider.calls,[])
        self.provider.dispatch_catalog=original
        recovered=self.recover(plan);self.assertEqual(recovered['state'],'catalog_confirmed')
        self.assertNotIn(b'raw_provider_body',canonical(recovered));self.assertEqual(self.authorize(plan)['state'],'received')

    def test_schema_reapply_preserves_admitted_journal_lane_and_default_gates(self):
        self.provider.catalog_behavior='lost_response';plan=self.new_plan();unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown')
        def snapshot():
            with self.database.transaction() as db:
                self.assertRegex(db.execute('SELECT current_schema() AS name').fetchone()['name'],r'^ss_test_[a-f0-9]{32}$')
                return (dict(db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s',(self.tenant,)).fetchone()),
                    dict(db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s',(self.tenant,)).fetchone()),
                    dict(db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE tenant_id=%s',(self.tenant,)).fetchone()),
                    dict(db.execute('SELECT * FROM ss_receiving_settings WHERE tenant_id=%s',(self.tenant,)).fetchone()))
        before=snapshot();self.assertFalse(before[3]['new_parent_enabled'])
        self.database.migrate();self.database.migrate();self.assertTrue(self.database.ready())
        self.assertEqual(snapshot(),before);self.assertEqual(self.wallet(),(0,0))
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,0))
        recovered=self.recover(plan);self.assertEqual(recovered['state'],'catalog_confirmed')
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls)),(1,1))

    def test_legal_thousand_rows_long_opaque_ids_and_exact_sku_complete_and_recover(self):
        self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'replace-short-fixture-binding','version':1,
            'reason':'Fictional source begins with maximum retained opaque identifiers'})
        variation=deepcopy(self.provider.objects['fixture-variation']);parent=deepcopy(self.provider.objects['fixture-parent'])
        variation_id='v'*255;parent_id='p'*255;sku='Exact SKU / '+('x'*88)
        self.assertEqual(len(sku),100)
        variation['id']=variation_id;variation['item_variation_data'].update(item_id=parent_id,sku=sku)
        parent['id']=parent_id;parent['item_data']['variations']=[deepcopy(variation)]
        self.provider.objects={variation_id:variation,parent_id:parent,'fixture-tax':self.provider.objects['fixture-tax']}
        preview=self.service.binding_preview(self.actor,{'identity':self.original['identity'],'variation_id':variation_id,'location_id':'fixture-location'})
        self.service.approve_binding(self.actor,preview['preview_id'],{'request_key':'large-opaque-source-binding','preview_hash':preview['preview_hash'],
            'identity_confirmed':True,'reason':'Fictional owner reviewed the exact retained SKU and source identity'})
        manifest=capture_manifest('manual',[{'identity':self.original['identity'],'quantity':1000}])
        claim=self.claims.create(self.actor,{'request_key':'large-physical-stack','physical_reference':'Fictional thousand-card stack',
            'source_manifest':manifest,'physical_delivery_verified':True})
        body=plan_data(claim);base=body['lines'][0];body['lines']=[dict(base,review_line_id=str(uuid.uuid4()),quantity=1) for _ in range(1000)]
        self.assertLessEqual(len(canonical(body)),512*1024)
        plan=self.service.create_plan(self.actor,body);terms=self.terms(plan,confirmed=True,excluded_not_received_ack=True)
        final=self.commit(plan,body=terms);self.assertEqual(final['state'],'received')
        wire=public_response_bytes({'found':True,'result':final})
        self.assertGreater(len(wire),2_000_000);self.assertLessEqual(len(wire),8*1024*1024)
        targets=final['receipt']['confirmed_targets'];self.assertEqual(len(targets),1000)
        self.assertEqual({(row['variation_id'],row['parent_id'],row['target_sku'],row['confirmed_quantity']) for row in targets},{(variation_id,parent_id,sku,1)})
        self.assertEqual(self.service.request_status(self.actor,terms['request_key'])['result'],final)
        self.assertEqual(self.service.get(self.actor,plan['receiving_id']),final)
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(self.wallet(),(0,0))
