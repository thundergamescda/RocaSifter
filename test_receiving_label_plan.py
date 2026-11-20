"""Independent bounded arithmetic and immutable full-mark label-plan cases."""
import copy
import json
import unittest
import uuid

from hosted_labels import fingerprint,mark
import station_receiving_label_plan as plan


def card(index=1, *, quantity=3, sku='ABC123', name='Pokémon fixture'):
    ident=str(uuid.UUID(int=index))
    value={'row_id':ident,'source_line_key':'b'*64,'card_name':name,'game':'pokemon',
        'card_set':'Fixture','card_num':'001','condition':'Near Mint','printing':'Normal',
        'qty':quantity,'sku':sku,'final_price':3.13,'include':True,'reviewed':True,
        'price_confirmed':True,'price_missing':False}
    value['hosted_sku_resolution']=mark(value,receiving_id='11111111-1111-4111-8111-111111111111',
        receipt_hash='a'*64,review_line_id=ident,source_line_key='b'*64,identity_hash='c'*64,
        sku=sku,selling_cents=313,confirmed_quantity=quantity)
    return value


PROFILE={'stock':'2x1','layout':'side','dpi':203}


class ReceivingLabelPlanTests(unittest.TestCase):
    def test_partial_range_crosses_sorted_rows_without_changing_full_quantities(self):
        cards=[card(2,quantity=4),card(1,quantity=3)];before=copy.deepcopy(cards)
        result=plan.build_plan(cards,PROFILE,copy_offset=2,copy_count=3);piece=plan.part(result,0)
        self.assertEqual(piece['renderCounts'],{cards[1]['row_id']:1,cards[0]['row_id']:2})
        self.assertEqual(result['totalCopies'],7);self.assertEqual(piece['copyCount'],3)
        self.assertEqual(cards,before);self.assertEqual(cards[1]['hosted_sku_resolution']['confirmed_quantity'],3)

    def test_reordered_rows_and_unprinted_mutable_fields_preserve_print_plan(self):
        cards=[card(2),card(1)];first=plan.build_plan(cards,PROFILE)
        other=copy.deepcopy(list(reversed(cards)))
        for value in other:value.update(storage_location='Edited',notes='日本語',customer_reference='א',title='Unprinted')
        self.assertEqual(plan.build_plan(other,PROFILE),first)

    def test_legal_million_copy_plan_is_compact_and_last_part_is_exact(self):
        cards=[card(index,quantity=100000) for index in range(1,11)]
        result=plan.build_plan(cards,{'stock':'4x2','layout':'side','dpi':300})
        self.assertEqual(result['totalCopies'],1000000);self.assertEqual(len(result['rows']),10)
        self.assertLess(len(json.dumps(result)),5000);self.assertGreater(result['partCount'],1000)
        self.assertLessEqual(result['partSize'],1000)
        first=plan.part(result,0);last=plan.part(result,result['partCount']-1)
        self.assertEqual(first['copyOffset'],0)
        self.assertEqual(last['copyOffset']+last['copyCount'],1000000)
        self.assertEqual(sum(last['renderCounts'].values()),last['copyCount'])

    def test_adaptive_profile_budget_bounds_every_worst_case_part(self):
        for stock in ('2x1','3x1','4x2'):
            for dpi in (203,300):
                with self.subTest(stock=stock,dpi=dpi):
                    result=plan.build_plan([card(quantity=100000)],{'stock':stock,'layout':'side','dpi':dpi})
                    budget=result['budget'];self.assertLessEqual(result['partSize'],1000)
                    self.assertLessEqual(result['partSize']*budget['maxZplRowBytes']+budget['reservedBytes'],plan.MAX_ARTIFACT_BYTES)

    def test_nonwhole_bool_negative_zero_and_overrun_ranges_reject(self):
        for offset,count in ((True,1),(0,False),(-1,1),(0,0),(3,None),(2,2),(0,1.0)):
            with self.subTest(offset=offset,count=count),self.assertRaises(plan.LabelPlanError):
                plan.build_plan([card()],PROFILE,offset,count)

    def test_profile_is_explicit_and_frozen_without_default_or_override(self):
        for chosen in (None,{},PROFILE|{'dpi':True},PROFILE|{'layout':'other'},PROFILE|{'extra':1}):
            with self.subTest(chosen=chosen),self.assertRaises(plan.LabelPlanError):plan.build_plan([card()],chosen)

    def test_full_mark_types_quantities_source_and_retail_binding_reject(self):
        for field,value in (('version',True),('confirmed_quantity',3.0),('selling_cents',313.0),('state','planned')):
            broken=card();broken['hosted_sku_resolution'][field]=value
            body={key:item for key,item in broken['hosted_sku_resolution'].items() if key!='fingerprint'}
            broken['hosted_sku_resolution']['fingerprint']=fingerprint(body)
            with self.subTest(field=field),self.assertRaises(plan.LabelPlanError):plan.build_plan([broken],PROFILE)
        for field,value in (('qty',True),('final_price',True),('final_price',9.99),('sku','DIFFERENT'),
                            ('source_line_key','d'*64),('reviewed',False),('price_confirmed',False)):
            broken=card();broken[field]=value
            with self.subTest(field=field),self.assertRaises(plan.LabelPlanError):plan.build_plan([broken],PROFILE)

    def test_duplicate_review_ids_and_cross_operation_or_terminal_marks_reject(self):
        with self.assertRaises(plan.LabelPlanError):plan.build_plan([card(),card()],PROFILE)
        for field,value in (('receiving_id',str(uuid.uuid4())),('receipt_hash','d'*64)):
            second=card(2);second['hosted_sku_resolution'][field]=value
            second['hosted_sku_resolution']['fingerprint']=fingerprint({key:item for key,item in second['hosted_sku_resolution'].items() if key!='fingerprint'})
            with self.subTest(field=field),self.assertRaises(plan.LabelPlanError):plan.build_plan([card(),second],PROFILE)

    def test_per_row_total_caps_and_unknown_count_ids_reject(self):
        for cards in ([card(quantity=100001)],[card(index,quantity=100000) for index in range(1,12)]):
            with self.assertRaises(plan.LabelPlanError):plan.build_plan(cards,PROFILE)
        for counts in ({str(uuid.uuid4()):1},{card()['row_id']:False},{card()['row_id']:4},{card()['row_id']:0}):
            with self.assertRaises(plan.LabelPlanError):plan.validate_render_counts([card()],PROFILE,counts)

    def test_rehashed_plan_tampering_and_invalid_part_indices_reject(self):
        original=plan.build_plan([card()],PROFILE)
        for field,value in (('partSize',True),('totalCopies',3.0),('partCount',False),('copyCount',4)):
            broken=copy.deepcopy(original);broken[field]=value
            broken['renderPlanHash']=fingerprint({'domain':plan.PLAN_DOMAIN,'plan':{key:item for key,item in broken.items() if key!='renderPlanHash'}})
            with self.subTest(field=field),self.assertRaises(plan.LabelPlanError):plan.part(broken,0)
        for index in (True,-1,1,0.0):
            with self.subTest(index=index),self.assertRaises(plan.LabelPlanError):plan.part(original,index)


if __name__=='__main__':unittest.main(verbosity=2)
