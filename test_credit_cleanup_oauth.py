"""Offline financial/control tests; all customers/orders/tokens are fictional."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import uuid
import roca_to_square as core
from station_service import StationService
from station_store import StationError, Store
from credit_tracker import Tracker, cents
from catalog_cleanup import Cleanup
from hosted_square_oauth import SquareOAuth


class CreditTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name)
        self.patch=patch.object(core,'SETTINGS_FILE',str(root/'settings.json'));self.patch.start();self.addCleanup(self.patch.stop)
        self.service=StationService(core);self.credit=Tracker(self.service)
        self.customer=self.credit.add_customer({'requestId':'customer','name':'Test Buyer','square_reference':'square-customer'})
        self.ident=self.customer['id']
        self.opening=self.entry('opening',50)

    def entry(self,kind,amount,**extra):
        c=self.credit.account(self.ident)['customer']
        return self.credit.transaction({'requestId':uuid.uuid4().hex,'customerId':self.ident,'kind':kind,'amount':amount,
                   'reference':'fixture-sale','note':'fixture adjustment','version':c['version']}|extra)

    def order(self,amount=2000,state='COMPLETED'):
        return {'id':'order1','customer_id':'square-customer','location_id':'loc','state':state,
                'discounts':[{'catalog_object_id':'credit-discount','applied_money':{'amount':amount,'currency':'USD'}}]}

    def reconcile(self,order):
        return self.credit.reconcile_order(order,location_id='loc',discount_id='credit-discount')

    def test_strict_money_and_overdraft(self):
        for invalid in ('nan','inf','1.001',True,-1,0):
            with self.subTest(value=invalid),self.assertRaises(StationError):cents(invalid)
        with self.assertRaises(StationError):self.entry('redeem',51)
        self.assertEqual(self.credit.account(self.ident)['customer']['balance'],50)

    def test_customer_retry_and_square_duplicate(self):
        same=self.credit.add_customer({'requestId':'customer','name':'Test Buyer','square_reference':'square-customer'})
        self.assertEqual(same['id'],self.ident)
        with self.assertRaises(StationError):self.credit.add_customer({'requestId':'other','name':'Duplicate','square_reference':'square-customer'})
        with self.assertRaises(StationError):self.credit.add_customer({'requestId':'customer','name':'Changed'})

    def test_credit_retry_conflicting_request_and_stale_version(self):
        data={'requestId':'use-once','customerId':self.ident,'version':1,'kind':'redeem','amount':20,'reference':'sale'}
        self.credit.transaction(data);self.credit.transaction(data)
        self.assertEqual(self.credit.account(self.ident)['customer']['balance'],30)
        with self.assertRaises(StationError):self.credit.transaction(data|{'amount':10})
        with self.assertRaises(StationError):self.credit.transaction(data|{'requestId':'stale'})

    def test_two_independent_station_connections_reserve_without_double_spending(self):
        other=Tracker(self.service)
        def reserve(args):
            tracker,key=args
            try:tracker.reserve(self.ident,40,key,key);return True
            except StationError:return False
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(reserve,[(self.credit,'till1'),(other,'till2')]))
        self.assertEqual(sum(results),1)
        self.assertEqual(self.credit.account(self.ident)['customer']['available'],10)
        with self.assertRaises(StationError):self.entry('adjust_remove',20)

    def test_completed_discount_debits_actual_applied_once_and_survives_restart(self):
        self.credit.reserve(self.ident,20,'reserve','order1')
        self.reconcile(self.order(1800));self.reconcile(self.order(1800))
        reloaded=Tracker(self.service).account(self.ident)
        self.assertEqual(reloaded['customer']['balance'],32)
        self.assertEqual(reloaded['customer']['reserved'],0)
        self.assertEqual(len(reloaded['entries']),2)

    def test_pending_unknown_and_canceled_do_not_debit(self):
        self.credit.reserve(self.ident,20,'reserve','order1')
        self.reconcile(self.order(state='OPEN'))
        self.assertEqual(self.credit.account(self.ident)['customer']['available'],30)
        self.reconcile(self.order(state='CANCELED'))
        self.assertEqual(self.credit.account(self.ident)['customer']['available'],50)

    def test_wrong_customer_location_amount_and_discount_fail_closed(self):
        self.credit.reserve(self.ident,20,'reserve','order1')
        for order in (self.order()|{'customer_id':'other'},self.order()|{'location_id':'wrong'},
                      self.order(2100),self.order()|{'discounts':[]}):
            with self.subTest(order=order),self.assertRaises(StationError):self.reconcile(order)
        c=self.credit.account(self.ident)['customer'];self.assertEqual(c['balance'],50);self.assertEqual(c['reserved'],20)

    def test_partial_refunds_idempotent_and_capped(self):
        self.credit.reserve(self.ident,20,'reserve','order1');self.reconcile(self.order())
        self.credit.restore('order1',8,'refund1','verified return');self.credit.restore('order1',8,'refund1','verified return')
        self.assertEqual(self.credit.account(self.ident)['customer']['balance'],38)
        with self.assertRaises(StationError):self.credit.restore('order1',13,'refund2','too much')
        self.credit.restore('order1',12,'refund2','remaining return')
        self.assertEqual(self.credit.account(self.ident)['customer']['balance'],50)

    def test_trade_uses_locked_approved_amount_once_and_marks_receipt(self):
        receipt={'id':'trade-fixture','cards':[],'source_hash':'fixture','purchase':{'type':'credit','amount':12.34}}
        self.service.store.save_receipt(receipt)
        self.entry('trade',999,receiptId='trade-fixture')
        self.assertEqual(self.credit.account(self.ident)['customer']['balance'],62.34)
        self.assertEqual(self.service.store.receipt('trade-fixture')['tracked_credit']['amount'],12.34)
        with self.assertRaises(StationError):self.entry('trade',999,receiptId='trade-fixture')

    def test_export_opening_and_manager_adjustment_guards(self):
        with self.assertRaises(StationError):self.entry('opening',1)
        with self.assertRaises(StationError):self.entry('adjust_add',1,note='')
        self.entry('adjust_add',2,reference='=UNTRUSTED()')
        self.assertIn("'=UNTRUSTED()",self.credit.export(self.ident))


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.cleanup=Cleanup(Store(Path(self.temp.name)))
        self.start=datetime(2026,1,1,tzinfo=timezone.utc)
        self.variation={'id':'v1','type':'ITEM_VARIATION','version':1,'item_variation_data':{'track_inventory':True,'sku':'sku','item_id':'parent'}}
        self.counts=[{'catalog_object_id':'v1','state':'IN_STOCK','location_id':'a','quantity':'0'}]

    def observe(self,day,counts=None,**kwargs):
        self.cleanup.observe(self.variation,self.counts if counts is None else counts,['a'],at=self.start+timedelta(days=day),**kwargs)

    def test_29_and_30_days_and_opt_in(self):
        for day in range(30):self.observe(day)
        self.assertFalse(self.cleanup.preview(at=self.start+timedelta(days=29),opted_in=True)['items'][0]['candidate'])
        self.observe(30)
        self.assertTrue(self.cleanup.preview(at=self.start+timedelta(days=30),opted_in=True)['items'][0]['candidate'])
        self.assertFalse(self.cleanup.preview(at=self.start+timedelta(days=30))['items'][0]['candidate'])
        self.assertFalse(self.cleanup.preview()['deletionEnabled'])

    def test_positive_unknown_reserved_and_history_gaps_reset_continuity(self):
        for index,counts in enumerate(([],[self.counts[0]|{'quantity':'1'}],[self.counts[0]|{'quantity':'NaN'}])):
            self.variation['id']='reset-'+str(index)
            self.counts[0]['catalog_object_id']=self.variation['id']
            counts=[c|{'catalog_object_id':self.variation['id']} for c in counts]
            self.observe(0);self.observe(1,counts);self.observe(2)
            self.assertEqual(self.cleanup.preview(at=self.start+timedelta(days=2),opted_in=True)['items'][0]['verified_days'],0)
        self.observe(3,blocked=True);self.observe(4);self.observe(7)
        current=next(x for x in self.cleanup.preview(at=self.start+timedelta(days=7),opted_in=True)['items'] if x['id']==self.variation['id'])
        self.assertEqual(current['verified_days'],0)

    def test_parent_unknown_location_and_out_of_order_blocked(self):
        self.variation['type']='ITEM';self.observe(0)
        self.assertIsNone(self.cleanup.preview()['items'][0]['zero_since'])
        self.variation['type']='ITEM_VARIATION';self.observe(1,self.counts+[self.counts[0]|{'location_id':'extra'}])
        self.assertIsNone(self.cleanup.preview()['items'][0]['zero_since'])
        with self.assertRaises(StationError):self.observe(0)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name));self.sent=[];self.clock=[1000]
        def request(url,**kwargs):
            self.sent.append((url,kwargs))
            class Response:
                def raise_for_status(self):pass
                def json(self):return {'access_token':'fictional-access','refresh_token':'fictional-refresh','expires_at':'2026-12-01T00:00:00Z','merchant_id':'fictional-merchant'}
            return Response()
        self.oauth=SquareOAuth(self.store,client_id='fictional-app',client_secret='fictional-server-secret',redirect_uri='https://example.test/square/callback',environment='sandbox',request=request,clock=lambda:self.clock[0])

    def state(self,tenant='store1',session='session1'):
        return parse_qs(urlsplit(self.oauth.begin(tenant,session)).query)['state'][0]

    def test_authorization_state_session_tenant_and_replay(self):
        state=self.state()
        with self.assertRaises(StationError):self.oauth.complete(tenant='store2',session='session1',state=state,code='code')
        with self.assertRaises(StationError):self.oauth.complete(tenant='store1',session='wrong',state=state,code='code')
        result=self.oauth.complete(tenant='store1',session='session1',state=state,code='code')
        self.assertTrue(result['connected']);self.assertNotIn('token',json.dumps(result))
        with self.assertRaises(StationError):self.oauth.complete(tenant='store1',session='session1',state=state,code='code')
        self.assertEqual(len(self.sent),1)

    def test_expired_denied_refresh_and_tenant_isolation(self):
        state=self.state();self.clock[0]+=301
        with self.assertRaises(StationError):self.oauth.complete(tenant='store1',session='session1',state=state,code='code')
        state=self.state()
        with self.assertRaises(StationError):self.oauth.complete(tenant='store1',session='session1',state=state,code='',error='denied')
        self.assertFalse(self.sent)
        self.oauth.complete(tenant='store1',session='session1',state=self.state(),code='code')
        self.oauth.refresh('store1');self.assertEqual(len(self.sent),2)
        self.assertFalse(self.oauth.status('store2')['connected'])
        with self.assertRaises(StationError):self.oauth.refresh('store2')


if __name__=='__main__':unittest.main()
