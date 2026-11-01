"""Installed Chrome checks of the actual owner UI, using fictional loopback APIs.

This deliberately does not test database authority or Square. Those have separate
integration evidence. No cookies, tokens, customer or catalog records are real.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import traceback
import uuid
from urllib.parse import parse_qs,urlsplit

from flask import Flask, jsonify, request, send_from_directory
from playwright.sync_api import sync_playwright, expect, Error as BrowserError
from werkzeug.serving import make_server, WSGIRequestHandler

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('hosted/templates/receiving_templates.html', 'hosted/static/receiving_templates.js',
           'hosted/static/receiving_templates.css', 'hosted/static/privacy.css',
           'hosted/static/brand-icon.png', 'tools/user_acceptance_receiving_templates.py')


def uid(number):
    return str(uuid.UUID(int=number))


def fingerprints():
    return {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in SOURCES}


class QuietHandler(WSGIRequestHandler):
    def log(self, *_args, **_kwargs):
        pass


class Fixture:
    def __init__(self):
        self.identity = {'name': 'Fictional Championship Rookie <catalog text>', 'game': 'sports',
                         'game_label': 'Sports', 'kind': 'slab', 'set': 'Fictional Collection',
                         'number': 'F-017', 'condition': 'Graded', 'language': 'English',
                         'grading_company': 'Fictional Grader', 'grade': '10',
                         'grade_qualifier': 'Pristine', 'certification': 'FICTIONAL-CERT-17',
                         'year': '2026', 'sport': 'Basketball', 'league': 'Fictional League',
                         'team': 'Fictional Team', 'player': 'Fictional Player',
                         'serial_number': '17/99', 'manufacturer': 'Fictional Maker'}
        self.binding = {'binding_id': uid(1), 'version': 1, 'identity': self.identity,
                        'identity_hash': '1'*64, 'source_sku': '123456789012', 'selling_cents': 2500,
                        'currency': 'USD', 'location': {'id': 'FICTIONAL-LOCATION', 'name': 'Fictional shop'},
                        'provider': {'merchant_id': 'FICTIONAL-MERCHANT', 'environment': 'production',
                                     'application_id': 'FICTIONAL-APPLICATION'}}
        self.reset()

    def reset(self):
        self.me = {'store': 'fictional-store', 'role': 'owner', 'csrf_token': 'fictional-csrf',
                   'mfa_enabled': True, 'mfa_authenticated': True}
        self.active = True
        self.fresh = True
        self.bindings = [deepcopy(self.binding)]
        self.templates = []
        self.previews = {}
        self.calls = []
        self.results = {}
        self.effects = {'approve': 0, 'revoke': 0, 'square': 0}
        self.preview_override = None
        self.repeat_cursor = False
        self.duplicate_bindings = False
        self.duplicate_templates = False
        self.template_limits = []

    def facts(self, binding=None, prefix='Retail'):
        binding = binding or self.binding
        return {key: deepcopy(binding[key]) for key in ('binding_id', 'identity', 'identity_hash', 'location', 'provider')} | {
            'binding_version': binding['version'], 'base_sku': binding['source_sku'],
            'parent': {'id': 'FICTIONAL-PARENT', 'name': 'Fictional existing item'},
            'variation_name_prefix': prefix,
            'taxes': [{'id': 'FICTIONAL-TAX', 'name': 'Fictional configured tax', 'percentage': '6',
                       'inclusion_type': 'ADDITIVE', 'enabled': True, 'calculation_phase': 'TAX_SUBTOTAL_PHASE'}],
            'categories': [{'id': 'FICTIONAL-CATEGORY', 'name': 'Fictional graded sports'}],
            'taxability': None, 'policy_reference': 'existing-parent-settings:FICTIONAL-PARENT:17',
            'parent_policy_hash': '2'*64, 'catalog_snapshot_hash': '3'*64,
            'new_parent_creation': False, 'receiving_enabled': False}

    def template(self, number=1001, facts=None):
        return {'template_id': uid(number), 'version': 1, 'state': 'approved',
                'facts': deepcopy(facts or self.facts()), 'approved_at': '2026-11-01T00:00:00Z',
                'revoked_at': None, 'approval_reason': 'Fictional owner reviewed settings'}

    def page(self, rows, key):
        after = request.args.get('cursor')
        limit = int(request.args.get('limit', '50'))
        choices = [row for row in rows if not after or row[key] > after]
        selected = deepcopy(choices[:limit])
        cursor = selected[-1][key] if len(choices) > limit and selected else None
        if self.repeat_cursor and after:
            cursor = after
        if selected and ((key == 'binding_id' and self.duplicate_bindings) or
                         (key == 'template_id' and self.duplicate_templates)):
            selected = [selected[0], deepcopy(selected[0])]
        return selected, cursor

    def app(self):
        app = Flask(__name__, static_folder=None)

        @app.get('/receiving/price-templates')
        def ui():
            return (ROOT/'hosted/templates/receiving_templates.html').read_text(encoding='utf-8')

        @app.get('/static/<path:name>')
        def static(name):
            return send_from_directory(ROOT/'hosted/static', name)

        @app.get('/')
        def index():
            return '<title>Fictional fixture sign-in</title><p>Fictional fixture only</p>'

        @app.get('/api/me')
        def me():
            return jsonify(self.me) if self.active else (jsonify(error='Fictional session expired'), 401)

        @app.before_request
        def authority():
            if not request.path.startswith('/api/receiving/'):
                return
            if not self.active:
                return jsonify(error='Fictional session expired'), 401
            if self.me['role'] != 'owner':
                return jsonify(error='Owner required'), 403
            if request.method == 'POST':
                self.calls.append({'path': request.path, 'body': request.get_json(),
                                   'csrf': request.headers.get('X-SS-CSRF')})
                if request.headers.get('X-SS-CSRF') != self.me['csrf_token']:
                    return jsonify(error='Fictional session changed'), 403

        @app.get('/api/receiving/catalog-bindings')
        def bindings():
            query = request.args.get('q', '').lower()
            values = [row for row in self.bindings if query in (row['identity']['name']+' '+row['source_sku']).lower()]
            selected, cursor = self.page(values, 'binding_id')
            return jsonify(bindings=selected, next_cursor=cursor)

        @app.get('/api/receiving/price-templates')
        def templates():
            self.template_limits.append(int(request.args.get('limit','50')))
            selected, cursor = self.page(self.templates, 'template_id')
            return jsonify(templates=selected, next_cursor=cursor)

        @app.post('/api/receiving/price-templates/previews')
        def preview():
            data = request.get_json()
            selected = next(row for row in self.bindings if row['binding_id'] == data['binding_id'])
            ident = str(uuid.uuid4())
            value = {'preview_id': ident, 'preview_hash': '4'*64, 'expires_in': 900,
                     'approval_required': True, 'facts': self.facts(selected, data['variation_name_prefix'])}
            if self.preview_override:
                self.preview_override(value)
            self.previews[ident] = deepcopy(value)
            return jsonify(value)

        @app.post('/api/receiving/price-templates/<ident>/<action>')
        def change(ident, action):
            data = request.get_json()
            key = (action, data['request_key'])
            if key in self.results:
                assert self.results[key]['body'] == data
                return jsonify(self.results[key]['result'])
            if not self.fresh:
                return jsonify(error='Fresh enrolled MFA verified within 15 minutes is required.'), 403
            if action == 'approve':
                assert all(data[name] is True for name in ('preserve_existing_tax_configuration_confirmed',
                    'preserve_existing_siblings_confirmed', 'source_identity_confirmed'))
                assert len(data['approval_reason']) >= 10
                row = self.template(1001+len(self.templates), self.previews[ident]['facts'])
                self.templates.append(row)
                result = {'template_id': row['template_id'], 'version': 1, 'state': 'approved',
                          'receiving_enabled': False, 'new_parent_creation': False}
            else:
                assert action == 'revoke' and len(data['reason']) >= 10
                row = next(row for row in self.templates if row['template_id'] == ident)
                row['state'] = 'revoked'
                row['revoked_at'] = '2026-11-01T00:01:00Z'
                result = {'template_id': ident, 'version': 1, 'state': 'revoked'}
            self.effects[action] += 1
            self.results[key] = {'body': deepcopy(data), 'result': result}
            return jsonify(result)

        return app


def run():
    report = ROOT/'Testing/receiving-saga'/('templates-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    report.mkdir(parents=True, exist_ok=False)
    before = fingerprints()
    results, errors, external, assets = [], [], [], []
    fixture = Fixture()
    server = make_server('127.0.0.1', 0, fixture.app(), threaded=True, request_handler=QuietHandler)
    origin = 'http://127.0.0.1:'+str(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    outcome = {'started_utc': datetime.now(timezone.utc).isoformat(), 'passed': False,
               'scope': 'Actual installed Chrome, actual owner UI sources, fictional loopback API. No PostgreSQL, provider, merchant data, grant or Square effect.',
               'sources_before': before, 'results': results}
    try:
        with tempfile.TemporaryDirectory(prefix='ss-template-browser-'), sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1100})
            context.add_init_script("""(() => {const original=window.setTimeout;window.setTimeout=(fn,ms,...rest)=>original(fn,window.__fixtureShortDeadline&&ms===65000?100:ms,...rest)})();""")
            def restrict(route):
                if route.request.url.startswith(origin+'/'):
                    route.continue_()
                else:
                    external.append(route.request.url)
                    route.abort()
            context.route('**/*', restrict)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: assets.append({'url': response.url, 'status': response.status})
                    if response.url.startswith(origin+'/static/') and response.status >= 400 else None)

            def open_owner(reset=True):
                if reset:
                    fixture.reset()
                page.goto(origin+'/receiving/price-templates')
                expect(page.locator('#templateWorkspace')).to_be_visible()
                expect(page.locator('#templateBindings button')).to_have_count(min(len(fixture.bindings), 50))
                expect(page.locator('#templateSearchButton')).to_be_enabled()

            def review():
                page.locator('#templateBindings button').first.click()
                page.locator('#templatePrefix').fill('Retail')
                page.locator('#templatePreviewButton').click()
                expect(page.locator('#templateReview')).to_be_visible()
                expect(page.locator('#templateApproveButton')).to_be_enabled()

            def attest():
                for name in ('templateIdentityConfirmed', 'templateTaxConfirmed', 'templateSiblingsConfirmed'):
                    page.locator('#'+name).check()
                page.locator('#templateApprovalReason').fill('Fictional owner reviewed source and settings')

            def wait_held(held):
                deadline = time.monotonic()+8
                while not held and time.monotonic() < deadline:
                    page.wait_for_timeout(20)
                assert held, 'Expected deferred fictional response did not arrive'

            fixture.me['role'] = 'manager'
            page.goto(origin+'/receiving/price-templates')
            expect(page.locator('#templateSession')).to_contain_text('Only the store owner')
            expect(page.locator('#templateWorkspace')).to_be_hidden()
            assert not fixture.calls
            fixture.active = False
            page.reload()
            expect(page.locator('#templateWorkspace')).to_be_hidden()
            results.append('PASS manager and signed-out views expose no owner records or write controls.')

            fixture.reset(); fixture.bindings[0]['source_sku']='  LEGACY>^~ \u03a9  '
            open_owner(False); review()
            expect(page.locator('#templateSelectedFacts dd code')).to_have_text('"  LEGACY>^~ \u03a9  "')
            expect(page.locator('#templateReviewFacts dd code')).to_have_text('"  LEGACY>^~ \u03a9  "')
            assert page.locator('#templateBindings small').evaluate('(node)=>getComputedStyle(node).whiteSpace')=='pre-wrap'
            results.append('PASS quoted retained SKU preserves exact leading/trailing spaces, Unicode and punctuation for owner review without changing the underlying value.')

            open_owner(); review()
            for value in ('Pristine', 'FICTIONAL-CERT-17', '17/99', 'Fictional Team'):
                expect(page.locator('#templateReviewFacts')).to_contain_text(value)
            expect(page.locator('#templateTaxFacts')).to_contain_text('not explicitly set')
            expect(page.locator('#templateTaxFacts')).to_contain_text('6%; added to price; enabled; subtotal calculation phase')
            expect(page.locator('#templateCategoryFacts')).to_contain_text('Fictional graded sports')
            assert page.locator('#templateReviewFacts img').count() == 0
            page.screenshot(path=str(report/'owner-review-desktop.png'), full_page=True)
            page.locator('#templateApproveButton').click()
            assert fixture.effects['approve'] == 0
            attest(); page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('Price family approved')
            expect(page.locator('#templateList .template-approval')).to_have_count(1)
            assert fixture.effects == {'approve': 1, 'revoke': 0, 'square': 0}
            assert all(call['csrf'] == fixture.me['csrf_token'] for call in fixture.calls)
            results.append('PASS full slab/sports identity, nullable item taxability and all tax calculation settings are visible; native attestations required; approval changes no Square state.')

            open_owner(); review(); attest()
            pattern = '**/api/receiving/price-templates/*/approve'
            def lose_reply(route):
                route.fetch()
                route.abort()
            page.route(pattern, lose_reply)
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('request is retained')
            expect(page.locator('#templatePrefix')).to_be_disabled()
            expect(page.locator('#templateApprovalReason')).to_be_disabled()
            assert fixture.effects['approve'] == 1
            page.unroute(pattern, lose_reply)
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('Price family approved')
            calls = [row['body'] for row in fixture.calls if row['path'].endswith('/approve')]
            assert len(calls) == 2 and calls[0] == calls[1] and fixture.effects['approve'] == 1
            results.append('PASS lost approval response retains the exact key/body and locks edits; retry recovers one approval.')

            page.locator('#templateList button').click()
            page.locator('#templateRevokeReason').fill('Fictional owner is retiring this policy')
            revoke_pattern = '**/api/receiving/price-templates/*/revoke'
            page.route(revoke_pattern, lose_reply)
            page.locator('#templateRevokeButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('same revocation request is retained')
            expect(page.locator('#templateCancelRevoke')).to_be_disabled()
            page.unroute(revoke_pattern, lose_reply)
            page.locator('#templateRevokeButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('Approval revoked')
            expect(page.locator('#templateList .pill')).to_have_text('Revoked')
            calls = [row['body'] for row in fixture.calls if row['path'].endswith('/revoke')]
            assert len(calls) == 2 and calls[0] == calls[1] and fixture.effects['revoke'] == 1
            results.append('PASS lost revocation response recovers the same request once and retains revoked history.')

            open_owner(); review(); attest(); fixture.fresh = False
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('Fresh enrolled MFA')
            assert fixture.effects['approve'] == 0
            fixture.reset(); fixture.me['mfa_authenticated'] = False
            open_owner(False)
            page.locator('#templateBindings button').click(); page.locator('#templatePrefix').fill('Retail')
            page.locator('#templatePreviewButton').click()
            expect(page.locator('#templateReview')).to_be_visible()
            expect(page.locator('#templateApproveButton')).to_be_disabled()
            expect(page.locator('#templateProofGuidance')).to_contain_text('sign out and sign in again')
            results.append('PASS server stale-MFA denial creates no approval; owner without MFA sees explicit sign-in guidance and disabled approval.')

            for mode in ('provider', 'identity', 'prefix', 'tax-phase'):
                open_owner()
                def corrupt(value):
                    if mode == 'provider': value['facts']['provider']['merchant_id'] = 'OTHER-FICTIONAL-MERCHANT'
                    elif mode == 'identity': value['facts']['identity']['grade_qualifier'] = 'Changed qualifier'
                    elif mode == 'prefix': value['facts']['variation_name_prefix'] = 'Different prefix'
                    else: value['facts']['taxes'][0]['calculation_phase'] = None
                fixture.preview_override = corrupt
                page.locator('#templateBindings button').click(); page.locator('#templatePrefix').fill('Retail')
                page.locator('#templatePreviewButton').click()
                expect(page.locator('#templateStatus')).to_contain_text('differs from the selected source')
                expect(page.locator('#templateReview')).to_be_hidden()
                assert fixture.effects['approve'] == 0
            results.append('PASS mismatched provider, displayed identity, requested prefix and unresolved tax phase reject the review before approval.')

            open_owner(); review(); page.locator('#templatePrefix').fill('Updated prefix')
            expect(page.locator('#templateReview')).to_be_hidden()
            assert fixture.effects['approve'] == 0
            results.append('PASS changing the requested prefix invalidates the entire prior review and attestations.')

            fixture.reset()
            fixture.bindings = [deepcopy(fixture.binding) | {'binding_id': uid(i)} for i in range(1,128)]
            fixture.templates = [fixture.template(i) for i in range(1001,1128)]
            open_owner(False)
            for count in (100,127):
                page.locator('#templateMoreBindings').click()
                expect(page.locator('#templateBindings button')).to_have_count(count)
                page.locator('#templateMoreList').click()
                expect(page.locator('#templateList .template-approval')).to_have_count(count)
            expect(page.locator('#templateMoreList')).to_be_hidden()
            expect(page.locator('#templateMoreBindings')).to_be_hidden()
            results.append('PASS all127 mappings and127 saved approvals remain reachable through bounded manual pagination.')

            fixture.reset();large=fixture.facts()
            large['taxes']=[deepcopy(large['taxes'][0])|{'id':'FICTIONAL-TAX-'+str(i),'name':'\u754c'*255} for i in range(40)]
            large['categories']=[{'id':'FICTIONAL-CATEGORY-'+str(i),'name':'\u754c'*255} for i in range(40)]
            fixture.templates=[fixture.template(i,large) for i in range(1001,1052)]
            assert len(json.dumps({'templates':fixture.templates[:50],'next_cursor':uid(1050)}).encode())>2*1024*1024
            open_owner(False)
            shown=page.locator('#templateList .template-approval').count()
            assert 0<shown<50 and fixture.template_limits[0]==50 and min(fixture.template_limits)<50
            while shown<len(fixture.templates):
                expected=min(shown+fixture.template_limits[-1],len(fixture.templates))
                page.locator('#templateMoreList').click();expect(page.locator('#templateList .template-approval')).to_have_count(expected)
                shown=expected
            expect(page.locator('#templateListStatus')).to_contain_text('51 approvals loaded')
            assert len(set(page.locator('#templateList .template-approval').evaluate_all('(nodes)=>nodes.map(node=>node.dataset.id)')))==51
            assert fixture.effects=={'approve':0,'revoke':0,'square':0}
            fixture.reset();fixture.templates=[fixture.template(i) for i in range(1001,1052)];requested=[]
            list_pattern='**/api/receiving/price-templates?*'
            def server_page_limit(route):
                limit=int(parse_qs(urlsplit(route.request.url).query)['limit'][0]);requested.append(limit)
                if limit==50:route.fulfill(status=413,content_type='application/json',body='{"error":"Fictional bounded approval page; request fewer records."}')
                else:route.continue_()
            page.route(list_pattern,server_page_limit);open_owner(False)
            expect(page.locator('#templateList .template-approval')).to_have_count(25)
            assert requested[:2]==[50,25]
            page.unroute(list_pattern,server_page_limit)
            results.append('PASS large saved approvals automatically shrink only the read page after browser2MiB or server413 limits; all51 complete records remain reachable without duplicates or mutations.')

            fixture.reset(); fixture.duplicate_bindings = True
            page.goto(origin+'/receiving/price-templates')
            expect(page.locator('#templateBindingStatus')).to_contain_text('repeats a mapping')
            expect(page.locator('#templateBindings button')).to_have_count(0)
            fixture.duplicate_bindings = False
            fixture.templates = [fixture.template()]; fixture.duplicate_templates = True
            page.locator('#templateRefreshList').click()
            expect(page.locator('#templateListStatus')).to_contain_text('repeats a record')
            expect(page.locator('#templateList .template-approval')).to_have_count(0)
            fixture.reset(); fixture.bindings = [deepcopy(fixture.binding) | {'binding_id': uid(i)} for i in range(1,128)]
            open_owner(False); fixture.repeat_cursor = True
            page.locator('#templateMoreBindings').click()
            expect(page.locator('#templateBindingStatus')).to_contain_text('could not be validated')
            expect(page.locator('#templateBindings button')).to_have_count(50)
            results.append('PASS duplicate records and repeated pagination cursors fail clearly without appending inconsistent choices.')

            open_owner()
            def oversize(route):
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'padding':'x'*(2*1024*1024)}))
            preview_pattern = '**/api/receiving/price-templates/previews'
            page.route(preview_pattern, oversize)
            page.locator('#templateBindings button').click(); page.locator('#templatePrefix').fill('Retail')
            page.locator('#templatePreviewButton').click()
            expect(page.locator('#templateStatus')).to_contain_text('exceeds the supported response size')
            expect(page.locator('#templateReview')).to_be_hidden()
            page.unroute(preview_pattern, oversize)
            results.append('PASS streamed responses above2MiB are rejected with no partial approval view.')

            open_owner(); held = []
            page.route(preview_pattern, lambda route: held.append(route))
            page.evaluate('window.__fixtureShortDeadline=true')
            page.locator('#templateBindings button').click(); page.locator('#templatePrefix').fill('Retail')
            page.locator('#templatePreviewButton').click(); wait_held(held)
            expect(page.locator('#templateStatus')).to_contain_text('request timed out')
            expect(page.locator('#templatePreviewButton')).to_be_enabled()
            try: held[0].abort()
            except BrowserError: pass
            page.unroute(preview_pattern)
            results.append('PASS deadline abort restores controls and explains uncertain effects (fixture accelerates only the65-second timer).')

            open_owner(); held = []
            page.evaluate("""() => {const original=window.fetch;window.fetch=(url,options={})=>original(url,String(url).endsWith('/previews')?{...options,signal:undefined}:options)}""")
            def hold_old(route):
                response = route.fetch()
                held.append((route,response,response.body()))
            page.route(preview_pattern, hold_old)
            page.locator('#templateBindings button').click(); page.locator('#templatePrefix').fill('Retail')
            page.locator('#templatePreviewButton').click(); wait_held(held)
            other = context.new_page(); other.goto(origin+'/')
            other.evaluate("localStorage.setItem('ss-auth-epoch','fictional-new-session')")
            expect(page.locator('#templateWorkspace')).to_be_hidden()
            fixture.me['store'] = 'second-fictional-store'; fixture.me['csrf_token'] = 'second-fictional-csrf'
            page.locator('#templateRefreshSession').click()
            expect(page.locator('#templateStore')).to_have_text('second-fictional-store')
            expect(page.locator('#templateSearchButton')).to_be_enabled()
            route,response,body = held[0]
            route.fulfill(response=response, body=body)
            expect(page.locator('#templateReview')).to_be_hidden()
            expect(page.locator('#templateStore')).to_have_text('second-fictional-store')
            assert fixture.effects['approve'] == 0
            page.unroute(preview_pattern, hold_old); other.close()
            results.append('PASS cross-tab sign-in transition clears owner data; a delayed old review cannot repopulate a newer store even when fixture ignores abort.')

            open_owner(); review(); attest()
            fixture.me['csrf_token'] = 'silently-changed-fictional-session'
            page.locator('#templateApproveButton').click()
            expect(page.locator('#templateWorkspace')).to_be_hidden()
            assert not [row for row in fixture.calls if row['path'].endswith('/approve')]
            results.append('PASS silent session-cookie authority change is detected before approval POST and clears the selected store.')

            open_owner(); review()
            for width,height in ((1024,900),(390,844)):
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), width
                page.screenshot(path=str(report/f'owner-review-{width}.png'), full_page=True)
            assert not errors, errors
            assert not external, external
            assert not assets, assets
            results.append('PASS desktop/mobile review layouts have no horizontal overflow; no JavaScript errors, external requests or failed static assets.')
            outcome['passed'] = True
            browser.close()
    except Exception:
        outcome['failure'] = traceback.format_exc()
    finally:
        server.shutdown(); server.server_close(); worker.join(timeout=5)
        outcome['finished_utc'] = datetime.now(timezone.utc).isoformat()
        outcome['sources_after'] = fingerprints()
        outcome['source_stable'] = before == outcome['sources_after']
        outcome['passed'] = outcome['passed'] and outcome['source_stable']
        outcome['javascript_errors'] = errors
        outcome['external_requests'] = external
        outcome['failed_static_assets'] = assets
        (report/'owner-template-acceptance.json').write_text(json.dumps(outcome,indent=2)+'\n',encoding='utf-8')
        (report/'owner-template-acceptance.log').write_text('\n'.join(results)+('\nFAIL '+outcome['failure'] if 'failure' in outcome else '')+'\n',encoding='utf-8')
    print(json.dumps({'report':str(report),'passed':outcome['passed'],'cases':len(results),'source_stable':outcome['source_stable']}))
    if not outcome['passed']:
        raise RuntimeError(outcome.get('failure','Source changed during owner-template browser acceptance'))


if __name__ == '__main__':
    run()
