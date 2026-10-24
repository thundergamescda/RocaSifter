import copy
from datetime import datetime, timedelta, timezone
import unittest
from pricing_review import Identity, review, review_batch, REQUIRED_SOURCES


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
                         'url':('https://www.tcgplayer.com/product/fictional' if source.startswith('tcg') else 'https://www.ebay.com/itm/fictional'),
                         'attribution':'exact', 'is_lot':False,
                         'quantity':1, 'graded':False, 'signed':False, 'altered':False, 'proxy':False,
                         'sale_price_verified':True, 'active':True})
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
        self.assertEqual(len(result['sources']),5)
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


if __name__=='__main__':unittest.main()
