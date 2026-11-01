"""Receiving-only bounded response budget; no input/global cap increase."""
import copy
import json
import unittest
import uuid
from urllib.parse import urlsplit
from station_cloud import CloudClient,CloudError
import station_cloud
import test_hosted_receive as receiving_fixture


class LargeReceivingFixture(receiving_fixture.ReceivingFixtureTransport):
    def __init__(self):super().__init__();self.padded_responses=0

    def project(self,store,body,ident,revision=0):
        value=super().project(store,body,ident,revision);binding=str(uuid.uuid4())
        for row in value['targets']:
            row.update(source_sku='SKU-'+('X'*96),target_sku='SKU-'+('X'*96),variation_id='V'+('x'*254),parent_id='P'+('y'*254),binding_id=binding)
        return value

    def request(self,method,url,**options):
        result=super().request(method,url,**options);path=urlsplit(url).path
        if result.status_code==200 and (path=='/api/receiving/plans' or path.startswith('/api/receiving/') and result.json().get('receiving_id')):
            content=result.content
            if len(content)>8*1024*1024:raise AssertionError('Legal fixture exceeds the approved receiving response budget')
            result._content=content+b' '*(8*1024*1024-len(content));self.padded_responses+=1
        return result


class ReceivingResponseBudgetTests(unittest.TestCase):
    transport_type=LargeReceivingFixture
    setUp=receiving_fixture.HostedReceivingTests.setUp
    restart=receiving_fixture.HostedReceivingTests.restart
    receipt=receiving_fixture.HostedReceivingTests.receipt
    claim=receiving_fixture.HostedReceivingTests.claim
    prepare=receiving_fixture.HostedReceivingTests.prepare
    commit=receiving_fixture.HostedReceivingTests.commit

    def test_valid_1000_row_accepted_plan_and_final_receipt_recover_at_exact_8mib_wire_budget(self):
        receipt=self.receipt('Name,Game,Price,Quantity\nBudget Fixture,Magic,12.50,1000\n');original=copy.deepcopy(receipt['cards'][0]);receipt['cards']=[]
        for _ in range(1000):receipt['cards'].append(dict(original,row_id=uuid.uuid4().hex,qty=1))
        self.service.store.save_receipt(receipt);self.profile={'stock':'4x2','layout':'side','dpi':203}
        plan=self.prepare(self.claim(receipt));body=plan['receipt']['hosted_receive']['plan_body']
        self.assertLessEqual(len(json.dumps(body).encode()),512*1024)
        self.remote.drop_after.add('commit')
        with self.assertRaises(CloudError):self.commit(plan)
        operation=self.remote.receivings[('fictional-store',plan['operation']['receiving_id'])]
        self.assertGreater(len(json.dumps(operation).encode()),2_000_000)
        self.service=self.restart();received=self.service.hosted_receiving.status(receipt['id'],self.session['sessionId'])
        self.assertEqual(received['receipt']['inventory_status'],'received');self.assertEqual(len(received['receipt']['cards']),1000)
        self.assertEqual(sum(row['qty'] for row in received['receipt']['cards']),1000);self.assertEqual(self.remote.stock_calls,1)
        self.assertTrue(all(row['sku']=='SKU-'+('X'*96) for row in received['receipt']['cards']));self.assertGreaterEqual(self.remote.padded_responses,3)

    def test_only_exact_typed_receiving_method_and_path_pairs_get_larger_budget(self):
        ident=str(uuid.uuid4());large=8*1024*1024
        supported=[('POST','/api/receiving/plans'),('GET','/api/receiving/'+ident),('GET','/api/receiving/requests/original-key'),('PUT','/api/receiving/'+ident+'/plan')]
        supported += [('POST','/api/receiving/'+ident+'/'+action) for action in ('commit','recover','authorize','cancel')]
        for method,path in supported:self.assertEqual(station_cloud.response_budget(method,path),large)
        for method,path in [('GET','/api/credit/customers'),('GET','/api/receiving/readiness'),('GET','/api/receiving/locations'),('DELETE','/api/receiving/'+ident),('GET','/api/receiving/plans'),('POST','/api/receiving/'+ident+'/commit/extra'),('POST','/api/receiving/requests/key/retire'),('GET','/api/receiving/'+ident+'?other=1')]:
            with self.subTest(method=method,path=path):self.assertEqual(station_cloud.response_budget(method,path),2_000_000)

    def test_one_byte_over_receiving_budget_and_large_non_receiving_response_fail_closed(self):
        import requests
        class Transport:
            def request(self,*args,**kwargs):
                result=requests.Response();result.status_code=200;result._content=b'{}'+b' '*(8*1024*1024-1);return result
        client=CloudClient(self.root/'budget-only',transport=Transport())
        for path in ('/api/receiving/'+str(uuid.uuid4()),'/api/credit/customers'):
            with self.subTest(path=path),self.assertRaisesRegex(CloudError,'too large'):client._call('GET',path)


if __name__=='__main__':unittest.main()
