"""UNRUN exact-SKU read-adapter units; no database/desktop/browser imports.

Fictional provider responses exercise the real fixed receiving adapter. These
cases establish no live Square, scanner, printer or physical-identity evidence.
"""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hosted.receiving_plan import API_VERSION, ReceivingError, canonical
from hosted.receiving_square import ReceivingSquare
from hosted.security import ServiceError


def variation(sku=' 000Ab^>~7 ', ident='fixture-variation'):
    return {'type':'ITEM_VARIATION','id':ident,'version':17,
            'item_variation_data':{'item_id':'fixture-parent','sku':sku}}


class SquareSkuLookupTests(unittest.TestCase):
    def adapter(self, pages):
        adapter=ReceivingSquare(SimpleNamespace())
        adapter._call=Mock(side_effect=pages)
        return adapter

    def test_exact_raw_query_and_complete_pagination_preserve_ids_case_spaces_and_zeroes(self):
        row=variation(ident='opaque /?%2F>^~')
        adapter=self.adapter([{'objects':[row],'cursor':'next >/?%2F'}, {'objects':[deepcopy(row)],'cursor':''}])
        self.assertEqual(adapter.lookup_sku(None,row['item_variation_data']['sku']),[row])
        first=adapter._call.call_args_list[0]
        self.assertEqual(first.args[1],'/v2/catalog/search')
        self.assertEqual(first.args[2],{'object_types':['ITEM_VARIATION'],'include_deleted_objects':False,
            'include_related_objects':False,'limit':20,
            'query':{'set_query':{'attribute_name':'sku','attribute_values':[' 000Ab^>~7 ']}}})
        self.assertEqual(adapter._call.call_args_list[1].args[2]['cursor'],'next >/?%2F')
        self.assertEqual(adapter._call.call_count,2)

    def test_case_variants_are_retained_for_service_collision_checks_without_normalizing_skus(self):
        exact=variation();other=variation(' 000aB^>~7 ',ident='other-variation')
        adapter=self.adapter([{'objects':[exact,other]}])
        self.assertEqual(adapter.lookup_sku(None,' 000Ab^>~7 '),[exact,other])

    def test_empty_complete_pages_are_no_matches_only_after_a_valid_terminal_cursor(self):
        for ending in ({},{'cursor':None},{'cursor':''}):
            with self.subTest(ending=ending):
                adapter=self.adapter([{'objects':[],'cursor':'last'},dict(ending,objects=[])])
                self.assertEqual(adapter.lookup_sku(None,'000000000007'),[])
                self.assertEqual(adapter._call.call_count,2)

    def test_later_malformed_conflicting_unrelated_or_deleted_rows_never_return_a_partial_match(self):
        row=variation();changed=deepcopy(row);changed['version']=18
        bad_rows=[{'id':'parent','type':'ITEM'},dict(row,is_deleted=True),dict(row,is_deleted=0),dict(row,is_deleted=None),
            dict(row,item_variation_data={'item_id':'fixture-parent','sku':'WRONG'}),
            dict(row,id=' spaced-id '),dict(row,item_variation_data=None),changed]
        for bad in bad_rows:
            with self.subTest(bad=bad):
                adapter=self.adapter([{'objects':[row],'cursor':'last'},{'objects':[bad]}])
                with self.assertRaises(ReceivingError):adapter.lookup_sku(None,' 000Ab^>~7 ')
                self.assertEqual(adapter._call.call_count,2)
        for cursor in (False,0,[],{},'again'):
            with self.subTest(cursor=cursor):
                adapter=self.adapter([{'objects':[row],'cursor':'again'},{'objects':[],'cursor':cursor}])
                with self.assertRaises(ReceivingError):adapter.lookup_sku(None,' 000Ab^>~7 ')
        adapter=self.adapter([{'objects':[row],'cursor':'last'},ReceivingError('Fictional read failure',502)])
        with self.assertRaises(ReceivingError):adapter.lookup_sku(None,' 000Ab^>~7 ')

    def test_page_row_distinct_byte_and_total_deadline_bounds_never_return_partial_candidates(self):
        row=variation()
        pages=[{'objects':[],'cursor':str(index)} for index in range(10)]
        adapter=self.adapter(pages)
        with self.assertRaises(ReceivingError):adapter.lookup_sku(None,' 000Ab^>~7 ')
        self.assertEqual(adapter._call.call_count,10)
        for page in ({'objects':[row]*1001},
                     {'objects':[variation(ident='v-'+str(index)) for index in range(21)]},
                     {'objects':[row],'padding':'x'*(2*1024*1024)}):
            with self.subTest(bound=list(page)):
                with self.assertRaises(ReceivingError):self.adapter([page]).lookup_sku(None,' 000Ab^>~7 ')
        adapter=self.adapter([{'objects':[row]}])
        with patch('hosted.receiving_square.time.monotonic',side_effect=[100,131]):
            with self.assertRaises(ReceivingError):adapter.lookup_sku(None,' 000Ab^>~7 ')
        self.assertEqual(adapter._call.call_args.kwargs['deadline'],130)

    def test_invalid_sku_input_never_calls_provider_or_coerces_values(self):
        for value in (None,True,7,[],{},'', ' ', 'x'*101, 'A\nB','A\u202eB','A\ud800B'):
            with self.subTest(value=repr(value)):
                adapter=self.adapter([])
                with self.assertRaises(ServiceError):adapter.lookup_sku(None,value)
                adapter._call.assert_not_called()

    def test_fixed_read_transport_pins_version_forbids_redirects_and_closes_every_response(self):
        for status in (200,302):
            with self.subTest(status=status):
                response=Mock(status_code=status,headers={})
                response.iter_content.return_value=[canonical({'objects':[variation()]})]
                transport=SimpleNamespace(post=Mock(return_value=response),get=Mock())
                adapter=ReceivingSquare(SimpleNamespace(base='https://fixture.invalid',transport=transport))
                if status==200:self.assertEqual(adapter.lookup_sku(SimpleNamespace(token='FICTIONAL'),' 000Ab^>~7 '),[variation()])
                else:
                    with self.assertRaises(ReceivingError):adapter.lookup_sku(SimpleNamespace(token='FICTIONAL'),' 000Ab^>~7 ')
                call=transport.post.call_args
                self.assertEqual(call.args,('https://fixture.invalid/v2/catalog/search',))
                self.assertEqual(call.kwargs['headers']['Square-Version'],API_VERSION)
                self.assertFalse(call.kwargs['allow_redirects']);self.assertTrue(call.kwargs['stream'])
                self.assertIn(b' 000Ab^>~7 ',call.kwargs['data'])
                self.assertEqual(transport.post.call_count,1);transport.get.assert_not_called()
                response.close.assert_called_once()


if __name__=='__main__':
    unittest.main(verbosity=2)
