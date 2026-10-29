"""Actual Chrome download/race checks with fictional loopback-only export data."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import traceback
import sys
from unittest.mock import Mock
from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright,expect,Error as BrowserError
from werkzeug.serving import make_server

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hosted.app import create_app
from hosted.privacy import Limits
from hosted.privacy_test_fixtures import Database
from test_hosted_privacy import decode

REPORT=ROOT/'Testing'/'next-security'/'privacy-export'
SOURCES=('hosted/privacy.py','hosted/privacy_test_fixtures.py','hosted/app.py','hosted/auth.py','hosted/mfa.py',
         'hosted/schema.sql','hosted/static/privacy.js','hosted/static/privacy.css','hosted/templates/privacy.html',
         'hosted/static/portal.js','tools/user_acceptance_privacy.py')


def fingerprints():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def run():
    REPORT.mkdir(parents=True,exist_ok=True)
    before=fingerprints();results=[];downloads=[];errors=[];external=[]
    db=Database(entries=275)
    db.rows['idempotency_records']=[{'key':'fictional-request','result':{'customer_id':str(db.customer),'access_token':'FICTIONAL-SECRET-NEVER-EXPORT'}}]
    server=make_server('127.0.0.1',0,lambda *_:[],threaded=True)
    origin='http://127.0.0.1:'+str(server.server_port)
    app=create_app({'TESTING':True,'PUBLIC_BASE_URL':origin,'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=db)
    auth=app.extensions['ss_auth'];auth.resolve=Mock(side_effect=db.resolve)
    auth.list_staff=Mock(return_value=[])
    auth.logout=Mock(side_effect=lambda _:setattr(db,'session_active',False))
    def login(*_):
        db.session_active=True;db.actor['session_hash']='fictional-new-owner-session';db.actor['session_created_at']=datetime.now(timezone.utc)
        return 'fictional-cookie-second',{'store':db.actor['slug'],'role':'owner','mfa_enabled':False,'mfa_authenticated':False}
    auth.login=Mock(side_effect=login)
    app.extensions['ss_square'].status=Mock(return_value={'connected':False,'oauth_configured':False,'environment':'sandbox','connection_status':'not_connected'})
    app.extensions['ss_ledger'].customers=Mock(side_effect=lambda *_:[{'id':db.customer,'display_name':'Fictional rights customer'}])
    server.app=app;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    outcome={'started_utc':datetime.now(timezone.utc).isoformat(),'scope':'Actual installed Chrome against a fictional loopback API; no PostgreSQL, Square, actual customer export or external service.',
             'results':results,'passed':False,'sources_before':before}
    try:
        with tempfile.TemporaryDirectory(prefix='ss-privacy-download-') as temporary,sync_playwright() as playwright:
            browser=playwright.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(accept_downloads=True,viewport={'width':1440,'height':1100})
            context.add_cookies([{'name':'ss_session','value':'fictional-cookie-first','url':origin}])
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('download',lambda download:downloads.append(download))
            context.on('request',lambda req:external.append(req.url) if not req.url.startswith(origin+'/') else None)

            def open_form():
                page.goto(origin+'/privacy');expect(page.locator('#privacyWorkspace')).to_be_visible()
                page.locator('#privacySearch').fill('Fictional')
                expect(page.locator('#privacyCustomers button')).to_have_count(1)
                page.locator('#privacyCustomers button').click()
                page.locator('#privacyConfirmId').fill(str(db.customer));page.locator('#privacyPurpose').select_option('customer_access')
                page.locator('#privacyMethod').select_option('existing_verified_channel');page.locator('#privacyCase').fill('FICTIONAL-CASE-001')
                for ident in ('privacyIdentity','privacyAuthority','privacyScope','privacyCopies'):page.locator('#'+ident).check()

            def verified_count():return sum(row['action']=='customer-export-client-verified' for row in db.audits)

            def wait_held(held):
                deadline=time.monotonic()+8
                while not held and time.monotonic()<deadline:page.wait_for_timeout(20)
                assert held,'Expected fictional deferred response did not arrive.'

            open_form()
            with page.expect_download(timeout=20000) as event:page.locator('#privacyDownload').click()
            download=event.value;target=Path(temporary)/'verified.ndjson';download.save_as(target)
            assert download.failure() is None
            manifest,records,footer=decode(target.read_bytes().splitlines(keepends=True))
            assert len(records['credit_entries'])==275 and footer['record_counts']['credit_entries']==275
            assert b'FICTIONAL-SECRET-NEVER-EXPORT' not in target.read_bytes()
            assert verified_count()==1
            expect(page.locator('#privacyProgress')).to_contain_text('Verified hosted copy prepared')
            page.screenshot(path=str(REPORT/'privacy-verified-download.png'),full_page=True)
            outcome['download']={'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                                 'credit_entries':275,'record_counts':footer['record_counts'],'footer_hash_verified':True}
            results.append('PASS actual Chrome NDJSON download contains all275 entries; counts/hash verified; credential-like value excluded; audit confirms verification, not customer delivery.')

            pattern='**/api/privacy/customers/*/export'
            for mode in ('truncated','hash'):
                open_form();prior_downloads=len(downloads);prior_verified=verified_count()
                def corrupt(route,mode=mode):
                    response=route.fetch();lines=response.body().splitlines(keepends=True)
                    if mode=='truncated':lines=lines[:-1]
                    else:
                        for index,line in enumerate(lines):
                            value=json.loads(line)
                            if value['kind']=='page' and value['dataset']=='credit_entries':
                                value['records'][0]['reason']='ALTERED-FICTIONAL-PAYLOAD';lines[index]=(json.dumps(value)+'\n').encode();break
                    route.fulfill(response=response,body=b''.join(lines))
                page.route(pattern,corrupt);page.locator('#privacyDownload').click()
                expect(page.locator('#privacyProgress')).to_contain_text('footer' if mode=='truncated' else 'hash did not verify')
                expect(page.locator('#privacyDownload')).to_be_enabled()
                assert len(downloads)==prior_downloads and verified_count()==prior_verified
                page.screenshot(path=str(REPORT/('privacy-'+mode+'-rejected.png')),full_page=True)
                page.unroute(pattern,corrupt)
                results.append('PASS '+mode+' response rejected before download and client-verification audit; no incomplete copy claimed.')

            app.extensions['ss_privacy'].limits=Limits(browser_max_bytes=4096)
            open_form();prior_downloads=len(downloads);prior_verified=verified_count();page.locator('#privacyDownload').click()
            expect(page.locator('#privacyProgress')).to_contain_text('4 KiB browser limit')
            expect(page.locator('#privacyDownload')).to_be_enabled()
            assert len(downloads)==prior_downloads and verified_count()==prior_verified
            app.extensions['ss_privacy'].limits=Limits()
            results.append('PASS bounded browser buffer rejects oversized copy clearly and creates no download/verification audit.')

            open_form();prior_downloads=len(downloads);prior_verified=verified_count();held=[]
            def hold(route):
                response=route.fetch();held.append((route,response,response.body()))
            page.route(pattern,hold);page.locator('#privacyDownload').click();wait_held(held)
            other=context.new_page();other.goto(origin+'/');expect(other.locator('#workspace')).to_be_visible()
            other.locator('#signOut').click();expect(page.locator('#privacyWorkspace')).to_be_hidden()
            expect(page.locator('#privacyCustomerName')).to_have_text('')
            expect(other.locator('#loginStatus')).to_have_text('Signed out.')
            expect(other.locator('#signin')).not_to_have_attribute('inert','')
            other.locator('#storeSlug').fill(db.actor['slug']);other.locator('#email').fill('owner@example.invalid');other.locator('#password').fill('fictional-login-password')
            other.locator('#loginForm button').click();expect(other.locator('#workspace')).to_be_visible()
            route,response,body=held[0]
            try:route.fulfill(response=response,body=body)
            except BrowserError:pass  # The old request may already have been aborted.
            page.unroute(pattern,hold)
            expect(page.locator('#privacyWorkspace')).to_be_hidden()
            assert len(downloads)==prior_downloads and verified_count()==prior_verified
            assert page.locator('a[download]').count()==0
            page.screenshot(path=str(REPORT/'privacy-cross-tab-session-cleared.png'),full_page=True);other.close()
            results.append('PASS actual portal cross-tab logout/new sign-in aborts and clears selected customer/buffer; delayed old response cannot download.')

            # A delayed refresh401 is not canceled by a newer refresh; it must
            # be ignored by the request generation guard inside jsonRequest.
            page.goto(origin+'/privacy');expect(page.locator('#privacyWorkspace')).to_be_visible();held=[];calls=[0]
            def old_refresh(route):
                calls[0]+=1
                if calls[0]==1:held.append(route)
                else:route.continue_()
            context_pattern='**/api/privacy/context'
            page.route(context_pattern,old_refresh);page.locator('#privacyRefresh').click();wait_held(held)
            page.locator('#privacyRefresh').click();expect(page.locator('#privacyWorkspace')).to_be_visible()
            held[0].fulfill(status=401,content_type='application/json',body='{"error":"fictional old refresh expired"}')
            expect(page.locator('#privacyWorkspace')).to_be_visible();expect(page.locator('#privacySession')).to_contain_text('Signed in as the owner')
            page.unroute(context_pattern,old_refresh)
            results.append('PASS delayed old refresh401 cannot clear a newer successful owner context.')

            # Ignore abort only in this fixture so the sequence guard, rather
            # than browser cancellation, must reject the old search401.
            page.evaluate("""() => {const original=window.fetch;window.__privacyFixtureOriginal=original;window.fetch=(url,options={})=>original(url,String(url).includes('/api/privacy/customers?q=old')?{...options,signal:undefined}:options)}""")
            held=[]
            def old_search(route):
                if 'q=old' in route.request.url:held.append(route)
                else:route.continue_()
            search_pattern='**/api/privacy/customers?*'
            page.route(search_pattern,old_search);page.locator('#privacySearch').fill('old');wait_held(held)
            page.locator('#privacySearch').fill('new');expect(page.locator('#privacyCustomers button')).to_have_count(1)
            held[0].fulfill(status=401,content_type='application/json',body='{"error":"fictional old search expired"}')
            expect(page.locator('#privacyWorkspace')).to_be_visible();expect(page.locator('#privacyCustomers button')).to_have_count(1)
            page.unroute(search_pattern,old_search);page.evaluate('window.fetch=window.__privacyFixtureOriginal;delete window.__privacyFixtureOriginal')
            results.append('PASS deferred old search401 cannot invalidate newer results even when fixture ignores abort.')

            open_form();prior_downloads=len(downloads);prior_verified=verified_count()
            db.on_fetch=lambda *_:setattr(db,'session_active',False)
            page.locator('#privacyDownload').click();expect(page.locator('#privacyWorkspace')).to_be_hidden()
            expect(page.locator('#privacyCustomerName')).to_have_text('')
            assert len(downloads)==prior_downloads and verified_count()==prior_verified
            db.on_fetch=None;db.session_active=True
            results.append('PASS server-side revocation during fetch exposes no late customer page and clears the export view without download.')

            for width,height in ((1024,800),(390,844)):
                page.set_viewport_size({'width':width,'height':height});page.goto(origin+'/privacy')
                expect(page.locator('#privacyWorkspace')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
                page.screenshot(path=str(REPORT/('privacy-layout-'+str(width)+'.png')),full_page=True)
            assert not errors,errors
            assert not external,external
            assert db.active_transactions==0 and db.open_cursors==0
            results.append('PASS desktop/mobile layouts have no horizontal overflow; no JavaScript errors/external requests; all snapshots/cursors released.')
            outcome['passed']=True;browser.close()
    except Exception:
        outcome['failure']=traceback.format_exc()
        raise
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        outcome['finished_utc']=datetime.now(timezone.utc).isoformat();outcome['sources_after']=fingerprints()
        outcome['source_stable']=outcome['sources_before']==outcome['sources_after']
        outcome['passed']=outcome['passed'] and outcome['source_stable']
        outcome['javascript_errors']=errors;outcome['external_requests']=external
        (REPORT/'privacy-user-acceptance.json').write_text(json.dumps(outcome,indent=2)+'\n',encoding='utf-8')
        (REPORT/'privacy-user-acceptance.log').write_text('\n'.join(results)+('\nFAIL '+outcome['failure'] if 'failure' in outcome else '')+'\n',encoding='utf-8')
    print('\n'.join(results))
    if not outcome['passed']:raise RuntimeError('Privacy acceptance failed or source changed during the run.')


if __name__=='__main__':run()
