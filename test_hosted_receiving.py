"""Receiving type/cipher/HTTP boundaries; no provider/database calls."""
from copy import deepcopy
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.receiving import Receiving
from hosted.receiving_plan import (normalize_plan, inventory_body, fingerprint, canonical, ReceivingError,
                                  effective_variation, preserved_price_sibling)
from hosted.security import ServiceError
from intake_manifest import source_line_id
from test_hosted_trades import card_line
from test_receiving_fixtures import catalog_fixture


def plan_data(claim=None):
    line=card_line();identity=line['identity']
    return {'request_key':'plan-'+uuid.uuid4().hex,'physical_intake_id':str(uuid.uuid4()) if claim is None else claim['physical_intake_id'],
            'physical_manifest_hash':'a'*64 if claim is None else claim['manifest_hash'],'source_snapshot_hash':'b'*64,
            'workflow_kind':'inventory_only','location_id':'fixture-location','new_inventory_confirmed':True,
            'client_label_profile':{'stock':'2x1','layout':'standard','dpi':203},
            'excluded_not_received_ack':True,'external_ownership_confirmed':True,
            'lines':[{'review_line_id':str(uuid.uuid4()),'source_line_key':source_line_id(identity),'identity':identity,
                      'quantity':2,'selling_cents':1000,'zero_price_confirmed':False,'reviewed':True}]}


class ReceivingPureTests(unittest.TestCase):
    def test_label_profile_is_explicit_typed_and_part_of_the_immutable_plan(self):
        one=plan_data();normalized=normalize_plan(one)
        self.assertEqual(normalized['client_label_profile'],{'stock':'2x1','layout':'standard','dpi':203})
        for bad in (None,{}, {'stock':'2x1','layout':'standard','dpi':True}, {'stock':'unsupported','layout':'standard','dpi':203},
                    {'stock':'3x1','layout':'unknown','dpi':300}, {'stock':'4x2','layout':'fold','dpi':300,'raw_zpl':'unsupported'}):
            value=deepcopy(one);value['client_label_profile']=bad
            with self.subTest(profile=bad),self.assertRaises(ServiceError):normalize_plan(value)
        two=deepcopy(one);two['client_label_profile']={'stock':'4x2','layout':'fold','dpi':300}
        self.assertNotEqual(fingerprint(normalize_plan(one)),fingerprint(normalize_plan(two)))
    def test_integer_price_quantity_and_explicit_new_inventory_are_required(self):
        for field,bad in (('quantity',True),('quantity',1.5),('quantity',0),('selling_cents',True),('selling_cents',-1),('reviewed',False)):
            value=plan_data();value['lines'][0][field]=bad
            with self.subTest(field=field,bad=bad),self.assertRaises(ServiceError):normalize_plan(value)
        for field in ('new_inventory_confirmed','excluded_not_received_ack','external_ownership_confirmed'):
            value=plan_data();value[field]=False
            with self.subTest(field=field),self.assertRaises(ServiceError):normalize_plan(value)

    def test_no_raw_proxy_or_caller_provider_authority_or_zero_price_inference(self):
        for field in ('tenant_id','merchant_id','url','headers','payload','access_token','grants_verified'):
            value=plan_data();value[field]='unsupported'
            with self.subTest(field=field),self.assertRaises(ServiceError):normalize_plan(value)
        value=plan_data();value['lines'][0]['selling_cents']=0
        with self.assertRaises(ServiceError):normalize_plan(value)
        value['lines'][0]['zero_price_confirmed']=True
        self.assertEqual(normalize_plan(value)['lines'][0]['selling_cents'],0)

    def test_new_api_uses_both_locations_stable_occurrence_key_and_reference(self):
        operation=uuid.uuid4();key=uuid.uuid4();targets=[{'variation_id':'fixture-variation','quantity':2}]
        one=inventory_body(operation,targets,'fixture-location','2026-10-29T00:00:00+00:00',key)
        self.assertEqual(canonical(one),canonical(inventory_body(operation,targets,'fixture-location','2026-10-29T00:00:00+00:00',key)))
        row=one['changes'][0]['adjustment']
        self.assertNotIn('location_id',row);self.assertEqual((row['from_location_id'],row['to_location_id']),('fixture-location','fixture-location'))
        self.assertEqual((row['from_state'],row['to_state'],row['quantity']),('NONE','IN_STOCK','2'))

    def test_cipher_is_bound_to_tenant_operation_endpoint_version_and_body(self):
        cipher=Fernet(Fernet.generate_key());service=Receiving(Mock(),Mock(),cipher)
        tenant,op=uuid.uuid4(),uuid.uuid4();body=inventory_body(op,[{'variation_id':'v','quantity':1}],'loc','2026-10-29T00:00:00Z',uuid.uuid4())
        step={'encrypted_body':service._seal(tenant,op,body),'body_hash':fingerprint(body),'provider_key':body['idempotency_key'],
              'endpoint':'inventory_batch','api_version':'2026-09-16'}
        self.assertEqual(service._open(tenant,op,step),body)
        for wrong_tenant,wrong_op in ((uuid.uuid4(),op),(tenant,uuid.uuid4())):
            with self.assertRaises(ReceivingError):service._open(wrong_tenant,wrong_op,step)
        for field,bad in (('body_hash','0'*64),('endpoint','raw_proxy'),('api_version','2024-09-19'),('provider_key',str(uuid.uuid4()))):
            with self.subTest(field=field),self.assertRaises(ReceivingError):service._open(tenant,op,step|{field:bad})

    def test_new_price_plan_preserves_every_existing_sibling_and_parent_field(self):
        objects=catalog_fixture();parent=objects['fixture-parent'];variation=objects['fixture-variation'];before=deepcopy(parent)
        result=preserved_price_sibling(parent,variation,new_sku='000000009999',cents=1250,temporary_id='#new-price',
             template={'approved':True,'new_price_enabled':True,'location_ids':['fixture-location'],'name_prefix':'New sticker price'})
        self.assertEqual(parent,before);self.assertEqual(result['item_data']['variations'][:-1],before['item_data']['variations'])
        self.assertEqual(result['item_data']['tax_ids'],before['item_data']['tax_ids'])
        self.assertEqual(result['item_data']['variations'][-1]['item_variation_data']['price_money']['amount'],1250)


class ReceivingHTTPTests(unittest.TestCase):
    def setUp(self):
        self.actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-store'}
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://receiving.fixture.invalid','APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=Mock())
        self.app.extensions['ss_auth'].resolve=Mock(return_value=self.actor)
        self.service=self.app.extensions['ss_receiving'];self.client=self.app.test_client();self.base='https://receiving.fixture.invalid'
        self.headers={'Authorization':'Bearer fictional-staff-session'}
        for name in ('readiness','locations','binding_preview','approve_binding','revoke_binding','create_plan','get','request_status','revise','commit','recover','authorize','cancel'):
            setattr(self.service,name,Mock(return_value={'fixture':name}))

    def test_typed_authenticated_routes_use_actor_and_request_body(self):
        ident=str(uuid.uuid4())
        routes=[('get','/api/receiving/readiness','readiness'),('get','/api/receiving/locations','locations'),
            ('post','/api/receiving/catalog-bindings/previews','binding_preview'),('post','/api/receiving/plans','create_plan'),
            ('get','/api/receiving/'+ident,'get'),('get','/api/receiving/requests/key','request_status'),
            ('post','/api/receiving/catalog-bindings/'+ident+'/approve','approve_binding'),('post','/api/receiving/catalog-bindings/'+ident+'/revoke','revoke_binding'),
            ('put','/api/receiving/'+ident+'/plan','revise')]+[('post','/api/receiving/'+ident+'/'+action,action) for action in ('commit','recover','authorize','cancel')]
        for method,path,name in routes:
            with self.subTest(path=path):
                result=getattr(self.client,method)(path,base_url=self.base,headers=self.headers,**({'json':{}} if method!='get' else {}))
                self.assertIn(result.status_code,(200,201));self.assertEqual(result.json,{'fixture':name})
                self.assertEqual(getattr(self.service,name).call_args.args[0],self.actor)

    def test_cookie_csrf_cross_origin_and_tenant_proxy_are_rejected(self):
        self.client.set_cookie('ss_session','fictional-cookie',domain='receiving.fixture.invalid')
        self.assertEqual(self.client.post('/api/receiving/plans',base_url=self.base,json={}).status_code,403)
        self.assertEqual(self.client.post('/api/receiving/plans',base_url=self.base,headers=self.headers,json={'tenant_id':'forged'}).status_code,400)
        self.assertEqual(self.client.post('/api/receiving/plans',base_url=self.base,headers=self.headers|{'Origin':'https://foreign.invalid'},json={}).status_code,403)

    def test_machine_error_has_only_safe_same_tenant_operation_id(self):
        ident=uuid.uuid4();self.service.create_plan.side_effect=ReceivingError('Recover original operation.',code='CLAIM_WORKFLOW_CONFLICT',receiving_id=ident)
        result=self.client.post('/api/receiving/plans',base_url=self.base,headers=self.headers,json={})
        self.assertEqual(result.json,{'error':'Recover original operation.','code':'CLAIM_WORKFLOW_CONFLICT','receiving_id':str(ident)})
