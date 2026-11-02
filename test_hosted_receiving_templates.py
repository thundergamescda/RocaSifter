"""Pure/read-only owner template facts and typed HTTP boundaries."""
from copy import deepcopy
import json
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet

from hosted.app import create_app
from hosted.receiving_plan import effective_variation,ReceivingError
from hosted.receiving_templates import PriceTemplates,parent_policy_hash
from hosted.security import ServiceError
from intake_manifest import source_line_id
from test_hosted_trades import card_line
from test_receiving_fixtures import FictionalReceivingProvider


def approval(preview,*,key=None):
    return {'request_key':key or 'template-'+uuid.uuid4().hex,'preview_hash':preview['preview_hash'],
            'preserve_existing_tax_configuration_confirmed':True,'preserve_existing_siblings_confirmed':True,
            'source_identity_confirmed':True,'approval_reason':'Fictional owner verified existing settings',
            'policy_reference':preview['facts']['policy_reference']}


class ReceivingTemplateFactsTests(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid.uuid4();self.actor={'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':'owner'}
        self.provider=FictionalReceivingProvider(None,self.tenant)
        identity=card_line()['identity'];effective=effective_variation(self.provider.objects['fixture-variation'],self.provider.objects['fixture-parent'],'fixture-location')
        self.binding=effective|{'id':uuid.uuid4(),'version':1,'identity':identity,'identity_hash':source_line_id(identity),'location_id':'fixture-location',
            'provider':{key:self.provider.proof[key] for key in ('merchant_id','environment','application_id')}|{'location_id':'fixture-location'}}
        self.service=PriceTemplates(Mock(),self.provider,Mock())

    def test_readable_tax_category_location_facts_exclude_full_snapshots_and_never_enable_writes(self):
        self.provider.objects['fixture-parent']['item_data']['categories']=[{'id':'category-A'}]
        self.provider.objects['category-A']={'id':'category-A','type':'CATEGORY','category_data':{'name':'Fictional category'}}
        facts,_=self.service._facts(self.actor,self.binding,'New sticker price')
        self.assertEqual(facts['taxes'],[{'id':'fixture-tax','name':'Fictional existing tax setting','percentage':'6','inclusion_type':'ADDITIVE',
                                        'enabled':True,'calculation_phase':'TAX_SUBTOTAL_PHASE'}])
        self.assertEqual(facts['categories'],[{'id':'category-A','name':'Fictional category'}])
        self.assertEqual(facts['location'],{'id':'fixture-location','name':'Fictional till'})
        self.assertFalse(facts['new_parent_creation']);self.assertFalse(facts['receiving_enabled'])
        self.assertNotIn('fictional-server-token',json.dumps(facts));self.assertNotIn('variations',json.dumps(facts))
        self.assertEqual(self.provider.calls,[])

    def test_same_tax_id_percentage_enabled_or_inclusion_change_invalidates_policy_hash(self):
        parent=self.provider.objects['fixture-parent'];original=deepcopy(self.provider.objects)
        before=parent_policy_hash(parent,original)
        for change in ({'percentage':'7'},{'enabled':False},{'inclusion_type':'INCLUSIVE'}):
            objects=deepcopy(original);objects['fixture-tax']['tax_data'].update(change)
            with self.subTest(change=change):self.assertNotEqual(parent_policy_hash(parent,objects),before)

    def test_unreadable_or_ambiguous_tax_category_references_fail_before_approval(self):
        original=deepcopy(self.provider.objects)
        for mode in ('missing_tax','deleted_tax','wrong_tax_type','missing_tax_name','missing_category','duplicate_tax'):
            self.provider.objects=deepcopy(original)
            if mode=='missing_tax':del self.provider.objects['fixture-tax']
            elif mode=='deleted_tax':self.provider.objects['fixture-tax']['is_deleted']=True
            elif mode=='wrong_tax_type':self.provider.objects['fixture-tax']['type']='CATEGORY'
            elif mode=='missing_tax_name':self.provider.objects['fixture-tax']['tax_data'].pop('name')
            elif mode=='missing_category':self.provider.objects['fixture-parent']['item_data']['categories']=[{'id':'missing-category'}]
            else:self.provider.objects['fixture-parent']['item_data']['tax_ids']*=2
            with self.subTest(mode=mode),self.assertRaises(ReceivingError):self.service._facts(self.actor,self.binding,'Reviewed')
        self.assertEqual(self.provider.calls,[])

    def test_template_requests_reject_unreviewed_fields_false_attestations_or_unreadable_prefix(self):
        for data in ({'binding_id':str(self.binding['id']),'variation_name_prefix':'ok','raw_parent':{}},
                     {'binding_id':str(self.binding['id']),'variation_name_prefix':'bad\nname'}):
            with self.assertRaises(ServiceError):self.service.preview(self.actor,data)
        base={'request_key':'approve','preview_hash':'a'*64,'preserve_existing_tax_configuration_confirmed':True,
              'preserve_existing_siblings_confirmed':True,'source_identity_confirmed':True,'approval_reason':'Fictional reviewed reason','policy_reference':'fixture'}
        for field in ('preserve_existing_tax_configuration_confirmed','preserve_existing_siblings_confirmed','source_identity_confirmed'):
            with self.subTest(field=field),self.assertRaises(ServiceError):self.service.approve(self.actor,str(uuid.uuid4()),base|{field:False})
        with self.assertRaises(ServiceError):self.service.approve(self.actor,str(uuid.uuid4()),base|{'access_token':'not-accepted'})

    def test_tax_configuration_requires_complete_finite_explicit_values_without_guessed_defaults(self):
        original=deepcopy(self.provider.objects)
        for change in ({'percentage':None},{'percentage':'NaN'},{'percentage':'Infinity'},{'percentage':'101'},
                       {'percentage':'-1'},{'inclusion_type':None},{'enabled':None},{'enabled':1},{'calculation_phase':None}):
            self.provider.objects=deepcopy(original);self.provider.objects['fixture-tax']['tax_data'].update(change)
            with self.subTest(change=change),self.assertRaises(ReceivingError):self.service._facts(self.actor,self.binding,'Reviewed')


class ReceivingTemplateHTTPTests(unittest.TestCase):
    def setUp(self):
        self.actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-store'}
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://template.fixture.invalid','APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=Mock())
        self.app.extensions['ss_auth'].resolve=Mock(return_value=self.actor)
        self.service=self.app.extensions['ss_receiving'].templates
        for method in ('bindings','list','preview','approve','revoke'):setattr(self.service,method,Mock(return_value={'fixture':method}))
        self.client=self.app.test_client();self.base='https://template.fixture.invalid';self.headers={'Authorization':'Bearer fictional-staff-session'}

    def test_typed_routes_pass_authenticated_actor_and_owner_choices_without_raw_proxy(self):
        ident=str(uuid.uuid4());choices={'fictional':'explicit typed fixture'}
        for route,method in [('/api/receiving/price-templates/previews','preview'),('/api/receiving/price-templates/'+ident+'/approve','approve'),
                             ('/api/receiving/price-templates/'+ident+'/revoke','revoke')]:
            result=self.client.post(route,json=choices,headers=self.headers,base_url=self.base)
            self.assertIn(result.status_code,(200,201));call=getattr(self.service,method).call_args
            self.assertEqual(call.args[0],self.actor);self.assertEqual(call.args[-1],choices)
        result=self.client.get('/api/receiving/catalog-bindings?q=literal%25&limit=3&cursor='+ident,headers=self.headers,base_url=self.base)
        self.assertEqual(result.status_code,200);self.service.bindings.assert_called_once_with(self.actor,'literal%',limit=3,cursor=ident)
        self.assertEqual(self.client.get('/api/receiving/price-templates?limit=3',headers=self.headers,base_url=self.base).status_code,200)

    def test_csrf_origin_invalid_pagination_and_unknown_proxy_fail_before_service(self):
        path='/api/receiving/price-templates/previews'
        for headers in ({},self.headers|{'Origin':'https://different.invalid'}):
            self.assertIn(self.client.post(path,json={},headers=headers,base_url=self.base).status_code,(401,403))
        self.service.preview.assert_not_called()
        self.assertEqual(self.client.get('/api/receiving/price-templates?limit=nan',headers=self.headers,base_url=self.base).status_code,400)
        self.service.list.assert_not_called()
        self.assertEqual(self.client.post('/api/receiving/square-proxy',json={'url':'https://provider.invalid'},headers=self.headers,base_url=self.base).status_code,404)


if __name__=='__main__':unittest.main(verbosity=2)
