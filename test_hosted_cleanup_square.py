"""Read-only cleanup collection tests with fictional streamed Square responses.

These exercise pagination and incomplete evidence, not live Square, continuous
inventory history, shared SQL integration, or permission to delete a variation.
"""
from copy import deepcopy
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch

from hosted.cleanup_square import CleanupSquare
from hosted.receiving_plan import API_VERSION, READ_SCOPES
from hosted.security import ServiceError


def access():
    return SimpleNamespace(token='FICTIONAL-NOT-A-CREDENTIAL', generation='generation-A',
        proof={'merchant_id':'merchant-A','environment':'production','application_id':'application-A',
               'expires_at':'2030-01-01T00:00:00+00:00'})


def locations():
    return [{'id':'location-A','status':'ACTIVE','merchant_id':'merchant-A','currency':'USD'},
            {'id':'location-B','status':'INACTIVE','merchant_id':'merchant-A','currency':'CAD'}]


def objects():
    variation={'id':'variation-A','type':'ITEM_VARIATION','version':7,
        'item_variation_data':{'item_id':'parent-A','sku':'SKU-A','track_inventory':True},
        'future_field':{'keep':'unchanged'}}
    parent={'id':'parent-A','type':'ITEM','version':7,
        'item_data':{'name':'Fictional card','variations':[deepcopy(variation)]}}
    return [parent,variation]


def count(location='location-A',quantity='0',variation='variation-A'):
    return {'catalog_object_id':variation,'catalog_object_type':'ITEM_VARIATION',
            'location_id':location,'state':'IN_STOCK','quantity':quantity,
            'calculated_at':'2026-11-16T10:00:00Z'}


class Response:
    def __init__(self,body,status=200):
        self.status_code=status;self.headers={};self.body=deepcopy(body);self.closed=0
    def iter_content(self,chunk_size):
        raw=json.dumps(self.body,separators=(',',':')).encode()
        for start in range(0,len(raw),chunk_size):yield raw[start:start+chunk_size]
    def close(self):self.closed+=1


class Transport:
    def __init__(self,pages):
        self.pages=list(pages);self.calls=[];self.responses=[]
    def request(self,method,url,**kwargs):
        self.calls.append((method,url,deepcopy(kwargs)))
        if not self.pages:raise AssertionError('Unexpected extra provider read')
        page=self.pages.pop(0)
        response=Response(*page) if isinstance(page,tuple) else Response(page)
        self.responses.append(response);return response
    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,**kwargs)


def adapter(pages,**limits):
    transport=Transport(pages)
    square=SimpleNamespace(base='https://connect.squareup.com',transport=transport,
                           verified_access=Mock(return_value=access()))
    return CleanupSquare(square,**limits),square,transport


class CleanupReadCollectionTests(unittest.TestCase):
    def test_access_requests_only_inventory_catalog_and_merchant_read_grants(self):
        reader,square,transport=adapter([])
        self.assertIs(reader.access('tenant-A'),square.verified_access.return_value)
        square.verified_access.assert_called_once_with('tenant-A',READ_SCOPES)
        self.assertEqual(transport.calls,[])

    def test_inactive_and_other_currency_locations_are_not_filtered_out(self):
        rows=locations();reader,_,transport=adapter([{'locations':rows}])
        result=reader.locations(access())
        self.assertEqual(result,rows)
        result[0]['status']='changed'
        self.assertEqual(rows[0]['status'],'ACTIVE')
        self.assertEqual(transport.responses[0].body['locations'][0]['status'],'ACTIVE')
        self.assertEqual(transport.calls[0][0:2],('GET','https://connect.squareup.com/v2/locations'))

    def test_invalid_or_foreign_locations_fail_without_partial_coverage(self):
        for rows in ([],[locations()[0],locations()[0]],['bad'],[{'id':'location-A'}],
                     [dict(locations()[0],merchant_id='other-merchant')],
                     [dict(locations()[0],status='UNKNOWN')]):
            with self.subTest(rows=rows):
                reader,_,transport=adapter([{'locations':rows}])
                with self.assertRaises(ServiceError):reader.locations(access())
                self.assertEqual(transport.responses[0].closed,1)

    def test_catalog_consumes_every_page_and_preserves_nested_variations(self):
        parent,variation=objects()
        reader,_,transport=adapter([{'objects':[parent],'cursor':'next opaque /+?='},
                                    {'objects':[variation]}])
        result=reader.catalog(access())
        self.assertEqual(result,{'parent-A':parent,'variation-A':variation})
        self.assertEqual(len(transport.calls),2)
        query=json.loads(transport.calls[0][2]['data'])
        self.assertEqual(set(query['object_types']),{'ITEM','ITEM_VARIATION'})
        self.assertFalse(query['include_deleted_objects'])
        self.assertEqual(json.loads(transport.calls[1][2]['data'])['cursor'],'next opaque /+?=')
        result['variation-A']['future_field']['keep']='changed'
        self.assertEqual(transport.responses[1].body['objects'][0]['future_field']['keep'],'unchanged')

    def test_catalog_duplicate_top_level_and_conflicting_mirrors_are_rejected(self):
        parent,variation=objects();changed=deepcopy(variation);changed['version']=8
        for pages in ([{'objects':[parent,variation],'cursor':'next'},{'objects':[variation]}],
                      [{'objects':[parent,changed]}]):
            with self.subTest(pages=pages):
                reader,_,_=adapter(pages)
                with self.assertRaises(ServiceError):reader.catalog(access())

    def test_catalog_requires_parent_closure_and_consistent_child_binding(self):
        parent,variation=objects()
        foreign=deepcopy(parent);foreign['item_data']['variations'][0]['item_variation_data']['item_id']='other-parent'
        for rows in ([variation],[foreign,variation],
                     [dict(parent,item_data={'variations':[]}),variation]):
            with self.subTest(rows=rows):
                reader,_,_=adapter([{'objects':rows}])
                with self.assertRaises(ServiceError):reader.catalog(access())

    def test_catalog_bad_types_versions_deleted_and_foreign_merchant_are_rejected(self):
        parent,variation=objects()
        for change in ({'type':'TAX'},{'version':True},{'version':0},{'version':2**63},
                       {'is_deleted':True},{'merchant_id':'other-merchant'},{'id':'#temporary'}):
            with self.subTest(change=change):
                reader,_,_=adapter([{'objects':[dict(parent,**change),variation]}])
                with self.assertRaises(ServiceError):reader.catalog(access())

    def test_all_count_pages_are_consumed_and_missing_pairs_remain_unknown(self):
        first=count();reader,_,transport=adapter([{'counts':[first],'cursor':'tail'}, {'counts':[]}])
        result=reader.counts(access(),['variation-A'],['location-A','location-B'])
        self.assertEqual(result,{'counts':[first],'missing':[('variation-A','location-B')]})
        self.assertEqual(len(transport.calls),2)
        query=json.loads(transport.calls[0][2]['data'])
        self.assertEqual(query['catalog_object_ids'],['variation-A'])
        self.assertEqual(set(query['location_ids']),{'location-A','location-B'})
        self.assertEqual(query['states'],['IN_STOCK'])
        self.assertNotIn('updated_after',query)
        self.assertEqual(json.loads(transport.calls[1][2]['data'])['cursor'],'tail')

    def test_negative_and_positive_stock_are_retained_as_evidence_not_zeroed(self):
        rows=[count(quantity='-1.25'),count(location='location-B',quantity='2.000')]
        reader,_,_=adapter([{'counts':rows}])
        self.assertEqual(reader.counts(access(),['variation-A'],['location-A','location-B']),
                         {'counts':rows,'missing':[]})

    def test_unrequested_count_ids_locations_states_and_types_are_rejected(self):
        for change in ({'catalog_object_id':'unknown'},{'location_id':'unrequested'},
                       {'state':'SOLD'},{'catalog_object_type':'ITEM'},{'merchant_id':'other-merchant'}):
            with self.subTest(change=change):
                reader,_,_=adapter([{'counts':[dict(count(),**change)]}])
                with self.assertRaises(ServiceError):reader.counts(access(),['variation-A'],['location-A'])

    def test_duplicate_count_tuple_is_rejected_even_when_identical(self):
        for second in (count(),count(quantity='1')):
            with self.subTest(second=second):
                reader,_,transport=adapter([{'counts':[count()],'cursor':'next'},{'counts':[second]}])
                with self.assertRaises(ServiceError):reader.counts(access(),['variation-A'],['location-A'])
                self.assertEqual(len(transport.calls),2)

    def test_malformed_quantities_and_unusable_timestamps_are_rejected(self):
        changes=[{'quantity':value} for value in (0,True,None,'','NaN','Infinity','-Infinity',' 0 ')]
        changes += [{'calculated_at':value} for value in (None,'bad','2026-11-16T10:00:00',
                    '2026-11-16T10:00:00+00:60','2026-11-16T10:00:00+24:00')]
        for change in changes:
            with self.subTest(change=change):
                reader,_,_=adapter([{'counts':[dict(count(),**change)]}])
                with self.assertRaises(ServiceError):reader.counts(access(),['variation-A'],['location-A'])

    def test_invalid_or_duplicate_requested_ids_fail_before_provider_contact(self):
        for variations,places in ((['variation-A','variation-A'],['location-A']),
                                 (['variation-A'],['location-A','location-A']),
                                 (['#temporary'],['location-A']),(['variation-A'],[''])):
            with self.subTest(variations=variations,places=places):
                reader,_,transport=adapter([])
                with self.assertRaises(ServiceError):reader.counts(access(),variations,places)
                self.assertEqual(transport.calls,[])

    def test_opaque_count_identifiers_are_preserved_without_becoming_url_paths(self):
        variation='opaque:/+?=variation';place='opaque:/+?=location'
        row=count(location=place,variation=variation)
        reader,_,transport=adapter([{'counts':[row]}])
        self.assertEqual(reader.counts(access(),[variation],[place]),{'counts':[row],'missing':[]})
        self.assertEqual(transport.calls[0][1],'https://connect.squareup.com/v2/inventory/counts/batch-retrieve')
        self.assertEqual(json.loads(transport.calls[0][2]['data'])['catalog_object_ids'],[variation])

    def test_falsey_nontext_and_repeated_cursors_never_end_a_paged_read(self):
        for operation in ('catalog','counts'):
            field='objects' if operation=='catalog' else 'counts'
            for cursor in (False,0,0.0,[],{}):
                with self.subTest(operation=operation,cursor=cursor):
                    reader,_,_=adapter([{field:[],'cursor':cursor}])
                    with self.assertRaises(ServiceError):
                        if operation=='catalog':reader.catalog(access())
                        else:reader.counts(access(),['variation-A'],['location-A'])
            reader,_,transport=adapter([{field:[],'cursor':'repeat'},{field:[],'cursor':'repeat'}])
            with self.subTest(operation=operation),self.assertRaises(ServiceError):
                if operation=='catalog':reader.catalog(access())
                else:reader.counts(access(),['variation-A'],['location-A'])
            self.assertEqual(len(transport.calls),2)

    def test_missing_null_or_empty_text_cursor_end_only_after_all_rows_checked(self):
        for end in ({},{'cursor':None},{'cursor':''}):
            with self.subTest(end=end):
                reader,_,_=adapter([{'objects':[],**end}])
                self.assertEqual(reader.catalog(access()),{})
                reader,_,_=adapter([{'counts':[],**end}])
                self.assertEqual(reader.counts(access(),['variation-A'],['location-A']),
                                 {'counts':[],'missing':[('variation-A','location-A')]})

    def test_malformed_response_fields_and_nonfinite_values_are_incomplete(self):
        for page in ({},{'objects':None},{'objects':{}},{'objects':[],'unrelated':float('nan')}):
            with self.subTest(page=page):
                reader,_,_=adapter([page])
                with self.assertRaises(ServiceError):reader.catalog(access())
        for page in ({},{'counts':None},{'counts':{}},{'counts':[None]}):
            with self.subTest(page=page):
                reader,_,_=adapter([page])
                with self.assertRaises(ServiceError):reader.counts(access(),['variation-A'],['location-A'])

    def test_exhausting_page_or_byte_budget_never_returns_a_partial_catalog(self):
        for limits,pages,expected_calls in (({'max_pages':1},[{'objects':[],'cursor':'next'}],1),
            ({'max_bytes':100},[{'objects':[],'padding':'x'*200}],1)):
            with self.subTest(limits=limits):
                reader,_,transport=adapter(pages,**limits)
                with self.assertRaises(ServiceError):reader.catalog(access())
                self.assertEqual(len(transport.calls),expected_calls)

    def test_excessively_nested_catalog_metadata_fails_with_sanitized_read_error(self):
        for depth in (8,80):
            parent,variation=objects();nested='fictional-leaf'
            for _ in range(depth):nested={'next':nested}
            parent['future_metadata']=nested
            reader,_,transport=adapter([{'objects':[parent,variation]}])
            with self.subTest(depth=depth):
                if depth==8:
                    self.assertEqual(reader.catalog(access())['parent-A']['future_metadata'],nested)
                else:
                    with self.assertRaises(ServiceError) as caught:reader.catalog(access())
                    self.assertNotIn('fictional-leaf',str(caught.exception))
                self.assertEqual(transport.responses[0].closed,1)

    def test_count_pair_budget_is_checked_before_large_missing_output_or_requests(self):
        reader,_,transport=adapter([],max_records=2)
        with self.assertRaises(ServiceError):
            reader.counts(access(),['variation-A','variation-B'],['location-A','location-B'])
        self.assertEqual(transport.calls,[])

    def test_catalog_record_budget_includes_returned_objects_and_nested_rows(self):
        for bound in (1,2):
            with self.subTest(bound=bound):
                reader,_,transport=adapter([{'objects':objects()}],max_records=bound)
                with self.assertRaises(ServiceError):reader.catalog(access())
                self.assertEqual(len(transport.calls),1)

    def test_missing_optional_count_timestamp_is_preserved_without_invented_freshness(self):
        row=count();row.pop('calculated_at')
        reader,_,_=adapter([{'locations':[locations()[0]]},{'objects':objects()},{'counts':[row]}])
        result=reader.collect(access())
        self.assertEqual(result['counts'],[row])
        self.assertNotIn('calculated_at',result['counts'][0])
        self.assertFalse(result['atomic_snapshot'])
        self.assertFalse(result['continuous_zero_verified'])
        self.assertFalse(result['deletion_enabled'])

    def test_count_batches_cover_every_requested_pair_without_dropping_empty_pages(self):
        variations=['variation-'+str(i) for i in range(101)]
        places=['location-'+str(i) for i in range(101)]
        reader,_,transport=adapter([{'counts':[]} for _ in range(4)])
        result=reader.counts(access(),variations,places)
        self.assertEqual(len(result['missing']),10201)
        self.assertEqual(set(result['missing']),{(v,l) for v in variations for l in places})
        covered=set()
        for method,url,kwargs in transport.calls:
            query=json.loads(kwargs['data'])
            self.assertLessEqual(len(query['catalog_object_ids']),100)
            self.assertLessEqual(len(query['location_ids']),100)
            pairs={(v,l) for v in query['catalog_object_ids'] for l in query['location_ids']}
            self.assertFalse(covered.intersection(pairs));covered.update(pairs)
        self.assertEqual(covered,set(result['missing']))
        self.assertEqual(len(transport.calls),4)

    def test_collection_has_explicit_nonatomic_nondeletion_results_and_no_secret(self):
        pages=[{'locations':locations()},{'objects':objects()},
               {'counts':[count(),count(location='location-B')]}]
        original=deepcopy(pages);reader,_,transport=adapter(pages)
        result=reader.collect(access())
        self.assertTrue(result['pagination_complete'])
        self.assertFalse(result['atomic_snapshot'])
        self.assertFalse(result['continuous_zero_verified'])
        self.assertFalse(result['deletion_enabled'])
        self.assertEqual(result['missing'],[])
        self.assertEqual(pages,original)
        self.assertNotIn(access().token,json.dumps(result))
        self.assertEqual([call[0] for call in transport.calls],['GET','POST','POST'])

    def test_collection_retains_unknown_count_coverage_even_with_complete_pagination(self):
        reader,_,_=adapter([{'locations':locations()},{'objects':objects()},{'counts':[count()]}])
        result=reader.collect(access())
        self.assertTrue(result['pagination_complete'])
        self.assertEqual(result['missing'],[('variation-A','location-B')])
        self.assertFalse(result['continuous_zero_verified'])
        self.assertFalse(result['deletion_enabled'])

    def test_empty_catalog_does_not_request_unfiltered_inventory(self):
        reader,_,transport=adapter([{'locations':locations()},{'objects':[]}])
        result=reader.collect(access())
        self.assertEqual(result['objects'],{})
        self.assertEqual(result['counts'],[])
        self.assertEqual(result['missing'],[])
        self.assertEqual(len(transport.calls),2)

    def test_collection_shares_resource_budget_across_phases(self):
        reader,_,transport=adapter([{'locations':locations()},{'objects':objects()},
                                    {'counts':[count()]}],max_pages=2)
        with self.assertRaises(ServiceError):reader.collect(access())
        self.assertEqual(len(transport.calls),2)

    def test_transport_is_fixed_read_shapes_no_redirects_and_closes_every_response(self):
        reader,_,transport=adapter([{'locations':locations()},{'objects':objects()},
                                    {'counts':[count()]}])
        reader.collect(access())
        allowed={('GET','https://connect.squareup.com/v2/locations'),
                 ('POST','https://connect.squareup.com/v2/catalog/search'),
                 ('POST','https://connect.squareup.com/v2/inventory/counts/batch-retrieve')}
        for method,url,kwargs in transport.calls:
            self.assertIn((method,url),allowed)
            self.assertFalse(kwargs['allow_redirects'])
            self.assertTrue(kwargs['stream'])
            self.assertEqual(kwargs['headers']['Square-Version'],API_VERSION)
        self.assertTrue(all(response.closed==1 for response in transport.responses))

    def test_provider_errors_and_redirects_are_sanitized_incomplete_reads_without_retry(self):
        for page in (({'errors':[{'detail':'SENSITIVE-FIXTURE-DETAIL'}]},200),({},302),({},429),({},500)):
            with self.subTest(status=page[1]):
                reader,_,transport=adapter([page])
                with self.assertRaises(ServiceError) as caught:reader.locations(access())
                self.assertNotIn('SENSITIVE-FIXTURE-DETAIL',str(caught.exception))
                self.assertNotIn(access().token,str(caught.exception))
                self.assertEqual(len(transport.calls),1)
                self.assertEqual(transport.responses[0].closed,1)

    def test_expired_collection_budget_does_not_start_a_provider_read(self):
        reader,_,transport=adapter([])
        ticks=iter((100,10000))
        with patch('hosted.cleanup_square.time.monotonic',side_effect=lambda:next(ticks,10000)):
            with self.assertRaises(ServiceError):reader.locations(access())
        self.assertEqual(transport.calls,[])

    def test_invalid_resource_settings_are_rejected_before_any_read(self):
        for limits in ({'max_pages':0},{'max_pages':True},{'max_records':0},{'max_bytes':0},
                       {'deadline_seconds':0},{'deadline_seconds':float('inf')}):
            with self.subTest(limits=limits),self.assertRaises((ServiceError,ValueError)):
                adapter([],**limits)


class CleanupBracketedCollectionTests(unittest.TestCase):
    @staticmethod
    def pages(rows=None, *, after_locations=None, after_objects=None):
        return [{'locations': locations()}, {'objects': objects()},
                {'counts': [count(), count('location-B')] if rows is None else rows},
                {'locations': locations() if after_locations is None else after_locations},
                {'objects': objects() if after_objects is None else after_objects}]

    def test_complete_read_brackets_canonical_context_and_keeps_all_trust_limits(self):
        source = self.pages(after_locations=list(reversed(locations())),
                            after_objects=list(reversed(objects())))
        reader, _, transport = adapter(source)
        result = reader.collect_attempt(access())
        self.assertTrue(result['coverage_complete'])
        self.assertTrue(result['pagination_complete']); self.assertTrue(result['bracketed'])
        self.assertTrue(result['context_unchanged']); self.assertEqual(result['reason_codes'], [])
        self.assertEqual(result['missing'], []); self.assertEqual(len(result['counts']), 2)
        self.assertRegex(result['before_context_hash'], r'^[a-f0-9]{64}$')
        self.assertEqual(result['before_context_hash'], result['after_context_hash'])
        self.assertEqual([row['status'] for row in result['locations']], ['ACTIVE', 'INACTIVE'])
        self.assertEqual([call[0] for call in transport.calls], ['GET', 'POST', 'POST', 'GET', 'POST'])
        for key in ('atomic_snapshot', 'continuous_zero_verified', 'deletion_enabled'):
            self.assertIs(result[key], False)
        self.assertNotIn(access().token, json.dumps(result))
        result['counts'][0]['quantity'] = 'changed'
        self.assertEqual(transport.responses[2].body['counts'][0]['quantity'], '0')
        self.assertTrue(all(response.closed == 1 for response in transport.responses))

    def test_changed_catalog_version_or_same_version_payload_blocks_current_coverage(self):
        for change in ('version', 'same_version_payload'):
            with self.subTest(change=change):
                after = objects()
                if change == 'version':
                    after[0]['version'] = after[1]['version'] = 8
                else:
                    after[1]['future_field']['keep'] = 'changed'
                after[0]['item_data']['variations'] = [deepcopy(after[1])]
                reader, _, _ = adapter(self.pages([count(quantity='8'), count('location-B')], after_objects=after))
                result = reader.collect_attempt(access())
                self.assertFalse(result['coverage_complete']); self.assertTrue(result['pagination_complete'])
                self.assertTrue(result['bracketed']); self.assertFalse(result['context_unchanged'])
                self.assertIn('context_changed', result['reason_codes'])
                self.assertNotEqual(result['before_context_hash'], result['after_context_hash'])
                self.assertEqual(result['counts'][0]['quantity'], '8')

    def test_location_status_currency_or_membership_change_blocks_current_coverage(self):
        for changed in ({'status': 'ACTIVE'}, {'currency': 'USD'}, {'id': 'location-C'}):
            with self.subTest(change=changed):
                after = locations(); after[1].update(changed)
                reader, _, _ = adapter(self.pages(after_locations=after))
                result = reader.collect_attempt(access())
                self.assertFalse(result['coverage_complete']); self.assertTrue(result['pagination_complete'])
                self.assertIn('context_changed', result['reason_codes'])
                self.assertEqual(result['missing'], [])

    def test_later_count_page_failure_retains_positive_rows_and_unknown_pairs(self):
        reader, _, transport = adapter([{'locations': locations()}, {'objects': objects()},
            {'counts': [count(quantity='9')], 'cursor': 'next'},
            ({'errors': [{'detail': 'PRIVATE-PROVIDER-FIXTURE'}]}, 429)])
        result = reader.collect_attempt(access())
        self.assertFalse(result['coverage_complete']); self.assertFalse(result['pagination_complete'])
        self.assertFalse(result['bracketed']); self.assertEqual(result['reason_codes'], ['counts_incomplete'])
        self.assertEqual([row['quantity'] for row in result['counts']], ['9'])
        self.assertEqual(result['missing'], [('variation-A', 'location-B')])
        self.assertNotIn('PRIVATE-PROVIDER-FIXTURE', json.dumps(result))
        self.assertTrue(all(response.closed == 1 for response in transport.responses))

    def test_duplicate_or_malformed_later_row_cannot_erase_a_valid_positive_prefix(self):
        cases = ((count(quantity='7'), count(quantity='0'), ['7', '0']),
                 (count(quantity='0'), count(quantity='7'), ['0', '7']),
                 (count(quantity='7'), count('location-B', quantity='NaN'), ['7']),
                 (count(quantity='7'), count('location-B', quantity='1.000001'), ['7']),
                 (count(quantity='7'), count('location-B', quantity='9' * 27), ['7']))
        for first, second, expected in cases:
            with self.subTest(first=first, second=second):
                reader, _, _ = adapter([{'locations': locations()}, {'objects': objects()},
                                        {'counts': [first, second]}])
                result = reader.collect_attempt(access())
                self.assertFalse(result['coverage_complete']); self.assertIn('counts_incomplete', result['reason_codes'])
                self.assertEqual([row['quantity'] for row in result['counts']], expected)
                self.assertEqual(result['missing'], [('variation-A', 'location-B')])

    def test_missing_count_is_explicit_partial_coverage_after_successful_bookends(self):
        reader, _, _ = adapter(self.pages([count()]))
        result = reader.collect_attempt(access())
        self.assertTrue(result['pagination_complete']); self.assertTrue(result['bracketed'])
        self.assertTrue(result['context_unchanged']); self.assertFalse(result['coverage_complete'])
        self.assertEqual(result['reason_codes'], ['missing_counts'])
        self.assertEqual(result['missing'], [('variation-A', 'location-B')])

    def test_unknown_expected_matrix_is_not_reported_as_no_missing_counts(self):
        for pages, reason in (([{'locations': []}], 'locations_incomplete'),
                              ([{'locations': locations()}, {'objects': [objects()[1]]}], 'catalog_incomplete')):
            with self.subTest(reason=reason):
                reader, _, _ = adapter(pages)
                result = reader.collect_attempt(access())
                self.assertFalse(result['coverage_complete']); self.assertIsNone(result['missing'])
                self.assertEqual(result['counts'], []); self.assertEqual(result['reason_codes'], [reason])
                self.assertIsNone(result['before_context_hash'])

    def test_one_shared_budget_covers_both_context_bookends_and_counts(self):
        pages = self.pages()
        first_bytes = sum(len(json.dumps(page, ensure_ascii=False, sort_keys=True,
                                        separators=(',', ':')).encode()) for page in pages[:3])
        for limits in ({'max_pages': 4}, {'max_records': 7}, {'max_bytes': first_bytes}):
            with self.subTest(limits=limits):
                reader, _, transport = adapter(pages, **limits)
                result = reader.collect_attempt(access())
                self.assertFalse(result['coverage_complete']); self.assertFalse(result['bracketed'])
                self.assertIn('context_check_incomplete', result['reason_codes'])
                self.assertEqual(len(result['counts']), 2); self.assertEqual(result['missing'], [])
                self.assertLessEqual(len(transport.calls), 4)

    def test_missing_timestamps_and_composed_estimates_are_retained_without_complete_coverage(self):
        for quality, reason in (('timestamp_absent', 'count_timestamps_unknown'),
                                ('composed', 'estimated_counts')):
            with self.subTest(quality=quality):
                row = count(quantity='4')
                if quality == 'timestamp_absent': del row['calculated_at']
                else: row['is_estimated'] = True
                reader, _, _ = adapter(self.pages([row, count('location-B')]))
                result = reader.collect_attempt(access())
                self.assertTrue(result['bracketed']); self.assertTrue(result['pagination_complete'])
                self.assertTrue(result['context_unchanged']); self.assertFalse(result['coverage_complete'])
                self.assertIn(reason, result['reason_codes']); self.assertEqual(result['counts'][0], row)

    def test_wrong_estimate_flag_types_are_not_validated_count_evidence(self):
        for value in (None, 0, 1, '', 'false', [], {}):
            with self.subTest(value=value):
                row = count(); row['is_estimated'] = value
                reader, _, _ = adapter(self.pages([row, count('location-B')]))
                result = reader.collect_attempt(access())
                self.assertFalse(result['coverage_complete']); self.assertEqual(result['counts'], [])
                self.assertIn('counts_incomplete', result['reason_codes'])

    def test_count_matrix_bound_does_not_allocate_an_unbounded_missing_list(self):
        variants = []
        for index in range(5):
            variant = deepcopy(objects()[1]); variant['id'] = 'variation-' + str(index)
            variants.append(variant)
        parent = deepcopy(objects()[0]); parent['item_data']['variations'] = deepcopy(variants)
        places = [dict(locations()[0], id='location-' + str(index)) for index in range(5)]
        reader, _, transport = adapter([{'locations': places}, {'objects': [parent] + variants}], max_records=20)
        result = reader.collect_attempt(access())
        self.assertFalse(result['coverage_complete']); self.assertEqual(result['counts'], [])
        self.assertIsNone(result['missing']); self.assertIn('counts_incomplete', result['reason_codes'])
        self.assertEqual(len(transport.calls), 2)

    def test_empty_catalog_is_bracketed_without_an_unfiltered_inventory_request(self):
        reader, _, transport = adapter([{'locations': locations()}, {'objects': []},
                                        {'locations': locations()}, {'objects': []}])
        result = reader.collect_attempt(access())
        self.assertTrue(result['coverage_complete']); self.assertEqual(result['counts'], [])
        self.assertEqual(result['missing'], []); self.assertFalse(result['deletion_enabled'])
        self.assertEqual(len(transport.calls), 4)
        self.assertFalse(any('/inventory/' in url for _, url, _ in transport.calls))

    def test_expired_attempt_returns_incomplete_without_contacting_provider(self):
        reader, _, transport = adapter([])
        ticks = iter((100, 10000))
        with patch('hosted.cleanup_square.time.monotonic', side_effect=lambda: next(ticks, 10000)):
            result = reader.collect_attempt(access())
        self.assertFalse(result['coverage_complete']); self.assertEqual(result['reason_codes'], ['locations_incomplete'])
        self.assertIsNone(result['missing']); self.assertEqual(transport.calls, [])


if __name__=='__main__':unittest.main()
