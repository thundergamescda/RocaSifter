"""Actual Chrome MFA UX with fictional loopback auth responses; no real enrollment."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import traceback
from unittest.mock import Mock
import uuid
from cryptography.fernet import Fernet
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from hosted.app import create_app
from hosted.security import ServiceError
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server


def run():
    report=ROOT/'Testing'/'next-security';report.mkdir(parents=True,exist_ok=True)
    names=('hosted/static/portal.js','hosted/static/mfa.js','hosted/static/portal.css','hosted/templates/portal.html','hosted/app.py','hosted/mfa.py','tools/test_hosted_mfa.py')
    fingerprints=lambda:{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in names}
    initial=fingerprints();results=[];errors=[];requests=[]
    outcome={'started_utc':datetime.now(timezone.utc).isoformat(),'scope':'Actual Chrome and loopback Flask cookie/CSRF routes with fictional MFA responses; not PostgreSQL or real authenticator/account enrollment.',
             'source_sha256_at_start':initial,'results':results,'browser_errors':errors,'passed':False,'live_square_contacted':False}
    server=make_server('127.0.0.1',0,lambda environ,start_response:[],threaded=True)
    base=f'http://127.0.0.1:{server.server_port}'
    app=create_app({'TESTING':True,'PUBLIC_BASE_URL':base,'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=Mock())
    auth=app.extensions['ss_auth'];sessions={};state={'enabled':False,'recovery_used':False}
    actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-mfa-store'}
    fixture_codes=['-'.join(f'{index:032x}'[offset:offset+4] for offset in range(0,32,4)) for index in range(1,11)]
    def session():
        token='fixture-'+uuid.uuid4().hex;sessions[token]=actor|{'mfa_enabled':state['enabled'],'mfa_authenticated':state['enabled']}
        return token,{'store':actor['slug'],'role':'owner','mfa_authenticated':state['enabled']}
    def resolve(token):
        if token not in sessions:raise ServiceError('Sign in to your store.',401)
        return sessions[token]
    def login(data,address):
        assert data.get('mfa_supported') is True
        if state['enabled']:return None,{'mfa_required':True,'challenge_token':'c'*43,'expires_in':300,'methods':['totp','recovery_code'],'store':actor['slug']}
        return session()
    def confirm(actor,data,address):
        if data.get('code')!='123456':raise ServiceError('The authenticator code is incorrect.',400)
        state['enabled']=True
        for value in sessions.values():value.update(mfa_enabled=True,mfa_authenticated=True)
        return {'enabled':True,'recovery_codes':fixture_codes,'other_sessions_revoked':True}
    def verify(data,address):
        if data.get('code')==fixture_codes[0] and not state['recovery_used']:state['recovery_used']=True
        elif data.get('code')!='123456':raise ServiceError('The sign-in verification is invalid, expired or already used.',401)
        return session()
    def change(actor,data,address,*,disable=False):
        assert data.get('password')=='fictional-owner-password' and data.get('code')=='234567'
        if disable:
            state['enabled']=False;sessions.clear();return {'enabled':False,'reauthentication_required':True}
        return {'enabled':True,'recovery_codes':list(reversed(fixture_codes))}
    auth.resolve=Mock(side_effect=resolve);auth.login=Mock(side_effect=login)
    auth.logout=Mock(side_effect=lambda token:sessions.pop(token,None));auth.list_staff=Mock(return_value=[])
    auth.mfa.begin=Mock(return_value={'enrollment_token':'e'*43,'secret':'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP','provisioning_uri':'otpauth://fixture','expires_in':600})
    auth.mfa.confirm=Mock(side_effect=confirm);auth.mfa.verify_challenge=Mock(side_effect=verify)
    auth.mfa.cancel_challenge=Mock(return_value={'cancelled':True});auth.mfa.cancel_enrollment=Mock(return_value={'cancelled':True})
    auth.mfa.change=Mock(side_effect=change)
    app.extensions['ss_square'].status=Mock(return_value={'connected':False,'oauth_configured':False,'environment':'sandbox'})
    server.app=app;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def record(message):results.append(message);print(message,flush=True)
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True)
            context=browser.new_context(viewport={'width':1440,'height':1100});page=context.new_page();page.set_default_timeout(10_000)
            page.on('pageerror',lambda error:errors.append(str(error)));page.on('request',lambda req:requests.append(req.url));page.on('dialog',lambda dialog:dialog.accept())
            def signin():
                page.wait_for_function('() => !authTransitionBusy')
                for identifier,value in {'storeSlug':'fictional-mfa-store','email':'owner@example.invalid','password':'fictional-owner-password'}.items():page.locator('#'+identifier).fill(value)
                page.locator('#loginForm button').click()
            def enroll():
                page.locator('#mfaEnrollPassword').fill('fictional-owner-password');page.locator('#mfaEnrollForm button').click();expect(page.locator('#mfaEnrollment')).to_be_visible()
            def security_screenshots(view, masks=()):
                for width,height in ((1440,1100),(390,844)):
                    page.set_viewport_size({'width':width,'height':height})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'MFA horizontal overflow'
                    page.screenshot(path=str(report/f'hosted-mfa-{view}-{width}.png'),full_page=True,
                                    mask=[page.locator(selector) for selector in masks],mask_color='#dbe1e7')
                    if view=='verification' and width==390:
                        page.screenshot(path=str(report/'hosted-mfa-390.png'),full_page=True)
                page.set_viewport_size({'width':1440,'height':1100})
            try:
                page.goto(base);signin();expect(page.locator('#accountSecurity')).to_be_visible()
                enroll();expect(page.locator('#mfaSetupKey')).not_to_have_value('')
                security_screenshots('setup',('#mfaSetupKey',))
                page.locator('#cancelMfaEnrollment').click();expect(page.locator('#mfaEnrollment')).to_be_hidden();expect(page.locator('#mfaSetupKey')).to_have_value('')
                assert page.evaluate('mfaEnrollmentToken===null');auth.mfa.cancel_enrollment.assert_called_once()
                record('Authenticated setup requires password; cancel calls the CSRF-protected server route and clears its seed/token from DOM and memory.')
                enroll();page.locator('#mfaConfirmCode').fill('000000');page.locator('#mfaConfirmForm button').click();expect(page.locator('#mfaSecurityStatus')).to_contain_text('incorrect')
                page.locator('#mfaConfirmCode').fill('123456');page.locator('#mfaConfirmForm button').click();expect(page.locator('#mfaRecoveryResult')).to_be_visible();expect(page.locator('#mfaSetupKey')).to_have_value('')
                assert page.evaluate('mfaEnrollmentToken===null')
                security_screenshots('recovery',('#mfaRecoveryCodes',))
                page.locator('#dismissMfaRecovery').click();expect(page.locator('#mfaRecoveryCodes')).to_have_text('')
                record('Incorrect confirmation is actionable; successful confirmation clears the seed/token and shows recovery codes once, then removes them when dismissed.')
                page.locator('#signOut').click();expect(page.locator('#signin')).to_be_visible();signin();expect(page.locator('#mfaSignin')).to_be_visible();expect(page.locator('#workspace')).to_be_hidden()
                assert not [cookie for cookie in context.cookies() if cookie['name']=='ss_session']
                expect(page.locator('#mfaSigninCode')).to_have_value('');security_screenshots('verification')
                page.locator('#mfaSigninCode').fill('000000');page.locator('#mfaSigninForm button').click();expect(page.locator('#mfaSigninStatus')).to_contain_text('invalid')
                expect(page.locator('#mfaSigninCode')).to_have_value('');assert page.evaluate('hasMfaChallenge()')
                page.locator('#mfaSigninCode').fill('123456');page.locator('#mfaSigninForm button').click();expect(page.locator('#workspace')).to_be_visible();assert page.evaluate('!hasMfaChallenge()')
                record('Password-only MFA login sets no full cookie/workspace; wrong code clears the input, and verified sign-in establishes the authenticated workspace and clears the challenge.')
                page.locator('#signOut').click();expect(page.locator('#signin')).to_be_visible();signin();expect(page.locator('#mfaSignin')).to_be_visible()
                page.locator('#mfaSigninCode').fill(fixture_codes[0]);page.locator('#mfaSigninForm button').click();expect(page.locator('#workspace')).to_be_visible()
                page.locator('#signOut').click();expect(page.locator('#signin')).to_be_visible();signin();expect(page.locator('#mfaSignin')).to_be_visible()
                page.locator('#mfaSigninCode').fill(fixture_codes[0]);page.locator('#mfaSigninForm button').click();expect(page.locator('#mfaSigninStatus')).to_contain_text('already used')
                page.locator('#cancelMfaSignin').click();expect(page.locator('#signin')).to_be_visible();assert page.evaluate('!hasMfaChallenge()');expect(page.locator('#mfaSigninCode')).to_have_value('')
                record('Recovery sign-in works through the same challenge UI; a consumed-code rejection retains a cancellable challenge, and cancel clears it without storing the code.')
                signin();expect(page.locator('#mfaSignin')).to_be_visible()
                page.route('**/api/auth/mfa/challenge',lambda route:route.abort('failed'))
                page.locator('#mfaSigninCode').fill('123456');page.locator('#mfaSigninForm button').click();expect(page.locator('#mfaSigninStatus')).to_contain_text('did not respond')
                assert page.evaluate('hasMfaChallenge()');page.unroute('**/api/auth/mfa/challenge');page.locator('#cancelMfaSignin').click();expect(page.locator('#signin')).to_be_visible()
                record('Uncertain network failure leaves a cancellable challenge and clears the entered proof; confirmed cancellation returns to sign-in.')
                signin();expect(page.locator('#mfaSignin')).to_be_visible();page.locator('#mfaSigninCode').fill('123456');page.locator('#mfaSigninForm button').click();expect(page.locator('#workspace')).to_be_visible()
                page.locator('#mfaManagePassword').fill('fictional-owner-password');page.locator('#mfaManageCode').fill('234567');page.locator('#mfaManageForm button[type="submit"]').click();expect(page.locator('#mfaRecoveryResult')).to_be_visible()
                expect(page.locator('#mfaManagePassword')).to_have_value('');expect(page.locator('#mfaManageCode')).to_have_value('')
                page.locator('#dismissMfaRecovery').click();page.locator('#mfaManagePassword').fill('fictional-owner-password');page.locator('#mfaManageCode').fill('234567');page.locator('#disableMfa').click();expect(page.locator('#loginStatus')).to_contain_text('All sessions were revoked')
                expect(page.locator('#accountSecurity')).to_be_hidden();expect(page.locator('#mfaRecoveryCodes')).to_have_text('');assert not sessions
                record('Recovery replacement clears password/proof fields; explicit disable confirms intent, signs out and clears all visible security secrets.')
                storage=page.evaluate('({local:{...localStorage},session:{...sessionStorage}})')
                assert set(storage['local'])<= {'ss-auth-epoch'} and set(storage['session'])<= {'ss-signed-out'}
                assert errors==[],errors
                assert all(url.startswith(base+'/') for url in requests),'Unexpected external request'
                assert not any('otpauth' in url or 'JBSWY' in url or 'challenge_token' in url for url in requests)
                page.set_viewport_size({'width':390,'height':844});page.locator('#showInvitation').click();assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(report/'hosted-mfa-invitation-390.png'),full_page=True)
                record('No JavaScript errors, external requests or secrets in URLs/storage; only the non-PII auth epoch and signed-out marker persist.')
                outcome['passed']=True
            except Exception:page.screenshot(path=str(report/'hosted-mfa-failure.png'),full_page=True);raise
            finally:browser.close()
    except Exception:outcome['failure']=traceback.format_exc();print(outcome['failure'],flush=True)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)
        final=fingerprints();changed=[name for name in names if initial[name]!=final[name]]
        outcome.update(finished_utc=datetime.now(timezone.utc).isoformat(),source_sha256=final,source_snapshot_stable=not changed,changed_during_run=changed,tests_run=len(results))
        if changed:outcome['passed']=False
        (report/'hosted-mfa-browser.json').write_text(json.dumps(outcome,indent=2),encoding='utf-8')
        (report/'hosted-mfa-browser.log').write_text('\n'.join(results)+'\n'+outcome.get('failure','PASS\n'),encoding='utf-8')
    return 0 if outcome['passed'] else 1


if __name__=='__main__':sys.exit(run())
