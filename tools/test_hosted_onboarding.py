"""Actual Chrome onboarding/staff UI acceptance against fictional local services."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import traceback
from unittest.mock import Mock
import uuid
from urllib.parse import urlsplit,parse_qs

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from hosted.app import create_app
from hosted.security import ServiceError
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server


def run():
    report=ROOT/'Testing'/'2.6.0';report.mkdir(parents=True,exist_ok=True)
    results=[];errors=[];requests=[];outcome={'started_utc':datetime.now(timezone.utc).isoformat(),
        'scope':'Actual Chrome, Flask cookie/CSRF routes and fictional auth/staff responses; no PostgreSQL, emails or Square calls.',
        'source_sha256':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                        ('hosted/static/portal.js','hosted/static/portal.css','hosted/templates/portal.html','hosted/auth.py','hosted/app.py')},
        'results':results,'browser_errors':errors,'passed':False}
    server=make_server('127.0.0.1',0,lambda environ,start_response:[],threaded=True)
    base=f'http://127.0.0.1:{server.server_port}'
    app=create_app({'TESTING':True,'PUBLIC_BASE_URL':base,'APP_ENCRYPTION_KEYS':'fictional-key-not-for-deployment'},database=Mock())
    auth=app.extensions['ss_auth'];sessions={};tenant=uuid.uuid4();owner_id=uuid.uuid4();staff_id=uuid.uuid4()
    staff=[{'id':str(owner_id),'email':'owner@example.invalid','role':'owner','enabled':True},
           {'id':str(staff_id),'email':'cashier@example.invalid','role':'cashier','enabled':True}]
    def resolve(token):
        if token not in sessions:raise ServiceError('Sign in to your store.',401)
        return sessions[token]
    def login(data,address):
        role='owner' if data['email']=='owner@example.invalid' else 'cashier'
        token=role+'-fixture-token';sessions[token]={'tenant_id':tenant,'staff_id':owner_id if role=='owner' else staff_id,'role':role,'slug':'fictional-store'}
        return token,{'store':'fictional-store','role':role}
    def disable(actor,identifier):
        assert actor['role']=='owner';assert str(identifier)==str(staff_id)
        staff[1]['enabled']=False
    auth.resolve=Mock(side_effect=resolve);auth.login=Mock(side_effect=login)
    auth.logout=Mock(side_effect=lambda token:sessions.pop(token,None))
    auth.list_staff=Mock(side_effect=lambda actor:staff)
    auth.disable_staff=Mock(side_effect=disable)
    auth.bootstrap=Mock(side_effect=ServiceError('Pilot onboarding is not enabled.',503))
    auth.invite=Mock(return_value={'invite_token':'n'*43,'expires_in':86400,'delivery':'Share privately. No email delivery.'})
    auth.accept_invite=Mock(return_value={'staff_created':True})
    app.extensions['ss_square'].status=Mock(return_value={'connected':False,'oauth_configured':False,'environment':'sandbox'})
    server.app=app;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def record(value):results.append(value);print(value,flush=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page();page.set_default_timeout(10_000)
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:requests.append(request.url))
            page.on('dialog',lambda dialog:dialog.accept())
            try:
                page.goto(base);page.locator('#showPilotSetup').click()
                for identifier,value in {'setupAuthorization':'fixture-setup-authorization','setupStoreName':'Fictional Store','setupStoreSlug':'fictional-store','setupEmail':'owner@example.invalid','setupPassword':'fixture-owner-password','setupPasswordConfirm':'fixture-owner-password'}.items():page.locator('#'+identifier).fill(value)
                page.locator('#pilotSetupForm button[type="submit"]').click()
                expect(page.locator('#setupStatus')).to_contain_text('not enabled')
                expect(page.locator('#setupAuthorization')).to_have_value('')
                expect(page.locator('#setupPassword')).to_have_value('')
                record('Disabled pilot setup shows the actual server gate and clears the setup secret/password.')
                auth.bootstrap.side_effect=None;auth.bootstrap.return_value={'store_slug':'fictional-store','owner_created':True}
                for identifier,value in {'setupAuthorization':'fixture-setup-authorization','setupPassword':'fixture-owner-password','setupPasswordConfirm':'fixture-owner-password'}.items():page.locator('#'+identifier).fill(value)
                page.locator('#pilotSetupForm button[type="submit"]').click()
                expect(page.locator('#loginStatus')).to_contain_text('Pilot owner created')
                expect(page.locator('#storeSlug')).to_have_value('fictional-store')
                expect(page.locator('#email')).to_have_value('owner@example.invalid')
                page.locator('#password').fill('fixture-owner-password');page.locator('#loginForm button').click()
                expect(page.locator('#staffPanel')).to_be_visible();expect(page.locator('#staffList .staff-row')).to_have_count(2)
                expect(page.locator('#connectSquare')).to_be_disabled()
                record('Owner setup returns to sign-in; the owner sees only this store’s staff and the Square setup gate.')
                page.locator('#staffInviteEmail').fill('cashier@example.invalid');page.locator('#staffInviteRole').select_option('cashier')
                page.locator('#inviteStaffForm button').click();expect(page.locator('#staffInvitationResult')).to_be_visible()
                invitation=page.locator('#staffInvitationLink').input_value();uri=urlsplit(invitation)
                assert not uri.query and parse_qs(uri.fragment)['invite']==['n'*43]
                expect(page.locator('#staffStatus')).to_contain_text('no message was sent')
                record('An owner can generate a one-time private invitation link; the code stays in its fragment and no email is sent.')
                page.screenshot(path=str(report/'hosted-owner-staff.png'),full_page=True)
                page.get_by_role('button',name='Disable access').click()
                expect(page.locator('#staffList')).to_contain_text('Disabled');auth.disable_staff.assert_called_once()
                record('Disable access uses the authenticated CSRF route and refreshes the disabled staff state.')
                page.locator('#signOut').click();expect(page.locator('#signin')).to_be_visible()
                expect(page.locator('#staffInvitationLink')).to_have_value('');expect(page.locator('#staffList .staff-row')).to_have_count(0)
                page.reload();expect(page.locator('#signin')).to_be_visible()
                record('Sign-out clears the private invitation and staff details and keeps sign-in visible after reload.')
                page.goto(invitation);expect(page.locator('#invitation')).to_be_visible()
                assert page.url==base+'/'
                expect(page.locator('#inviteCode')).to_have_value('n'*43)
                expect(page.locator('#inviteStore')).to_have_value('fictional-store')
                page.locator('#inviteEmail').fill('cashier@example.invalid');page.locator('#invitePassword').fill('fixture-staff-password');page.locator('#invitePasswordConfirm').fill('different-fixture-password')
                page.locator('#acceptInviteForm button').click();expect(page.locator('#inviteStatus')).to_contain_text('do not match');auth.accept_invite.assert_not_called()
                for identifier in ('invitePassword','invitePasswordConfirm'):page.locator('#'+identifier).fill('fixture-staff-password')
                page.locator('#acceptInviteForm button').click();expect(page.locator('#loginStatus')).to_contain_text('staff account is ready')
                page.locator('#password').fill('fixture-staff-password');page.locator('#loginForm button').click()
                expect(page.locator('#workspace')).to_be_visible();expect(page.locator('#staffPanel')).to_be_hidden();expect(page.locator('#connectSquare')).to_be_disabled()
                record('Invitation acceptance removes the code from the URL, rejects mismatched passwords before submission, and gives a cashier no staff-management controls.')
                page.locator('#signOut').click();expect(page.locator('#signin')).to_be_visible()
                for width,height in ((1024,768),(390,844)):
                    page.set_viewport_size({'width':width,'height':height});page.locator('#showInvitation').click()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(report/f'hosted-invitation-{width}.png'),full_page=True)
                    page.locator('#invitation [data-return-signin]').click()
                record('Invitation forms fit 1024 px and 390 px viewports without horizontal overflow.')
                assert errors==[],errors
                assert all(url.startswith(base+'/') for url in requests),requests
                assert not any('invite=' in url or 'fixture-setup-authorization' in url for url in requests)
                record('No JavaScript errors, third-party requests or invitation/setup secrets in HTTP URLs.')
                outcome['passed']=True
            except Exception:
                page.screenshot(path=str(report/'hosted-onboarding-failure.png'),full_page=True);raise
            finally:browser.close()
    except Exception:outcome['failure']=traceback.format_exc();print(outcome['failure'])
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        outcome['finished_utc']=datetime.now(timezone.utc).isoformat()
        (report/'hosted-onboarding-acceptance.json').write_text(json.dumps(outcome,indent=2),encoding='utf-8')
        (report/'hosted-onboarding-acceptance.log').write_text('\n'.join(results)+'\n'+outcome.get('failure','PASS\n'),encoding='utf-8')
    return 0 if outcome['passed'] else 1


if __name__=='__main__':sys.exit(run())
