"""Actual Chrome -> app/auth/CSRF -> disposable PostgreSQL template workflow.

Only Square is replaced by the explicit fictional provider. This is not owner
login/enrollment proof: an unusable test MFA seed/session is seeded in ss_test_*.
Run through the private fixture wrapper; never supply a merchant connection.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import threading
import traceback
import uuid

from cryptography.fernet import Fernet
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hosted.app import create_app
from hosted.security import digest
from test_hosted_receiving_templates_postgres import PostgreSQLReceivingTemplateTests
from tools.user_acceptance_receiving_templates import QuietHandler


def fingerprints():
    names = ['hosted/schema.sql', 'intake_manifest.py', 'test_receiving_fixtures.py',
             'test_intake_fixtures.py', 'test_hosted_receiving_templates_postgres.py',
             'test_hosted_receiving_postgres.py', 'test_hosted_settlement_postgres.py',
             'tools/user_acceptance_receiving_templates.py',
             'tools/user_acceptance_receiving_templates_postgres.py']
    names.extend(str(path.relative_to(ROOT)).replace('\\','/') for path in (ROOT/'hosted').rglob('*')
                 if path.is_file() and path.suffix in ('.py','.js','.css','.html','.png'))
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in sorted(set(names))}


def run():
    destination = ROOT/'Testing/receiving-saga'/('templates-pg-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    destination.mkdir(parents=True, exist_ok=False)
    before = fingerprints()
    results, errors, external, assets = [], [], [], []
    outcome = {'started_utc':datetime.now(timezone.utc).isoformat(),'passed':False,
               'scope':'Installed Chrome, real Flask routes, actual Auth.resolve/CSRF and PostgreSQL in one disposable ss_test namespace; seeded unusable fictional MFA/session and injected fictional Square provider. No login/enrollment or live Square proof.',
               'sources_before':before,'results':results}
    fixture_class = PostgreSQLReceivingTemplateTests
    case = fixture_class('test_owner_fresh_approval_is_idempotent_and_preserves_disabled_write_flags_and_catalog')
    prepared = False
    server = worker = None
    try:
        fixture_class.setUpClass(); prepared = True; case.setUp()
        assert fixture_class.schema.startswith('ss_test_') and len(fixture_class.schema)==40
        token = 'fictional-browser-session-'+uuid.uuid4().hex
        with case.database.transaction() as db:
            db.execute("""INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                          VALUES (%s,%s,%s,clock_timestamp()+interval '1 hour',1,clock_timestamp())""",
                       (digest(token),case.tenant,case.actor['staff_id']))
            db.execute('UPDATE ss_receiving_settings SET enabled=FALSE WHERE tenant_id=%s', (case.tenant,))
        case.actor['session_hash'] = digest(token)
        original = deepcopy(case.provider.objects)
        server = make_server('127.0.0.1',0,lambda *_:[],threaded=True,request_handler=QuietHandler)
        origin = 'http://127.0.0.1:'+str(server.server_port)
        app = create_app({'TESTING':True,'PUBLIC_BASE_URL':origin,'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode(),
                          'BOOTSTRAP_TOKEN':'','SQUARE_APPLICATION_ID':'','SQUARE_APPLICATION_SECRET':'',
                          'SQUARE_WEBHOOK_SIGNATURE_KEY':'','SQUARE_ENVIRONMENT':'sandbox',
                          'RECEIVING_WRITES_ENABLED':False},database=case.database)
        service = app.extensions['ss_receiving']
        service.provider = case.provider
        service.templates.provider = case.provider
        server.app = app
        worker = threading.Thread(target=server.serve_forever,daemon=True); worker.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome',headless=True)
            context = browser.new_context(viewport={'width':1280,'height':1000})
            context.add_cookies([{'name':'ss_session','value':token,'url':origin,'httpOnly':True,'sameSite':'Lax'}])
            def restrict(route):
                if route.request.url.startswith(origin+'/'):route.continue_()
                else:external.append(route.request.url);route.abort()
            context.route('**/*',restrict)
            page = context.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('response',lambda response:assets.append({'url':response.url,'status':response.status})
                    if response.url.startswith(origin+'/static/') and response.status>=400 else None)
            page.goto(origin+'/receiving/price-templates')
            expect(page.locator('#templateBindings button')).to_have_count(1)
            expect(page.locator('#templateSearchButton')).to_be_enabled()
            assert app.extensions['ss_auth'].resolve(token)['staff_id']==case.actor['staff_id']
            bad = context.request.post(origin+'/api/receiving/price-templates/previews',
                    data={'binding_id':case.binding['binding_id'],'variation_name_prefix':'Retail'},
                    headers={'X-SS-CSRF':'fictional-invalid-csrf'})
            assert bad.status==403 and case.count()==0
            results.append('PASS real cookie session resolves from PostgreSQL; missing/incorrect CSRF rejects owner mutation before any template is stored.')

            page.locator('#templateBindings button').click(); page.locator('#templatePrefix').fill('Retail')
            page.locator('#templatePreviewButton').click()
            expect(page.locator('#templateReview')).to_be_visible()
            expect(page.locator('#templateTaxFacts')).to_contain_text('subtotal calculation phase')
            for ident in ('templateIdentityConfirmed','templateTaxConfirmed','templateSiblingsConfirmed'):
                page.locator('#'+ident).check()
            page.locator('#templateApprovalReason').fill('Fictional browser owner reviewed current source settings')
            with case.database.transaction() as db:
                db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s", (digest(token),))
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('fresh second-factor proof')
            assert case.count()==0
            with case.database.transaction() as db:
                db.execute('UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp() WHERE token_hash=%s',(digest(token),))
            results.append('PASS real domain transaction rejects a stale enrolled MFA proof even while the browser session flags remain true; no approval is written.')

            approval_pattern = '**/api/receiving/price-templates/*/approve'
            bodies = []
            def lose_reply(route):
                bodies.append(route.request.post_data_json)
                response=route.fetch(); assert response.status==200
                route.abort()
            page.route(approval_pattern,lose_reply)
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('request is retained')
            assert case.count()==1
            page.unroute(approval_pattern,lose_reply)
            def record_retry(route):
                bodies.append(route.request.post_data_json); route.continue_()
            page.route(approval_pattern,record_retry)
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('Price family approved')
            expect(page.locator('#templateList .template-approval')).to_have_count(1)
            assert len(bodies)==2 and bodies[0]==bodies[1] and case.count()==1
            with case.database.transaction() as db:
                assert db.execute("SELECT count(*) AS n FROM ss_receiving_requests WHERE tenant_id=%s AND action='template_approve'",(case.tenant,)).fetchone()['n']==1
                assert db.execute("SELECT count(*) AS n FROM ss_audit WHERE tenant_id=%s AND action='receiving-price-template-approved'",(case.tenant,)).fetchone()['n']==1
            page.screenshot(path=str(destination/'real-api-approved.png'),full_page=True)
            results.append('PASS actual API commit followed by lost response recovers identical key/body into one template, one immutable request and one approval audit.')

            page.locator('#templateList button').click()
            page.locator('#templateRevokeReason').fill('Fictional owner retires this reviewed price family')
            page.locator('#templateRevokeButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('Approval revoked')
            expect(page.locator('#templateList .pill')).to_have_text('Revoked')
            page.reload(); expect(page.locator('#templateList .pill')).to_have_text('Revoked')
            assert case.count()==1
            assert case.provider.objects==original and not case.provider.calls and not case.provider.catalog_calls
            assert not service.writes_enabled
            results.append('PASS owner revocation survives a fresh browser load; original history remains and disabled receiving/Square catalog/stock are unchanged.')

            with case.database.transaction() as db:
                db.execute("UPDATE ss_staff SET role='cashier' WHERE tenant_id=%s AND id=%s",(case.tenant,case.actor['staff_id']))
            page.locator('#templateRefreshSession').click()
            expect(page.locator('#templateWorkspace')).to_be_hidden()
            expect(page.locator('#templateSession')).to_contain_text('Only the store owner')
            assert page.locator('#templateList .template-approval').count()==0
            context.clear_cookies(); page.reload()
            expect(page.locator('#templateWorkspace')).to_be_hidden()
            assert page.locator('#templateBindings button').count()==0
            assert not errors, errors
            assert not external, external
            assert not assets, assets
            results.append('PASS current PostgreSQL role change and cookie removal clear browser records; no JavaScript, external-network or asset failures.')
            browser.close()
            outcome['passed']=True
    except Exception:
        outcome['failure']=traceback.format_exc()
    finally:
        if server:
            if worker:server.shutdown()
            server.server_close()
        if worker:worker.join(timeout=5)
        case.doCleanups()
        if prepared:fixture_class.tearDownClass()
        outcome['finished_utc']=datetime.now(timezone.utc).isoformat()
        outcome['sources_after']=fingerprints()
        outcome['source_stable']=before==outcome['sources_after']
        outcome['passed']=outcome['passed'] and outcome['source_stable']
        outcome['javascript_errors']=errors;outcome['external_requests']=external;outcome['failed_static_assets']=assets
        (destination/'owner-template-postgres-acceptance.json').write_text(json.dumps(outcome,indent=2)+'\n',encoding='utf-8')
        (destination/'owner-template-postgres-acceptance.log').write_text('\n'.join(results)+('\nFAIL '+outcome['failure'] if 'failure' in outcome else '')+'\n',encoding='utf-8')
    print(json.dumps({'report':str(destination),'passed':outcome['passed'],'cases':len(results),'source_stable':outcome['source_stable']}))
    if not outcome['passed']:raise RuntimeError(outcome.get('failure','Source drift during browser/PostgreSQL acceptance'))


if __name__=='__main__':run()
