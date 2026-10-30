"""Real Chrome shared-trade handoff; fictional hosted fixture, no external I/O."""
from datetime import datetime,timezone
import hashlib
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
import uuid
from unittest.mock import patch
from urllib.parse import urlsplit
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from playwright.sync_api import sync_playwright,expect
import roca_to_square as core
from station_cloud import CloudClient
from station_service import StationService
from test_shared_trade import TradeFixtureTransport

REPORT=ROOT/'Testing'/'oauth-receiving'
SOURCES=('station_shared_trade.py','station_cloud.py','station_service.py','station_server.py','station_store.py',
         'credit_tracker.py','shared-trade.js','shared-trade.css','cloud.js','station.js','roca_square_label_app.html',
         'tools/user_acceptance_shared_trade.py','test_shared_trade.py','intake_manifest.py','station_intake_claim.py','intake-claim.js','intake-claim.css','test_intake_claim.py')
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
                allowed_ports={server.server_port}
                def route(request):
                    uri=urlsplit(request.request.url)
                    if uri.scheme=='http' and uri.hostname=='127.0.0.1' and uri.port in allowed_ports:request.continue_()
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
                    open_shared(receipt)
                    if not service.store.receipt(receipt['id']).get('intake_claim'):
                        page.locator('#intakePhysicalReference').fill('TAG-'+receipt['id']);page.locator('#intakePhysicalVerified').check();page.locator('#createIntakeClaim').click()
                        expect(page.locator('#intakeClaimFacts')).to_contain_text('Central tag')
                    expect(page.locator('#sharedTradeCustomers [data-shared-customer="'+customer+'"]')).to_be_visible()
                    assert remote.issuances==0 or service.store.receipt(receipt['id']).get('purchase') is None
                    page.locator('[data-shared-customer="'+customer+'"]').click();expect(page.locator('#sharedTradeSelectedName')).to_have_text('Fictional Shared Customer')
                    page.locator('#saveSharedTradeDraft').click();expect(page.locator('#sharedTradeOffer')).to_be_visible();expect(page.locator('#refreshSharedTrade')).to_be_enabled()
                def consent(target=None):
                    target=target or page
                    expect(target.locator('#acceptSharedTrade')).to_be_disabled();target.locator('#sharedTradeAgreed').check();expect(target.locator('#acceptSharedTrade')).to_be_disabled();target.locator('#sharedTradeReceived').check();expect(target.locator('#acceptSharedTrade')).to_be_disabled();target.locator('#sharedTradeClaimReviewed').check();expect(target.locator('#acceptSharedTrade')).to_be_enabled()
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
                    page.screenshot(path=str(REPORT/'shared-trade-cancelled.png'),full_page=True);page.locator('#closeSharedTrade').click();expect(page.locator('#addManualItem')).to_be_disabled();expect(page.locator('#newReceipt')).to_be_enabled()
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
                def lost_claim_response():
                    receipt=create('Fictional Lost Physical Reference');before_claims=len(remote.claims);open_shared(receipt)
                    page.locator('#intakePhysicalReference').fill('TAG-LOST-'+receipt['id']);page.locator('#intakePhysicalVerified').check();remote.drop_after.add('claim');page.locator('#createIntakeClaim').click()
                    expect(page.locator('#sharedTradeMessage')).to_contain_text('unavailable');expect(page.locator('#checkIntakeClaim')).to_be_enabled()
                    stored=service.store.receipt(receipt['id']);key=stored['intake_claim']['request_key'];assert len(remote.claims)==before_claims+1 and stored.get('purchase') is None
                    assert (service.store.root/'cloud.intake').read_bytes().startswith(b'DP1');assert 'source_manifest' not in json.dumps(service.cloud.pending())
                    page.screenshot(path=str(REPORT/'intake-claim-response-lost.png'),full_page=True)
                    page.locator('#closeSharedTrade').click();open_shared(receipt);expect(page.locator('#intakeClaimFacts')).to_contain_text('Central tag')
                    expect(page.locator('#sharedTradeCustomerPicker')).to_be_visible();assert service.store.receipt(receipt['id'])['intake_claim']['request_key']==key
                    assert len(remote.claims)==before_claims+1 and service.cloud._intake_pending()==[];page.locator('#closeSharedTrade').click()
                def cross_pc_same_physical_source():
                    receipt=create('Fictional Same Physical Cards Across PCs');draft(receipt);claim=service.store.receipt(receipt['id'])['intake_claim'];before_issues=remote.issuances
                    with patch.object(core,'SETTINGS_FILE',str(Path(folder)/'other-pc/settings.json')):
                        other=StationService(core);other.store.put('preferences',other.preferences()|{'pricingEnabled':False});other.cloud=CloudClient(other.store.root,transport=remote);other.shared_trades.cloud=other.cloud
                        other.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
                        original=service.store.receipt(receipt['id']);copied=copy.deepcopy(original)
                        copied['id']='R-OTHER-PC-'+receipt['id'];copied.pop('shared_trade');copied.pop('intake_claim');copied['purchase']=None;other.store.save_receipt(copied)
                        for card in copied['cards']:card['row_id']=uuid.uuid4().hex
                        other.store.save_receipt(copied)
                    second=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);second.station=other;second_thread=threading.Thread(target=second.serve_forever,daemon=True);second_thread.start();allowed_ports.add(second.server_port)
                    another=context.new_page();another.set_default_timeout(10000);another.on('pageerror',lambda error:errors.append(str(error)))
                    try:
                        another.goto(f'http://127.0.0.1:{second.server_port}',wait_until='domcontentloaded');another.locator('#openHistory').click();another.locator('#historyList [data-open="'+copied['id']+'"]').click();expect(another.locator('#receiptPicker')).to_have_value(copied['id']);another.locator('#openSharedTrade').click()
                        another.locator('#intakePhysicalReference').fill('TAG-OTHER-PC-SAME-CARDS');another.locator('#intakePhysicalVerified').check();another.locator('#createIntakeClaim').click()
                        expect(another.locator('#intakeClaimFacts')).to_contain_text(claim['physical_intake_id']);expect(another.locator('#intakeClaimFacts')).to_contain_text(claim['physical_reference'])
                        other_saved=other.store.receipt(copied['id']);assert other_saved['intake_claim']['physical_intake_id']==claim['physical_intake_id']
                        assert other_saved['cards'][0]['source_line_key']==original['cards'][0]['source_line_key']
                        expect(another.locator('[data-shared-customer="'+customer+'"]').first).to_be_visible();another.locator('[data-shared-customer="'+customer+'"]').first.click();another.locator('#saveSharedTradeDraft').click();expect(another.locator('#sharedTradeOffer')).to_be_visible()
                        consent();page.locator('#acceptSharedTrade').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('Shared credit confirmed once')
                        consent(another);another.locator('#acceptSharedTrade').click();expect(another.locator('#sharedTradeMessage')).to_contain_text('already consumed')
                        assert remote.issuances==before_issues+1 and other.store.receipt(copied['id']).get('purchase') is None
                        another.screenshot(path=str(REPORT/'intake-claim-second-pc-blocked.png'),full_page=True)
                        another.locator('#cancelSharedTradeDraft').click();another.locator('#confirmOkay').click();expect(another.locator('#sharedTradeMessage')).to_contain_text('cancellation confirmed')
                        assert other.cloud.pending()['pending']==[];page.locator('#closeSharedTrade').click()
                    finally:another.close();second.shutdown();second.server_close();second_thread.join(5);allowed_ports.remove(second.server_port)
                def server_mfa_denial_can_cancel_without_local_fallback():
                    receipt=create('Fictional Fresh MFA Required');draft(receipt);consent();before_issues=remote.issuances;remote.mfa_fresh=False
                    try:
                        page.locator('#acceptSharedTrade').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('fresh proof');expect(page.locator('#sharedTradeSignIn')).to_be_visible()
                        expect(page.locator('#intakeMfaSetup')).to_be_visible();expect(page.locator('#intakeMfaSetup')).to_have_attribute('href','https://stock-and-sleeve-api.onrender.com/');expect(page.locator('#intakeMfaSetup')).to_have_attribute('rel','noopener noreferrer');expect(page.locator('#intakeMfaHelp')).to_contain_text('sign out and sign in HERE')
                        assert remote.issuances==before_issues and service.store.receipt(receipt['id']).get('purchase') is None
                        assert service.cloud._pending()[0]['kind']=='trade_accept'
                        page.locator('#cancelSharedTradeDraft').click();page.locator('#confirmOkay').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('cancellation confirmed')
                        assert service.cloud.pending()['pending']==[] and remote.issuances==before_issues;page.locator('#closeSharedTrade').click()
                    finally:remote.mfa_fresh=True
                def selected_all_and_legacy_upgrade_are_explicit():
                    receipt=service.import_csv('Name,Game,Price,Quantity\nSelected fictional,Magic,3.13,2\nExcluded fictional,Riftbound,2.13,1\n','selected.csv','trade')['receipt']
                    receipt['cards'][0]['reviewed']=True;receipt['cards'][1]['include']=False;service.store.save_receipt(receipt);draft(receipt)
                    expect(page.locator('#intakeClaimFacts')).to_contain_text('Selected 2 / original 3 copies');expect(page.locator('#intakeClaimFacts')).to_contain_text('1 excluded copies remain NOT RECEIVED')
                    page.screenshot(path=str(REPORT/'intake-claim-selected-excluded.png'),full_page=True);page.locator('#cancelSharedTradeDraft').click();page.locator('#confirmOkay').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('cancellation confirmed');page.locator('#closeSharedTrade').click()
                    legacy=create('Fictional Legacy No Original Manifest');legacy.pop('source_manifest');service.store.save_receipt(legacy);open_shared(legacy)
                    expect(page.locator('#intakeClaimFacts')).to_contain_text('no original intake manifest');expect(page.locator('#intakeClaimInputs')).to_be_hidden();expect(page.locator('#sharedTradeCustomerPicker')).to_be_hidden();page.locator('#closeSharedTrade').click()
                def manual_tag_and_additional_delivery():
                    receipt=service.add_manual_item({'requestId':str(uuid.uuid4()),'game':'magic','mode':'trade','item':{'card_name':'Fictional manual physical stack','qty':1,'final_price':4.13}})
                    receipt['cards'][0]['reviewed']=True;service.store.save_receipt(receipt);open_shared(receipt)
                    expect(page.locator('#sharedTradeCustomerPicker')).to_be_hidden();page.locator('#intakePhysicalReference').fill('MANUAL-PHYSICAL-TAG-001');page.locator('#intakePhysicalVerified').check();page.locator('#createIntakeClaim').click()
                    expect(page.locator('#intakeClaimFacts')).to_contain_text('manual-physical-tag-001');expect(page.locator('#intakeAdditionalSection')).to_be_visible()
                    original=service.store.receipt(receipt['id'])['intake_claim']['physical_intake_id'];page.locator('#intakeAdditionalSection summary').click()
                    page.locator('#intakeAdditionalTag').fill('MANUAL-SEPARATE-DELIVERY-002');page.locator('#intakeAdditionalReason').fill('Manager verified a separate physical delivery of identical cards');page.locator('#intakeAdditionalVerified').check();remote.mfa_fresh=False
                    try:
                        page.locator('#createAdditionalIntake').click();expect(page.locator('#sharedTradeMessage')).to_contain_text('fresh proof');expect(page.locator('#checkIntakeClaim')).to_be_enabled();expect(page.locator('#selectIntakeClaim')).to_be_disabled()
                        stored=service.store.receipt(receipt['id']);key=stored['intake_claim']['request_key'];assert stored['intake_claim']['additional_delivery_of']==original
                        page.screenshot(path=str(REPORT/'intake-additional-mfa-required.png'),full_page=True)
                        remote.mfa_fresh=True;page.locator('#checkIntakeClaim').click();expect(page.locator('#intakeClaimFacts')).to_contain_text('manual-separate-delivery-002')
                        stored=service.store.receipt(receipt['id']);assert stored['intake_claim']['request_key']==key and stored['intake_claim']['physical_intake_id']!=original
                        assert stored['intake_claim_history'][-1]['physical_intake_id']==original and stored.get('purchase') is None
                        page.screenshot(path=str(REPORT/'intake-additional-confirmed.png'),full_page=True);page.locator('#closeSharedTrade').click()
                    finally:remote.mfa_fresh=True
                case('Explicit central physical reference, shared customer and all confirmations issue credit once',normal_issuance)
                case('Lost acceptance response remains locked and full-page reopen recovers one confirmed credit',lost_acceptance_recovery)
                case('Draft cancellation is confirmed by the server before the local batch unlocks',draft_cancel)
                case('Pending drawers and saved-card research stay read-only without dirty state, Quit can cancel, and confirmed void restores editing',pending_drawer_readonly_and_restore)
                case('Lost physical-claim response recovers its original encrypted request after full-page reopen without a new claim',lost_claim_response)
                case('Two actual Chrome desktop origins bind the same physical claim and only one accepted trade issues credit',cross_pc_same_physical_source)
                case('Server fresh-MFA denial preserves the original agreement and permits authoritative draft cancellation without fallback',server_mfa_denial_can_cancel_without_local_fallback)
                case('Selected/all/excluded-copy limits and legacy missing-original reimport guidance are explicit',selected_all_and_legacy_upgrade_are_explicit)
                case('Manual physical tags precede trade selection and separate deliveries retain linked intent through server MFA denial/recovery',manual_tag_and_additional_delivery)
                assert not errors,errors;assert not external,external;assert not backend,backend;assert square.call_count==0
                record('PASS no external browser/backend requests, no Square uploads and no uncaught JavaScript errors');browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5);after=hashes();stable=before==after
            payload={'started_utc':started,'finished_utc':datetime.now(timezone.utc).isoformat(),'scope':'Real Chrome loopback desktop UI with deterministic fictional hosted fixture; not live hosted/Square acceptance.',
                'source_stable':stable,'sources_before':before,'sources_after':after,'cases':cases,'browser_errors':errors,'external_browser_attempts':external,'backend_http_attempts':backend,'square_upload_attempts':square.call_count,'fixture_credit_issuances':remote.issuances}
            (REPORT/'shared-trade-user-acceptance.json').write_text(json.dumps(payload,indent=2),encoding='utf-8');(REPORT/'shared-trade-user-acceptance.log').write_text('\n'.join(steps)+'\n',encoding='utf-8')
    if not stable or any(item['status']!='pass' for item in cases):raise SystemExit(1)


if __name__=='__main__':run()
