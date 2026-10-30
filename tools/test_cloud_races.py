"""Deferred-response browser regressions for shared-credit identity/session races.

Loads the real cloud.js with minimal desktop-shell helpers. Every request is
intercepted with fictional responses; no station, hosting or Square call occurs.
"""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import time
import unittest
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
REPORT=ROOT/'Testing'/'2.6.0'
A='11111111-1111-4111-8111-111111111111'
B='22222222-2222-4222-8222-222222222222'
C='33333333-3333-4333-8333-333333333333'
HTML='''<!doctype html><html><head><meta charset="utf-8"></head><body><nav id="navigation"></nav><section id="workflowContent"></section><div id="toast"></div><script>
const $=id=>document.getElementById(id),state={mode:'cloud'};
const icon=()=>'',usd=n=>Number(n||0).toLocaleString('en-US',{style:'currency',currency:'USD'});
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function toast(message){$('toast').textContent=message}
function navigate(){ $('cloudView').hidden=false;window.enterCloudWorkspace() }
</script><script src="/cloud.js"></script></body></html>'''


class FictionalCloud:
    def __init__(self,page):
        self.page=page;self.deferred=set();self.pending={};self.adjustments=[];self.requests=[]
        self.session={'signedIn':True,'store':'fictional-store','role':'manager'}
        self.customers={ident:{'id':ident,'display_name':name,'contact':'','square_customer_id':'',
                              'balance_cents':5000,'available_cents':5000,'reserved_cents':0,'version':1}
                        for ident,name in ((A,'Customer Alpha'),(B,'Customer Beta'),(C,'Customer Gamma'))}
        page.route('**/*',self.route)

    def reply(self,route,data,status=200):route.fulfill(status=status,content_type='application/json',body=json.dumps(data))

    def account(self,ident):return {'customer':dict(self.customers[ident]),'entries':[]}

    def rows(self):return [self.customers[C]] if self.session.get('store')=='other-store' else [self.customers[A],self.customers[B]]

    def route(self,route):
        request=route.request;uri=urlsplit(request.url);self.requests.append(request.url)
        if uri.hostname!='fictional-desktop.test':raise AssertionError('Unexpected external request: '+request.url)
        if uri.path=='/':return route.fulfill(status=200,content_type='text/html',body=HTML)
        if uri.path=='/cloud.js':return route.fulfill(status=200,content_type='text/javascript',body=(ROOT/'cloud.js').read_text(encoding='utf-8'))
        if uri.path=='/cloud.css':return route.fulfill(status=200,content_type='text/css',body='')
        if uri.path=='/favicon.ico':return route.fulfill(status=204,body='')
        if not uri.path.startswith('/api/cloud/'):raise AssertionError('Unexpected fixture path: '+uri.path)
        action=uri.path.rsplit('/',1)[-1];body=request.post_data_json if request.method=='POST' else {}
        key=(action,body.get('customerId') if action=='account' else body.get('query','') if action=='search' else '')
        if key in self.deferred:
            self.pending.setdefault(key,[]).append(route);return
        if action=='session':return self.reply(route,self.session)
        if action=='pending':return self.reply(route,{'pending':[],'hasOtherPending':False})
        if action=='search':return self.reply(route,{'customers':self.rows()})
        if action=='account':return self.reply(route,self.account(body['customerId']))
        if action=='logout':
            self.session={'signedIn':False};return self.reply(route,{'signedIn':False,'serverRevoked':True,'message':'Signed out.'})
        if action=='login':
            self.session={'signedIn':True,'store':body['storeSlug'],'role':'manager'};return self.reply(route,self.session)
        if action=='adjust':
            self.adjustments.append(body);return self.reply(route,{'entry_id':'fictional-entry'})
        raise AssertionError('Unexpected fixture action: '+action)

    def wait(self,key):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if self.pending.get(key):return
            self.page.wait_for_timeout(10)
        raise AssertionError('Expected deferred request did not arrive: '+repr(key))

    def release(self,key,data,status=200):
        self.wait(key);route=self.pending[key].pop(0);self.deferred.discard(key)
        if key[0]=='logout':self.session={'signedIn':False}
        self.reply(route,data,status)


class CloudRaceAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright=sync_playwright().start()
        cls.browser=cls.playwright.chromium.launch(channel='chrome',headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()

    def setUp(self):
        self.context=self.browser.new_context(viewport={'width':1280,'height':900})
        self.page=self.context.new_page();self.errors=[]
        self.page.on('pageerror',lambda error:self.errors.append(str(error)))
        self.cloud=FictionalCloud(self.page);self.page.goto('https://fictional-desktop.test/')
        self.page.get_by_role('button',name='Shared credit',exact=True).click()
        expect(self.page.locator('#cloudStoreName')).to_have_text('fictional-store')
        expect(self.page.locator('[data-cloud-customer]')).to_have_count(2)

    def tearDown(self):
        try:self.assertEqual(self.errors,[],'Unexpected JavaScript errors')
        finally:self.context.close()

    def select(self,ident):self.page.locator('[data-cloud-customer="'+ident+'"]').click()

    def test_reverse_account_responses_cannot_correct_the_wrong_customer(self):
        self.cloud.deferred.update({('account',A),('account',B)})
        self.select(A);self.cloud.wait(('account',A));self.select(B);self.cloud.wait(('account',B))
        expect(self.page.locator('#cloudAccount')).to_contain_text('Checking this customer')
        self.cloud.release(('account',B),self.cloud.account(B))
        expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Beta')
        self.cloud.release(('account',A),self.cloud.account(A))
        expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Beta')
        self.page.locator('#cloudOpenEntry').click()
        expect(self.page.locator('#cloudEntryCustomer')).to_have_text('Customer Beta')
        # Model an unrelated late selection change: the dialog's identity must
        # remain the captured customer, independent of this navigation global.
        self.page.evaluate('cloudSelected="'+A+'"')
        self.page.locator('#cloudEntryKind').select_option('adjust_add')
        self.page.locator('#cloudEntryAmount').fill('10.00');self.page.locator('#cloudEntryReference').fill('RACE TEST')
        self.page.locator('#cloudEntryReason').fill('Fictional correction identity check')
        self.page.get_by_role('button',name='Save shared entry',exact=True).click()
        expect(self.page.locator('#toast')).to_have_text('Shared entry saved and balance refreshed.')
        self.assertEqual(len(self.cloud.adjustments),1)
        self.assertEqual(self.cloud.adjustments[0]['customerId'],B)
        self.assertEqual(self.cloud.adjustments[0]['version'],1)

    def test_old_account_error_cannot_sign_out_the_newer_selection(self):
        self.cloud.deferred.update({('account',A),('account',B)})
        self.select(A);self.cloud.wait(('account',A));self.select(B);self.cloud.wait(('account',B))
        self.cloud.release(('account',B),self.cloud.account(B))
        expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Beta')
        self.cloud.release(('account',A),{'errors':['Old expired request'],'offline':False},401)
        expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Beta')
        expect(self.page.locator('#cloudConnection')).to_have_text('Signed in · fictional-store')
        expect(self.page.locator('#cloudMessage')).to_have_text('')

    def test_reverse_search_results_keep_the_latest_query(self):
        self.cloud.deferred.update({('search','alpha'),('search','beta')})
        self.page.locator('#cloudSearch').fill('alpha');self.cloud.wait(('search','alpha'))
        self.page.locator('#cloudSearch').fill('beta');self.cloud.wait(('search','beta'))
        self.cloud.release(('search','beta'),{'customers':[self.cloud.customers[B]]})
        expect(self.page.locator('#cloudCustomers')).to_contain_text('Customer Beta')
        self.cloud.release(('search','alpha'),{'customers':[self.cloud.customers[A]]})
        expect(self.page.locator('#cloudCustomers')).not_to_contain_text('Customer Alpha')
        expect(self.page.locator('#cloudCustomers')).to_contain_text('Customer Beta')

    def test_logout_immediately_hides_balances_and_discards_old_store_responses(self):
        self.cloud.deferred.update({('account',A),('search','old'),('logout','')})
        self.select(A);self.cloud.wait(('account',A))
        self.page.locator('#cloudSearch').fill('old');self.cloud.wait(('search','old'))
        self.page.locator('#cloudSignOut').click();self.cloud.wait(('logout',''))
        expect(self.page.locator('#cloudWorkspace')).to_be_hidden()
        self.assertTrue(self.page.locator('#cloudLoginForm').evaluate('(form)=>form.inert'))
        self.cloud.release(('search','old'),{'customers':[self.cloud.customers[A]]})
        expect(self.page.locator('#cloudCustomers')).to_have_text('')
        self.cloud.release(('logout',''),{'signedIn':False,'serverRevoked':True,'message':'Signed out.'})
        expect(self.page.locator('#cloudMessage')).to_have_text('Signed out.')
        self.page.locator('#cloudStore').fill('other-store');self.page.locator('#cloudEmail').fill('fixture@example.invalid')
        self.page.locator('#cloudPassword').fill('fictional-staff-password')
        self.page.get_by_role('button',name='Sign in to store',exact=True).click()
        expect(self.page.locator('#cloudStoreName')).to_have_text('other-store')
        expect(self.page.locator('#cloudCustomers')).to_contain_text('Customer Gamma')
        self.select(C);expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Gamma')
        self.cloud.release(('account',A),self.cloud.account(A))
        expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Gamma')
        expect(self.page.locator('#cloudCustomers')).not_to_contain_text('Customer Alpha')

    def test_selection_clears_old_balance_before_outage_response(self):
        self.select(A);expect(self.page.locator('#cloudAccount h2')).to_have_text('Customer Alpha')
        self.cloud.deferred.add(('account',B));self.select(B);self.cloud.wait(('account',B))
        expect(self.page.locator('#cloudAccount')).not_to_contain_text('Customer Alpha')
        expect(self.page.locator('#cloudAccount')).not_to_contain_text('$50.00')
        self.cloud.release(('account',B),{'errors':['Fictional service outage'],'offline':True},503)
        expect(self.page.locator('#cloudAccount')).to_contain_text('balance cannot be confirmed')
        expect(self.page.locator('#cloudNewCustomer')).to_be_disabled()
        expect(self.page.locator('#cloudOpenEntry')).to_have_count(0)


def main():
    REPORT.mkdir(parents=True,exist_ok=True)
    output=io.StringIO();suite=unittest.defaultTestLoader.loadTestsFromTestCase(CloudRaceAcceptance)
    result=unittest.TextTestRunner(stream=output,verbosity=2).run(suite)
    (REPORT/'cloud-races.log').write_text(output.getvalue(),encoding='utf-8')
    summary={'timestamp_utc':datetime.now(timezone.utc).isoformat(),'tests_run':result.testsRun,
             'passed':result.wasSuccessful(),'cloud_js_sha256':hashlib.sha256((ROOT/'cloud.js').read_bytes()).hexdigest(),
             'scope':'Actual cloud.js in Chrome, fully intercepted fictional HTTP responses; no live station/cloud/Square calls.'}
    (REPORT/'cloud-races-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    print(output.getvalue());return 0 if result.wasSuccessful() else 1


if __name__=='__main__':sys.exit(main())
