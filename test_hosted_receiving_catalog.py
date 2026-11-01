"""Pure catalog preservation/acknowledgement proof; no provider calls."""
from copy import deepcopy
import json
import unittest
import uuid

from hosted.receiving_catalog import price_sku,build_catalog_request,catalog_ack,catalog_state
from hosted.receiving_plan import canonical,fingerprint,ReceivingError
from hosted.security import ServiceError
from square_variations import variant_sku
from test_hosted_receiving_adversarial import catalog


def fixture():
    variation,parent=catalog()
    variation['item_variation_data'].update(vendor_information=[{'vendor_id':'DO-NOT-COPY-VENDOR'}],
        unit_cost_money={'amount':11,'currency':'USD'})
    parent['item_data']['variations'][0]=deepcopy(variation)
    addition={'review_line_id':'00000000-0000-0000-0000-000000000011','identity_hash':'a'*64,
        'parent_id':'parent-A','source_variation_id':'variation-A','target_sku':price_sku('SKU-A>^~',1250),
        'selling_cents':1250,'template_id':'00000000-0000-0000-0000-000000000012','template_version':2,
        'template':{'approved':True,'new_price_enabled':True,'location_ids':['location-A'],'name_prefix':'Reviewed price'}}
    body=build_catalog_request('00000000-0000-0000-0000-000000000013',{'parent-A':parent},[addition],
                               '00000000-0000-0000-0000-000000000014')
    return parent,addition,body


def response(body):
    values=deepcopy(body['batches'][0]['objects']);mappings=[]
    for parent in values:
        parent['version']+=1;parent['updated_at']='2026-10-31T06:00:00Z'
        for index,child in enumerate(parent['item_data']['variations']):
            child['version']=100+index;child['item_variation_data']['ordinal']=index
            if child['id'].startswith('#'):
                temporary=child['id'];child['id']='created-'+temporary[1:]
                mappings.append({'client_object_id':temporary,'object_id':child['id']})
    return {'objects':values,'id_mappings':mappings}


class ReceivingCatalogProofTests(unittest.TestCase):
    def test_price_sku_keeps_exact_legacy_bytes_and_pinned_vectors(self):
        for source,cents,expected in [('000000001234',1250,'903059372656'),('000000001234',0,'197174693580'),
                                      ('SKU-A>^~',1250,'106478669935')]:
            self.assertEqual(price_sku(source,cents),expected)
        for source in (' leading space ','A>^~',chr(233)):
            self.assertEqual(price_sku(source,1250),variant_sku(source,1250))
        for cents in (True,-1,1.5,100000001):
            with self.subTest(cents=cents),self.assertRaises(ServiceError):price_sku('123',cents)

    def test_full_parent_old_sibling_order_versions_and_unknown_business_fields_are_preserved(self):
        parent,addition,body=fixture();saved=deepcopy(parent);terms=deepcopy(addition)
        rebuilt=build_catalog_request('00000000-0000-0000-0000-000000000013',{'parent-A':parent},[addition],body['idempotency_key'])
        self.assertEqual(body,rebuilt);self.assertEqual(parent,saved);self.assertEqual(addition,terms)
        appended=deepcopy(body['batches'][0]['objects'][0]);new=appended['item_data']['variations'].pop()
        self.assertEqual(appended,parent)
        self.assertEqual(new['item_variation_data']['price_money'],{'amount':1250,'currency':'USD'})
        self.assertNotIn('DO-NOT-COPY-VENDOR',json.dumps(new))
        marker=json.loads(new['item_variation_data']['user_data'])
        self.assertEqual(marker,{'ss_price':2,'identity':'a'*64,'operation':'00000000-0000-0000-0000-000000000013',
                                 'review':addition['review_line_id']})
        self.assertLessEqual(len(new['item_variation_data']['user_data']),255)

    def test_successful_ack_and_lost_ack_state_have_same_safe_resolutions(self):
        _,addition,body=fixture();result=response(body)
        acknowledged=catalog_ack(body,result,[addition])
        proven=catalog_state(body,{obj['id']:obj for obj in result['objects']},[addition])
        self.assertEqual(acknowledged['resolutions'],proven['resolutions'])
        self.assertEqual(acknowledged['proof'],{'kind':'catalog_batch_ack','request_hash':fingerprint(body),'response_hash':fingerprint(result)})
        self.assertEqual(proven['proof']['kind'],'catalog_state_match')
        self.assertEqual(proven['resolutions'][0]['variation_id'],'created-'+addition['review_line_id'].replace('-',''))
        self.assertEqual(set(acknowledged),{'proof','resolutions'})
        self.assertNotIn('DO-NOT-COPY-VENDOR',json.dumps(acknowledged))

    def test_ack_requires_unique_complete_mapping_and_no_partial_error(self):
        _,addition,body=fixture();good=response(body)
        wrong=deepcopy(good);wrong['id_mappings'][0]['object_id']='different-provider-id'
        duplicate=deepcopy(good);duplicate['id_mappings']*=2
        extra=deepcopy(good);extra['id_mappings'][0]['unreviewed']='unsupported'
        for value in (good|{'id_mappings':[]},good|{'errors':[{'code':'PARTIAL'}]},wrong,duplicate,extra,
                      good|{'objects':[]}):
            with self.subTest(value=value),self.assertRaises(ReceivingError) as error:catalog_ack(body,value,[addition])
            self.assertEqual(error.exception.code,'PROVIDER_OUTCOME_UNKNOWN')

    def test_two_new_prices_need_distinct_provider_ids_and_deterministic_append_order(self):
        parent,addition,_=fixture()
        second=addition|{'review_line_id':'00000000-0000-0000-0000-000000000021','selling_cents':1500,
                         'target_sku':price_sku('SKU-A>^~',1500)}
        operation,key=str(uuid.uuid4()),str(uuid.uuid4())
        first=build_catalog_request(operation,{'parent-A':parent},[second,addition],key)
        self.assertEqual(first,build_catalog_request(operation,{'parent-A':parent},[addition,second],key))
        good=response(first);self.assertEqual(len(catalog_ack(first,good,[addition,second])['resolutions']),2)
        good['id_mappings'][1]['object_id']=good['id_mappings'][0]['object_id']
        with self.assertRaises(ReceivingError):catalog_ack(first,good,[addition,second])

    def test_changed_old_business_facts_never_prove_catalog_creation(self):
        _,addition,body=fixture();good=response(body)
        mutations=[lambda p:p['item_data'].update(tax_ids=['different-tax']),
                   lambda p:p['item_data'].update(is_archived=True),
                   lambda p:p['custom_attribute_values']['owner-fact'].update(string_value='changed'),
                   lambda p:p['item_data']['variations'][0]['item_variation_data'].update(price_money={'amount':999,'currency':'USD'}),
                   lambda p:p['item_data']['variations'][0]['item_variation_data'].update(location_overrides=[]),
                   lambda p:p['item_data']['variations'][1]['item_variation_data'].update(sku='changed-old-SKU'),
                   lambda p:p['item_data']['variations'].pop(1),
                   lambda p:p['item_data']['variations'].__setitem__(slice(0,2),list(reversed(p['item_data']['variations'][:2])))]
        for index,mutate in enumerate(mutations):
            bad=deepcopy(good);mutate(bad['objects'][0])
            with self.subTest(index=index),self.assertRaises(ReceivingError):catalog_ack(body,bad,[addition])
            with self.assertRaises(ReceivingError):catalog_state(body,{obj['id']:obj for obj in bad['objects']},[addition])

    def test_sku_alone_wrong_marker_price_tracking_or_location_cannot_prove_the_creator(self):
        _,addition,body=fixture();good=response(body)
        for change in ({'user_data':'{}'},{'price_money':{'amount':1251,'currency':'USD'}},
                       {'track_inventory':False},{'sku':'000000000999'},{'item_id':'other-parent'}):
            bad=deepcopy(good);bad['objects'][0]['item_data']['variations'][-1]['item_variation_data'].update(change)
            with self.subTest(change=change),self.assertRaises(ReceivingError):catalog_state(body,{obj['id']:obj for obj in bad['objects']},[addition])
        for mode in ('wrong_operation','duplicate_marker','wrong_location'):
            bad=deepcopy(good);new=bad['objects'][0]['item_data']['variations'][-1]
            if mode=='wrong_operation':
                marker=json.loads(new['item_variation_data']['user_data']);marker['operation']=str(uuid.uuid4());new['item_variation_data']['user_data']=canonical(marker).decode()
            elif mode=='duplicate_marker':
                clone=deepcopy(new);clone['id']='duplicate-created';bad['objects'][0]['item_data']['variations'].append(clone)
            else:new['present_at_location_ids']=['wrong-location']
            with self.subTest(mode=mode),self.assertRaises(ReceivingError):catalog_state(body,{obj['id']:obj for obj in bad['objects']},[addition])

    def test_later_extra_children_are_allowed_only_with_all_frozen_children_and_business_facts(self):
        _,addition,body=fixture();good=response(body);extra=deepcopy(good['objects'][0]['item_data']['variations'][0])
        extra['id']='later-unrelated-child';extra['item_variation_data'].update(sku='LATER-SKU',name='Later sibling')
        good['objects'][0]['item_data']['variations'].append(extra)
        self.assertEqual(len(catalog_state(body,{obj['id']:obj for obj in good['objects']},[addition])['resolutions']),1)
        good['objects'][0]['item_data']['tax_ids']=[]
        with self.assertRaises(ReceivingError):catalog_state(body,{obj['id']:obj for obj in good['objects']},[addition])

    def test_readonly_versions_can_advance_but_cannot_roll_back_old_evidence(self):
        _,addition,body=fixture();good=response(body)
        for target in ('parent','old_child'):
            bad=deepcopy(good)
            if target=='parent':bad['objects'][0]['version']=1
            else:bad['objects'][0]['item_data']['variations'][0]['version']=1
            with self.subTest(target=target),self.assertRaises(ReceivingError):catalog_ack(body,bad,[addition])

    def test_nonfinite_or_oversized_provider_evidence_never_receives_a_success_hash(self):
        parent,addition,body=fixture();good=response(body)
        good['unsupported_number']=float('nan')
        with self.assertRaises(ReceivingError):catalog_ack(body,good,[addition])
        parent['custom_attribute_values']['invalid']={'number_value':float('inf')}
        with self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':parent},[addition],str(uuid.uuid4()))
        good=response(body);good['raw_padding']='x'*(4*1024*1024)
        with self.assertRaises(ReceivingError):catalog_ack(body,good,[addition])

    def test_saved_request_tampering_missing_version_or_arbitrary_target_sku_is_rejected(self):
        parent,addition,body=fixture();good=response(body)
        changed=deepcopy(body);changed['batches'][0]['objects'][0]['item_data']['variations'][-1]['item_variation_data']['track_inventory']=False
        with self.assertRaises(ReceivingError):catalog_ack(changed,good,[addition])
        for field,bad in (('version',None),('version',True)):
            original=deepcopy(parent);original[field]=bad
            with self.subTest(field=field,bad=bad),self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':original},[addition],str(uuid.uuid4()))
        with self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':parent},[addition|{'target_sku':'000000000000'}],str(uuid.uuid4()))

    def test_duplicate_review_or_sku_and_unapproved_template_are_rejected(self):
        parent,addition,_=fixture()
        for rows in ([addition,deepcopy(addition)], [addition,addition|{'review_line_id':str(uuid.uuid4())}],
                     [addition|{'template':addition['template']|{'approved':False}}]):
            with self.subTest(rows=len(rows)),self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':parent},rows,str(uuid.uuid4()))

    def test_sibling_total_nested_and_byte_capacity_fail_closed(self):
        parent,addition,_=fixture();source=parent['item_data']['variations'][0]
        huge=deepcopy(parent);huge['item_data']['variations']=[deepcopy(source)|{'id':'old-'+str(i)} for i in range(250)]
        huge['item_data']['variations'][0]['id']=addition['source_variation_id']
        with self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':huge},[addition],str(uuid.uuid4()))
        parents={};rows=[]
        for index in range(4):
            ident='parent-'+str(index);item=deepcopy(parent);item['id']=ident
            item['item_data']['variations']=[]
            for child_index in range(249):
                child=deepcopy(source);child['id']=f'v-{index}-{child_index}';child['item_variation_data'].update(item_id=ident,sku=f'{index:04d}{child_index:08d}')
                item['item_data']['variations'].append(child)
            parents[ident]=item;base=item['item_data']['variations'][0]
            rows.append(addition|{'parent_id':ident,'source_variation_id':base['id'],'review_line_id':str(uuid.uuid4()),
                                  'target_sku':price_sku(base['item_variation_data']['sku'],1250)})
        with self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),parents,rows,str(uuid.uuid4()))
        oversized=deepcopy(parent);oversized['custom_attribute_values']['oversize']={'string_value':'x'*(4*1024*1024)}
        with self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':oversized},[addition],str(uuid.uuid4()))

    def test_legacy_frozen_name_and_marker_keep_golden_request_bytes_and_recovery(self):
        _,addition,body=fixture()
        self.assertEqual(fingerprint(body),'2af736e6accdb49faa3fd7b7f1d05e7aabb4688843034b805caf23096b8b1eef')
        new=body['batches'][0]['objects'][0]['item_data']['variations'][-1]
        self.assertEqual(new['item_variation_data']['name'],'Reviewed price 1250')
        self.assertNotIn('name_format',json.loads(new['item_variation_data']['user_data']))
        result=response(body)
        self.assertEqual(catalog_ack(body,result,[addition])['resolutions'],catalog_state(body,{row['id']:row for row in result['objects']},[addition])['resolutions'])

    def test_explicit_currency_name_format_uses_integer_cents_and_proves_saved_marker(self):
        parent,addition,_=fixture()
        for cents,label in ((0,'$0.00'),(1,'$0.01'),(5,'$0.05'),(99,'$0.99'),(1250,'$12.50'),(100000000,'$1000000.00')):
            row=deepcopy(addition);row['selling_cents']=cents;row['target_sku']=price_sku('SKU-A>^~',cents)
            row['template']['name_format_version']=2
            body=build_catalog_request(str(uuid.uuid4()),{'parent-A':parent},[row],str(uuid.uuid4()))
            new=body['batches'][0]['objects'][0]['item_data']['variations'][-1]
            with self.subTest(cents=cents):
                self.assertEqual(new['item_variation_data']['name'],'Reviewed price · '+label)
                self.assertEqual(json.loads(new['item_variation_data']['user_data'])['name_format'],2)
                self.assertLessEqual(len(new['item_variation_data']['user_data']),255)
                result=response(body)
                self.assertEqual(catalog_ack(body,result,[row])['resolutions'],catalog_state(body,{obj['id']:obj for obj in result['objects']},[row])['resolutions'])

    def test_name_format_pin_cannot_be_changed_for_an_existing_saved_request(self):
        parent,addition,legacy=fixture();updated=deepcopy(addition);updated['template']['name_format_version']=2
        modern=build_catalog_request(str(uuid.uuid4()),{'parent-A':parent},[updated],str(uuid.uuid4()))
        with self.assertRaises(ReceivingError):catalog_ack(legacy,response(legacy),[updated])
        with self.assertRaises(ReceivingError):catalog_ack(modern,response(modern),[addition])
        for invalid in (None,True,'2',3):
            row=deepcopy(addition);row['template']['name_format_version']=invalid
            with self.subTest(invalid=invalid),self.assertRaises(ReceivingError):build_catalog_request(str(uuid.uuid4()),{'parent-A':parent},[row],str(uuid.uuid4()))


if __name__=='__main__':unittest.main(verbosity=2)
