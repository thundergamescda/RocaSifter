import copy
from datetime import datetime, timedelta, timezone
import unittest
from pricing_review import Identity, review, review_batch, REQUIRED_SOURCES, GRADED_SOURCES, lookup_links
from urllib.parse import urlsplit, parse_qs


NOW = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
CARD = {'game':'Pokémon', 'card_name':'Charizard ex', 'card_set':'Scarlet & Violet 151',
        'card_num':'006/165', 'printing':'Holofoil', 'condition':'Near Mint', 'language':'English',
        'final_price':1.25, 'qty':1, 'row_id':'one'}


def evidence():
    rows=[]
    for source in REQUIRED_SOURCES:
        for n in range(3 if source in ('tcg_sales','ebay_sold') else 1):
            rows.append({'source':source, 'record_id':f'{source}-{n}', 'identity':copy.deepcopy(CARD),
                         'currency':'USD', 'price_basis':'item', 'amount':10+n/10,
                         'observed_at':NOW.isoformat(), 'sold_at':(NOW-timedelta(days=2)).isoformat(),
                         'url':('https://www.tcgplayer.com/product/fictional' if source.startswith('tcg') else
                                'https://www.pricecharting.com/game/fictional' if source=='pricecharting' else 'https://www.ebay.com/itm/fictional'),
                         'attribution':'exact', 'is_lot':False,
                         'quantity':1, 'graded':False, 'signed':False, 'altered':False, 'proxy':False,
                         'sale_price_verified':True, 'active':True,'guide_verified':True})
    return rows


class PricingReviewTests(unittest.TestCase):
    def test_all_sources_agree_proposal_does_not_change_price_or_card(self):
        card=copy.deepcopy(CARD);rows=evidence();before=copy.deepcopy(rows)
        result=review(card,rows,NOW)
        self.assertEqual(result['status'],'proposal_ready')
        self.assertEqual(result['suggested_price'],10)
        self.assertEqual(card,CARD);self.assertEqual(rows,before)

    def test_different_finishes_conditions_languages_numbers_and_sets_never_mix(self):
        changes={'printing':'Reverse Holofoil','condition':'Lightly Played','language':'Japanese',
                 'card_set':'Obsidian Flames','card_num':'006/198','card_name':'Charizard V'}
        for key,value in changes.items():
            with self.subTest(key=key):
                rows=evidence();rows[0]['identity'][key]=value
                self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_gender_and_premium_treatments_are_not_normalized_away(self):
        for key,a,b in [('card_name','Nidoran♀','Nidoran♂'),('printing','Poké Ball Reverse Holo','Master Ball Reverse Holo'),
                        ('printing','1st Edition Holofoil','Unlimited Holofoil'),('printing','Surge Foil','Foil')]:
            left=dict(CARD,**{key:a});right=dict(CARD,**{key:b})
            self.assertNotEqual(Identity.from_card(left).cache_key,Identity.from_card(right).cache_key)

    def test_supported_aliases_match_but_price_and_quantity_do_not_change_identity(self):
        aliases=dict(CARD,game='pokemon',condition='NM',language='en',card_num='6/165',final_price=100,qty=20)
        self.assertEqual(Identity.from_card(CARD),Identity.from_card(aliases))
        self.assertNotEqual(Identity.from_card(CARD),Identity.from_card(dict(CARD,card_num='6')))

    def test_missing_sources_never_become_zero_or_a_verified_price(self):
        result=review(CARD,[],NOW)
        self.assertEqual(len(result['sources']),6)
        self.assertTrue(all(s['status']=='unavailable' for s in result['sources'].values()))
        self.assertIsNone(result['suggested_price'])

    def test_one_marketplace_even_with_many_listings_cannot_verify_price(self):
        rows=[e for e in evidence() if e['source'].startswith('tcg')]
        self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_listings_are_not_averaged_into_sold_prices(self):
        rows=evidence()
        for e in rows:
            if e['source'].endswith('listings'):e['amount']=14
        self.assertEqual(review(CARD,rows,NOW)['suggested_price'],10)

    def test_large_current_listing_disagreement_flags_review_without_setting_price(self):
        rows=evidence()
        for e in rows:
            if e['source'].endswith('listings'):e['amount']=10000
        result=review(CARD,rows,NOW)
        self.assertEqual(result['status'],'needs_review');self.assertIsNone(result['suggested_price'])

    def test_source_links_match_the_stated_marketplace(self):
        for url in ['javascript:alert(1)','https://tcgplayer.com.evil.example/product/1',
                    'https://www.ebay.com/itm/1','http://www.tcgplayer.com/product/1']:
            rows=evidence();rows[0]['url']=url
            self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_source_freshness_and_future_dates(self):
        for key,value in [('observed_at',(NOW-timedelta(days=3)).isoformat()),
                          ('observed_at',(NOW+timedelta(days=1)).isoformat()),
                          ('observed_at','2026-10-24T18:00:00')]:
            rows=evidence();rows[0][key]=value
            self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')
        rows=evidence()
        for e in rows:
            if e['source']=='ebay_sold':e['sold_at']=(NOW-timedelta(days=31)).isoformat()
        self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_shared_unknown_or_unverified_accepted_offer_sales_are_rejected(self):
        for change in [{'attribution':'shared'},{'attribution':'unknown'},{'sale_price_verified':False},
                       {'condition':None}]:
            rows=evidence()
            for e in rows:
                if e['source']=='ebay_sold':
                    if 'condition' in change:e['identity']['condition']=None
                    else:e.update(change)
            self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_currency_shipping_basis_and_non_positive_prices(self):
        for change in [{'currency':'EUR'},{'price_basis':'item_plus_shipping'},{'amount':0},
                       {'amount':float('nan')},{'amount':True},{'amount':-10}]:
            rows=evidence();rows[0].update(change)
            self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_lots_grades_proxies_and_missing_attributes_are_rejected(self):
        for key,value in [('is_lot',True),('quantity',2),('quantity',True),('graded',True),('signed',True),
                          ('altered',True),('proxy',True),('proxy',None)]:
            rows=evidence();rows[0][key]=value
            self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_duplicate_sales_do_not_create_fake_liquidity(self):
        rows=evidence()
        for e in rows:
            if e['source']=='ebay_sold':e['record_id']='one-sale'
        result=review(CARD,rows,NOW)
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['sources']['ebay_sold']['count'],1)

    def test_disagreement_and_outliers_need_manual_review(self):
        rows=evidence()
        for e in rows:
            if e['source']=='ebay_sold':e['amount']=25
        self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')
        rows=evidence();next(e for e in rows if e['source']=='ebay_sold')['amount']=100
        self.assertEqual(review(CARD,rows,NOW)['status'],'needs_review')

    def test_manual_custom_prices_preserved_and_all_pokemon_receive_a_result(self):
        cards=[CARD,dict(CARD,row_id='two',printing='Reverse Holofoil'),
               dict(CARD,row_id='three',entry_source='manual'),
               dict(CARD,row_id='four',game='Magic'),dict(CARD,row_id='five',game='')]
        results=review_batch(cards,{Identity.from_card(CARD).cache_key:evidence()},NOW)
        self.assertEqual([r['status'] for r in results],
                         ['proposal_ready','needs_review','manual_price','not_pokemon','needs_identity'])
        self.assertEqual(len(results),len(cards))

    def slab_evidence(self,card):
        rows=[]
        for source in GRADED_SOURCES:
            for n in range(3 if source=='ebay_sold' else 1):
                e=copy.deepcopy(evidence()[0]);e.update(source=source,record_id=f'{source}-{n}',
                    amount=100+n,identity=copy.deepcopy(card),graded=True,guide_verified=True,
                    url='https://www.pricecharting.com/game/fictional' if source=='pricecharting' else 'https://www.ebay.com/itm/fictional')
                rows.append(e)
        return rows

    def test_explicit_slab_lookup_compares_exact_grade_and_preserves_manual_price(self):
        card=dict(CARD,entry_source='manual',condition='PSA 10',final_price=150)
        result=review(card,self.slab_evidence(card),NOW,lookup_manual=True)
        self.assertEqual(result['status'],'proposal_ready');self.assertEqual(result['suggested_price'],101)
        self.assertEqual(result['tcgplayer_role'],'raw_card_reference')
        self.assertEqual(result['required_sources'],list(GRADED_SOURCES));self.assertEqual(card['final_price'],150)
        self.assertEqual(review(card,self.slab_evidence(card),NOW)['status'],'manual_price')

    def test_graders_grades_and_premium_tiers_are_not_pooled(self):
        pairs=[('PSA 10','PSA 9'),('PSA 10','BGS 10'),('BGS Black Label 10','BGS 10'),
               ('CGC Pristine 10','CGC Gem Mint 10'),('CGC Perfect 10','CGC Pristine 10')]
        for target,wrong in pairs:
            with self.subTest(target=target,wrong=wrong):
                card=dict(CARD,condition=target);rows=self.slab_evidence(card)
                for e in rows:e['identity']['condition']=wrong
                self.assertEqual(review(card,rows,NOW)['status'],'needs_review')

    def test_ambiguous_or_invalid_grades_need_identification(self):
        for grade in ['CGC 10','PSA 9.5','Grade 9','BGS Black Label 9.5']:
            with self.subTest(grade=grade):
                self.assertEqual(review(dict(CARD,condition=grade),[],NOW)['status'],'needs_identity')

    def test_raw_tcg_prices_never_set_a_slab_price(self):
        card=dict(CARD,condition='PSA 10');rows=self.slab_evidence(card)
        raw=[e for e in evidence() if e['source'].startswith('tcg')]
        for e in raw:
            if e['source'].startswith('tcg'):e['amount']=10000
        result=review(card,rows+raw,NOW)
        self.assertEqual(result['suggested_price'],101)
        result=review(card,raw,NOW)
        self.assertIsNone(result['suggested_price'])

    def test_generic_pricecharting_grade_cannot_be_claimed_as_psa(self):
        card=dict(CARD,condition='PSA 9');rows=self.slab_evidence(card)
        for e in rows:
            if e['source']=='pricecharting':e['identity']['condition']='Grade 9'
        result=review(card,rows,NOW)
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(result['sources']['pricecharting']['count'],0)

    def test_separate_game_batch_applies_once_without_changing_rows(self):
        card=dict(CARD);card.pop('game');before=copy.deepcopy(card)
        result=review_batch([card],{Identity.from_card(CARD).cache_key:evidence()},NOW,batch_game='pokemon')
        self.assertEqual(result[0]['status'],'proposal_ready');self.assertEqual(card,before)
        self.assertEqual(review_batch([CARD],{},NOW,batch_game='magic')[0]['status'],'needs_identity')

    def test_direct_links_preserve_grade_finish_and_encoded_collector_number(self):
        card=dict(CARD,card_name='Nidoran♀',condition='BGS Black Label 10',printing='Master Ball Reverse Holo')
        links=lookup_links(card)
        query=parse_qs(urlsplit(links['ebay_sold']).query)
        self.assertEqual(query['LH_Sold'],['1']);self.assertEqual(query['LH_Complete'],['1'])
        self.assertIn('black label 10',query['_nkw'][0]);self.assertIn('006/165',query['_nkw'][0])
        self.assertIn('Master Ball Reverse Holo',query['_nkw'][0]);self.assertIn('Nidoran♀',query['_nkw'][0])
        self.assertEqual(links['tcgplayer_role'],'raw_card_reference')
        self.assertNotIn('LH_Sold',parse_qs(urlsplit(links['ebay_listings']).query))
        self.assertEqual(urlsplit(links['pricecharting']).hostname,'www.pricecharting.com')

    def test_magic_source_search_uses_selected_game(self):
        links=lookup_links(dict(CARD,game='magic'))
        self.assertIn('/search/magic/product',links['tcgplayer'])
        self.assertIn('Magic The Gathering',parse_qs(urlsplit(links['ebay_sold']).query)['_nkw'][0])


if __name__=='__main__':unittest.main()
