"""Actual Chrome desktop verification; isolated fictional service/proofs only."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import secrets
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
from test_station_mfa import MfaFixtureTransport,PASSWORD,RECOVERY_A,RECOVERY_B

REPORT=ROOT/'Testing'/'security-mfa'
SOURCES=('station_cloud.py','station_server.py','cloud.js','cloud.css','test_station_mfa.py','tools/user_acceptance_mfa.py')
def hashes():return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


def run():
    REPORT.mkdir(parents=True,exist_ok=True);before=hashes();started=datetime.now(timezone.utc).isoformat();steps=[];cases=[];errors=[];external=[];backend=[]
    remote=MfaFixtureTransport();extra_recovery=secrets.token_hex(16);cancel_recovery=secrets.token_hex(16);remote.recovery.update((extra_recovery,cancel_recovery))
    def redact(value):
        values={PASSWORD,RECOVERY_A,RECOVERY_B,extra_recovery,cancel_recovery,remote.current_code,'123456','654321','000000',*remote.challenges,*remote.sessions}
        values.update(row['issued_token'] for row in remote.challenges.values() if row.get('issued_token'))
        for secret in values:value=value.replace(secret,'[fixture credential redacted]')
        return value
    def record(value):value=redact(value);steps.append(value);print(value,flush=True)
    def forbidden(session,method,url,**kwargs):backend.append({'method':method,'host':urlsplit(url).hostname});raise AssertionError('External backend traffic forbidden')
    with tempfile.TemporaryDirectory() as folder,patch.object(core,'SETTINGS_FILE',str(Path(folder)/'settings.json')),patch.object(core,'OUTPUT_DIR',str(Path(folder)/'outputs')), \
         patch.object(core.requests.sessions.Session,'request',forbidden),patch.object(core,'upload_to_square',side_effect=AssertionError('Square forbidden')) as square:
        server=core.HTTPServer(('127.0.0.1',0),core.RocaStationHandler);service=server.station=StationService(core)
        service.store.put('preferences',service.preferences()|{'pricingEnabled':False});service.cloud=CloudClient(service.store.root,transport=remote,clock=lambda:remote.now);service.shared_trades.cloud=service.cloud
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel='chrome',headless=True);context=browser.new_context(viewport={'width':1440,'height':1050});page=context.new_page();page.set_default_timeout(10000)
                def route(request):
                    uri=urlsplit(request.request.url)
                    if uri.scheme=='http' and uri.hostname=='127.0.0.1' and uri.port==server.server_port:request.continue_()
                    else:external.append({'scheme':uri.scheme,'host':uri.hostname});request.abort()
                context.route('**/*',route);page.on('pageerror',lambda error:errors.append(redact(str(error))))
                def reset():
                    service.cloud.logout();page.goto(base,wait_until='domcontentloaded');expect(page.locator('#version')).to_have_text('2.6.0');page.locator('#cloudNav').click();expect(page.locator('#cloudLoginForm')).to_be_visible()
                def login(email='mfa-manager@example.invalid'):
                    page.locator('#cloudStore').fill('fictional-store');page.locator('#cloudEmail').fill(email);page.locator('#cloudPassword').fill(PASSWORD)
                    page.locator('#cloudLoginForm button[type=submit]').click();expect(page.locator('#cloudPassword')).to_have_value('')
                    expect(page.locator('#cloudMfa' if email.startswith('mfa-') else '#cloudWorkspace')).to_be_visible()
                def proof(value,method='totp'):
                    page.locator('#cloudMfaMethod').select_option(method);page.locator('#cloudMfaCode').fill(value)
                    with page.expect_response(lambda response:response.url==base+'/api/cloud/mfa-verify') as info:page.locator('#cloudMfaForm button[type=submit]').click()
                    expect(page.locator('#cloudMfaCode')).to_have_value('');return info.value
                def signout():
                    with page.expect_response(lambda response:response.url==base+'/api/cloud/logout') as info:page.locator('#cloudSignOut').click()
                    assert info.value.status==200;expect(page.locator('#cloudLoginForm')).to_be_visible()
                def private():
                    assert page.evaluate('localStorage.length===0&&sessionStorage.length===0')
                    public=page.evaluate('JSON.stringify(cloudSession)')
                    values=[PASSWORD,RECOVERY_A,RECOVERY_B,extra_recovery,cancel_recovery,*remote.challenges,*remote.sessions]
                    assert not any(value in public for value in values)
                    for path in Path(folder).rglob('*'):
                        if path.is_file():
                            body=path.read_bytes()
                            assert not any(value.encode() in body for value in values),'Fixture credential unexpectedly persisted.'
                def case(name,callback):
                    begin=time.monotonic()
                    try:reset();callback();private();cases.append({'name':name,'status':'pass','elapsed_seconds':round(time.monotonic()-begin,3)});record('PASS '+name)
                    except Exception:
                        failure=redact(traceback.format_exc());cases.append({'name':name,'status':'fail','error':failure});record('FAIL '+name+'\n'+failure)
                def unenrolled():
                    login('manager@example.invalid');assert service.cloud._saved()['mfa_authenticated'] is False
                    signout();assert service.cloud._saved() is None
                def authenticator():
                    login();assert service.cloud._saved() is None and not service.cloud.vault.path.exists();assert page.evaluate('cloudSession.signedIn') is False
                    assert proof('000000').status==401;expect(page.locator('#cloudMfaMessage')).to_contain_text('could not be verified');assert service.cloud._saved() is None
                    page.screenshot(path=str(REPORT/'mfa-verification-required.png'),full_page=True)
                    assert proof(remote.current_code).status==200;expect(page.locator('#cloudWorkspace')).to_be_visible();assert service.cloud._saved()['mfa_authenticated'] is True
                    assert service.cloud.vault.path.read_bytes().startswith(b'DP1');expect(page.locator('#cloudMfa')).to_be_hidden()
                def recovery_one_use():
                    login();formatted='-'.join(RECOVERY_A[index:index+4] for index in range(0,32,4)).upper()
                    assert proof(' '+formatted+' ','recovery_code').status==200;expect(page.locator('#cloudWorkspace')).to_be_visible();signout()
                    login();assert proof(RECOVERY_A,'recovery_code').status==401;expect(page.locator('#cloudMfa')).to_be_visible();assert service.cloud._saved() is None
                    page.screenshot(path=str(REPORT/'mfa-recovery-code.png'),full_page=True)
                    assert proof(RECOVERY_B,'recovery_code').status==200;expect(page.locator('#cloudWorkspace')).to_be_visible()
                    assert RECOVERY_A not in remote.recovery and RECOVERY_B not in remote.recovery
                def expired_cancel_signout():
                    login();remote.now+=301;assert proof('654321').status==401;expect(page.locator('#cloudLoginForm')).to_be_visible();expect(page.locator('#cloudLoginMessage')).to_contain_text('expired');assert service.cloud._mfa is None
                    login();page.locator('#cloudMfaCode').fill('654321');page.locator('#cloudMfaCancel').click();expect(page.locator('#cloudLoginMessage')).to_contain_text('cancelled');assert service.cloud._mfa is None;expect(page.locator('#cloudMfaCode')).to_have_value('')
                    login();signout();assert service.cloud._mfa is None and service.cloud._saved() is None
                def lost_response():
                    login();remote.drop_after_verify=True;assert proof(extra_recovery,'recovery_code').status==503
                    expect(page.locator('#cloudMfaMessage')).to_contain_text('may already have been used');assert service.cloud._saved() is None and len(remote.sessions)==1
                    page.screenshot(path=str(REPORT/'mfa-uncertain-verification.png'),full_page=True)
                    page.locator('#cloudMfaCancel').click();expect(page.locator('#cloudLoginMessage')).to_contain_text('cancelled');assert not remote.sessions and service.cloud._saved() is None
                    login();assert proof(extra_recovery,'recovery_code').status==401;assert service.cloud._saved() is None
                def cancellation_race():
                    login();remote.current_code='654321';remote.verify_entered=threading.Event();remote.verify_release=threading.Event()
                    try:
                        page.locator('#cloudMfaMethod').select_option('totp');page.locator('#cloudMfaCode').fill(remote.current_code);page.locator('#cloudMfaForm button[type=submit]').click()
                        deadline=time.monotonic()+5
                        while not remote.verify_entered.is_set() and time.monotonic()<deadline:page.wait_for_timeout(20)
                        assert remote.verify_entered.is_set();expect(page.locator('#cloudMfaCancel')).to_be_enabled()
                        page.locator('#cloudMfaCancel').click();remote.verify_release.set();expect(page.locator('#cloudLoginMessage')).to_contain_text('cancelled')
                        expect(page.locator('#cloudWorkspace')).to_be_hidden();assert service.cloud._saved() is None and not remote.sessions
                    finally:remote.verify_release.set();remote.verify_entered=None;remote.verify_release=None
                def unreadable_success_and_late_cancel():
                    login();remote.current_code='112233';actual=remote.request
                    def unreadable(method,url,**options):
                        result=actual(method,url,**options)
                        if url.endswith('/api/auth/mfa/challenge') and result.status_code==200:result._content=b'not-json'
                        return result
                    with patch.object(remote,'request',side_effect=unreadable):assert proof(remote.current_code).status==502
                    expect(page.locator('#cloudMfaMessage')).to_contain_text('could not be confirmed');assert service.cloud._saved() is None and len(remote.sessions)==1
                    remote.now+=301;page.locator('#cloudRefresh').click();expect(page.locator('#cloudMfaStore')).to_contain_text('Verification expired')
                    expect(page.locator('#cloudMfaCode')).to_be_disabled();expect(page.locator('#cloudMfaForm button[type=submit]')).to_be_disabled();expect(page.locator('#cloudMfaCancel')).to_be_enabled()
                    page.screenshot(path=str(REPORT/'mfa-expired-uncertain-verification.png'),full_page=True)
                    page.locator('#cloudMfaCancel').click();expect(page.locator('#cloudLoginMessage')).to_contain_text('cancelled');assert service.cloud._saved() is None and not remote.sessions
                    expect(page.locator('#cloudLoginForm button[type=submit]')).to_be_enabled()
                def failed_cancellation_retry_and_local_escape():
                    login();remote.drop_after_verify=True;assert proof(cancel_recovery,'recovery_code').status==503
                    remote.cancel_status=503;page.locator('#cloudMfaCancel').click();expect(page.locator('#cloudMfaCancel')).to_have_text('Retry server cancellation')
                    expect(page.locator('#cloudLoginForm')).to_be_hidden();expect(page.locator('#cloudMfaForm')).to_be_hidden();expect(page.locator('#cloudMfaClearLocal')).to_be_visible()
                    assert service.cloud.status()['cancelRequired'] and service.cloud._saved() is None and len(remote.sessions)==1
                    remote.cancel_status=None;page.locator('#cloudMfaCancel').click();expect(page.locator('#cloudLoginMessage')).to_contain_text('cancelled');assert not remote.sessions
                    login();remote.current_code='776655';remote.drop_after_verify=True;assert proof(remote.current_code).status==503
                    remote.cancel_status=401;page.locator('#cloudMfaCancel').click();expect(page.locator('#cloudMfaClearLocal')).to_be_visible()
                    expect(page.locator('#cloudMfaStore')).to_contain_text('eight hours');page.screenshot(path=str(REPORT/'mfa-server-cancel-unconfirmed.png'),full_page=True)
                    page.locator('#cloudMfaClearLocal').click();expect(page.locator('#cloudLoginForm')).to_be_visible();expect(page.locator('#cloudLoginMessage')).to_contain_text('revocation was not confirmed')
                    assert service.cloud._mfa is None and service.cloud._saved() is None and len(remote.sessions)==1
                    remote.cancel_status=None;login();expect(page.locator('#cloudMfaForm')).to_be_visible()
                case('Unenrolled pilot sign-in remains compatible and sign-out clears the session',unenrolled)
                case('Password alone grants no session; a wrong proof stays signed out and valid authenticator proof saves only an encrypted session',authenticator)
                case('Recovery codes are consumed once by the service and cannot be replayed',recovery_one_use)
                case('Expiry, cancellation and sign-out clear pending verification and entered codes',expired_cancel_signout)
                case('Lost verification response saves no local session and same-challenge cancellation revokes the issued server session',lost_response)
                case('Unreadable successful verification retains cancellation after expiry and revokes the exact issued session',unreadable_success_and_late_cancel)
                case('Cancellation racing verification prevents stale success from restoring the workspace or vault session',cancellation_race)
                case('Failed server cancellation keeps a retry handle; address denial has an explicit honest local-clear escape without permanent lockout',failed_cancellation_retry_and_local_escape)
                assert not errors,errors;assert not external,external;assert not backend,backend;assert square.call_count==0
                record('PASS browser storage and fixture profile contain no password, proof, recovery code or server challenge; zero external traffic or Square writes');browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(5);after=hashes();stable=before==after
            payload={'started_utc':started,'finished_utc':datetime.now(timezone.utc).isoformat(),'scope':'Actual Chrome, temporary fictional profile, deterministic MFA transport; no live enrollment or deployed-account acceptance.',
                'source_stable':stable,'sources_before':before,'sources_after':after,'cases':cases,'browser_errors':errors,'external_browser_attempts':external,'backend_http_attempts':backend,'square_upload_attempts':square.call_count}
            (REPORT/'desktop-mfa-user-acceptance.json').write_text(json.dumps(payload,indent=2),encoding='utf-8');(REPORT/'desktop-mfa-user-acceptance.log').write_text('\n'.join(steps)+'\n',encoding='utf-8')
    if not stable or any(item['status']!='pass' for item in cases):raise SystemExit(1)


if __name__=='__main__':run()
