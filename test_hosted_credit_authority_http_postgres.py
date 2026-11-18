"""Three real PostgreSQL HTTP cases for manual-credit authority; draft UNRUN.

Actual login responses populate the test client's cookie jar; enrolled sign-in
uses the actual MFA challenge and an unused issued recovery proof. No cookie,
authentication result, current actor or production authority check is injected.
TESTING is Flask's transport fixture mode, never a credit authorization bypass.
Auth assertions use fixed messages rather than rendering secret-bearing DTOs.
"""
import hmac
from unittest.mock import patch

from hosted.app import create_app
from hosted.security import digest
import test_hosted_credit_authority_postgres as credit_fixture


class PostgreSQLCreditAuthorityHTTPAcceptance(credit_fixture._CreditAuthorityFixture):
    origin = 'https://credit-authority.fixture.invalid'
    cookie_domain = 'credit-authority.fixture.invalid'

    def _http_sign_in(self, role):
        app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': self.origin,
            'APP_ENCRYPTION_KEYS': self.key, 'BOOTSTRAP_TOKEN': '',
            'SQUARE_APPLICATION_ID': 'fictional-credit-http-app',
            'SQUARE_APPLICATION_SECRET': 'fictional-credit-http-secret'}, database=self.database)
        client = app.test_client()
        response = client.post('/api/auth/login', base_url=self.origin,
            headers={'Origin': self.origin}, environ_overrides={'REMOTE_ADDR': self.address},
            json={'store_slug': self.slug, 'email': self.accounts[role]['email'],
                  'password': self.password, 'mfa_supported': True})
        self.assertEqual(response.status_code, 200)
        info = response.get_json()
        self.assertTrue(isinstance(info, dict), 'Login must return a typed envelope.')
        if role == 'owner':
            self.assertTrue(info.get('mfa_required') is True, 'Enrolled owner must complete a real challenge.')
            self.assertTrue(not info.get('access_token'), 'Password-only challenge must not issue a session token.')
            challenge = info.get('challenge_token')
            self.assertTrue(isinstance(challenge, str) and len(challenge) == 43,
                            'Challenge must have the actual opaque-token shape.')
            response = client.post('/api/auth/mfa/challenge', base_url=self.origin,
                headers={'Origin': self.origin}, environ_overrides={'REMOTE_ADDR': self.address},
                json={'challenge_token': challenge, 'code': self.recovery_codes[role].pop()})
            self.assertEqual(response.status_code, 200)
            info = response.get_json()
            self.assertTrue(isinstance(info, dict) and info.get('mfa_authenticated') is True,
                            'Actual MFA verification must issue an authenticated session.')
        else:
            self.assertTrue(info.get('mfa_authenticated') is False,
                            'Unenrolled manager must remain password-only.')
        token, csrf = info.get('access_token'), info.get('csrf_token')
        self.assertTrue(isinstance(token, str) and 1 <= len(token) <= 256,
                        'Sign-in must issue a bounded private session token.')
        self.assertTrue(isinstance(csrf, str) and len(csrf) == 64,
                        'Sign-in must issue its actual CSRF proof.')
        cookie = client.get_cookie('ss_session', domain=self.cookie_domain)
        self.assertTrue(cookie is not None and isinstance(cookie.value, str) and
                        hmac.compare_digest(cookie.value, token),
                        'The client must retain only the actual sign-in cookie.')
        me = client.get('/api/me', base_url=self.origin)
        self.assertEqual(me.status_code, 200)
        current = me.get_json()
        self.assertTrue(isinstance(current, dict) and current.get('role') == role and
                        current.get('csrf_token') == csrf,
                        'The actual cookie must resolve the expected current account and CSRF proof.')
        return app, client, token, csrf

    def _post_credit(self, client, csrf, body):
        response = client.post('/api/credit/customers/' + str(self.customer) + '/adjust',
            base_url=self.origin, headers={'Origin': self.origin, 'X-SS-CSRF': csrf},
            environ_overrides={'REMOTE_ADDR': self.address}, json=body)
        self.assertFalse('Set-Cookie' in response.headers, 'Credit responses must not change cookies.')
        return response

    def _assert_credit_error(self, response, status, code):
        self.assertEqual(response.status_code, status)
        value = response.get_json()
        self.assertTrue(isinstance(value, dict) and set(value) == {'error', 'code', 'sign_in_required'},
                        'Credit denial must expose only the typed safe error fields.')
        self.assertTrue(value.get('code') == code and value.get('sign_in_required') is True,
                        'Credit denial must preserve its authority code and sign-in requirement.')

    def test_unenrolled_manager_cookie_and_csrf_cannot_create_manual_credit(self):
        _, client, _, csrf = self._http_sign_in('manager')
        before = self._financial_rows()
        response = self._post_credit(client, csrf, self._body('http-unenrolled-manager'))
        self._assert_credit_error(response, 403, 'MFA_STEP_UP_REQUIRED')
        self.assertTrue(self._financial_rows() == before, 'Denied correction changed financial evidence.')

    def test_enrolled_owner_http_replay_after_proof_ages_does_not_issue_more_credit(self):
        _, client, token, csrf = self._http_sign_in('owner')
        body = self._body('http-original-correction')
        response = self._post_credit(client, csrf, body)
        self.assertEqual(response.status_code, 200)
        original = response.get_json()
        self.assertTrue(isinstance(original, dict) and set(original) == {'entry_id', 'balance_cents', 'version'} and
                        original.get('balance_cents') == 100 and original.get('version') == 1,
                        'Successful correction must retain the existing financial result shape.')
        after_first = self._financial_rows()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",
                       (digest(token),))
        replay = self._post_credit(client, csrf, body)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json() == original, 'Exact committed replay changed its original result.')
        denied = self._post_credit(client, csrf, self._body('http-new-aged-correction', version=1))
        self._assert_credit_error(denied, 403, 'MFA_STEP_UP_REQUIRED')
        self.assertTrue(self._financial_rows() == after_first,
                        'Replay or denied new key changed committed financial evidence.')
        self.assertEqual(len(after_first['ss_credit_entries']), 1)
        self.assertEqual(len(after_first['ss_requests']), 1)

    def test_logout_after_http_resolution_denies_original_adjustment_and_keeps_cookie(self):
        app, client, token, csrf = self._http_sign_in('owner')
        ledger = app.extensions['ss_ledger']
        original_adjust = ledger.adjust
        before = self._financial_rows()
        revocations = []
        def revoke_before_adjust(actor, customer_id, data):
            self.assertTrue(actor.get('session_hash') == digest(token),
                            'Race must target the session actually resolved by this HTTP request.')
            app.extensions['ss_auth'].logout(token)
            revocations.append(True)
            return original_adjust(actor, customer_id, data)
        with patch.object(ledger, 'adjust', side_effect=revoke_before_adjust):
            response = self._post_credit(client, csrf, self._body('http-resolve-logout-race'))
        self.assertEqual(len(revocations), 1)
        self._assert_credit_error(response, 401, 'CREDIT_SESSION_CHANGED')
        cookie = client.get_cookie('ss_session', domain=self.cookie_domain)
        self.assertTrue(cookie is not None and isinstance(cookie.value, str) and
                        hmac.compare_digest(cookie.value, token),
                        'Credit denial must keep the original browser cookie unchanged.')
        self.assertTrue(self._financial_rows() == before,
                        'Logout-before-adjust race changed financial evidence.')
