"""Independent receiving response/admission probes in disposable PostgreSQL.

Fictional catalog/stock provider only; the shared fixture forbids external HTTP.
"""
from copy import deepcopy
import json
import uuid
from unittest.mock import Mock, patch
from cryptography.fernet import Fernet

from intake_manifest import capture_manifest
from hosted.app import create_app
from hosted.receiving_plan import ReceivingError, canonical
from hosted.security import ServiceError
from station_cloud import CloudClient
import test_hosted_settlement_postgres as postgres_fixture
import test_hosted_receiving_pipeline_postgres as pipeline_fixture
from test_hosted_receiving import plan_data
from test_hosted_receiving_projection_budget import opaque


class PostgreSQLReceivingWireAdversarialTests(postgres_fixture._PostgreSQLSettlementFixture):
    setUp=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.setUp
    approve=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.approve
    terms=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.terms
    wallet=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.wallet
    trade=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.trade
    accept_trade=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.accept_trade
    commit=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.commit
    new_plan=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.new_plan
    authorize=pipeline_fixture.PostgreSQLReceivingPipelineAcceptance.authorize

    def durable_state(self,ident):
        with self.database.transaction() as db:
            operation=db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',
                (self.tenant,ident)).fetchone()
            steps=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s ORDER BY ordinal',
                (self.tenant,ident)).fetchall()
            entries=db.execute('SELECT * FROM ss_credit_entries WHERE tenant_id=%s ORDER BY id',(self.tenant,)).fetchall()
            lots=db.execute('SELECT * FROM ss_credit_lots WHERE tenant_id=%s ORDER BY id',(self.tenant,)).fetchall()
            return operation,steps,entries,lots

    def test_measured_limit_rejection_preserves_old_unadmitted_plan_and_financial_eligibility(self):
        plan=self.service.create_plan(self.actor,plan_data(self.claim));before=self.durable_state(plan['receiving_id'])
        maximum=self.service._projection_budget(before[0]['plan'],plan['receiving_id'],plan['revision'])
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',maximum-1),self.assertRaises(ReceivingError) as caught:
            self.commit(plan)
        self.assertEqual(caught.exception.code,'RECEIVING_PROJECTION_LIMIT')
        self.assertEqual(self.durable_state(plan['receiving_id']),before)
        self.assertEqual((self.provider.catalog_calls,self.provider.calls,self.wallet()),([],[],(0,0)))
        # An engineering-size rejection is not a provider admission. It must not
        # accidentally occupy the claim or block an otherwise valid trade.
        trade=self.trade();accepted=self.accept_trade(trade)
        self.assertEqual(accepted['state'],'accepted_credit');self.assertGreater(self.wallet()[0],0)

    def test_measured_continuation_rejection_retains_catalog_and_financial_block_then_resumes_once(self):
        plan=self.new_plan();self.assertEqual(self.commit(plan)['state'],'catalog_confirmed')
        before=self.durable_state(plan['receiving_id'])
        maximum=self.service._projection_budget(before[0]['plan'],plan['receiving_id'],plan['revision'])
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',maximum-1),self.assertRaises(ReceivingError) as caught:
            self.authorize(plan)
        self.assertEqual(caught.exception.code,'RECEIVING_PROJECTION_LIMIT')
        self.assertEqual(self.durable_state(plan['receiving_id']),before)
        self.assertEqual((len(self.provider.catalog_calls),self.provider.calls,self.wallet()),(1,[],(0,0)))
        with self.assertRaises(ServiceError):self.accept_trade(self.trade())
        self.assertEqual(self.wallet(),(0,0))
        self.assertEqual(self.authorize(plan)['state'],'received')
        self.assertEqual((len(self.provider.catalog_calls),len(self.provider.calls),self.wallet()),(1,1,(0,0)))

    def test_legal_unicode_thousand_row_completed_http_status_is_exact_and_desktop_readable(self):
        self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'replace-ascii-source',
            'version':1,'reason':'Fictional boundary test uses long opaque provider identifiers'})
        parent=deepcopy(self.provider.objects['fixture-parent']);variation=deepcopy(self.provider.objects['fixture-variation'])
        variation_id,parent_id=opaque(301),opaque(302);sku='\U0001f0a1'*97+'?>~'
        self.assertEqual((len(variation_id),len(parent_id),len(sku)),(255,255,100))
        variation['id']=variation_id;variation['item_variation_data'].update(item_id=parent_id,sku=sku)
        parent['id']=parent_id;parent['item_data']['variations']=[deepcopy(variation)]
        self.provider.objects={parent_id:parent,variation_id:variation,'fixture-tax':self.provider.objects['fixture-tax']}
        preview=self.service.binding_preview(self.actor,{'identity':self.original['identity'],
            'variation_id':variation_id,'location_id':'fixture-location'})
        self.service.approve_binding(self.actor,preview['preview_id'],{'request_key':'approve-unicode-source',
            'preview_hash':preview['preview_hash'],'identity_confirmed':True,
            'reason':'Fictional owner reviewed the exact Unicode SKU and physical identity'})
        manifest=capture_manifest('manual',[{'identity':self.original['identity'],'quantity':1000}])
        claim=self.claims.create(self.actor,{'request_key':'unicode-large-stack','physical_reference':'Fictional Unicode budget stack',
            'source_manifest':manifest,'physical_delivery_verified':True})
        body=plan_data(claim);base=body['lines'][0]
        body['lines']=[dict(base,review_line_id=str(uuid.uuid4()),quantity=1) for _ in range(1000)]
        self.assertLessEqual(len(canonical(body)),512*1024)
        plan=self.service.create_plan(self.actor,body);terms=self.terms(plan,confirmed=True,excluded_not_received_ack=True)
        final=self.commit(plan,body=terms);self.assertEqual(final['state'],'received')
        expected={'found':True,'result':final};expected_wire=canonical(expected)
        self.assertGreater(len(expected_wire),2_000_000);self.assertLessEqual(len(expected_wire),8*1024*1024)
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://wire-pg.fixture.invalid',
            'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.database)
        # Existing enrolled fixture session is rechecked by domain SQL on every
        # read. This replaces HTTP token decoding only, not domain authorization.
        app.extensions['ss_auth'].resolve=Mock(return_value=self.actor)
        # Routes close over the created service; bind that service's typed reads
        # to our actual-operation instance with the same private database.
        http_service=app.extensions['ss_receiving']
        http_service.get=self.service.get;http_service.request_status=self.service.request_status
        client=app.test_client();headers={'Authorization':'Bearer fictional-fixture-session'}
        reply=client.get('/api/receiving/requests/'+terms['request_key'],base_url='https://wire-pg.fixture.invalid',headers=headers)
        self.assertEqual(reply.status_code,200);self.assertEqual(reply.data,expected_wire)
        self.assertEqual(reply.json,expected)
        class Response:
            status_code=reply.status_code
            content=reply.data
            def json(self):return json.loads(self.content)
        transport=Mock();transport.request.return_value=Response()
        cloud=CloudClient.__new__(CloudClient);cloud.transport=transport
        recovered=cloud._call('GET','/api/receiving/requests/'+terms['request_key'])
        rows=recovered['result']['receipt']['confirmed_targets']
        self.assertEqual(len(rows),1000);self.assertEqual(sum(row['confirmed_quantity'] for row in rows),1000)
        self.assertEqual({(row['variation_id'],row['parent_id'],row['target_sku']) for row in rows},{(variation_id,parent_id,sku)})
        get_reply=client.get('/api/receiving/'+plan['receiving_id'],base_url='https://wire-pg.fixture.invalid',headers=headers)
        self.assertEqual(get_reply.data,canonical(final));self.assertEqual((len(self.provider.calls),self.wallet()),(1,(0,0)))
        transport.request.assert_called_once()

