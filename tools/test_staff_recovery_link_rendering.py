"""DRAFT UNRUN: actual portal DOM/script, fictional browser-only HTTP contract.

Deferred responses prove rendering order; these responses confer no real auth,
MFA or recovery authority. No service, database, merchant or external network is
used. Intended only for a separately admitted browser-capable Linux fixture.
"""
from pathlib import Path
import os
import sys
from urllib.parse import urlsplit
import unittest

from playwright.sync_api import sync_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = 'https://recovery-ui.fixture.invalid'
FIRST = '00000000-0000-0000-0000-000000000001'
SECOND = '00000000-0000-0000-0000-000000000002'


class StaffRecoveryLinkRendering(unittest.TestCase):
    def setUp(self):
        if (sys.platform != 'linux' or os.geteuid() == 0 or
                os.environ.get('SS_STAFF_RECOVERY_BROWSER_FIXTURE') != 'account-security-candidate-20261119-r2'):
            raise RuntimeError('Requires separate Linux non-root browser-fixture admission; no local or automatic fallback is allowed.')

    def test_new_issue_clear_auth_context_and_principal_changes_fence_delayed_links(self):
        pending, errors = {}, []
        assets = {'/': (ROOT / 'hosted/templates/portal.html', 'text/html'),
            **{'/static/' + name: (ROOT / 'hosted/static' / name, mime) for name, mime in
               (('portal.js', 'text/javascript'), ('portal.css', 'text/css'),
                ('mfa.js', 'text/javascript'), ('password.js', 'text/javascript'),
                ('cleanup_observer.js', 'text/javascript'), ('cleanup_observer.css', 'text/css'),
                ('brand-icon.png', 'image/png'))}}
        def respond(route):
            uri = urlsplit(route.request.url)
            if uri.scheme + '://' + uri.netloc != ORIGIN:
                raise AssertionError('Unexpected external browser request.')
            path = uri.path
            if path in assets:
                source, mime = assets[path]
                route.fulfill(body=source.read_bytes(), content_type=mime)
            elif path == '/api/me':
                route.fulfill(json={'store': 'Fictional shop', 'role': 'owner', 'csrf_token': 'fictional-csrf',
                    'mfa_enabled': True, 'mfa_authenticated': True, 'mfa_fresh': True})
            elif path == '/api/square/status':
                route.fulfill(json={'connected': False, 'oauth_configured': False,
                    'connection_status': 'not_connected', 'environment': 'sandbox'})
            elif path == '/api/staff':
                route.fulfill(json={'staff': [{'id': staff, 'email': email, 'role': 'buyer', 'enabled': True}
                    for staff, email in ((FIRST, 'first@example.invalid'), (SECOND, 'second@example.invalid'))]})
            elif path.endswith('/password-recovery'):
                pending[path.split('/')[-2]] = route
            else:
                route.fulfill(status=503, json={'error': 'Unrelated fictional UI service unavailable.'})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, chromium_sandbox=True)
            try:
                page = browser.new_page()
                page.route('**/*', respond)
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.goto(ORIGIN)
                buttons = page.locator('#staffList').get_by_role('button', name='Reset password', exact=True)
                expect(buttons).to_have_count(2)
                first, second = buttons.nth(0), buttons.nth(1)
                link, result = page.locator('#staffRecoveryLink'), page.locator('#staffRecoveryResult')
                def issue(staff, button):
                    with page.expect_request(lambda request: request.url.endswith('/' + staff + '/password-recovery')):
                        button.click()
                    expect(button).to_be_disabled()
                    self.assertIn(staff, pending)
                def release(staff, button, token):
                    pending.pop(staff).fulfill(status=201, json={'recovery_token': token, 'expires_in': 900})
                    # Finally re-enables this exact request's button, proving its
                    # handler has finished before checking suppressed publication.
                    expect(button).to_be_enabled()

                issue(FIRST, first); issue(SECOND, second)
                release(FIRST, first, 'a' * 43)
                expect(result).to_be_hidden(); expect(link).to_have_value('')
                release(SECOND, second, 'b' * 43)
                expect(result).to_be_visible(); expect(link).to_have_value(ORIGIN + '/staff-password-recovery#recovery=' + 'b' * 43)

                issue(FIRST, first); issue(SECOND, second)
                release(SECOND, second, 'c' * 43)
                page.get_by_role('button', name='Clear link from this page', exact=True).click()
                release(FIRST, first, 'd' * 43)
                expect(result).to_be_hidden(); expect(link).to_have_value('')

                for change in (
                    "document.dispatchEvent(new CustomEvent('ss-portal-auth-context',{detail:{active:false}}))",
                    "window.dispatchEvent(new PageTransitionEvent('pagehide'))",
                    "portalMe={...portalMe};csrfToken='changed-fictional-csrf'",
                ):
                    issue(FIRST, first)
                    page.evaluate(change)
                    release(FIRST, first, 'e' * 43)
                    expect(result).to_be_hidden(); expect(link).to_have_value('')
                self.assertEqual(errors, [])
            finally:
                browser.close()
