"""Real Chrome shared-trade handoff; fictional hosted fixture, no external I/O."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright,expect
import roca_to_square as core
from station_cloud import CloudClient
from station_service import StationService
from test_shared_trade import TradeFixtureTransport

REPORT=ROOT/'Testing'/'2.6.0'
SOURCES=('station_shared_trade.py','station_cloud.py','station_service.py','station_server.py','station_store.py',
         'credit_tracker.py','shared-trade.js','shared-trade.css','cloud.js','station.js','roca_square_label_app.html',
         'tools/user_acceptance_shared_trade.py','test_shared_trade.py')
def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def run():
    REPORT.mkdir(parents=True,exist_ok=True);before=hashes();started=datetime.now(timezone.utc).isoformat();steps=[];cases=[];errors=[];external=[];backend=[]
    def record(text):steps.append(text);print(text,flush=True)
    def forbidden(session,method,url,**kwargs):backend.append({'method':method,'host':urlsplit(url).hostname});raise AssertionError('External HTTP forbidden')
    with tempfile.TemporaryDirectory() as folder,patch.object(core,'SETTINGS_FILE',str(Path(folder)/'settings.json')),patch.object(core,'OUTPUT_DIR',str(Path(folder)/'outputs')), \
         patch.object(core.requests.sessions.Session,'request',forbidden),patch.object(core,'upload_to_square',side_effect=AssertionError('Square forbidden')) as square:
        server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);service=server.station=StationService(core)
        service.store.put('preferences',service.preferences()|{'pricingEnabled':False})
        remote=TradeFixtureTransport();service.cloud=CloudClient(service.store.root,transport=remote);service.shared_trades.cloud=service.cloud
        service.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
        customer=service.cloud.create_customer({'request_key':'fixture-shared-customer','display_name':'Fictional Shared Customer','permission_confirmed':True})['customer_id']
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True);context=browser.new_context(viewport={'width':1440,'height':1100});page=context.new_page();page.set_default_timeout(10000)
                def route(request):
                    uri=urlsplit(request.request.url)
                    if uri.scheme=='http' and uri.hostname=='127.0.0.1' and uri.port==server.server_port:request.continue_()
                    else:external.append({'scheme':uri.scheme,'host':uri.hostname});request.abort()
                context.route('**/*',route);page.on('pageerror',lambda error:errors.append(str(error)))
                def create(name):
                    receipt=service.import_csv(f'Name,Game,Set,Number,Condition,Printing,Language,Price,Quantity\n{name},Riftbound,Fixture,001,Near Mint,Normal,English,8.13,2\n','fictional-shared-trade.csv','trade',new_delivery=True)['receipt']
                    receipt['title']=name;receipt['cards'][0]['reviewed']=True;service.store.save_receipt(receipt);return receipt
                def reopen(receipt):
                    page.goto(base,wait_until='domcontentloaded');expect(page.locator('#version')).to_have_text('2.6.0')
                    page.locator('#openHistory').click();page.locator('#historyList [data-open="'+receipt['id']+'"]').click();expect(page.locator('#receiptPicker')).to_have_value(receipt['id']);expect(page.locator('#newReceipt')).to_be_enabled()
                def open_shared(receipt):
                    reopen(receipt);page.locator('#openSharedTrade').click();expect(page.locator('#sharedTradeDialog')).to_be_visible()
                def draft(receipt):
                    open_shared(receipt);expect(page.locator('#sharedTradeCustomers [data-shared-customer="'+customer+'"]')).to_be_visible()
                    assert remote.issuances==0 or service.store.receipt(receipt['id']).get('purchase') is None
                    page.locator('[data-shared-customer="'+customer+'"]').click();expect(page.locator('#sharedTradeSelectedName')).to_have_text('Fictional Shared Customer')
                    page.locator('#saveSharedTradeDraft').click();expect(page.locator('#sharedTradeOffer')).to_be_visible();expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                def consent():
                    expect(page.locator('#acceptSharedTrade')).to_be_disabled();page.locator('#sharedTradeAgreed').check();expect(page.locator('#acceptSharedTrade')).to_be_disabled();page.locator('#sharedTradeReceived').check();expect(page.locator('#acceptSharedTrade')).to_be_enabled()
                def case(name,callback):
                    begin=time.monotonic()
                    try:callback();cases.append({'name':name,'status':'pass','elapsed_seconds':round(time.monotonic()-begin,3)});record('PASS '+name)
                    except Exception:
                        failure=traceback.format_exc();shot=f'shared-trade-failure-{len(cases)+1}.png';page.screenshot(path=str(REPORT/shot),full_page=True)
                        cases.append({'name':name,'status':'fail','error':failure,'screenshot':shot});record('FAIL '+name+'\n'+failure)
                def normal_issuance():
                    receipt=create('Fictional Shared Agreement');draft(receipt)
                    expect(page.locator('#sharedTradeOffer')).to_contain_text('$8.13');expect(page.locator('#addManualItem')).to_be_disabled();expect(page.locator('#deleteReceipt')).to_be_disabled()
                    self=service.store.receipt(receipt['id']);assert self.get('purchase') is None and remote.issuances==0
                    consent();page.screenshot(path=str(REPORT/'shared-trade-offer-review.png'),full_page=True)
                    page.locator('#acceptSharedTrade').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('Shared credit confirmed once');expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                    result=service.store.receipt(receipt['id']);assert result['purchase']['scope']=='hosted' and remote.issuances==1 and 'tracked_credit' not in result
                    page.screenshot(path=str(REPORT/'shared-trade-confirmed.png'),full_page=True)
                    page.locator('#refreshSharedTrade').click();expect(page.locator('#refreshSharedTrade')).to_be_enabled();assert remote.issuances==1
                    page.locator('#closeSharedTrade').click();expect(page.locator('#trackApprovedCredit')).to_have_count(0);expect(page.locator('#acceptedBuy')).to_contain_text('No local wallet credit')
                    page.locator('#openHistory').click()
                    row=page.locator('#historyList [data-open="'+receipt['id']+'"]')
                    expect(row).to_contain_text('SHARED CREDIT ISSUED');page.locator('#closeHistory').click()
                def lost_acceptance_recovery():
                    receipt=create('Fictional Lost Response');draft(receipt);consent();previous=remote.issuances;remote.drop_after.add('accept')
                    page.locator('#acceptSharedTrade').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('unavailable');expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                    assert remote.issuances==previous+1 and service.store.receipt(receipt['id']).get('purchase') is None
                    page.screenshot(path=str(REPORT/'shared-trade-pending-recovery.png'),full_page=True)
                    # Reopen the whole browser page: no browser state retains the
                    # agreement; the encrypted durable journal is the retry source.
                    open_shared(receipt);expect(page.locator('#sharedTradeMessage')).to_contain_text('Shared credit confirmed once');expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                    assert remote.issuances==previous+1 and service.store.receipt(receipt['id'])['purchase']['scope']=='hosted'
                    assert service.cloud.pending()['pending']==[];page.locator('#closeSharedTrade').click()
                def draft_cancel():
                    receipt=create('Fictional Cancelled Draft');draft(receipt);previous=remote.issuances
                    page.locator('#cancelSharedTradeDraft').click();expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmOkay').click()
                    expect(page.locator('#sharedTradeMessage')).to_contain_text('cancellation confirmed');expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                    result=service.store.receipt(receipt['id']);assert result.get('purchase') is None and result['shared_trade']['state']=='cancelled' and remote.issuances==previous
                    page.screenshot(path=str(REPORT/'shared-trade-cancelled.png'),full_page=True);page.locator('#closeSharedTrade').click();expect(page.locator('#addManualItem')).to_be_enabled()
                    page.locator('#openHistory').click()
                    row=page.locator('#historyList [data-open="'+receipt['id']+'"]')
                    expect(row).to_contain_text('CHECKED');page.locator('#closeHistory').click()
                def pending_drawer_readonly_and_restore():
                    receipt=create('Fictional Read-only Pending Trade');draft(receipt)
                    page.locator('#closeSharedTrade').click();reopen(receipt);page.locator('#backToReview').click()
                    page.locator('#cardRows [data-edit]').first.click();expect(page.locator('#cardDrawer')).to_be_visible()
                    current=service.store.receipt(receipt['id']);revision=current['revision'];stored=current['cards'][0]
                    inputs=page.locator('#cardForm input,#cardForm select,#cardForm textarea')
                    for index in range(inputs.count()):expect(inputs.nth(index)).to_be_disabled()
                    expect(page.locator('#cardForm button[type=submit]')).to_be_disabled();expect(page.locator('#splitCopy')).to_be_hidden();expect(page.locator('#splitCopy')).to_be_disabled()
                    for identifier in ('customerReference','minPrice','applyThreshold','includePage','markReviewed'):expect(page.locator('#'+identifier)).to_be_disabled()
                    assert page.evaluate('state.dirty') is False
                    writes=[]
                    def track(request):
                        if request.method=='POST' and request.url==base+'/api/receipts/'+receipt['id']:writes.append(request.url)
                    page.on('request',track)
                    # Force the event as well as checking the disabled native
                    # button: a no-op submit must return before marking dirty.
                    page.locator('#cardForm').evaluate('(form)=>form.requestSubmit()')
                    expect(page.locator('#drawerMessage')).to_contain_text('read-only');assert page.evaluate('state.dirty') is False
                    guarded=page.evaluate("()=>{try{editCardFromForm();return false}catch{return true}}")
                    assert guarded and page.evaluate('state.dirty') is False
                    # Tampered disabled form values must never replace the saved
                    # research identity or current price.
                    page.locator('#editName').evaluate("input=>input.value='Unsaved altered name'")
                    page.locator('#editPrice').evaluate("input=>input.value='999'")
                    service.store.put('preferences',service.preferences()|{'pricingEnabled':True})
                    fake={'key':'d'*64,'status':'complete','message':'Fictional cached research','checked_at':datetime.now(timezone.utc).isoformat(),
                          'intake_price':7.99,'retail_price':10.01,'flags':[],'sources':{}}
                    try:
                        with patch.object(service.pricing,'enqueue',return_value='d'*64),patch.object(service.pricing,'get',return_value=fake):
                            with page.expect_request(lambda request:request.method=='POST' and request.url==base+'/api/pricing/research') as researched:
                                page.locator('#compareCardPrices').click()
                            expect(page.locator('#priceResearchDialog')).to_be_visible();expect(page.locator('#priceCurrent')).to_have_text('$8.13')
                            expect(page.locator('#priceResearchName')).to_have_text(stored['card_name']);expect(page.locator('#approveSuggestedPrice')).to_be_disabled()
                            assert researched.value.post_data_json['card_name']==stored['card_name']
                            assert researched.value.post_data_json['game']==stored['game']
                            assert page.evaluate('state.dirty') is False and service.store.receipt(receipt['id'])['revision']==revision
                            page.screenshot(path=str(REPORT/'shared-trade-readonly-research.png'),full_page=True)
                            page.locator('#closePriceResearch').click()
                    finally:service.store.put('preferences',service.preferences()|{'pricingEnabled':False})
                    page.locator('#closeDrawer').click();page.locator('#quitApp').click()
                    expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmCancel').click();expect(page.locator('#newReceipt')).to_be_enabled()
                    assert page.evaluate('state.dirty') is False and not writes
                    assert service.store.receipt(receipt['id'])['revision']==revision
                    blocked=page.request.post(base+'/api/receipts/'+receipt['id'],data={'revision':revision,'title':'Forbidden pending edit'})
                    assert blocked.status==400 and 'saved shared trade' in blocked.text()
                    assert service.store.receipt(receipt['id'])['revision']==revision
                    page.remove_listener('request',track)
                    # Recover the exact hosted draft and confirm its cancellation
                    # before checking that all editable controls return.
                    page.locator('#openSharedTrade').click();expect(page.locator('#sharedTradeOffer')).to_be_visible();expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                    page.locator('#cancelSharedTradeDraft').click();expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmOkay').click()
                    expect(page.locator('#sharedTradeMessage')).to_contain_text('cancellation confirmed');expect(page.locator('#refreshSharedTrade')).to_be_enabled();page.locator('#closeSharedTrade').click()
                    page.locator('#cardRows [data-edit]').first.click();expect(page.locator('#editLocation')).to_be_enabled();expect(page.locator('#editGameLabel')).to_be_enabled()
                    expect(page.locator('#cardForm button[type=submit]')).to_be_enabled();expect(page.locator('#splitCopy')).to_be_enabled();expect(page.locator('#customerReference')).to_be_enabled()
                    with page.expect_response(lambda response:response.url==base+'/api/receipts/'+receipt['id'] and response.request.method=='POST') as saved:
                        page.locator('#cardForm button[type=submit]').click()
                    assert saved.value.status==200;expect(page.locator('#cardDrawer')).to_be_hidden();assert page.evaluate('state.dirty') is False
                    fresh=create('Fictional Editable After Recovery');reopen(fresh);page.locator('#cardRows [data-edit]').first.click()
                    expect(page.locator('#editName')).to_be_enabled();expect(page.locator('#editLocation')).to_be_enabled();expect(page.locator('#cardForm button[type=submit]')).to_be_enabled();page.locator('#closeDrawer').click()
                case('Explicit shared customer and two confirmations issue hosted credit once without a local-wallet action',normal_issuance)
                case('Lost acceptance response remains locked and full-page reopen recovers one confirmed credit',lost_acceptance_recovery)
                case('Draft cancellation is confirmed by the server before the local batch unlocks',draft_cancel)
                case('Pending drawers and saved-card research stay read-only without dirty state, Quit can cancel, and confirmed void restores editing',pending_drawer_readonly_and_restore)
                assert not errors,errors;assert not external,external;assert not backend,backend;assert square.call_count==0
                record('PASS no external browser/backend requests, no Square uploads and no uncaught JavaScript errors');browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5);after=hashes();stable=before==after
            payload={'started_utc':started,'finished_utc':datetime.now(timezone.utc).isoformat(),'scope':'Real Chrome loopback desktop UI with deterministic fictional hosted fixture; not live hosted/Square acceptance.',
                'source_stable':stable,'sources_before':before,'sources_after':after,'cases':cases,'browser_errors':errors,'external_browser_attempts':external,'backend_http_attempts':backend,'square_upload_attempts':square.call_count,'fixture_credit_issuances':remote.issuances}
            (REPORT/'shared-trade-user-acceptance.json').write_text(json.dumps(payload,indent=2),encoding='utf-8');(REPORT/'shared-trade-user-acceptance.log').write_text('\n'.join(steps)+'\n',encoding='utf-8')
    if not stable or any(item['status']!='pass' for item in cases):raise SystemExit(1)


if __name__=='__main__':run()
