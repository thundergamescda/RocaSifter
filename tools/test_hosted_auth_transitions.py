"""Actual Chrome cookie-transition acceptance against fictional loopback auth.

Threading Events hold Flask login/logout and /me responses so tests exercise the
browser cookie jar as well as the UI. No PostgreSQL, Square, email, or live host.
"""
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from flask import request
from hosted.app import create_app
from hosted.security import ServiceError
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


SOURCE_NAMES = ('hosted/static/portal.js', 'hosted/static/portal.css',
                'hosted/templates/portal.html', 'hosted/auth.py', 'hosted/app.py',
                'tools/test_hosted_auth_transitions.py')


def fingerprints():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_NAMES}


class FictionalAuth:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}
        self.login_calls = 0
        self.logout_calls = 0
        self.login_entered = threading.Event()
        self.logout_entered = threading.Event()
        self.me_entered = threading.Event()
        self.login_release = threading.Event()
        self.logout_release = threading.Event()
        self.me_release = threading.Event()
        self.release_all()

    def release_all(self):
        for event in (self.login_release, self.logout_release, self.me_release):
            event.set()

    @staticmethod
    def wait(event):
        if not event.wait(30):
            raise ServiceError('Fictional test response was not released.', 503)

    def login(self, data, address):
        with self.lock:
            self.login_calls += 1
            sequence = self.login_calls
        self.login_entered.set()
        self.wait(self.login_release)
        role = 'owner' if data['email'] == 'owner@example.invalid' else 'cashier'
        slug = 'fictional-a' if role == 'owner' else 'fictional-b'
        token = f'fictional-{sequence}-{uuid.uuid4()}'
        with self.lock:
            self.sessions[token] = {'tenant_id': uuid.uuid4(), 'staff_id': uuid.uuid4(),
                                    'role': role, 'slug': slug}
        return token, {'store': slug, 'role': role}

    def resolve(self, token):
        if request.path == '/api/me':
            self.me_entered.set()
            self.wait(self.me_release)
        with self.lock:
            actor = self.sessions.get(token)
        if not actor:
            raise ServiceError('Sign in to your store.', 401)
        return actor

    def logout(self, token):
        with self.lock:
            self.logout_calls += 1
        self.logout_entered.set()
        self.wait(self.logout_release)
        with self.lock:
            self.sessions.pop(token, None)


def run():
    report = ROOT / 'Testing' / '2.6.0'
    report.mkdir(parents=True, exist_ok=True)
    initial = fingerprints()
    results, errors, requests = [], [], []
    outcome = {'started_utc': datetime.now(timezone.utc).isoformat(),
               'scope': 'Actual Chrome with threaded loopback Flask auth and cookie/CSRF routes. Event-held fictional responses; no PostgreSQL, Square, emails, or live hosts.',
               'source_sha256_at_start': initial, 'results': results,
               'browser_errors': errors, 'passed': False,
               'live_square_contacted': False, 'live_host_contacted': False}
    server = make_server('127.0.0.1', 0, lambda environ, start_response: [], threaded=True)
    base = f'http://127.0.0.1:{server.server_port}'
    app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': base,
                      'APP_ENCRYPTION_KEYS': Fernet.generate_key().decode()}, database=Mock())
    fixture = FictionalAuth()
    auth = app.extensions['ss_auth']
    auth.resolve = fixture.resolve
    auth.login = fixture.login
    auth.logout = fixture.logout
    auth.list_staff = Mock(return_value=[])
    auth.bootstrap = Mock(side_effect=ServiceError('Fictional setup not enabled.', 503))
    auth.accept_invite = Mock(return_value={'staff_created': True})
    app.extensions['ss_square'].status = Mock(return_value={
        'connected': False, 'oauth_configured': False, 'environment': 'sandbox'})
    server.app = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def record(message):
        results.append(message)
        print(message, flush=True)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='chrome', headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = context.new_page()
            page.set_default_timeout(10_000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: requests.append(req.url))

            def fill_login(email='owner@example.invalid'):
                for identifier, value in {'storeSlug': 'fictional-store', 'email': email,
                                          'password': 'fictional-test-password'}.items():
                    page.locator('#' + identifier).fill(value)

            def assert_busy():
                assert page.evaluate('authTransitionBusy') is True
                assert page.evaluate("['signin','workspace','invitation','pilotSetup'].every(id=>document.getElementById(id).inert)")

            def assert_released():
                expect(page.locator('#signin')).not_to_have_attribute('inert', '')
                assert page.evaluate('authTransitionBusy') is False
                assert page.evaluate("['signin','workspace','invitation','pilotSetup'].every(id=>!document.getElementById(id).inert)")

            def try_conflicting_transitions():
                # Pointer hit-testing exercises native inert behavior; synthetic
                # dispatch separately verifies the handler-level guards.
                bounds = page.locator('#showInvitation').bounding_box()
                assert bounds
                page.mouse.click(bounds['x'] + bounds['width'] / 2,
                                 bounds['y'] + bounds['height'] / 2)
                page.locator('#showInvitation').dispatch_event('click')
                page.evaluate("location.hash='#pilot-setup'")
                page.wait_for_function("() => location.hash!=='#pilot-setup'")
                page.evaluate("document.getElementById('showPilotSetup').click();document.getElementById('storeSlug').value='fictional-other';document.getElementById('email').value='cashier@example.invalid';document.getElementById('password').value='fictional-test-password';document.getElementById('loginForm').requestSubmit();document.getElementById('signOut').click()")
                expect(page.locator('#invitation')).to_be_hidden()
                expect(page.locator('#pilotSetup')).to_be_hidden()

            try:
                page.goto(base)
                expect(page.locator('#signin')).to_be_visible()
                fill_login()
                fixture.login_release.clear()
                fixture.login_entered.clear()
                fixture.me_release.clear()
                fixture.me_entered.clear()
                page.locator('#loginForm button').click()
                assert fixture.login_entered.wait(5), 'Login did not reach Flask fixture'
                assert_busy()
                try_conflicting_transitions()
                assert fixture.login_calls == 1 and fixture.logout_calls == 0
                expect(page.locator('#signin')).to_be_visible()
                record('Held login blocks account-form navigation, fragment navigation, duplicate sign-in and sign-out; only one cookie-changing request reaches Flask.')

                fixture.login_release.set()
                assert fixture.me_entered.wait(5), 'Successful login did not verify /api/me'
                assert_busy()
                try_conflicting_transitions()
                assert fixture.login_calls == 1 and fixture.logout_calls == 0
                fixture.me_release.set()
                expect(page.locator('#workspace')).to_be_visible()
                page.wait_for_function('() => !authTransitionBusy')
                assert_released()
                me = page.request.get(base + '/api/me')
                assert me.status == 200 and me.json()['store'] == 'fictional-a' and me.json()['role'] == 'owner'
                cookies = [cookie for cookie in context.cookies() if cookie['name'] == 'ss_session']
                assert len(cookies) == 1 and cookies[0]['httpOnly']
                with fixture.lock:
                    assert fixture.sessions[cookies[0]['value']]['slug'] == 'fictional-a'
                record('Login remains locked until the authoritative /api/me response; released UI, HttpOnly cookie and server identity all agree on the owner account.')

                fixture.logout_release.clear()
                fixture.logout_entered.clear()
                page.locator('#signOut').click()
                assert fixture.logout_entered.wait(5), 'Logout did not reach Flask fixture'
                assert_busy()
                expect(page.locator('#workspace')).to_be_hidden()
                expect(page.locator('#signin')).to_be_visible()
                try_conflicting_transitions()
                assert fixture.login_calls == 1 and fixture.logout_calls == 1
                fixture.logout_release.set()
                expect(page.locator('#loginStatus')).to_contain_text('Signed out.')
                page.wait_for_function('() => !authTransitionBusy')
                assert_released()
                assert not [cookie for cookie in context.cookies() if cookie['name'] == 'ss_session']
                assert page.request.get(base + '/api/me').status == 401
                record('Held logout blocks a new login and account switching until its response clears the browser cookie; server /api/me then rejects the revoked session.')

                fill_login('cashier@example.invalid')
                page.locator('#loginForm button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.wait_for_function('() => !authTransitionBusy')
                assert page.request.get(base + '/api/me').json()['store'] == 'fictional-b'
                expect(page.locator('#staffPanel')).to_be_hidden()
                page.locator('#signOut').click()
                expect(page.locator('#loginStatus')).to_contain_text('Signed out.')
                page.wait_for_function('() => !authTransitionBusy')
                record('After logout completes, a different cashier account signs in with its own cookie/tenant view and no owner staff controls.')

                calls_before_failure = fixture.login_calls
                page.route('**/api/auth/login', lambda route: route.abort('failed'))
                fill_login()
                page.locator('#loginForm button').click()
                expect(page.locator('#loginStatus')).to_contain_text('did not respond')
                page.wait_for_function('() => !authTransitionBusy')
                assert_released()
                assert fixture.login_calls == calls_before_failure
                expect(page.locator('#workspace')).to_be_hidden()
                expect(page.locator('#password')).to_have_value('')
                expect(page.locator('#loginForm button')).to_be_enabled()
                page.unroute('**/api/auth/login')
                fill_login()
                page.locator('#loginForm button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.wait_for_function('() => !authTransitionBusy')
                assert page.request.get(base + '/api/me').json()['store'] == 'fictional-a'
                record('A browser-aborted login releases the UI, clears the password and leaves no stale workspace; a subsequent online login verifies the correct identity.')

                page.route('**/api/auth/logout', lambda route: route.abort('failed'))
                page.locator('#signOut').click()
                expect(page.locator('#loginStatus')).to_contain_text('could not confirm session revocation')
                page.wait_for_function('() => !authTransitionBusy')
                assert_released()
                expect(page.locator('#workspace')).to_be_hidden()
                expect(page.locator('#staffList .staff-row')).to_have_count(0)
                assert page.evaluate("portalMe===null&&csrfToken===''")
                page.unroute('**/api/auth/logout')
                record('A browser-aborted logout clears the private workspace and releases controls while explicitly warning that server revocation was not confirmed; it does not falsely claim cookie/session revocation.')

                assert errors == [], errors
                assert all(url.startswith(base + '/') for url in requests), 'Unexpected external HTTP request'
                page.screenshot(path=str(report / 'hosted-auth-transitions.png'), full_page=True)
                record('No JavaScript errors or external requests; every auth call used only the fictional loopback service.')
                outcome['passed'] = True
            except Exception:
                page.screenshot(path=str(report / 'hosted-auth-transitions-failure.png'), full_page=True)
                raise
            finally:
                fixture.release_all()
                browser.close()
    except Exception:
        outcome['failure'] = traceback.format_exc()
        print(outcome['failure'], flush=True)
    finally:
        fixture.release_all()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        final = fingerprints()
        changed = [name for name in SOURCE_NAMES if initial[name] != final[name]]
        outcome.update(finished_utc=datetime.now(timezone.utc).isoformat(),
                       source_sha256=final, source_snapshot_stable=not changed,
                       changed_during_run=changed, tests_run=len(results))
        if changed:
            outcome['passed'] = False
        (report / 'hosted-auth-transitions-acceptance.json').write_text(json.dumps(outcome, indent=2), encoding='utf-8')
        (report / 'hosted-auth-transitions-acceptance.log').write_text(
            '\n'.join(results) + '\n' + outcome.get('failure', 'PASS\n'), encoding='utf-8')
    return 0 if outcome['passed'] else 1


if __name__ == '__main__':
    sys.exit(run())
