"""Price-specific SKU contract against a deterministic fake Square catalog."""
import copy
import json
import unittest
from unittest.mock import patch
import roca_to_square as core
from square_variations import upload, variant_sku


class VariationsTests(unittest.TestCase):
    def setUp(self):
        self.card={'card_name':'Fixture Card','card_num':'1','card_set':'TEST','condition':'Near Mint','printing':'Normal','language':'English','qty':2,'final_price':10}
        self.base=core.generate_sku('Fixture Card','1','Near Mint','Normal',0,'English','TEST')
        self.card['sku']=self.base
        self.original={'id':'old-v','type':'ITEM_VARIATION','version':7,'item_variation_data':{'item_id':'parent','sku':self.base,'track_inventory':True,'pricing_type':'FIXED_PRICING','price_money':{'amount':1000,'currency':'USD'}}}
        self.catalog={self.base:copy.deepcopy(self.original)};self.writes=[]

    def request(self,key,url,payload,headers):
        class Response:
            def __init__(self,data):self.data=data
            def raise_for_status(self):pass
            def json(self):return self.data
        if url.endswith('/search'):
            v=self.catalog.get(payload['query']['exact_query']['attribute_value'])
            return Response({'objects':[copy.deepcopy(v)] if v else []})
        self.writes.append((key,copy.deepcopy(payload)))
        if url.endswith('/batch-upsert'):
            maps=[]
            for obj in payload['batches'][0]['objects']:
                data=obj['item_variation_data'];self.catalog[data['sku']]=obj|{'id':'new-v'}
                maps.append({'client_object_id':obj['id'],'object_id':'new-v'})
            return Response({'id_mappings':maps})
        return Response({})

    def run_upload(self,cards):
        with patch.object(core,'square_locations',return_value=[{'id':'loc','name':'Test','status':'ACTIVE'}]):
            return upload(core,cards,'fictional-token','sandbox','loc',lambda *_:None,self.request)

    def test_matching_price_adds_quantity_without_catalog_reprice(self):
        result=self.run_upload([self.card,self.card|{'qty':1}])
        self.assertFalse(result['errors']);self.assertEqual(result['inventory_count'],3)
        self.assertEqual(len(self.writes),1)
        adjustment=self.writes[0][1]['changes'][0]['adjustment']
        self.assertEqual(adjustment['catalog_object_id'],'old-v');self.assertEqual(adjustment['quantity'],'3')
        self.assertEqual(self.catalog[self.base],self.original)

    def test_different_price_creates_sibling_sku_and_preserves_original(self):
        result=self.run_upload([self.card|{'final_price':12.50}])
        self.assertFalse(result['errors'])
        newsku=variant_sku(self.base,1250)
        self.assertNotEqual(newsku,self.base);self.assertIn(newsku,self.catalog)
        self.assertEqual(self.catalog[self.base],self.original)
        self.assertEqual(self.catalog[newsku]['item_variation_data']['item_id'],'parent')
        self.assertEqual(self.writes[-1][1]['changes'][0]['adjustment']['catalog_object_id'],'new-v')

    def test_same_details_and_new_price_reuses_existing_sibling(self):
        newsku=variant_sku(self.base,1250)
        self.run_upload([self.card|{'final_price':12.50}]);self.writes=[]
        result=self.run_upload([self.card|{'final_price':12.50}])
        self.assertFalse(result['errors']);self.assertEqual(len(self.writes),1)
        self.assertEqual(self.catalog[newsku]['item_variation_data']['price_money']['amount'],1250)


if __name__=='__main__':unittest.main()
