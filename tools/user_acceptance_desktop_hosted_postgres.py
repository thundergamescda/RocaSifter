"""Two real desktop profiles -> real typed hosted API -> disposable PostgreSQL.

Only Square is fictional. Sessions/MFA seeds are explicit test fixtures, so this
does not claim sign-in/enrollment, actual physical PCs/devices or provider proof.
No outbound HTTP is allowed; Flask's test client handles the fixed-origin relay.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
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
import uuid

import requests
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import roca_to_square as core
from hosted.app import create_app
from hosted.security import digest
from hosted_labels import trusted_resolution
from station_cloud import CloudClient,CloudError,ORIGIN
from station_intake_claim import original_identity
from station_service import StationService
import test_hosted_receiving_postgres as receiving_fixtures
from test_hosted_receiving_pipeline_postgres import PostgreSQLReceivingPipelineAcceptance


def hashes():
    paths=[p for p in ROOT.iterdir() if p.is_file() and p.suffix in ('.py','.js','.css','.html') and not p.name.startswith('test_')]
    paths.extend(p for p in (ROOT/'hosted').rglob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.sql','.png'))
    paths.extend(ROOT/name for name in ('test_hosted_receiving_postgres.py','test_hosted_receiving_pipeline_postgres.py',
                  'test_hosted_settlement_postgres.py','test_receiving_fixtures.py','test_intake_fixtures.py',
                  'tools/user_acceptance_desktop_hosted_postgres.py'))
    return {str(p.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


class RealAPITransport:
    def __init__(self,app):self.app=app;self.calls=[];self.drop_authorize=False;self.dropped_authorize=0;self.guard=threading.Lock()

    def request(self,method,url,*,headers,json=None,timeout=None,allow_redirects=None):
        parsed=urlsplit(url)
        assert url.startswith(ORIGIN+'/api/') and parsed.hostname==urlsplit(ORIGIN).hostname
        assert timeout==(5,20) and allow_redirects is False
        # Never retain headers: they contain only fictional, but still secret,
        # fixture tokens. Contract facts are actual API bodies, not shaped mocks.
        with self.guard:self.calls.append({'method':method,'path':parsed.path,'body_hash':hashlib.sha256(__import__('json').dumps(json,sort_keys=True).encode()).hexdigest()})
        client=self.app.test_client()
        result=client.open(parsed.path+('?' + parsed.query if parsed.query else ''),method=method,
                           headers=headers,json=json,base_url=ORIGIN)
        response=requests.Response();response.status_code=result.status_code;response._content=result.data
        response.headers.update(dict(result.headers))
        with self.guard:
            drop=self.drop_authorize and parsed.path.endswith('/authorize') and result.status_code==200
            if drop:self.drop_authorize=False;self.dropped_authorize+=1
        if drop:raise requests.Timeout('Fictional completed local API authorization response lost')
        return response


def run():
    destination=ROOT/'Testing/receiving-saga'/('desktop-hosted-pg-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    destination.mkdir(parents=True,exist_ok=False)
    before=hashes();results=[];network=[];browser_errors=[];browser_external=[]
    outcome={'started_utc':datetime.now(timezone.utc).isoformat(),'passed':False,'results':results,'sources_before':before,
             'scope':'Installed Chrome on two independent real desktop HTTP origins/profiles, actual fixed-origin CloudClient relay and Flask API/Auth.resolve with disposable PostgreSQL; injected fictional Square provider and seeded fictional MFA sessions. No external HTTP, real store/customer, login/enrollment, device or live Square proof.'}
    fixture_class=PostgreSQLReceivingPipelineAcceptance
    case=fixture_class('test_new_price_creates_preserved_sibling_then_freshly_admits_exact_stock')
    prepared=False
    try:
        with tempfile.TemporaryDirectory(prefix='ss-full-path-',dir=destination) as temporary,ExitStack() as stack:
            home=Path(temporary);stack.enter_context(patch.object(core,'OUTPUT_DIR',str(home/'outputs')))
            stack.enter_context(patch.object(core,'upload_to_square',side_effect=AssertionError('Legacy local Square upload forbidden')))
            def forbidden(_session,method,url,**_options):
                network.append({'method':method,'host':urlsplit(url).hostname})
                raise AssertionError('External HTTP forbidden in real API/PostgreSQL desktop fixture')
            stack.enter_context(patch('requests.sessions.Session.request',forbidden))
            stations=[];receipts=[]
            source='Name,Game,Set,Number,Condition,Printing,Language,Price,Quantity\nFictional card,Pok\u00e9mon,Fictional set,001,Near Mint,Normal,English,12.50,2\n'
            for name in ('computer-a','computer-b'):
                folder=home/name;folder.mkdir()
                with patch.object(core,'SETTINGS_FILE',str(folder/'settings.json')):station=StationService(core)
                station.store.put('preferences',station.preferences()|{'pricingEnabled':False})
                receipt=station.import_csv(source,'fictional-full-path.csv','labels',new_delivery=True)['receipt']
                receipt['inventory_intent']='new'
                for row in receipt['cards']:row.update(reviewed=True,operator_price=True)
                station.store.save_receipt(receipt);stations.append(station);receipts.append(receipt)
            assert receipts[0]['cards'][0]['row_id']!=receipts[1]['cards'][0]['row_id']
            fixture_class.setUpClass();prepared=True
            native_identity=original_identity(receipts[0]['cards'][0]);original_card=receiving_fixtures.card_line
            def exact_card(**kwargs):
                value=original_card(**kwargs);value['identity']=deepcopy(native_identity);return value
            with patch.object(receiving_fixtures,'card_line',exact_card),patch.object(receiving_fixtures,'capture_manifest',return_value=deepcopy(receipts[0]['source_manifest'])):
                case.setUp()
            app=create_app({'TESTING':True,'PUBLIC_BASE_URL':ORIGIN,'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode(),
                            'BOOTSTRAP_TOKEN':'','SQUARE_APPLICATION_ID':'','SQUARE_APPLICATION_SECRET':'',
                            'SQUARE_WEBHOOK_SIGNATURE_KEY':'','SQUARE_ENVIRONMENT':'sandbox','RECEIVING_WRITES_ENABLED':True},database=case.database)
            service=app.extensions['ss_receiving'];service.provider=case.provider;service.templates.provider=case.provider
            remote=RealAPITransport(app)
            with case.database.transaction() as db:slug=db.execute('SELECT slug FROM ss_tenants WHERE id=%s',(case.tenant,)).fetchone()['slug']
            sessions=[]
            for station,role in zip(stations,('owner','manager')):
                actor=case.actors[role];token='fictional-desktop-session-'+uuid.uuid4().hex
                with case.database.transaction() as db:
                    db.execute("""INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                                  VALUES (%s,%s,%s,clock_timestamp()+interval '1 hour',1,clock_timestamp())""",(digest(token),case.tenant,actor['staff_id']))
                station.cloud=CloudClient(station.store.root,transport=remote);station.shared_trades.cloud=station.cloud
                session=station.cloud._commit_login({'access_token':token,'expires_in':3600,'store':slug,'role':role,'mfa_authenticated':True},slug,require_mfa=True)
                assert station.cloud.status()['role']==role;sessions.append(session['sessionId'])
            for index,(station,receipt,session) in enumerate(zip(stations,receipts,sessions)):
                receipts[index]=station.intake_claims.prepare(receipt['id'],{'revision':receipt['revision'],
                    'physicalIntakeId':case.claim['physical_intake_id'],'physicalDeliveryVerified':True},session)['receipt']
            results.append('PASS two independent desktop profiles with different local row IDs bind one actual PostgreSQL intake through real owner/manager sessions and CloudClient API relay.')

            first,second=stations;one,two=receipts;session_a,session_b=sessions
            origins=[]
            stack.enter_context(patch.object(core.RocaStationHandler,'log_message',lambda *_args:None))
            for station in stations:
                server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);server.station=station
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                stack.callback(thread.join,5);stack.callback(server.server_close);stack.callback(server.shutdown)
                origins.append('http://127.0.0.1:'+str(server.server_port))
            playwright=stack.enter_context(sync_playwright())
            browser=playwright.chromium.launch(channel='chrome',headless=True);stack.callback(browser.close)
            context=browser.new_context(viewport={'width':1450,'height':1100})
            def allow_browser(route):
                if any(route.request.url.startswith(origin+'/') for origin in origins):route.continue_()
                else:browser_external.append(route.request.url);route.abort()
            context.route('**/*',allow_browser)
            pages=[context.new_page(),context.new_page()]
            for page in pages:
                page.set_default_timeout(15000);page.on('pageerror',lambda error:browser_errors.append(str(error)))
            def open_receipt(page,origin,receipt):
                page.goto(origin,wait_until='domcontentloaded')
                expect(page.locator('#version')).to_have_text('2.6.0')
                page.locator('#openHistory').click();page.locator('#historyList [data-open="'+receipt['id']+'"]').click()
                expect(page.locator('#receiptPicker')).to_have_value(receipt['id'])
                page.locator('#openHostedReceiving').click();expect(page.locator('#receivingDialog')).to_be_visible()
                expect(page.locator('#closeReceiving')).to_be_enabled()
            page_a,page_b=pages
            open_receipt(page_a,origins[0],one)
            expect(page_a.locator('#receivingSetup')).to_be_visible()
            page_a.locator('#receivingLocation').select_option('fixture-location')
            for ident in ('receivingOwned','receivingNew','receivingExcluded'):page_a.locator('#'+ident).check()
            page_a.locator('#receivingPrepare').click();expect(page_a.locator('#receivingReview')).to_be_visible()
            expect(page_a.locator('#closeReceiving')).to_be_enabled()
            planned=first.hosted_receiving.status(one['id'],session_a)
            assert planned['operation']['state']=='ready' and planned['operation']['targets'][0]['action']=='create_price'
            operation=planned['operation']['receiving_id'];old=deepcopy(case.provider.objects['fixture-variation'])
            expect(page_a.locator('#receivingCommit')).to_be_disabled()
            page_a.locator('#receivingConfirm').check();expect(page_a.locator('#receivingCommit')).to_be_disabled()
            page_a.locator('#receivingCommitExcluded').check();page_a.locator('#receivingCommit').click()
            expect(page_a.locator('#receivingFacts')).to_contain_text('New price confirmed')
            expect(page_a.locator('#closeReceiving')).to_be_enabled()
            catalog=first.hosted_receiving.status(one['id'],session_a)
            assert catalog['operation']['state']=='catalog_confirmed' and not case.provider.calls
            page_a.screenshot(path=str(destination/'chrome-catalog-confirmed.png'),full_page=True)
            count=len([row for row in remote.calls if row['method']!='GET'])
            open_receipt(page_b,origins[1],two)
            page_b.locator('#receivingExistingId').fill(operation);page_b.locator('#receivingExisting').click()
            expect(page_b.locator('#receivingFacts')).to_contain_text('New price confirmed')
            expect(page_b.locator('#closeReceiving')).to_be_enabled()
            bound=second.hosted_receiving.status(two['id'],session_b)
            assert bound['operation']['state']=='catalog_confirmed' and bound.get('readOnly') is not True
            assert len([row for row in remote.calls if row['method']!='GET'])==count and not case.provider.calls
            results.append('PASS actual Chrome requires both plan confirmations, creates one reviewed catalog sibling through real API/PG, and the second Chrome desktop reads/rebinds its exact proof without new action keys or stock.')

            page_b.locator('#receivingAuthorize').click();expect(page_b.locator('#confirmDialog')).to_be_visible()
            expect(page_b.locator('#confirmTitle')).to_contain_text('same confirmed catalog plan')
            page_b.locator('#confirmCancel').click();assert not case.provider.calls
            assert len([row for row in remote.calls if row['method']!='GET'])==count
            results.append('PASS second desktop explicitly cancels the same-plan stock confirmation with no provider effect or authorization request.')

            remote.drop_authorize=True
            def authorize(pair):
                station,receipt,session=pair
                try:return station.hosted_receiving.act(receipt['id'],{'stockContinuationConfirmed':True},session,'authorize')
                except CloudError as error:
                    assert error.offline and error.pending
                    return {'lost_response':True}
            page_b.locator('#receivingAuthorize').click();expect(page_b.locator('#confirmDialog')).to_be_visible()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending=pool.submit(authorize,(first,one,session_a))
                page_b.locator('#confirmOkay').click()
                response_a=pending.result(timeout=20)
                expect(page_b.locator('#closeReceiving')).to_be_enabled()
            assert remote.dropped_authorize==1
            assert (len(case.provider.catalog_calls),len(case.provider.calls))==(1,1)
            for station,receipt,session in zip(stations,receipts,sessions):
                final=station.hosted_receiving.status(receipt['id'],session)
                assert final['receipt']['inventory_status']=='received'
                row=final['receipt']['cards'][0];assert trusted_resolution(row)['sku']==row['sku']
                assert row['qty']==2 and row['final_price']==12.5 and station.cloud.receiving.read()==[]
            assert case.provider.objects['fixture-variation']==old
            assert stations[0].store.receipt(one['id'])['cards'][0]['sku']==stations[1].store.receipt(two['id'])['cards'][0]['sku']
            assert case.wallet()==(0,0) and not network
            for page in pages:
                page.locator('#receivingCheck').click();expect(page.locator('#receivingFacts')).to_contain_text('Selected stock confirmed')
                expect(page.locator('#closeReceiving')).to_be_enabled()
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page_b.screenshot(path=str(destination/'chrome-both-profiles-received.png'),full_page=True)
            assert not browser_errors and not browser_external
            results.append('PASS concurrent owner controller/manager Chrome confirmation with one lost completed response yields one catalog and one inventory effect; both durable profiles/screens recover exact trusted SKU/quantity/price, no credit/legacy fallback/browser errors/external requests.')
            outcome['passed']=True
    except Exception:
        outcome['failure']=traceback.format_exc()
    finally:
        case.doCleanups()
        if prepared:fixture_class.tearDownClass()
        outcome['finished_utc']=datetime.now(timezone.utc).isoformat();outcome['sources_after']=hashes()
        outcome['source_stable']=before==outcome['sources_after'];outcome['passed']=outcome['passed'] and outcome['source_stable']
        outcome['outbound_http_attempts']=network
        outcome['browser_errors']=browser_errors;outcome['external_browser_requests']=browser_external
        (destination/'desktop-hosted-postgres-acceptance.json').write_text(json.dumps(outcome,indent=2)+'\n',encoding='utf-8')
        (destination/'desktop-hosted-postgres-acceptance.log').write_text('\n'.join(results)+('\nFAIL '+outcome['failure'] if 'failure' in outcome else '')+'\n',encoding='utf-8')
    print(json.dumps({'report':str(destination),'passed':outcome['passed'],'cases':len(results),'source_stable':outcome['source_stable']}))
    if not outcome['passed']:raise RuntimeError(outcome.get('failure','Source drift during full desktop/hosted/PostgreSQL acceptance'))


if __name__=='__main__':run()
