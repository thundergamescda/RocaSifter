"""Actual PostgreSQL export checks in disposable ss_test_<uuid> fixture schemas.

No application/merchant tables outside that schema and no provider requests.
"""
from datetime import timedelta
from copy import deepcopy
import json
import threading
import unittest
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from hosted.auth import Auth
from hosted.ledger import Ledger
from hosted.privacy import Privacy, DATASET_NAMES, Limits
from hosted.privacy_test_fixtures import request
from hosted.security import ServiceError,password_hash
from hosted.trades import Trades
from hosted.credit_lots import record_credit_lot
from test_hosted_privacy import decode
from test_hosted_settlement import order,returned_order,payment_refunds
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import draft_data,acceptance_data,card_line
from hosted.intake import IntakeClaims
from test_intake_fixtures import fresh_mfa_actor,claim_draft
from hosted.receiving import Receiving
from hosted.receiving_plan import fingerprint
from test_hosted_receiving import plan_data
from test_receiving_fixtures import FictionalReceivingProvider


class PostgreSQLPrivacyAcceptance(_PostgreSQLSettlementFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture_password='Fictional-export-password-1234'
        cls.fixture_hash=password_hash(cls.fixture_password)

    def setUp(self):
        super().setUp()
        self.reconcile(order())
        self.settlement.reconcile_return(self.tenant,'sale-1',returned_order(),payment_refunds())
        with self.database.transaction() as db:
            owner=db.execute("SELECT id FROM ss_staff WHERE tenant_id=%s AND role='owner'",(self.tenant,)).fetchone()
            self.staff=owner['id'];self.email='fixture-owner-'+self.tenant.hex+'@example.invalid'
            db.execute('UPDATE ss_staff SET email=%s,password_hash=%s WHERE tenant_id=%s AND id=%s',
                       (self.email,self.fixture_hash,self.tenant,self.staff))
            self.slug=db.execute('SELECT slug FROM ss_tenants WHERE id=%s',(self.tenant,)).fetchone()['slug']
            db.execute("UPDATE ss_customers SET display_name='Fictional rights customer',contact='own-export@example.invalid' WHERE tenant_id=%s AND id=%s",(self.tenant,self.customer))
            for index in range(250):
                entry=uuid.uuid4();key='fixture-history-'+str(index)
                db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason,staff_id)
                              VALUES (%s,%s,%s,1,%s,'adjust_add',%s,%s,'Fictional export history',%s)''',
                           (entry,self.tenant,self.customer,4001+index,'request:'+key,'FICTIONAL-'+str(index),self.staff))
                db.execute('UPDATE ss_customers SET balance_cents=%s,version=version+1 WHERE tenant_id=%s AND id=%s',
                           (4001+index,self.tenant,self.customer))
                record_credit_lot(db,self.tenant,self.customer,entry)
                db.execute("INSERT INTO ss_requests(tenant_id,key,body_hash,result,operation) VALUES (%s,%s,%s,%s,'ledger')",
                           (self.tenant,key,'0'*64,Jsonb({'entry_id':str(entry),'balance_cents':4001+index})))
            self.other_customer=uuid.uuid4()
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,contact,square_customer_id,privacy_basis,balance_cents) VALUES (%s,%s,'OTHER-CUSTOMER-NAME','OTHER-CUSTOMER-CONTACT','other-square-customer','fixture-only',123)",
                       (self.other_customer,self.tenant))
            other_entry=uuid.uuid4()
            db.execute("INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason) VALUES (%s,%s,%s,123,123,'opening','other-entry','OTHER-CUSTOMER-REFERENCE','OTHER-CUSTOMER-REASON')",
                       (other_entry,self.tenant,self.other_customer))
            self.other_entry=other_entry
            foreign=uuid.uuid4();self.foreign_customer=uuid.uuid4()
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'OTHER-TENANT-NAME')",(foreign,'foreign-'+foreign.hex))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,contact,privacy_basis) VALUES (%s,%s,'FOREIGN-CUSTOMER-NAME','FOREIGN-CUSTOMER-CONTACT','fixture-only')",
                       (self.foreign_customer,foreign))
            db.execute("INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details) VALUES (%s,%s,'fixture-customer-note',%s,%s)",
                       (self.tenant,self.staff,str(self.customer),Jsonb({'kind':'fixture','access_token':'DO-NOT-EXPORT-SECRET'})))
            db.execute("INSERT INTO ss_audit(tenant_id,action,entity_id,details) VALUES (%s,'fixture-other-note',%s,%s)",
                       (self.tenant,str(self.other_customer),Jsonb({'customer_id':str(self.other_customer),'reference':'OTHER-CUSTOMER-AUDIT'})))
        self.auth=Auth(self.database,encryption_keys=Fernet.generate_key().decode())
        self.token,_=self.auth.login({'store_slug':self.slug,'email':self.email,'password':self.fixture_password},'127.0.0.1')
        self.actor=self.auth.resolve(self.token)
        self.service=Privacy(self.database,authorize_session=self.auth.resolve,proof_key='fictional-export-proof-key')
        trades=Trades(self.database)
        # Keep the rights-request owner unenrolled so its original recent-login
        # tests retain their meaning. A separate fictional enrolled manager
        # authorizes the newly guarded trade fixture; never bypass claim/MFA.
        with self.database.transaction() as db:
            manager={'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':'manager'}
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'fixture-export-trade-manager@example.invalid','fixture-invalid-password-hash','manager')",
                       (manager['staff_id'],self.tenant))
            manager=fresh_mfa_actor(db,manager)
        self.trade_manager=manager
        original=draft_data(customer_id=self.customer,request_key='fictional-trade-create')
        excluded=card_line(name='EXCLUDED-ORIGINAL-CUSTOMER-ITEM',quantity=1,cents=50)
        excluded['included']=False
        original['lines'].append(excluded)
        claimed,self.intake_claim=claim_draft(self.database,manager,original)
        created=trades.create(manager,claimed)
        # A competing other-customer draft may refer to the still-open claim.
        # Only the finally bound accepted customer owns its all-source export.
        competing=deepcopy(claimed)
        competing.update(customer_id=str(self.other_customer),request_key='other-customer-competing-draft',source_reference='other-customer-competing-source')
        competing['lines']=competing['lines'][:1]
        self.competing_trade_id=trades.create(manager,competing)['trade_id']
        accepted=trades.accept(manager,created['trade_id'],acceptance_data(self.customer,request_key='fictional-trade-accept')|{'claim_reviewed':True})
        self.trade_id=created['trade_id'];self.trade_hash=accepted['receipt_hash']
        no_network=patch('requests.sessions.Session.request',side_effect=AssertionError('Provider HTTP is forbidden in privacy acceptance.'))
        no_network.start();self.addCleanup(no_network.stop)

    def test_claim_original_exclusions_requests_and_audit_export_only_for_final_bound_customer(self):
        trades=Trades(self.database)
        # This unrelated customer's opening entry existed only to test export
        # exclusion. Complete its fictional provenance before asking the domain
        # to append a new trade credit; do not bypass the money-origin guard.
        with self.database.transaction() as db:
            record_credit_lot(db,self.tenant,self.other_customer,self.other_entry)
        # Same-tenant unrelated source is intentionally unlinked to a customer.
        unlinked_data=draft_data(customer_id=self.customer,request_key='unlinked-intake')
        unlinked_data['source_reference']='unlinked-physical-intake'
        unlinked_data['lines'][0]['identity']['name']='UNLINKED-ORIGINAL-CLAIM'
        _,unlinked=claim_draft(self.database,self.trade_manager,unlinked_data)
        other_data=draft_data(customer_id=self.other_customer,request_key='other-accepted-intake')
        other_data['source_reference']='other-accepted-physical-source'
        other_data['lines'][0]['identity']['name']='OTHER-CUSTOMER-CLAIM-ORIGINAL'
        other_data,other_claim=claim_draft(self.database,self.trade_manager,other_data)
        other_trade=trades.create(self.trade_manager,other_data)
        trades.accept(self.trade_manager,other_trade['trade_id'],acceptance_data(self.other_customer,request_key='other-intake-accept')|{'claim_reviewed':True})
        # A foreign tenant's accepted claim must not enter either projection.
        with self.database.transaction() as db:
            foreign=db.execute('SELECT tenant_id FROM ss_customers WHERE id=%s',(self.foreign_customer,)).fetchone()['tenant_id']
            foreign_manager={'tenant_id':foreign,'staff_id':uuid.uuid4(),'role':'manager'}
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'foreign-intake-manager@example.invalid','fixture-invalid-password-hash','manager')",
                       (foreign_manager['staff_id'],foreign))
            foreign_manager=fresh_mfa_actor(db,foreign_manager)
        foreign_data=draft_data(customer_id=self.foreign_customer,request_key='foreign-accepted-intake')
        foreign_data['source_reference']='foreign-physical-intake'
        foreign_data['lines'][0]['identity']['name']='FOREIGN-CLAIM-ORIGINAL'
        foreign_data,foreign_claim=claim_draft(self.database,foreign_manager,foreign_data)
        foreign_trade=trades.create(foreign_manager,foreign_data)
        trades.accept(foreign_manager,foreign_trade['trade_id'],acceptance_data(self.foreign_customer,request_key='foreign-intake-accept')|{'claim_reviewed':True})

        before=self.business_snapshot()
        output=self.begin();chunks=list(output.iter_bytes());manifest,records,footer=decode(chunks)
        own_id=self.intake_claim['physical_intake_id']
        self.assertEqual(footer['record_counts']['physical_intakes'],1)
        self.assertEqual({row['id'] for row in records['physical_intakes']},{own_id})
        source=records['physical_intakes'][0]['source_manifest']
        self.assertEqual(len(source['rows']),2)
        self.assertIn('EXCLUDED-ORIGINAL-CUSTOMER-ITEM',{row['identity']['name'] for row in source['rows']})
        self.assertEqual(records['physical_intakes'][0]['bound_trade_id'],self.trade_id)
        self.assertTrue(records['physical_intake_requests'])
        self.assertTrue(all(row['claim_id']==own_id for row in records['physical_intake_requests']))
        self.assertTrue(any(row['entity_id']==own_id and row['action']=='physical-intake-created' for row in records['audit']))
        self.assertEqual(records['trade_receipts'][0]['physical_intake_id'],own_id)
        self.assertTrue(all(row['source_line_key'] for row in records['trade_lines']))
        body=b''.join(chunks)
        for forbidden in ('UNLINKED-ORIGINAL-CLAIM','OTHER-CUSTOMER-CLAIM-ORIGINAL','FOREIGN-CLAIM-ORIGINAL','DO-NOT-EXPORT-SECRET',
                          unlinked['physical_intake_id'],other_claim['physical_intake_id'],foreign_claim['physical_intake_id']):
            self.assertNotIn(forbidden.encode(),body)
        self.assertEqual(before,self.business_snapshot())

        # This customer has a competing draft pointing to our consumed claim;
        # that draft must not expose our original rows or claim request copies.
        other_output=self.begin(customer=self.other_customer)
        other_chunks=list(other_output.iter_bytes());_,other_records,other_footer=decode(other_chunks)
        self.assertEqual(other_footer['record_counts']['physical_intakes'],1)
        self.assertEqual({row['id'] for row in other_records['physical_intakes']},{other_claim['physical_intake_id']})
        self.assertNotIn(own_id,{row['claim_id'] for row in other_records['physical_intake_requests']})
        self.assertFalse(any(row['entity_id']==own_id for row in other_records['audit']))
        self.assertNotIn(b'EXCLUDED-ORIGINAL-CUSTOMER-ITEM',b''.join(other_chunks))

    def begin(self,actor=None,customer=None):
        selected=customer or self.customer
        return self.service.begin(actor or self.actor,selected,request(actor or self.actor,selected),self.token)

    def receiving_fixture(self):
        with self.database.transaction() as db:
            owner={'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':'owner'}
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,'fictional-invalid-hash','owner')",
                       (owner['staff_id'],self.tenant,'receiving-owner-'+uuid.uuid4().hex+'@example.invalid'))
            owner=fresh_mfa_actor(db,owner)
            db.execute('INSERT INTO ss_receiving_settings(tenant_id,enabled) VALUES (%s,TRUE)',(self.tenant,))
        provider=FictionalReceivingProvider(self.database,self.tenant)
        service=Receiving(self.database,provider,Fernet(Fernet.generate_key()),writes_enabled=True)
        accepted=Trades(self.database).get(self.trade_manager,self.trade_id)['accepted_receipt']
        line=next(row for row in accepted['lines'] if row['included'])
        preview=service.binding_preview(owner,{'identity':line['identity'],'variation_id':'fixture-variation','location_id':'fixture-location'})
        service.approve_binding(owner,preview['preview_id'],{'request_key':'approve-export-mapping','preview_hash':preview['preview_hash'],
            'identity_confirmed':True,'reason':'Fictional owner reviewed export receiving fixture'})
        data=plan_data(self.intake_claim);data.pop('external_ownership_confirmed')
        data.update(workflow_kind='accepted_trade',accepted_trade_id=self.trade_id,accepted_receipt_hash=self.trade_hash)
        data['lines'][0].pop('identity');data['lines'][0]['accepted_line_id']=line['id']
        return service,provider,owner,data

    def test_receiving_v4_complete_history_tenant_edges_and_competing_customer_are_projected_safely(self):
        service,provider,owner,data=self.receiving_fixture()
        plan=service.create_plan(owner,data)
        revised=deepcopy(data);revised.update(request_key='export-revision',revision=0);revised['lines'][0]['quantity']=1
        plan=service.revise(owner,plan['receiving_id'],revised)
        terms={'request_key':'export-commit','revision':plan['revision'],'plan_hash':plan['plan_hash']}
        provider.behavior='lost_response'
        unknown=service.commit(owner,plan['receiving_id'],terms|{'confirmed':True,'excluded_not_received_ack':True})
        self.assertEqual(unknown['state'],'outcome_unknown')
        received=service.recover(owner,plan['receiving_id'],terms|{'request_key':'export-recover'})
        self.assertEqual(received['state'],'received')
        for index in range(121):
            service.commit(owner,plan['receiving_id'],terms|{'request_key':'export-replay-'+str(index),'confirmed':True,'excluded_not_received_ack':True})
        # A separately attested inventory-only delivery has no customer binding.
        extra=IntakeClaims(self.database).create(owner,{'request_key':'export-inventory-only','physical_reference':'INVENTORY-ONLY-PRIVATE',
            'source_manifest':self.intake_claim['source_manifest'],'physical_delivery_verified':True,
            'additional_delivery_of':self.intake_claim['physical_intake_id'],'additional_delivery_confirmed':True,
            'additional_delivery_reason':'Fictional separate stack owned by store, no customer transaction'})
        inventory=service.create_plan(owner,plan_data(extra))
        with self.database.transaction() as db:
            foreign_tenant=db.execute('SELECT tenant_id FROM ss_customers WHERE id=%s',(self.foreign_customer,)).fetchone()['tenant_id']
            foreign={'tenant_id':foreign_tenant,'staff_id':uuid.uuid4(),'role':'manager'}
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'foreign-receiving@example.invalid','fictional-invalid','manager')",(foreign['staff_id'],foreign_tenant))
            foreign=fresh_mfa_actor(db,foreign)
        foreign_claim=IntakeClaims(self.database).create(foreign,{'request_key':'foreign-claim','physical_reference':'FOREIGN-RECEIVING-CLAIM',
            'source_manifest':self.intake_claim['source_manifest'],'physical_delivery_verified':True})
        with self.database.transaction() as db:
            # Reuse operation and request IDs in another tenant to exercise every
            # tenant join, especially the request-outcome compound association.
            foreign_plan={'customer_id':str(self.foreign_customer),'raw':'FOREIGN-RECEIVING-PAYLOAD'}
            db.execute('''INSERT INTO ss_receiving_operations(tenant_id,id,claim_id,workflow_kind,plan_hash,plan,created_by,updated_by)
                          VALUES (%s,%s,%s,'inventory_only',%s,%s,%s,%s)''',
                       (foreign_tenant,plan['receiving_id'],foreign_claim['physical_intake_id'],fingerprint(foreign_plan),Jsonb(foreign_plan),foreign['staff_id'],foreign['staff_id']))
            db.execute("INSERT INTO ss_receiving_requests(tenant_id,request_key,action,intent_hash,operation_id,result) VALUES (%s,'export-commit','commit',%s,%s,%s)",
                       (foreign_tenant,'0'*64,plan['receiving_id'],Jsonb({'raw':'FOREIGN-RECEIVING-REQUEST'})))
            db.execute("INSERT INTO ss_receiving_request_outcomes(tenant_id,request_key,result) VALUES (%s,'export-commit',%s)",
                       (foreign_tenant,Jsonb({'raw':'FOREIGN-RECEIVING-OUTCOME'})))
            db.execute("INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details) VALUES (%s,%s,'foreign-receiving',%s,%s)",
                       (foreign_tenant,foreign['staff_id'],plan['receiving_id'],Jsonb({'kind':'FOREIGN-RECEIVING-AUDIT'})))
            step=db.execute('SELECT encrypted_body,session_hash,authority_hash FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s',
                            (self.tenant,plan['receiving_id'])).fetchone()
        before=self.business_snapshot();output=self.begin();chunks=list(output.iter_bytes());header,records,footer=decode(chunks)
        self.assertEqual((header['version'],footer['version'],len(footer['record_counts'])),(4,4,26))
        self.assertEqual(footer['bytes_before_footer'],sum(len(chunk) for chunk in chunks[:-1]))
        self.assertEqual({row['id'] for row in records['receiving_operations']},{plan['receiving_id']})
        self.assertEqual(len(records['receiving_plan_revisions']),2)
        self.assertGreater(len(records['receiving_requests']),121)
        self.assertEqual(len({row['request_key'] for row in records['receiving_requests']}),len(records['receiving_requests']))
        self.assertEqual(len(records['receiving_steps']),1)
        self.assertTrue(records['receiving_attempts']);self.assertTrue(records['receiving_request_outcomes'])
        self.assertTrue(records['receiving_exceptions'])
        current=records['receiving_operations'][0]
        self.assertEqual(current['receipt']['confirmed_targets'][0]['confirmed_quantity'],1)
        self.assertEqual(current['receipt']['receipt_hash'],received['receipt']['receipt_hash'])
        self.assertEqual(current['inventory_proof']['kind'],'history_adjustments')
        self.assertTrue(any(row['entity_id']==plan['receiving_id'] for row in records['audit']))
        body=b''.join(chunks)
        for marker in ('FOREIGN-RECEIVING','INVENTORY-ONLY-PRIVATE',inventory['receiving_id'],owner['session_hash'],step['encrypted_body'],step['authority_hash']):
            self.assertNotIn(marker.encode(),body)
        self.assertEqual(before,self.business_snapshot());self.assertEqual(len(provider.calls),1)
        confirmation={key:footer[key] for key in ('start_audit_id','content_chain_sha256','record_counts')}
        self.assertTrue(self.service.confirm(self.actor,output.id,confirmation,self.token)['verified'])
        other=self.begin(customer=self.other_customer);_,other_records,other_footer=decode(list(other.iter_bytes()))
        self.assertEqual(other_footer['kind'],'complete')
        self.assertTrue(all(not other_records[name] for name in DATASET_NAMES if name.startswith('receiving_')))

    def test_receiving_v4_corrupt_customer_binding_or_nested_json_cannot_complete(self):
        service,provider,owner,data=self.receiving_fixture();plan=service.create_plan(owner,data)
        with self.database.transaction() as db:
            saved=db.execute('SELECT plan FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',(self.tenant,plan['receiving_id'])).fetchone()['plan']
        for corruption in ('binding','nested'):
            damaged=deepcopy(saved)
            if corruption=='binding':damaged['accepted_trade_id']=self.competing_trade_id
            else:damaged['future_catalog_snapshot']={'customer_id':str(self.other_customer),'name':'OTHER-RECEIVING-NESTED-SENTINEL'}
            with self.database.transaction() as db:
                db.execute('UPDATE ss_receiving_operations SET plan=%s,plan_hash=%s WHERE tenant_id=%s AND id=%s',
                           (Jsonb(damaged),fingerprint(damaged),self.tenant,plan['receiving_id']))
            if corruption=='binding':
                with self.assertRaises(ServiceError):self.begin()
            else:
                output=self.begin();lines=list(output.iter_bytes())
                self.assertEqual(json.loads(lines[-1])['kind'],'error')
                self.assertNotIn(b'OTHER-RECEIVING-NESTED-SENTINEL',b''.join(lines))
        self.assertEqual(provider.calls,[])

    def test_catalog_step_projection_exports_only_hashes_resolved_fields_and_safe_admission_metadata(self):
        # This is a read-projection fixture, not proof of a provider catalog write.
        service,provider,owner,data=self.receiving_fixture();plan=service.create_plan(owner,data)
        proof={'kind':'catalog_batch_ack','request_hash':'1'*64,'response_hash':'2'*64,
               'objects':[{'raw':'DO-NOT-EXPORT-FULL-CATALOG-PROOF'}]}
        resolution={'review_line_id':data['lines'][0]['review_line_id'],'source_line_key':data['lines'][0]['source_line_key'],
                    'identity_hash':'3'*64,'target_sku':'000000009999','variation_id':'fictional-created-variation',
                    'parent_id':'fixture-parent','selling_cents':1250,'currency':'USD',
                    'template_id':str(uuid.uuid4()),'template_version':2,'raw_parent':{'value':'DO-NOT-EXPORT-TEMPLATE-SIBLINGS'}}
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_operations SET state='catalog_confirmed',first_admitted_at=clock_timestamp(),catalog_proof=%s,catalog_resolutions=%s WHERE tenant_id=%s AND id=%s",
                       (Jsonb(proof),Jsonb([resolution]),self.tenant,plan['receiving_id']))
            db.execute('''INSERT INTO ss_receiving_steps(tenant_id,operation_id,ordinal,endpoint,api_version,body_hash,encrypted_body,provider_key,occurred_at,authorized_by,session_hash,authority_hash)
                          VALUES (%s,%s,0,'catalog_batch','2026-09-16',%s,'DO-NOT-EXPORT-CATALOG-ENCRYPTED',%s,clock_timestamp(),%s,'DO-NOT-EXPORT-CATALOG-SESSION',%s)''',
                       (self.tenant,plan['receiving_id'],'1'*64,uuid.uuid4(),owner['staff_id'],'4'*64))
        output=self.begin();lines=list(output.iter_bytes());_,records,footer=decode(lines)
        self.assertEqual(footer['kind'],'complete')
        record=records['receiving_operations'][0]
        self.assertEqual(record['catalog_proof'],{key:proof[key] for key in ('kind','request_hash','response_hash')})
        self.assertEqual(record['catalog_resolutions'][0]['template_version'],2)
        self.assertEqual(records['receiving_steps'][0]['ordinal'],0)
        self.assertEqual(records['receiving_steps'][0]['endpoint'],'catalog_batch')
        self.assertNotIn(b'DO-NOT-EXPORT',b''.join(lines))
        self.assertEqual(provider.calls,[])

    def test_v4_classified_plan_intents_and_retirements_exclude_other_workflows_drafts_and_tenants(self):
        service,provider,owner,data=self.receiving_fixture()
        selected=[]
        for index in range(121):
            rejected=deepcopy(data);rejected['request_key']='privacy-retired-'+str(index);rejected['lines'][0]['selling_cents']=1250
            with self.assertRaises(ServiceError) as error:service.create_plan(owner,rejected)
            self.assertEqual(getattr(error.exception,'code',None),'CATALOG_MAPPING_REVIEW_REQUIRED')
            proof=service.retire_plan_request(owner,rejected['request_key'],{'original_plan':rejected,'retire_uncreated_request':True})
            self.assertEqual(proof['retirement_basis'],'registered_original_wire');selected.append(rejected['request_key'])
        wrong_workflow=plan_data(self.intake_claim);wrong_workflow['request_key']='WITHHOLD-INVENTORY-INTENT'
        wrong_trade=deepcopy(data);wrong_trade.update(request_key='WITHHOLD-COMPETING-DRAFT',accepted_trade_id=self.competing_trade_id)
        wrong_hash=deepcopy(data);wrong_hash.update(request_key='WITHHOLD-RECEIPT-HASH',accepted_receipt_hash='0'*64)
        unlinked=deepcopy(data);unlinked.update(request_key='WITHHOLD-UNLINKED-CLAIM',physical_intake_id=str(uuid.uuid4()))
        for rejected in (wrong_workflow,wrong_trade,wrong_hash,unlinked):
            with self.subTest(key=rejected['request_key']),self.assertRaises(ServiceError):service.create_plan(owner,rejected)
        invalid={'request_key':'WITHHOLD-UNCLASSIFIED-RETIREMENT','unsupported_original':'fictional invalid body'}
        proof=service.retire_plan_request(owner,invalid['request_key'],{'original_plan':invalid,'retire_uncreated_request':True})
        self.assertEqual(proof['retirement_basis'],'unregistered_key_no_operation')
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_receiving_plan_intents(tenant_id,request_key,intent_hash,wire_hash,claim_id,created_by)
                          VALUES (%s,'WITHHOLD-LEGACY-UNCLASSIFIED',%s,%s,%s,%s)''',
                       (self.tenant,'a'*64,'b'*64,self.intake_claim['physical_intake_id'],owner['staff_id']))
            foreign_tenant=db.execute('SELECT tenant_id FROM ss_customers WHERE id=%s',(self.foreign_customer,)).fetchone()['tenant_id']
            foreign_staff=uuid.uuid4()
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'foreign-intent@example.invalid','fictional-invalid-hash','owner')",(foreign_staff,foreign_tenant))
            db.execute('''INSERT INTO ss_receiving_plan_intents(tenant_id,request_key,intent_hash,wire_hash,claim_id,created_by,workflow_kind,accepted_trade_id,accepted_receipt_hash)
                          VALUES (%s,%s,%s,%s,%s,%s,'accepted_trade',%s,%s)''',
                       (foreign_tenant,selected[0],'a'*64,'b'*64,self.intake_claim['physical_intake_id'],foreign_staff,self.trade_id,self.trade_hash))
        created=service.create_plan(owner,data)
        before=self.business_snapshot();output=self.begin();lines=list(output.iter_bytes());header,records,footer=decode(lines)
        self.assertEqual((header['version'],footer['version'],len(footer['record_counts'])),(4,4,26))
        self.assertEqual(footer['record_counts']['receiving_plan_intents'],122)
        intents=records['receiving_plan_intents'];self.assertEqual({row['request_key'] for row in intents},set(selected+[data['request_key']]))
        retired=[row for row in intents if row['request_state']=='retired_before_creation']
        self.assertEqual(len(retired),121)
        self.assertTrue(all(row['wire_hash']==row['retired_original_plan_sha256'] and row['retirement_basis']=='registered_original_wire' for row in retired))
        self.assertEqual(next(row for row in intents if row['request_key']==data['request_key'])['operation_id'],created['receiving_id'])
        retired_audits=[row for row in records['audit'] if row['action']=='receiving-plan-request-retired']
        self.assertEqual({row['entity_id'] for row in retired_audits},set(selected))
        self.assertTrue(all(row['details'].get('original_plan_sha256') for row in retired_audits))
        self.assertNotIn(b'WITHHOLD-',b''.join(lines));self.assertNotIn(b'"original_plan":',b''.join(lines))
        self.assertEqual(before,self.business_snapshot());self.assertEqual(provider.calls,[])
        other=self.begin(customer=self.other_customer);_,other_records,other_footer=decode(list(other.iter_bytes()))
        self.assertEqual(other_footer['record_counts']['receiving_plan_intents'],0)
        self.assertFalse(any(row['action']=='receiving-plan-request-retired' for row in other_records['audit']))

    def test_v4_corrupted_retirement_proof_is_rejected_before_customer_export(self):
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_receiving_plan_intents(tenant_id,request_key,intent_hash,wire_hash,claim_id,created_by,workflow_kind,accepted_trade_id,accepted_receipt_hash)
                          VALUES (%s,'corrupted-retirement-proof',%s,%s,%s,%s,'accepted_trade',%s,%s)''',
                       (self.tenant,'a'*64,'b'*64,self.intake_claim['physical_intake_id'],self.trade_manager['staff_id'],self.trade_id,self.trade_hash))
            malformed={'request_key':'corrupted-retirement-proof','retired':True,'state':'retired_before_creation','receiving_id':None,
                       'original_plan_sha256':'c'*64,'retirement_basis':'registered_original_wire'}
            db.execute("INSERT INTO ss_receiving_requests(tenant_id,request_key,action,intent_hash,result) VALUES (%s,'corrupted-retirement-proof','plan',%s,%s)",
                       (self.tenant,'a'*64,Jsonb(malformed)))
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,409)

    def business_snapshot(self):
        with self.database.transaction() as db:
            customer=db.execute('SELECT display_name,contact,balance_cents,version FROM ss_customers WHERE tenant_id=%s AND id=%s',(self.tenant,self.customer)).fetchone()
            counts={table:db.execute('SELECT count(*) AS count FROM '+table+' WHERE tenant_id=%s',(self.tenant,)).fetchone()['count']
                    for table in ('ss_credit_entries','ss_credit_lots','ss_credit_lot_reservations','ss_reservations','ss_reservation_terms','ss_square_settlements','ss_credit_returns','ss_trade_receipts','ss_trade_receipt_lines','ss_requests',
                                  'ss_receiving_plan_intents','ss_receiving_operations','ss_receiving_plan_revisions','ss_receiving_steps','ss_receiving_requests','ss_receiving_request_outcomes','ss_receiving_attempts','ss_receiving_exceptions')}
        return customer,counts

    def test_complete_all_linked_records_counts_receipts_and_no_credential_or_other_customer_leak(self):
        before=self.business_snapshot();output=self.begin();lines=list(output.iter_bytes())
        manifest,records,footer=decode(lines)
        self.assertEqual(footer['record_counts']['customer'],1)
        self.assertGreater(footer['record_counts']['credit_entries'],250)
        self.assertEqual(footer['record_counts']['idempotency_records'],252)
        for collection in ('credit_lots','lot_mutations','reservations','lot_reservations','reservation_terms','square_settlements','credit_returns','trade_receipts','trade_lines','origin_policies','audit'):
            self.assertGreater(footer['record_counts'][collection],0,collection)
        accepted=records['trade_receipts'][0]['accepted_receipt']
        self.assertEqual(accepted['customer']['id'],str(self.customer))
        self.assertEqual(records['trade_receipts'][0]['receipt_hash'],self.trade_hash)
        self.assertEqual(accepted['lines'][0]['identity']['name'],'Fictional card')
        body=b''.join(lines)
        for marker in ('OTHER-CUSTOMER','FOREIGN-CUSTOMER','DO-NOT-EXPORT-SECRET',self.fixture_hash,self.token):
            self.assertNotIn(marker.encode(),body)
        self.assertIn(b'own-export@example.invalid',body)
        self.assertEqual(self.business_snapshot(),before)
        with self.database.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) AS count FROM ss_audit WHERE tenant_id=%s AND action='customer-export-client-verified'",(self.tenant,)).fetchone()['count'],0)
        data={key:footer[key] for key in ('start_audit_id','content_chain_sha256','record_counts')}
        self.assertTrue(self.service.confirm(self.actor,output.id,data,self.token)['verified'])
        self.assertTrue(self.service.confirm(self.actor,output.id,data,self.token)['already_verified'])
        self.assertEqual(self.business_snapshot(),before)

    def test_repeatable_read_snapshot_excludes_concurrent_committed_credit(self):
        before=self.business_snapshot();output=self.begin();errors=[]
        def mutate():
            try:Ledger(self.database).adjust(self.actor,self.customer,{'request_key':'concurrent-export-write','amount_cents':1,'kind':'adjust_add',
                 'reference':'LATER-WRITE','reason':'Fictional concurrent snapshot test','version':before[0]['version']})
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=mutate);worker.start();worker.join(timeout=10)
        self.assertFalse(worker.is_alive());self.assertFalse(errors)
        manifest,records,footer=decode(list(output.iter_bytes()))
        self.assertEqual(records['customer'][0]['balance_cents'],before[0]['balance_cents'])
        self.assertEqual(len(records['credit_entries']),before[1]['ss_credit_entries']-1)  # one other customer's entry excluded
        self.assertNotIn('LATER-WRITE',json.dumps(records))
        self.assertEqual(self.business_snapshot()[0]['balance_cents'],before[0]['balance_cents']+1)

    def test_logout_and_staff_revocation_during_stream_close_snapshot_and_never_confirm(self):
        for revoke in ('logout','disable'):
            if revoke=='disable':
                self.token,_=self.auth.login({'store_slug':self.slug,'email':self.email,'password':self.fixture_password},'127.0.0.1')
                self.actor=self.auth.resolve(self.token)
            output=self.begin();iterator=output.iter_bytes();header=next(iterator)
            if revoke=='logout':self.auth.logout(self.token)
            else:
                with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,self.staff))
            remaining=list(iterator)
            self.assertEqual(json.loads(remaining[-1])['kind'],'error')
            self.assertNotIn(b'own-export@example.invalid',b''.join(remaining))
            with self.database.transaction() as db:
                ended=db.execute("SELECT details FROM ss_audit WHERE tenant_id=%s AND entity_id=%s AND action='customer-export-stream-ended'",(self.tenant,str(output.id))).fetchone()
                self.assertEqual(ended['details']['outcome'],'interrupted')
                self.assertEqual(db.execute("SELECT count(*) AS count FROM ss_audit WHERE tenant_id=%s AND action='customer-export-client-verified'",(self.tenant,)).fetchone()['count'],0)

    def test_cancel_releases_snapshot_pool_and_export_slot_without_touching_records(self):
        before=self.business_snapshot();output=self.begin();iterator=output.iter_bytes();next(iterator);next(iterator);iterator.close()
        self.assertTrue(output.closed);self.assertIsNone(output.db)
        replacement=self.begin();replacement.close()
        self.assertEqual(self.business_snapshot(),before)
        with self.database.transaction() as db:
            self.assertTrue(db.execute('SELECT 1 AS ok').fetchone()['ok'])

    def test_role_tenant_recency_and_cross_customer_corruption_fail_without_data(self):
        with self.assertRaises(ServiceError):self.begin(actor=self.actor|{'role':'manager'})
        for customer in (self.other_customer,self.foreign_customer):
            if customer==self.other_customer:
                # A store owner can export a separately verified same-store
                # account, but cannot make it masquerade as the selected account.
                with self.assertRaises(ServiceError):self.service.begin(self.actor,customer,request(self.actor,self.customer),self.token)
            else:
                with self.assertRaises(ServiceError) as error:self.begin(customer=customer)
                self.assertEqual(error.exception.status,404)
        actual_clock=self.service.wall_clock
        self.service.wall_clock=lambda:self.actor['session_created_at']+timedelta(minutes=16)
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,401)
        self.service.wall_clock=actual_clock
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_credit_lots(id,tenant_id,customer_id,source_entry_id,issued_cents,remaining_cents)
                          VALUES (%s,%s,%s,%s,123,123)''',(uuid.uuid4(),self.tenant,self.customer,self.other_entry))
        self.actor=self.auth.resolve(self.token)
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,409)


if __name__=='__main__':unittest.main()
