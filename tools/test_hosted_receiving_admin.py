"""Actual Chrome owner mapping UI against fictional Flask responses only."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import traceback
from unittest.mock import Mock
import uuid

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hosted.app import create_app
from hosted.security import ServiceError
from hosted.receiving_plan import ReceivingError
from intake_manifest import normalize_identity
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def run():
    report=ROOT/'Testing'/'receiving-saga'/'admin-browser';report.mkdir(parents=True,exist_ok=True)
    sources=('hosted/app.py','hosted/square.py','hosted/receiving.py','hosted/receiving_plan.py','hosted/static/receiving_admin.js',
             'hosted/static/receiving_admin.css','hosted/templates/receiving_admin.html','tools/test_hosted_receiving_admin.py')
    hashes=lambda:{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sources}
    results=[];errors=[];requests=[];evidence={'started_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Actual Chrome and Flask cookie/CSRF routes with fictional auth/receiving/OAuth responses and an intercepted fake consent page; no PostgreSQL, real Square requests/actions, email, production accounts or credentials.',
        'source_sha256_start':hashes(),'results':results,'browser_errors':errors,'passed':False}
    server=make_server('127.0.0.1',0,lambda environ,start_response:[],threaded=True)
    base='http://127.0.0.1:'+str(server.server_port)
    app=create_app({'TESTING':True,'PUBLIC_BASE_URL':base,'APP_ENCRYPTION_KEYS':'fictional-receiving-browser-key-not-live'},database=Mock())
    actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-store','mfa_enabled':True,'mfa_authenticated':True}
    sessions={'fixture-owner-token':actor};service=app.extensions['ss_receiving'];auth=app.extensions['ss_auth']
    auth.resolve=Mock(side_effect=lambda token:sessions[token] if token in sessions else (_ for _ in ()).throw(ServiceError('Sign in.',401)))
    service.readiness=Mock(return_value={'protocol_version':1,'write_enabled':False})
    service.locations=Mock(return_value={'locations':[{'id':'fixture-location','name':'Fictional store location','currency':'USD','status':'ACTIVE'}]})
    preview_entered=threading.Event();preview_release=threading.Event();delay={'enabled':False};approval={'denied':False};seen=[]
    def preview(current,data):
        identity=normalize_identity(data['identity']);seen.append(identity)
        if delay['enabled']:
            preview_entered.set()
            if not preview_release.wait(10):raise RuntimeError('Fictional deferred response deadline')
        return {'preview_id':str(uuid.uuid4()),'preview_hash':'a'*64,'expires_in':900,'facts':{'identity':identity,'sku':'000000001234',
            'selling_cents':1250,'variation_id':'fixture-variation','parent_id':'fixture-parent','location_id':'fixture-location'},'approval_required':True}
    service.binding_preview=Mock(side_effect=preview)
    def approve(current,ident,data):
        if approval['denied']:raise ReceivingError('Sign in with a fresh MFA proof before approving.',403,code='MFA_STEP_UP_REQUIRED')
        assert data['identity_confirmed'] is True
        return {'binding_id':str(uuid.uuid4()),'version':1,'approved':True,'provider_write_performed':False}
    service.approve_binding=Mock(side_effect=approve)
    server.app=app;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def record(text):results.append(text);print('PASS '+text,flush=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1050});page=context.new_page();page.set_default_timeout(10_000)
            page.on('pageerror',lambda error:errors.append(str(error)));page.on('request',lambda request:requests.append(request.url))
            try:
                page.goto(base+'/receiving/catalog')
                expect(page.locator('#receivingAdminWorkspace')).to_be_hidden()
                expect(page.locator('#receivingAdminSession')).to_contain_text('Sign in')
                service.binding_preview.assert_not_called();record('Signed-out owner page hides all card forms and makes no mapping request.')
                context.add_cookies([{'name':'ss_session','value':'fixture-owner-token','url':base}]);page.reload()
                expect(page.locator('#receivingAdminWorkspace')).to_be_visible()
                expect(page.locator('#receivingAdminReadiness')).to_contain_text('remain disabled')
                expect(page.locator('[name=location_id]')).to_have_value('')
                record('Owner sees the disabled receiving gate and explicitly selects a location; mapping review does not enable writes.')
                expect(page.locator('#receivingAdminOAuth')).to_contain_text('ITEMS_WRITE');expect(page.locator('#receivingAdminOAuth')).to_contain_text('INVENTORY_WRITE')
                connect=app.extensions['ss_square'];connect.begin=Mock(return_value='https://connect.squareup.com/oauth2/authorize?client_id=fictional-app&scope=ITEMS_WRITE+INVENTORY_WRITE&state=fictional-state')
                page.locator('#receivingAdminOAuthButton').click();connect.begin.assert_not_called()
                record('Additional permission names/purpose are visible and no consent starts without the explicit owner checkbox.')
                def fill_card():
                    page.locator('[name=variation_id]').fill('fixture-variation');page.locator('[name=location_id]').select_option('fixture-location')
                    page.locator('[name=name]').fill('Fictional reviewed card');page.locator('[name=game]').fill('Pokémon')
                fill_card();page.locator('#receivingAdminPreview').click()
                expect(page.locator('#receivingAdminReview')).to_be_visible()
                expect(page.locator('#receivingAdminFacts')).to_contain_text('000000001234')
                expect(page.locator('#receivingAdminFacts')).to_contain_text('12.50')
                page.locator('[name=identity_confirmed]').check();page.locator('[name=reason]').fill('Fictional exact card identity reviewed')
                page.locator('#receivingAdminApprove').click()
                expect(page.locator('#receivingAdminProgress')).to_contain_text('mapping approved')
                expect(page.locator('#receivingAdminReview')).to_be_hidden()
                record('Current target preview shows retained SKU/price and explicit owner attestation saves only the mapping registry.')
                page.locator('[name=kind]').select_option('slab');page.locator('summary').nth(1).click()
                for name,value in {'grading_company':'Fictional grader','grade':'10','grade_qualifier':'Pristine black label',
                                   'certification':'FIXTURE-CERT','serial_number':'07/25','manufacturer':'Fictional manufacturer'}.items():page.locator('[name='+name+']').fill(value)
                page.locator('#receivingAdminPreview').click();expect(page.locator('#receivingAdminReview')).to_be_visible()
                for value in ('Pristine black label','FIXTURE-CERT','07/25','Fictional manufacturer'):expect(page.locator('#receivingAdminFacts')).to_contain_text(value)
                self_check=seen[-1];assert self_check['grade_qualifier']=='Pristine black label' and self_check['serial_number']=='07/25'
                record('Slab tier, certificate, sports serial and manufacturer survive preview without being collapsed or stripped.')
                approval['denied']=True;page.locator('[name=identity_confirmed]').check();page.locator('[name=reason]').fill('Fictional owner review needing fresh MFA')
                page.locator('#receivingAdminApprove').click();expect(page.locator('#receivingAdminProgress')).to_contain_text('fresh MFA')
                expect(page.locator('#receivingAdminReview')).to_be_visible();approval['denied']=False
                record('Stale proof rejection remains actionable and never displays approval success.')
                for width,height in ((1440,1050),(390,844)):
                    page.set_viewport_size({'width':width,'height':height});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(report/f'catalog-mapping-{width}.png'),full_page=True)
                record('The actual preview, grading fields and confirmation fit 1440px and 390px without horizontal overflow.')
                page.set_viewport_size({'width':1440,'height':1050});delay['enabled']=True;preview_entered.clear();preview_release.clear()
                page.locator('[name=name]').fill('Fictional delayed old session card');page.locator('#receivingAdminPreview').click();assert preview_entered.wait(5)
                page.evaluate("localStorage.setItem('ss-auth-epoch',crypto.randomUUID());const c=new BroadcastChannel('ss-auth-transition');c.postMessage({changed:true});c.close()")
                expect(page.locator('#receivingAdminWorkspace')).to_be_hidden();preview_release.set()
                page.wait_for_timeout(150);expect(page.locator('#receivingAdminReview')).to_be_hidden();expect(page.locator('[name=name]')).to_have_value('')
                expect(page.locator('#receivingAdminRefresh')).to_be_enabled();delay['enabled']=False
                record('A cross-tab auth transition aborts/clears a deferred preview; its late response cannot refill a new session or strand Refresh disabled.')
                page.locator('#receivingAdminRefresh').click();expect(page.locator('#receivingAdminWorkspace')).to_be_visible()
                fill_card();page.locator('#receivingAdminPreview').click();expect(page.locator('#receivingAdminReview')).to_be_visible()
                before=service.approve_binding.call_count
                context.add_cookies([{'name':'ss_session','value':'unrelated-or-revoked-token','url':base}])
                page.locator('[name=identity_confirmed]').check();page.locator('[name=reason]').fill('Fictional stale preview must not approve')
                page.locator('#receivingAdminApprove').click();expect(page.locator('#receivingAdminWorkspace')).to_be_hidden()
                assert service.approve_binding.call_count==before
                record('Silent cookie change is caught by a fresh same-session check before approval, even without an auth broadcast.')
                context.add_cookies([{'name':'ss_session','value':'fixture-owner-token','url':base}]);service.locations.side_effect=ReceivingError('Fictional Square token proof unavailable.')
                page.reload();expect(page.locator('#receivingAdminOAuth')).to_be_visible();expect(page.locator('#receivingAdminWorkspace')).to_be_hidden()
                expect(page.locator('#receivingAdminOAuthStatus')).to_contain_text('proof unavailable')
                record('An owner can reach explicit re-consent when location permission proof fails; receiving/mapping remains unavailable.')
                intercepted=[]
                def consent_stub(route):
                    intercepted.append(route.request.url);route.fulfill(status=200,content_type='text/html',body='<html><body>Fictional consent stub. No real Square action.</body></html>')
                page.route('https://connect.squareup.com/oauth2/authorize**',consent_stub)
                page.locator('[name=permissions_confirmed]').check();page.locator('#receivingAdminOAuthButton').click()
                expect(page.locator('body')).to_contain_text('Fictional consent stub')
                connect.begin.assert_called_once_with(actor,profile='receiving');assert len(intercepted)==1
                record('Explicit owner consent uses the receiving profile and fixed Square authorization destination, intercepted locally without any provider action.')
                assert not errors,errors;assert all(url.startswith(base+'/') or url in intercepted for url in requests),requests
                assert not any('fixture-owner-token' in url for url in requests)
                record('No JavaScript errors, live third-party requests, browser-stored credentials or staff tokens in HTTP URLs.')
                evidence['passed']=True
            except Exception:
                page.screenshot(path=str(report/'failure.png'),full_page=True);raise
            finally:browser.close()
    except Exception:evidence['failure']=traceback.format_exc();print(evidence['failure'],flush=True)
    finally:
        preview_release.set();server.shutdown();server.server_close();thread.join(timeout=5)
        evidence['source_sha256_end']=hashes();evidence['source_stable']=evidence['source_sha256_start']==evidence['source_sha256_end']
        evidence['passed']=evidence['passed'] and evidence['source_stable'] and not errors
        evidence['finished_utc']=datetime.now(timezone.utc).isoformat();text='\n'.join(results)+'\n'+evidence.get('failure','PASS\n')
        (report/'acceptance.log').write_text(text,encoding='utf-8');evidence['log_sha256']=hashlib.sha256((report/'acceptance.log').read_bytes()).hexdigest()
        (report/'acceptance.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    return 0 if evidence['passed'] else 1


if __name__=='__main__':sys.exit(run())
