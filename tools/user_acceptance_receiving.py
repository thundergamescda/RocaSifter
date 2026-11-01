"""Real Chrome, disposable desktop origins, fictional hosted receiving only."""
from datetime import datetime,timezone
import copy
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
from station_service import StationService
from station_cloud import CloudClient
from test_catalog_receive import CatalogReceivingFixtureTransport

SOURCES=('station_hosted_receive.py','receiving_evidence.py','hosted_labels.py','station_cloud.py','station_service.py','station_server.py','station_store.py','station.js','receiving.js','receiving.css','roca_square_label_app.html','test_hosted_receive.py','test_catalog_receive.py','tools/user_acceptance_receiving.py','station_shared_trade.py','intake-claim.js')
REPORT=ROOT/'Testing'/'receiving-saga'
def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def run():
    REPORT.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ');runroot=REPORT/('receiving-'+stamp);runroot.mkdir()
    before=hashes();started=datetime.now(timezone.utc).isoformat();cases=[];log=[];errors=[];external=[];provider=[];servers=[]
    def record(message):log.append(message);print(message,flush=True)
    def forbidden(session,method,url,**kwargs):provider.append({'method':method,'host':urlsplit(url).hostname});raise AssertionError('External provider HTTP forbidden')
    with tempfile.TemporaryDirectory() as folder,patch.object(core,'OUTPUT_DIR',str(Path(folder)/'outputs')),patch.object(core.requests.sessions.Session,'request',forbidden),patch.object(core,'upload_to_square',side_effect=AssertionError('Legacy upload forbidden')) as square:
        remote=CatalogReceivingFixtureTransport()
        def station(name):
            home=Path(folder)/name;home.mkdir()
            with patch.object(core,'SETTINGS_FILE',str(home/'settings.json')):service=StationService(core)
            service.store.put('preferences',service.preferences()|{'pricingEnabled':False});service.cloud=CloudClient(service.store.root,transport=remote);service.shared_trades.cloud=service.cloud
            service.cloud.login({'storeSlug':'fictional-store','email':'manager@example.invalid','password':'fixture-staff-password'})
            server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);server.station=service
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();servers.append((server,thread))
            return service,f'http://127.0.0.1:{server.server_port}'
        service,base=station('computer-a');second,base2=station('computer-b');allowed={urlsplit(base).port,urlsplit(base2).port}
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True);context=browser.new_context(viewport={'width':1450,'height':1100});page=context.new_page();page.set_default_timeout(12000)
                def route(item):
                    url=urlsplit(item.request.url)
                    if url.scheme=='http' and url.hostname=='127.0.0.1' and url.port in allowed:item.continue_()
                    else:external.append({'scheme':url.scheme,'host':url.hostname});item.abort()
                context.route('**/*',route);page.on('pageerror',lambda error:errors.append(str(error)))
                def create(name,target=service,reversed_order=False,price='3.13'):
                    rows=[f'{name} A,Magic,{price},2',f'{name} B,Riftbound,4.13,1']
                    if reversed_order:rows.reverse()
                    receipt=target.import_csv('Name,Game,Price,Quantity\n'+'\n'.join(rows)+'\n','fictional.csv','labels',new_delivery=True)['receipt']
                    receipt['inventory_intent']='new';receipt['title']=name
                    for row in receipt['cards']:row.update(reviewed=True,operator_price=True)
                    target.store.save_receipt(receipt);return receipt
                def open_receipt(receipt,url=base):
                    page.goto(url,wait_until='domcontentloaded');expect(page.locator('#version')).to_have_text('2.6.0');page.locator('#openHistory').click();page.locator('#historyList [data-open="'+receipt['id']+'"]').click();expect(page.locator('#receiptPicker')).to_have_value(receipt['id'])
                    page.locator('#openHostedReceiving').click();expect(page.locator('#receivingDialog')).to_be_visible();expect(page.locator('#closeReceiving')).to_be_enabled()
                def claim(receipt):
                    page.locator('#receivingTag').fill('TAG-'+receipt['id']);page.locator('#receivingPhysicalVerified').check();page.locator('#receivingCreateClaim').click();expect(page.locator('#receivingSetup')).to_be_visible();expect(page.locator('#receivingPrepare')).to_be_enabled()
                def configure():
                    expect(page.locator('#receivingLocation')).to_have_value('');page.locator('#receivingLocation').select_option('FIXTURE-LOC')
                    for name in ('receivingOwned','receivingNew','receivingExcluded'):page.locator('#'+name).check()
                def plan(receipt):
                    open_receipt(receipt);claim(receipt);configure();page.locator('#receivingPrepare').click();expect(page.locator('#receivingReview')).to_be_visible();expect(page.locator('#closeReceiving')).to_be_enabled()
                def confirm():
                    expect(page.locator('#receivingCommit')).to_be_disabled();page.locator('#receivingConfirm').check();expect(page.locator('#receivingCommit')).to_be_disabled();page.locator('#receivingCommitExcluded').check();expect(page.locator('#receivingCommit')).to_be_enabled();page.locator('#receivingCommit').click()
                def case(name,work):
                    begin=time.monotonic()
                    try:work();cases.append({'name':name,'status':'pass','elapsed_seconds':round(time.monotonic()-begin,3)});record('PASS '+name)
                    except Exception:
                        failure=traceback.format_exc();shot='failure-'+str(len(cases)+1)+'.png';page.screenshot(path=str(runroot/shot),full_page=True);cases.append({'name':name,'status':'fail','error':failure,'screenshot':shot});record('FAIL '+name+'\n'+failure)
                    finally:
                        page.goto(base,wait_until='domcontentloaded')
                shared={}
                def normal():
                    receipt=create('Fictional Commerce');plan(receipt);expect(page.locator('#receivingFacts')).to_contain_text('3 selected / 3 original copies');expect(page.locator('#receivingTargets')).to_contain_text('SS-');assert remote.stock_calls==0
                    page.screenshot(path=str(runroot/'plan-review.png'),full_page=True);confirm();expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');expect(page.locator('#closeReceiving')).to_be_enabled()
                    saved=service.store.receipt(receipt['id']);assert saved['inventory_status']=='received' and not saved['purchase'];assert remote.stock_calls==1
                    page.screenshot(path=str(runroot/'received-proof.png'),full_page=True);page.locator('#closeReceiving').click();expect(page.locator('#labelLayout')).to_be_disabled();expect(page.locator('#printerDpi')).to_be_disabled()
                    shared.update(receipt=saved,operation=saved['hosted_receive']['receiving_id'])
                def lost_and_restart():
                    receipt=create('Fictional Lost Reply');open_receipt(receipt);claim(receipt);configure();remote.drop_after.add('plan');previous=len(remote.receivings)
                    page.locator('#receivingPrepare').click();expect(page.locator('#receivingMessage')).to_contain_text('unavailable');expect(page.locator('#receivingResume')).to_be_enabled()
                    original=copy.deepcopy(service.store.receipt(receipt['id'])['hosted_receive']['actions']['plan']);assert len(remote.receivings)==previous+1
                    open_receipt(receipt);expect(page.locator('#receivingFacts')).to_contain_text('Ready for review');expect(page.locator('#closeReceiving')).to_be_enabled();assert service.store.receipt(receipt['id'])['hosted_receive']['actions']['plan']==original
                    remote.drop_after.add('commit');previous=remote.stock_calls;confirm();expect(page.locator('#receivingMessage')).to_contain_text('unavailable');expect(page.locator('#receivingResume')).to_be_enabled();assert remote.stock_calls==previous+1
                    key=service.store.receipt(receipt['id'])['hosted_receive']['actions']['commit']['body']['request_key'];page.screenshot(path=str(runroot/'lost-confirmation.png'),full_page=True)
                    open_receipt(receipt);expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');assert remote.stock_calls==previous+1;assert service.store.receipt(receipt['id'])['hosted_receive']['actions']['commit']['body']['request_key']==key
                def unknown():
                    receipt=create('Fictional Unknown');plan(receipt);remote.unknown=True;previous=remote.stock_calls;confirm();expect(page.locator('#receivingFacts')).to_contain_text('Awaiting provider evidence');expect(page.locator('#receivingConsent')).to_be_hidden();expect(page.locator('#receivingCancel')).to_be_hidden()
                    page.locator('#receivingRecover').click();expect(page.locator('#closeReceiving')).to_be_enabled();assert remote.stock_calls==previous+1;assert service.store.receipt(receipt['id'])['inventory_status']=='not_received'
                    page.screenshot(path=str(runroot/'unknown-evidence-only.png'),full_page=True);remote.unknown=False;page.locator('#receivingRecover').click();expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');assert remote.stock_calls==previous+1
                def cross_pc():
                    old=shared['receipt'];fresh=create('Fictional Commerce',second,True);assert set(row['row_id'] for row in fresh['cards']).isdisjoint(row['row_id'] for row in old['cards']);keys=set(remote.requests);count=remote.stock_calls
                    open_receipt(fresh,base2);page.locator('#receivingClaimId').fill(old['intake_claim']['physical_intake_id']);page.locator('#receivingPhysicalVerified').check();page.locator('#receivingSelectClaim').click();expect(page.locator('#receivingSetup')).to_be_visible();expect(page.locator('#closeReceiving')).to_be_enabled()
                    page.locator('#receivingExistingId').fill(shared['operation']);page.locator('#receivingExisting').click();expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');expect(page.locator('#closeReceiving')).to_be_enabled()
                    saved=second.store.receipt(fresh['id']);assert saved['inventory_status']=='received' and saved['hosted_receive']['label_profile']==old['hosted_receive']['label_profile'];assert set(remote.requests)==keys and remote.stock_calls==count
                    assert {row['card_name']:row['sku'] for row in saved['cards']}=={row['card_name']:row['sku'] for row in old['cards']};expect(page.locator('#receivingTargets')).to_contain_text('Fictional Commerce A');expect(page.locator('#receivingTargets')).to_contain_text('Fictional Commerce B');page.screenshot(path=str(runroot/'cross-pc-independent-import.png'),full_page=True)
                def changed_csv_price():
                    old=shared['receipt'];fresh=create('Fictional Commerce',second,True,'99');open_receipt(fresh,base2)
                    page.locator('#receivingClaimId').fill(old['intake_claim']['physical_intake_id']);page.locator('#receivingPhysicalVerified').check();page.locator('#receivingSelectClaim').click();expect(page.locator('#receivingSetup')).to_be_visible();expect(page.locator('#closeReceiving')).to_be_enabled()
                    before=second.store.receipt(fresh['id']);keys=set(remote.requests);count=remote.stock_calls
                    page.locator('#receivingExistingId').fill(shared['operation']);page.locator('#receivingExisting').click();expect(page.locator('#receivingMessage')).to_contain_text('Fresh CSV prices cannot replace');expect(page.locator('#receivingConsent')).to_be_hidden();expect(page.locator('#closeReceiving')).to_be_enabled()
                    assert second.store.receipt(fresh['id'])==before and set(remote.requests)==keys and remote.stock_calls==count;page.screenshot(path=str(runroot/'different-price-read-only.png'),full_page=True)
                def label_gate_and_revision():
                    receipt=create('Fictional Wide Label');remote.target_skus['Fictional Wide Label A']='Exact-'+('X'*90);plan(receipt);expect(page.locator('#receivingLabelGate')).to_contain_text('does not fit');page.locator('#receivingConfirm').check();page.locator('#receivingCommitExcluded').check();expect(page.locator('#receivingCommit')).to_be_disabled();previous=remote.stock_calls
                    operation=service.store.receipt(receipt['id'])['hosted_receive']['receiving_id'];page.locator('#receivingCancel').click();expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmOkay').click();expect(page.locator('#receivingSetup')).to_be_visible();expect(page.locator('#receivingRevise')).to_be_enabled()
                    page.locator('#receivingStock').select_option('4x2');page.locator('#receivingLocation').select_option('FIXTURE-LOC');page.locator('#receivingRevise').click();expect(page.locator('#receivingFacts')).to_contain_text('4x2');expect(page.locator('#closeReceiving')).to_be_enabled();assert service.store.receipt(receipt['id'])['hosted_receive']['receiving_id']==operation;assert remote.stock_calls==previous
                    page.screenshot(path=str(runroot/'explicit-wider-stock-same-operation.png'),full_page=True)
                def rejected_request_retirement():
                    receipt=create('Fictional Rejected Plan');open_receipt(receipt);claim(receipt);configure();remote.plan_reject=True
                    page.locator('#receivingPrepare').click();expect(page.locator('#receivingMessage')).to_contain_text('Approve exact catalog binding');expect(page.locator('#receivingRetire')).to_be_enabled()
                    original=copy.deepcopy(service.store.receipt(receipt['id'])['hosted_receive']['actions']['plan']['body']);remote.drop_after.add('retire')
                    page.locator('#receivingRetire').click();page.locator('#confirmOkay').click();expect(page.locator('#receivingMessage')).to_contain_text('unavailable');expect(page.locator('#receivingResume')).to_be_enabled()
                    assert service.store.receipt(receipt['id'])['hosted_receive']['state']=='retiring';page.screenshot(path=str(runroot/'retirement-lost-reply.png'),full_page=True)
                    open_receipt(receipt);expect(page.locator('#receivingMessage')).to_contain_text('retired the original request');expect(page.locator('#closeReceiving')).to_be_enabled()
                    saved=service.store.receipt(receipt['id']);assert saved['hosted_receive']['server_retirement']['original_plan_sha256']==__import__('hosted_labels').fingerprint(original);assert saved['hosted_receive']['state']=='retired_before_creation'
                    page.screenshot(path=str(runroot/'retirement-proved-before-unlock.png'),full_page=True);remote.plan_reject=False
                def accepted_trade_stock_and_second_computer():
                    receipt=create('Fictional Accepted Stock');receipt['mode']='trade';receipt['requires_purchase']=True;service.store.save_receipt(receipt)
                    session=service.cloud.status()['sessionId'];receipt=service.intake_claims.prepare(receipt['id'],{'revision':receipt['revision'],'physicalReference':'TAG-'+receipt['id'],'physicalDeliveryVerified':True},session)['receipt']
                    customer=service.cloud.create_customer({'request_key':'receiving-chrome-customer','display_name':'Fictional Receiving Customer','permission_confirmed':True})['customer_id']
                    draft=service.shared_trades.prepare(receipt['id'],{'revision':receipt['revision'],'customerId':customer},session)
                    accepted=service.shared_trades.accept(receipt['id'],{'confirmationHash':draft['confirmationHash'],'customerAgreed':True,'itemsReceived':True,'claimReviewed':True},session)['receipt'];issues=remote.issuances
                    open_receipt(accepted);expect(page.locator('#receivingKind')).to_have_value('accepted_trade');expect(page.locator('#receivingKind')).to_be_disabled();configure();page.locator('#receivingPrepare').click();expect(page.locator('#receivingFacts')).to_contain_text('Original accepted hosted trade');expect(page.locator('#closeReceiving')).to_be_enabled();confirm();expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');expect(page.locator('#closeReceiving')).to_be_enabled();assert remote.issuances==issues
                    original=service.store.receipt(receipt['id']);fresh=create('Fictional Accepted Stock',second,True);fresh.update(mode='trade',requires_purchase=True);second.store.save_receipt(fresh);keys=set(remote.requests);count=remote.stock_calls
                    open_receipt(fresh,base2);page.locator('#receivingClaimId').fill(original['intake_claim']['physical_intake_id']);page.locator('#receivingPhysicalVerified').check();page.locator('#receivingSelectClaim').click();expect(page.locator('#receivingSetup')).to_be_visible();expect(page.locator('#closeReceiving')).to_be_enabled()
                    page.locator('#receivingExistingId').fill(original['hosted_receive']['receiving_id']);page.locator('#receivingExisting').click();expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');expect(page.locator('#closeReceiving')).to_be_enabled();assert set(remote.requests)==keys and remote.stock_calls==count and remote.issuances==issues
                    page.screenshot(path=str(runroot/'accepted-trade-cross-pc-original-financial-proof.png'),full_page=True);page.locator('#closeReceiving').click();expect(page.locator('#tradePanel')).to_be_hidden();page.locator('#reviewContinue').click();expect(page.locator('#labelsPanel')).to_be_visible();assert not second.store.receipt(fresh['id'])['purchase']
                def catalog_unknown_then_other_pc_fresh_stock():
                    name='Fictional Catalog Price';remote.new_price_names.add(name+' A');receipt=create(name);plan(receipt);previous_catalog=remote.catalog_calls;previous_stock=remote.stock_calls
                    expect(page.locator('#receivingTargets')).to_contain_text('New price SKU');expect(page.locator('#receivingCommit')).to_have_text('Confirm new price catalog step');remote.catalog_unknown=True
                    confirm();expect(page.locator('#receivingFacts')).to_contain_text('Awaiting provider evidence');expect(page.locator('#closeReceiving')).to_be_enabled();assert remote.catalog_calls==previous_catalog+1 and remote.stock_calls==previous_stock
                    original_key=service.store.receipt(receipt['id'])['hosted_receive']['actions']['commit']['body']['request_key'];remote.catalog_unknown=False
                    page.locator('#receivingRecover').click();expect(page.locator('#receivingFacts')).to_contain_text('New price confirmed · stock not added');expect(page.locator('#receivingAuthorize')).to_be_enabled();assert remote.stock_calls==previous_stock
                    page.screenshot(path=str(runroot/'catalog-confirmed-stock-not-added.png'),full_page=True);original=service.store.receipt(receipt['id']);keys=set(remote.requests)
                    fresh=create(name,second,True);open_receipt(fresh,base2);page.locator('#receivingClaimId').fill(original['intake_claim']['physical_intake_id']);page.locator('#receivingPhysicalVerified').check();page.locator('#receivingSelectClaim').click();expect(page.locator('#receivingSetup')).to_be_visible();expect(page.locator('#closeReceiving')).to_be_enabled()
                    page.locator('#receivingExistingId').fill(original['hosted_receive']['receiving_id']);page.locator('#receivingExisting').click();expect(page.locator('#receivingFacts')).to_contain_text('New price confirmed · stock not added');expect(page.locator('#receivingAuthorize')).to_be_enabled();assert set(remote.requests)==keys and remote.stock_calls==previous_stock
                    remote.mfa_fresh=False;page.locator('#receivingAuthorize').click();expect(page.locator('#confirmDialog')).to_be_visible();page.locator('#confirmOkay').click();expect(page.locator('#receivingMessage')).to_contain_text('Fresh enrolled MFA');expect(page.locator('#receivingAuthorize')).to_be_enabled();assert remote.stock_calls==previous_stock
                    action=copy.deepcopy(second.store.receipt(fresh['id'])['hosted_receive']['actions']['authorize']);remote.mfa_fresh=True;remote.drop_after.add('authorize')
                    page.locator('#receivingAuthorize').click();page.locator('#confirmOkay').click();expect(page.locator('#receivingMessage')).to_contain_text('unavailable');expect(page.locator('#closeReceiving')).to_be_enabled();assert remote.stock_calls==previous_stock+1;assert second.store.receipt(fresh['id'])['inventory_status']=='not_received'
                    page.screenshot(path=str(runroot/'new-price-stock-lost-reply.png'),full_page=True);open_receipt(fresh,base2);expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');expect(page.locator('#closeReceiving')).to_be_enabled();assert second.store.receipt(fresh['id'])['hosted_receive']['actions']['authorize']==action
                    open_receipt(receipt);expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed');expect(page.locator('#closeReceiving')).to_be_enabled();assert service.store.receipt(receipt['id'])['hosted_receive']['actions']['commit']['body']['request_key']==original_key
                    assert remote.catalog_calls==previous_catalog+1 and remote.stock_calls==previous_stock+1
                    page.screenshot(path=str(runroot/'new-price-both-pcs-original-proof.png'),full_page=True)
                def full_slab_sports_identity_and_exact_spaces():
                    name='Fictional Sports Slab';receipt=service.add_manual_item({'requestId':str(__import__('uuid').uuid4()),'game':'sports','mode':'labels',
                        'item':{'card_name':name,'card_set':'Fictional Series','card_num':'007','qty':1,'final_price':25,'condition':'BGS 10 Black Label',
                        'printing':'Refractor','language':'English','grader':'BGS','grade':'10','grade_qualifier':'Black Label','cert_number':'FICTIONAL-CERT-001',
                        'year':'2026','manufacturer':'Fictional Maker','team':'Fictional Team','player':'Fictional Player','serial_number':'01/10'}})
                    receipt['inventory_intent']='new';receipt['cards'][0].update(reviewed=True,operator_price=True,sport='Basketball',league='Fictional League');service.store.save_receipt(receipt)
                    sku='  Sports >5^~  ';remote.target_skus[name]=sku;plan(receipt)
                    for field in ('Black Label','2026','Fictional Maker','Fictional Team','Fictional Player','Basketball','Fictional League','01/10','FICTIONAL-CERT-001'):expect(page.locator('#receivingTargets')).to_contain_text(field)
                    expect(page.locator('#receivingTargets code').first).to_have_text('“'+sku+'”');assert page.locator('#receivingTechnical').evaluate('(node)=>node.open') is False
                    expect(page.locator('#receivingTechnical .receiving-hash')).to_be_hidden();expect(page.locator('#receivingFacts')).to_contain_text('Ready for review')
                    assert page.locator('#receivingTargets tbody tr').first.evaluate('(row)=>{const cells=row.querySelectorAll("td"),identity=cells[0].querySelector("small");return identity.scrollWidth<=identity.clientWidth+1&&identity.getBoundingClientRect().right<cells[1].getBoundingClientRect().left&&cells[0].getBoundingClientRect().bottom>=identity.getBoundingClientRect().bottom}')
                    assert 'VAR-' not in page.locator('#receivingTargets').inner_text();page.screenshot(path=str(runroot/'full-sports-slab-identity-and-exact-sku-spaces.png'),full_page=True)
                case('Reviewed server plan and both confirmations receive selected stock once, with frozen labels',normal)
                case('Lost plan and stock replies survive full-page restart with original keys and one stock admission',lost_and_restart)
                case('Unknown stock stays locked, cannot cancel/replan, and read-only evidence recovery adds no stock',unknown)
                case('Independent second-computer shuffled import with different UUIDs recovers exact original operation and profile without new keys',cross_pc)
                case('Different fresh CSV retail prices show original server record read-only and never replace the frozen plan',changed_csv_price)
                case('Unfit exact SKU blocks commit and explicit wider stock revises the same pre-admission operation',label_gate_and_revision)
                case('Definitively rejected planning request stays locked through lost retirement reply and unlocks only with exact server tombstone',rejected_request_retirement)
                case('Accepted hosted trade stock and independently imported second-computer receipt preserve original financial authority without new credit',accepted_trade_stock_and_second_computer)
                case('Unknown new-price catalog recovers read-only; a second PC requires explicit fresh-MFA stock authorization and lost reply recovers one stock effect',catalog_unknown_then_other_pc_fresh_stock)
                case('Full slab tier and sports identity stay visible, exact SKU spaces are quoted, and raw references start collapsed',full_slab_sports_identity_and_exact_spaces)
                assert not errors,errors;assert not external,external;assert not provider,provider;assert square.call_count==0
                record('PASS no external browser/backend calls, local Square uploads, or uncaught JavaScript errors');browser.close()
        finally:
            for server,thread in servers:server.shutdown();server.server_close();thread.join(5)
            after=hashes();stable=before==after
            payload={'started_utc':started,'finished_utc':datetime.now(timezone.utc).isoformat(),'scope':'Actual Chrome on two loopback desktop origins using fictional deterministic hosted receiving transport. No real Square, hardware, hosted PostgreSQL or merchant acceptance claim.',
                'source_stable':stable,'sources_before':before,'sources_after':after,'cases':cases,'browser_errors':errors,'external_browser_attempts':external,'backend_http_attempts':provider,'legacy_square_uploads':square.call_count,'fixture_provider_stock_admissions':remote.stock_calls}
            (runroot/'receiving-user-acceptance.json').write_text(json.dumps(payload,indent=2),encoding='utf-8');(runroot/'receiving-user-acceptance.log').write_text('\n'.join(log)+'\n',encoding='utf-8')
            print('EVIDENCE',runroot,flush=True)
    if not stable or any(case['status']!='pass' for case in cases):raise SystemExit(1)


if __name__=='__main__':run()
