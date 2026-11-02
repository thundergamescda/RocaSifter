"""Independent adversarial receiving proof/transport tests; no DB/provider calls."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from intake_manifest import source_line_id
from hosted.receiving_plan import (
    API_VERSION, ReceivingError, bind_targets, canonical, effective_variation,
    history_proof, inventory_body, preserved_price_sibling, validate_inventory_ack,
)
from hosted.receiving_square import ReceivingSquare


def body():
    return inventory_body('fictional-operation', [{'variation_id': 'variation-A', 'quantity': 2}],
                          'location-A', '2026-10-29T05:00:00+00:00', '00000000-0000-0000-0000-000000000001')


def adjustment(ident='adjustment-A', **changes):
    value=deepcopy(body()['changes'][0]['adjustment'])
    value.update(id=ident,source={'application_id':'app-A','product':'EXTERNAL_API'})
    value.update(changes)
    return {'type':'ADJUSTMENT','adjustment':value}


def counts(quantity='912'):
    return {'counts':[{'catalog_object_id':'variation-A','catalog_object_type':'ITEM_VARIATION',
                       'location_id':'location-A','state':'IN_STOCK','quantity':quantity}]}


def catalog():
    variation={'id':'variation-A','type':'ITEM_VARIATION','version':7,'present_at_all_locations':True,
        'item_variation_data':{'item_id':'parent-A','sku':'SKU-A>^~','track_inventory':True,
            'sellable':True,'stockable':True,'pricing_type':'FIXED_PRICING',
            'price_money':{'amount':1000,'currency':'USD'},'user_data':'owner-reviewed-existing-data',
            'location_overrides':[{'location_id':'other-location','track_inventory':False,
                                   'price_money':{'amount':1700,'currency':'USD'}}]}}
    sibling=deepcopy(variation);sibling.update(id='old-sibling',version=6)
    sibling['item_variation_data'].update(sku='OLD-SKU',price_money={'amount':500,'currency':'USD'})
    parent={'id':'parent-A','type':'ITEM','version':7,'present_at_all_locations':True,
        'custom_attribute_values':{'owner-fact':{'string_value':'keep exactly'}},
        'item_data':{'name':'Fictional card','product_type':'REGULAR','tax_ids':['tax-A'],
                     'categories':[{'id':'category-A'}],'variations':[variation,sibling]}}
    return variation,parent


class ReceivingEvidenceAdversarialTests(unittest.TestCase):
    def test_exact_history_accepts_equivalent_time_offset_and_quantity_not_wrong_application(self):
        proof=history_proof(body(),[adjustment(quantity='2.00000',occurred_at='2026-10-28T22:00:00-07:00')],'app-A')
        self.assertEqual(proof['adjustment_ids'],['adjustment-A'])
        for source in ({'application_id':'other-app'}, {'product':'SQUARE_POS'}, None):
            with self.subTest(source=source),self.assertRaises(ReceivingError):
                history_proof(body(),[adjustment(source=source)],'app-A')

    def test_history_requires_both_current_locations_and_all_frozen_facts(self):
        for change in ({'from_location_id':'other'},{'to_location_id':'other'},{'catalog_object_id':'other'},
                       {'catalog_object_type':'ITEM'},{'from_state':'IN_STOCK'},{'to_state':'SOLD'},
                       {'quantity':'3'},{'quantity':'NaN'},{'occurred_at':'2026-10-29T05:00:01Z'},
                       {'occurred_at':'2026-10-29T05:00:00'}):
            with self.subTest(change=change),self.assertRaises(ReceivingError):history_proof(body(),[adjustment(**change)],'app-A')
        old=adjustment();old['adjustment'].pop('from_location_id');old['adjustment'].pop('to_location_id')
        old['adjustment']['location_id']='location-A'
        with self.assertRaises(ReceivingError):history_proof(body(),[old],'app-A')

    def test_history_deduplicates_exact_repeated_ids_but_not_two_adjustments_or_conflicts(self):
        one=adjustment()
        self.assertEqual(history_proof(body(),[one,deepcopy(one)],'app-A')['adjustment_ids'],['adjustment-A'])
        for changes in ([one,adjustment(ident='adjustment-B')],[one,adjustment(quantity='3')],[]):
            with self.subTest(changes=changes),self.assertRaises(ReceivingError):history_proof(body(),changes,'app-A')

    def test_ack_is_exact_request_success_not_an_inferred_inventory_delta(self):
        self.assertEqual(validate_inventory_ack(body(),counts('912'))['kind'],'batch_create_ack')
        for bad in ({},{'counts':[]},{'counts':counts()['counts'],'errors':[{'code':'partial'}]},
                    counts('NaN'),counts('Infinity'),counts(2),{'counts':[{}]},
                    {'counts':[dict(counts()['counts'][0],location_id='other-location')]}):
            with self.subTest(bad=bad),self.assertRaises(ReceivingError):validate_inventory_ack(body(),bad)

    def test_parent_and_every_old_sibling_are_preserved_without_mutating_inputs(self):
        variation,parent=catalog();original=deepcopy(parent)
        expanded=preserved_price_sibling(parent,variation,new_sku='NEW-PRICE-SKU',cents=1250,temporary_id='#new',
            template={'approved':True,'new_price_enabled':True,'location_ids':['location-A'],'name_prefix':'Reviewed'})
        self.assertEqual(parent,original)
        old_only=deepcopy(expanded);old_only['item_data']['variations'].pop()
        self.assertEqual(old_only,original)
        new=expanded['item_data']['variations'][-1]
        self.assertEqual(new['item_variation_data']['item_id'],'parent-A')
        self.assertEqual(new['item_variation_data']['price_money'],{'amount':1250,'currency':'USD'})
        for sku,template in (('OLD-SKU',{'approved':True,'new_price_enabled':True}),
                             ('NEW',{'approved':False,'new_price_enabled':True}),
                             ('NEW',{'approved':True,'new_price_enabled':False})):
            with self.subTest(sku=sku,template=template),self.assertRaises(ReceivingError):
                preserved_price_sibling(parent,variation,new_sku=sku,cents=1250,temporary_id='#new',template=template)

    def test_explicit_false_tracking_override_is_not_treated_as_absent(self):
        variation,parent=catalog();self.assertEqual(effective_variation(variation,parent,'location-A')['selling_cents'],1000)
        variation['item_variation_data']['location_overrides'].append({'location_id':'location-A','track_inventory':False})
        with self.assertRaises(ReceivingError):effective_variation(variation,parent,'location-A')

    def test_archived_parent_is_unavailable_even_with_sellable_tracked_child(self):
        variation,parent=catalog();parent['item_data']['is_archived']=True
        with self.assertRaises(ReceivingError):effective_variation(variation,parent,'location-A')

    def test_excluded_accepted_lines_and_price_split_overages_fail_with_other_bindings_valid(self):
        variation,parent=catalog();identity={'name':'Fictional card','game':'magic','kind':'card'};key=source_line_id(identity)
        effective=effective_variation(variation,parent,'location-A')
        binding=effective|{'id':str(uuid.uuid4()),'version':1,'identity_hash':key,'location_id':'location-A','evidence_hash':'a'*64}
        line_id=str(uuid.uuid4())
        line={'review_line_id':str(uuid.uuid4()),'source_line_key':key,'accepted_line_id':line_id,'quantity':2,'selling_cents':1000,'reviewed':True}
        plan={'workflow_kind':'accepted_trade','location_id':'location-A','lines':[line]}
        accepted={'id':line_id,'source_line_key':key,'identity':identity,'quantity':2,'included':True}
        objects={'variation-A':variation,'parent-A':parent}
        self.assertEqual(len(bind_targets(plan,{key:4},[accepted],[binding],objects)[0]),1)
        with self.assertRaisesRegex(ReceivingError,'accepted line binding'):
            bind_targets(plan,{key:4},[accepted|{'included':False}],[binding],objects)
        split=deepcopy(plan);split['lines'].append(line|{'review_line_id':str(uuid.uuid4()),'quantity':1})
        with self.assertRaisesRegex(ReceivingError,'accepted line quantity'):
            bind_targets(split,{key:4},[accepted],[binding],objects)


class ReceivingRecoveryResourceTests(unittest.TestCase):
    def adapter(self,pages):
        adapter=ReceivingSquare(SimpleNamespace())
        adapter._call=Mock(side_effect=pages)
        return adapter

    def test_complete_history_pages_keep_only_expected_references_and_do_not_stop_early(self):
        unrelated=[adjustment(ident='other-'+str(i),reference_id='other-operation-'+str(i)) for i in range(1000)]
        adapter=self.adapter([{'changes':unrelated,'cursor':'page-2'},
                              {'changes':[adjustment()],'cursor':'page-3'}, {'changes':unrelated}])
        found=adapter.history(None,body(),datetime.now(timezone.utc))
        self.assertEqual(found,[adjustment()]);self.assertEqual(adapter._call.call_count,3)
        self.assertEqual(history_proof(body(),found,'app-A')['adjustment_ids'],['adjustment-A'])
        self.assertTrue(all(call.kwargs['deadline']>0 for call in adapter._call.call_args_list))
        self.assertEqual(adapter._call.call_args_list[1].args[2]['cursor'],'page-2')

    def test_matching_first_page_does_not_hide_later_duplicate_or_repeated_cursor(self):
        adapter=self.adapter([{'changes':[adjustment()],'cursor':'page-2'}, {'changes':[adjustment(ident='second')]}])
        found=adapter.history(None,body(),datetime.now(timezone.utc))
        with self.assertRaises(ReceivingError):history_proof(body(),found,'app-A')
        for pages in ([{'changes':[],'cursor':'again'},{'changes':[],'cursor':'again'}],
                      [{'changes':[adjustment()],'cursor':'next'},{'changes':[adjustment(quantity='3')]}]):
            with self.subTest(pages=pages),self.assertRaises(ReceivingError):
                self.adapter(pages).history(None,body(),datetime.now(timezone.utc))

    def test_recovery_aggregate_bytes_and_total_deadline_fail_honestly(self):
        pages=[{'changes':[],'cursor':str(i),'irrelevant_padding':'x'*(3*1024*1024)} for i in range(4)]
        adapter=self.adapter(pages)
        with self.assertRaisesRegex(ReceivingError,'resource budget'):adapter.history(None,body(),datetime.now(timezone.utc))
        self.assertEqual(adapter._call.call_count,3)
        adapter=self.adapter([{'changes':[adjustment()]}])
        with patch('hosted.receiving_square.time.monotonic',side_effect=[100,146]),self.assertRaises(ReceivingError):
            adapter.history(None,body(),datetime.now(timezone.utc))
        self.assertEqual(adapter._call.call_args.kwargs['deadline'],145)

    def test_transport_uses_exact_saved_bytes_fixed_version_no_redirect_and_closes_response(self):
        payload=canonical(body());response=Mock(status_code=200,headers={})
        response.iter_content.return_value=[canonical(counts())]
        transport=SimpleNamespace(post=Mock(return_value=response))
        adapter=ReceivingSquare(SimpleNamespace(base='https://fictional-provider.invalid',transport=transport))
        access=SimpleNamespace(token='FICTIONAL-NOT-A-CREDENTIAL')
        proof=adapter.dispatch_inventory(access,payload)
        call=transport.post.call_args
        self.assertEqual(call.args[0],'https://fictional-provider.invalid/v2/inventory/changes/batch-create')
        self.assertIs(call.kwargs['data'],payload)
        self.assertFalse(call.kwargs['allow_redirects']);self.assertTrue(call.kwargs['stream'])
        self.assertEqual(call.kwargs['headers']['Square-Version'],API_VERSION)
        self.assertEqual(proof['kind'],'batch_create_ack');response.close.assert_called_once()

    def test_stream_deadline_and_redirect_are_unknown_not_success_or_automatic_retry(self):
        for status,times in ((302,[100,100]),(200,[100,100,106])):
            response=Mock(status_code=status,headers={});response.iter_content.return_value=[canonical(counts())]
            transport=SimpleNamespace(post=Mock(return_value=response))
            adapter=ReceivingSquare(SimpleNamespace(base='https://fictional-provider.invalid',transport=transport))
            with self.subTest(status=status),patch('hosted.receiving_square.time.monotonic',side_effect=times),self.assertRaises(ReceivingError) as caught:
                adapter._call(SimpleNamespace(token='FICTIONAL'),'/v2/inventory/changes/batch-create',body=canonical(body()),deadline=105)
            self.assertEqual(caught.exception.code,'PROVIDER_OUTCOME_UNKNOWN')
            self.assertEqual(transport.post.call_count,1);response.close.assert_called_once()

    def test_sku_search_consumes_all_pages_and_rejects_case_insensitive_collision(self):
        variation,_=catalog();target={'target_sku':variation['item_variation_data']['sku'],'variation_id':variation['id']}
        adapter=self.adapter([{'objects':[variation],'cursor':'last'},{'objects':[deepcopy(variation)]}])
        adapter.assert_skus(None,[target]);self.assertEqual(adapter._call.call_count,2)
        query=adapter._call.call_args_list[0].args[2]
        self.assertEqual(query['query']['set_query'],{'attribute_name':'sku','attribute_values':['SKU-A>^~']})
        other=deepcopy(variation);other['id']='different-variation';other['item_variation_data']['sku']='sku-a>^~'
        adapter=self.adapter([{'objects':[variation],'cursor':'last'},{'objects':[other]}])
        with self.assertRaises(ReceivingError):adapter.assert_skus(None,[target])
        self.assertEqual(adapter._call.call_count,2)

    def test_sku_search_incomplete_missing_wrong_or_conflicting_objects_never_prove_uniqueness(self):
        variation,_=catalog();target={'sku':variation['item_variation_data']['sku'],'variation_id':variation['id']}
        changed=deepcopy(variation);changed['version']=999
        for pages in ([{'objects':[]}],[{'objects':[variation],'cursor':'repeat'},{'objects':[],'cursor':'repeat'}],
                      [{'objects':[variation],'cursor':'last'},{'objects':[changed]}],
                      [{'objects':[dict(variation,id='wrong-variation')]}],[{'objects':[{'type':'ITEM','id':'parent'}]}]):
            with self.subTest(pages=len(pages)),self.assertRaises(ReceivingError):self.adapter(pages).assert_skus(None,[target])
        adapter=self.adapter([{'objects':[],'cursor':str(index)} for index in range(20)])
        with self.assertRaisesRegex(ReceivingError,'page bound'):adapter.assert_skus(None,[target])
        self.assertEqual(adapter._call.call_count,20)

    def test_sku_search_deadline_and_aggregate_budget_fail_without_silent_partial_success(self):
        variation,_=catalog();target={'sku':variation['item_variation_data']['sku'],'variation_id':variation['id']}
        adapter=self.adapter([{'objects':[variation]}])
        with patch('hosted.receiving_square.time.monotonic',side_effect=[100,146]),self.assertRaises(ReceivingError):
            adapter.assert_skus(None,[target])
        adapter=self.adapter([{'objects':[variation],'cursor':str(index),'padding':'x'*(3*1024*1024)} for index in range(3)])
        with self.assertRaisesRegex(ReceivingError,'supported bound'):adapter.assert_skus(None,[target])
        self.assertEqual(adapter._call.call_count,3)


if __name__=='__main__':unittest.main(verbosity=2)
