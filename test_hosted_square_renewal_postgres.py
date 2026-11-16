"""Source-only authored PostgreSQL renewal acceptance; UNRUN until admitted.

All credentials, signatures and streamed HTTP replies are fictional. Blocked
mock HTTP proves that real revocation/reconnect SQL can commit during renewal.
No live Square call, scheduler registration or desktop/native acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
from pathlib import Path
import threading
import uuid
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql

from hosted.app import create_app
from hosted.database import Database
from hosted.security import ServiceError, digest
from hosted.square import Square
from hosted.square_renewal import SquareRenewal, renewal_health
from test_hosted_square_authority import Response
import test_hosted_receiving_oauth_postgres as oauth_fixture
import test_hosted_settlement_postgres as settlement_fixture


class PostgreSQLSquareRenewalAcceptance(settlement_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        oauth_fixture.PostgreSQLReceivingOAuthAcceptance.setUp(self)
        self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'] = 'fictional-renewal-notice-key'
        self.connect()
        self.square._token = Square._token.__get__(self.square, Square)
        self.transport.post.side_effect = lambda *args, **kwargs: Response(self.reply())

    def connect(self):
        state, _ = oauth_fixture.PostgreSQLReceivingOAuthAcceptance.begin(self)
        return self.square.complete(self.actor, state, 'fictional-renewal-owner-code')

    def connection(self):
        return oauth_fixture.PostgreSQLReceivingOAuthAcceptance.connection(self)

    def authority(self, row=None):
        row = self.connection() if row is None else row
        return {key: row[key] for key in ('tenant_id', 'merchant_id', 'environment', 'encrypted_tokens', 'expires_at',
            'authorization_generation', 'oauth_application_id', 'token_obtained_at', 'requested_profile', 'revoked_at', 'pending_notice_cursor')}

    def read_saved_tokens(self, row=None):
        row = self.connection() if row is None else row
        return json.loads(self.square.cipher.decrypt(row['encrypted_tokens'].encode()))

    def due(self, *, days=7, expires_days=20):
        expires = datetime.now(timezone.utc) + timedelta(days=expires_days)
        tokens = self.read_saved_tokens() | {'expires_at': expires.isoformat()}
        sealed = self.square.cipher.encrypt(json.dumps(tokens).encode()).decode()
        with self.database.transaction() as db:
            db.execute('''UPDATE ss_square_connections SET encrypted_tokens=%s,expires_at=%s,
                token_obtained_at=clock_timestamp()-(%s * interval '1 day'),renewal_lease_token=NULL,
                renewal_lease_until=NULL,renewal_retry_after=NULL WHERE tenant_id=%s''',
                (sealed, expires, days, self.tenant))
        return self.authority()

    def reply(self, **changes):
        expires = datetime.now(timezone.utc) + timedelta(days=30)
        return self.read_saved_tokens() | {'access_token': 'fictional-renewed-access-private',
                               'expires_at': expires.isoformat(), 'token_type': 'bearer'} | changes

    def clear_fixture_attempt(self):
        with self.database.transaction() as db:
            db.execute('''UPDATE ss_square_connections SET renewal_lease_token=NULL,renewal_lease_until=NULL,
                renewal_retry_after=NULL WHERE tenant_id=%s''', (self.tenant,))

    def blocked(self, response=None):
        started = threading.Event(); release = threading.Event()
        response = response or self.reply()
        def post(url, **kwargs):
            self.assertEqual(url, 'https://connect.squareup.com/oauth2/token')
            self.assertFalse(kwargs['allow_redirects']); self.assertTrue(kwargs['stream'])
            self.assertIs(kwargs['verify'], True)
            self.assertEqual(kwargs['json']['client_id'], 'fictional-app')
            if kwargs['json']['grant_type'] == 'authorization_code':
                return Response(self.reply(access_token='fictional-new-owner-private', refresh_token='fictional-new-owner-refresh'))
            started.set()
            if not release.wait(timeout=20):
                raise AssertionError('Fictional refresh was not released.')
            return Response(response)
        self.transport.post.side_effect = post
        return started, release

    @contextmanager
    def namespace(self):
        name = 'ss_test_' + uuid.uuid4().hex
        database = None; created = False
        try:
            with psycopg.connect(self.url, sslmode='require') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (name,)).fetchone())
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
            created = True; database = Database(self.url, schema=name)
            yield database
        finally:
            if database is not None:
                database.close()
            if created:
                with psycopg.connect(self.url, sslmode='require') as db:
                    db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))
                with psycopg.connect(self.url, sslmode='require') as db:
                    self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (name,)).fetchone())

    def test_owner_completion_records_known_application_and_local_time_without_scope_claim(self):
        notice = {'event_id': 'fictional-owner-frozen-notice-' + uuid.uuid4().hex,
                  'merchant_id': self.read_saved_tokens()['merchant_id'], 'type': 'oauth.authorization.revoked'}
        raw = json.dumps(notice, separators=(',', ':')).encode()
        signature = base64.b64encode(hmac.new(self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'].encode(),
            self.square.webhook_url.encode() + raw, hashlib.sha256).digest()).decode()
        self.assertTrue(self.square.ingest(raw, signature)['revocation_pending'])
        with self.database.transaction() as db:
            original_notice = self.square._notice_cursor(db, notice['merchant_id'])
        state, _ = oauth_fixture.PostgreSQLReceivingOAuthAcceptance.begin(self)
        request = self.square._token
        original_config = dict(self.config); original_base = self.square.base
        original_barrier = self.square._notice_barrier; original_cursor = self.square._notice_cursor
        def barrier(db, *, configuration):
            self.assertEqual(configuration.application_id, original_config['SQUARE_APPLICATION_ID'])
            self.config['SQUARE_APPLICATION_ID'] = 'fictional-sql-aba-other-app'
            self.config['SQUARE_ENVIRONMENT'] = 'sandbox'; self.square.environment = 'sandbox'
            try:
                return original_barrier(db, configuration=configuration)
            finally:
                self.config.clear(); self.config.update(original_config); self.square.environment = 'production'
        def cursor(db, merchant=None, *, configuration):
            self.assertEqual(configuration.application_id, original_config['SQUARE_APPLICATION_ID'])
            self.config['SQUARE_APPLICATION_ID'] = 'fictional-sql-aba-other-app'
            self.config['SQUARE_ENVIRONMENT'] = 'sandbox'; self.square.environment = 'sandbox'
            try:
                observed = original_cursor(db, merchant, configuration=configuration)
            finally:
                self.config.clear(); self.config.update(original_config); self.square.environment = 'production'
            self.assertEqual(observed, original_notice)
            return observed
        self.square._notice_barrier = Mock(side_effect=barrier)
        self.square._notice_cursor = Mock(side_effect=cursor)
        def exchange(payload, *, configuration):
            self.assertEqual(configuration.application_id, original_config['SQUARE_APPLICATION_ID'])
            self.assertEqual(configuration.secret, original_config['SQUARE_APPLICATION_SECRET'])
            self.assertEqual(configuration.base, original_base)
            self.config['SQUARE_APPLICATION_ID'] = 'fictional-aba-other-app'
            self.config['SQUARE_APPLICATION_SECRET'] = 'fictional-aba-other-secret'
            self.square.base = 'https://unsupported.example.invalid'
            try:
                return request(payload, configuration=configuration)
            finally:
                self.config.clear(); self.config.update(original_config); self.square.base = original_base
        def post(url, **kwargs):
            self.assertEqual(url, original_base + '/oauth2/token')
            self.assertEqual(kwargs['json']['client_id'], original_config['SQUARE_APPLICATION_ID'])
            self.assertEqual(kwargs['json']['client_secret'], original_config['SQUARE_APPLICATION_SECRET'])
            self.assertEqual(kwargs['json']['redirect_uri'], original_config['PUBLIC_BASE_URL'] + '/oauth/square/callback')
            self.assertIs(kwargs['verify'], True)
            return Response(self.reply(access_token='fictional-owner-frozen-context-private'))
        self.square._token = Mock(side_effect=exchange)
        self.transport.post.side_effect = post
        self.assertTrue(self.square.complete(self.actor, state, 'fictional-frozen-context-code')['connected'])
        row = self.connection(); status = self.square.status(self.actor)
        self.assertEqual(row['oauth_application_id'], self.config['SQUARE_APPLICATION_ID'])
        self.assertIsNotNone(row['token_obtained_at'])
        self.assertEqual(row['requested_profile'], 'receiving')
        self.assertEqual(status['requested_scopes'], list(Square.RECEIVING_SCOPES))
        self.assertFalse(status['grants_verified']); self.assertFalse(status['write_checkout_enabled'])
        self.assertEqual(status['renewal']['age_status'], 'fresh')
        self.assertFalse(status['renewal']['automatic_renewal_active'])

    def test_fresh_saved_token_does_not_contact_provider_or_claim_renewal(self):
        before = self.connection()
        self.assertEqual(self.square.access_token(self.tenant), self.read_saved_tokens()['access_token'])
        self.assertEqual(self.connection(), before)
        self.assertEqual(self.square.renew(self.tenant), {'status': 'not_due'})
        self.transport.post.assert_not_called()

    def test_due_refresh_publishes_encrypted_tokens_and_keeps_authorization_lineage(self):
        before = self.due(days=6); expected = self.reply()
        response = Response(expected); self.transport.post.return_value = response; self.transport.post.side_effect = None
        self.assertEqual(self.square.access_token(self.tenant), expected['access_token'])
        row = self.connection()
        self.assertEqual(self.read_saved_tokens(row), expected)
        for key in ('authorization_generation', 'requested_profile', 'merchant_id', 'environment', 'oauth_application_id', 'revoked_at', 'pending_notice_cursor'):
            self.assertEqual(row[key], before[key])
        self.assertGreater(row['token_obtained_at'], before['token_obtained_at'])
        self.assertIsNone(row['renewal_lease_token']); self.assertIsNone(row['renewal_retry_after'])
        text = json.dumps(row, default=str)
        for secret in (expected['access_token'], expected['refresh_token'], self.config['SQUARE_APPLICATION_SECRET']):
            self.assertNotIn(secret, text)
        self.assertTrue(response.closed)
        self.assertEqual(self.transport.post.call_count, 1)
        self.assertEqual(self.transport.post.call_args.kwargs['headers']['Square-Version'], Square.API_VERSION)
        self.assertIs(self.transport.post.call_args.kwargs['verify'], True)

    def test_legacy_unknown_application_and_age_require_real_client_refresh_before_use(self):
        self.due()
        with self.database.transaction() as db:
            db.execute('UPDATE ss_square_connections SET oauth_application_id=NULL,token_obtained_at=NULL WHERE tenant_id=%s', (self.tenant,))
        before = self.authority(); status = self.square.status(self.actor)
        self.assertEqual(status['renewal']['age_status'], 'unknown')
        self.assertEqual(status['renewal']['application_binding'], 'unknown')
        self.assertIsNone(self.connection()['token_obtained_at'])
        with self.assertLogs('hosted.square', level='WARNING') as logs:
            self.assertEqual(self.square.renew(self.tenant), {'status': 'renewed'})
        after = self.connection()
        self.assertEqual(after['oauth_application_id'], self.config['SQUARE_APPLICATION_ID'])
        self.assertIsNotNone(after['token_obtained_at'])
        self.assertEqual(after['authorization_generation'], before['authorization_generation'])
        self.assertNotIn(self.read_saved_tokens()['access_token'], ''.join(logs.output))
        self.assertNotIn(self.read_saved_tokens()['refresh_token'], ''.join(logs.output))

    def test_expired_access_can_be_renewed_with_valid_private_refresh_token(self):
        self.due(days=31, expires_days=-1)
        self.assertTrue(self.square.status(self.actor)['renewal']['expired'])
        self.assertEqual(self.square.renew(self.tenant), {'status': 'renewed'})
        self.assertFalse(self.square.status(self.actor)['renewal']['expired'])
        self.assertEqual(self.transport.post.call_count, 1)

    def test_revocation_commits_while_http_is_blocked_and_stale_reply_cannot_resurrect(self):
        before = self.due(); started, release = self.blocked()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.square.access_token, self.tenant)
            try:
                self.assertTrue(started.wait(timeout=10))
                event = {'event_id': 'fictional-refresh-revocation-' + uuid.uuid4().hex,
                         'merchant_id': before['merchant_id'], 'type': 'oauth.authorization.revoked'}
                raw = json.dumps(event, separators=(',', ':')).encode()
                signature = base64.b64encode(hmac.new(self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'].encode(),
                    self.square.webhook_url.encode() + raw, hashlib.sha256).digest()).decode()
                # This must COMMIT before the mock provider is released.
                self.assertTrue(self.square.ingest(raw, signature)['revocation_pending'])
                held = self.authority()
                self.assertIsNotNone(held['revoked_at']); self.assertIsNotNone(held['pending_notice_cursor'])
            finally:
                release.set()
            with self.assertRaises(ServiceError) as caught:
                future.result(timeout=20)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.authority(), held)
        self.assertEqual(self.connection()['encrypted_tokens'], before['encrypted_tokens'])

    def test_owner_reconnect_commits_during_blocked_refresh_and_wins_permanently(self):
        before = self.due(); started, release = self.blocked()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.square.access_token, self.tenant)
            try:
                self.assertTrue(started.wait(timeout=10))
                self.connect()
                current = self.authority()
                self.assertNotEqual(current['authorization_generation'], before['authorization_generation'])
                self.assertEqual(self.read_saved_tokens()['access_token'], 'fictional-new-owner-private')
            finally:
                release.set()
            with self.assertRaises(ServiceError) as caught:
                future.result(timeout=20)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.authority(), current)
        self.assertIsNone(self.connection()['renewal_lease_token'])

    def test_concurrent_foreground_renewal_has_one_provider_call_and_one_saved_winner(self):
        self.due(); started, release = self.blocked()
        other = Square(self.database, self.config, self.transport)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.square.access_token, self.tenant)
            try:
                self.assertTrue(started.wait(timeout=10))
                with self.assertRaises(ServiceError) as busy:
                    other.access_token(self.tenant)
                self.assertEqual(busy.exception.status, 409)
                self.assertEqual(self.transport.post.call_count, 1)
            finally:
                release.set()
            winner = future.result(timeout=20)
        self.assertEqual(other.access_token(self.tenant), winner)
        self.assertEqual(self.read_saved_tokens()['access_token'], winner)
        self.assertEqual(self.transport.post.call_count, 1)

    def test_expired_lease_cannot_overwrite_a_later_successful_renewal(self):
        self.due(); started, release = self.blocked()
        other = Square(self.database, self.config, self.transport)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.square.access_token, self.tenant)
            try:
                self.assertTrue(started.wait(timeout=10))
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_square_connections SET renewal_lease_until=clock_timestamp()-interval '1 second',renewal_retry_after=clock_timestamp()-interval '1 second' WHERE tenant_id=%s", (self.tenant,))
                self.transport.post.side_effect = lambda *args, **kwargs: Response(self.reply(access_token='fictional-later-winner-private'))
                winner = other.access_token(self.tenant); saved = self.authority()
            finally:
                release.set()
            with self.assertRaises(ServiceError) as caught:
                future.result(timeout=20)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.authority(), saved)
        self.assertEqual(self.read_saved_tokens()['access_token'], winner)

    def test_provider_failure_preserves_saved_authority_and_sets_shared_cooldown(self):
        before = self.due(days=9)
        self.transport.post.side_effect = lambda *args, **kwargs: Response({}, status=500)
        with self.assertLogs('hosted.square', level='WARNING') as logs:
            with self.assertRaises(ServiceError) as caught:
                self.square.access_token(self.tenant)
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(self.authority(), before)
        row = self.connection()
        self.assertIsNone(row['renewal_lease_token']); self.assertIsNotNone(row['renewal_retry_after'])
        self.assertEqual(row['renewal_error_code'], 'provider_failure')
        with self.assertRaises(ServiceError):
            self.square.access_token(self.tenant)
        self.assertEqual(self.transport.post.call_count, 1)
        for secret in (self.read_saved_tokens()['access_token'], self.read_saved_tokens()['refresh_token'], self.config['SQUARE_APPLICATION_SECRET']):
            self.assertNotIn(secret, str(caught.exception) + ''.join(logs.output))

    def test_wrong_merchant_changed_refresh_and_cached_or_unbounded_replies_do_not_publish(self):
        before = self.due(); good = self.reply()
        cases = [Response(good | {'merchant_id': 'fictional-wrong-merchant'}),
                 Response({key: value for key, value in good.items() if key != 'merchant_id'}),
                 Response(good | {'refresh_token': 'fictional-pkce-like-rotation'}),
                 Response(self.read_saved_tokens()), Response(good, status=302),
                 Response(good, headers={'Content-Length': '65537'}),
                 Response(good, raw=b'x' * 65537),
                 Response(good, raw=b'{"access_token":"first","access_token":"second"}'),
                 Response(good | {'expires_at': '2030-01-01T00:00:00+00:60'}),
                 Response(good | {'expires_at': '2030-01-01T24:00:00Z'}),
                 *[Response(good | {'errors': value}) for value in ({}, False, 0, '')]]
        for response in cases:
            with self.subTest(status=response.status_code, bytes=len(response.raw)):
                self.clear_fixture_attempt()
                self.transport.post.side_effect = None; self.transport.post.return_value = response
                with self.assertRaises(ServiceError):
                    self.square.access_token(self.tenant)
                self.assertEqual(self.authority(), before)
                self.assertTrue(response.closed)

    def test_config_environment_host_secret_transport_and_cipher_changes_fence_blocked_reply(self):
        for change in ('application', 'environment', 'host', 'secret', 'transport', 'cipher', 'keys'):
            with self.subTest(change=change):
                self.due(); before = self.authority(); started, release = self.blocked()
                original_config = dict(self.config); original_base = self.square.base
                original_transport = self.square.transport; original_cipher = self.square.cipher
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.square.access_token, self.tenant)
                    try:
                        self.assertTrue(started.wait(timeout=10))
                        if change == 'application': self.config['SQUARE_APPLICATION_ID'] = 'fictional-different-app'
                        elif change == 'environment': self.config['SQUARE_ENVIRONMENT'] = 'sandbox'
                        elif change == 'host': self.square.base = 'https://unsupported.example.invalid'
                        elif change == 'secret': self.config['SQUARE_APPLICATION_SECRET'] = 'fictional-rotated-secret'
                        elif change == 'transport': self.square.transport = Mock(trust_env=False)
                        elif change == 'cipher': self.square.cipher = Square(self.database, original_config, original_transport).cipher
                        else: self.config['APP_ENCRYPTION_KEYS'] = 'fictional-new-key-configuration-123456789'
                    finally:
                        release.set()
                    with self.assertRaises(ServiceError) as caught:
                        future.result(timeout=20)
                    self.assertIn(caught.exception.status, (409, 503))
                self.config.clear(); self.config.update(original_config)
                self.square.base = original_base; self.square.transport = original_transport; self.square.cipher = original_cipher
                self.assertEqual(self.authority(), before)
                self.clear_fixture_attempt()

    def test_every_saved_authority_field_fences_blocked_refresh_publication(self):
        fields = ('authorization_generation', 'merchant_id', 'environment', 'encrypted_tokens', 'expires_at',
                  'oauth_application_id', 'token_obtained_at', 'requested_profile')
        for field in fields:
            with self.subTest(field=field):
                self.due(); before = self.connection(); started, release = self.blocked()
                changed = {'authorization_generation': uuid.uuid4(), 'merchant_id': 'fictional-rebound-' + uuid.uuid4().hex,
                    'environment': 'sandbox', 'encrypted_tokens': 'fictional-replaced-ciphertext',
                    'expires_at': before['expires_at'] + timedelta(hours=1), 'oauth_application_id': 'fictional-other-app',
                    'token_obtained_at': before['token_obtained_at'] + timedelta(hours=1), 'requested_profile': 'standard'}[field]
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(self.square.access_token, self.tenant)
                    try:
                        self.assertTrue(started.wait(timeout=10))
                        with self.database.transaction() as db:
                            db.execute(sql.SQL('UPDATE ss_square_connections SET {}=%s WHERE tenant_id=%s').format(sql.Identifier(field)), (changed, self.tenant))
                        altered = self.authority()
                    finally:
                        release.set()
                    with self.assertRaises(ServiceError) as caught:
                        future.result(timeout=20)
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(self.authority(), altered)
                with self.database.transaction() as db:
                    db.execute(sql.SQL('UPDATE ss_square_connections SET {}=%s,renewal_lease_token=NULL,renewal_lease_until=NULL,renewal_retry_after=NULL WHERE tenant_id=%s').format(sql.Identifier(field)), (before[field], self.tenant))

    def test_short_remaining_response_lifetime_is_rejected_at_publication_clock(self):
        before = self.due(days=31, expires_days=-1)
        response = self.reply(expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
        self.transport.post.side_effect = lambda *args, **kwargs: Response(response)
        with self.assertRaises(ServiceError) as caught:
            self.square.access_token(self.tenant)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.authority(), before)

    def test_known_wrong_application_or_environment_requires_reconnect_without_http(self):
        for field, value in (('oauth_application_id', 'fictional-other-app'), ('environment', 'sandbox')):
            with self.subTest(field=field):
                before = self.connection()
                with self.database.transaction() as db:
                    db.execute(sql.SQL('UPDATE ss_square_connections SET {}=%s WHERE tenant_id=%s').format(sql.Identifier(field)), (value, self.tenant))
                with self.assertRaises(ServiceError) as caught:
                    self.square.access_token(self.tenant)
                self.assertEqual(caught.exception.status, 409)
                with self.database.transaction() as db:
                    db.execute(sql.SQL('UPDATE ss_square_connections SET {}=%s WHERE tenant_id=%s').format(sql.Identifier(field)), (before[field], self.tenant))
        keys = self.config['APP_ENCRYPTION_KEYS']
        self.config['APP_ENCRYPTION_KEYS'] = 'fictional-pre-capture-rotated-key-123456789'
        try:
            with self.assertRaises(ServiceError) as changed_keys:
                self.square.access_token(self.tenant)
            self.assertEqual(changed_keys.exception.status, 503)
        finally:
            self.config['APP_ENCRYPTION_KEYS'] = keys
        self.transport.post.assert_not_called()

    def test_default_disabled_background_has_no_database_or_provider_side_effect(self):
        with patch.object(self.database, 'transaction', side_effect=AssertionError('Disabled background queried SQL')):
            self.assertEqual(SquareRenewal(self.square).run_once(),
                {'status': 'disabled', 'attempted': 0, 'renewed': 0, 'failed': 0, 'results': []})
        self.transport.post.assert_not_called()

    def test_corrupt_oldest_candidate_backs_off_and_next_seller_is_not_starved(self):
        with self.namespace() as database:
            database.migrate(); square = Square(database, self.config, self.transport)
            first = uuid.UUID(int=1); second = uuid.UUID(int=2)
            expires = datetime.now(timezone.utc) + timedelta(days=10)
            tokens = self.read_saved_tokens() | {'merchant_id': 'fictional-fairness-second', 'expires_at': expires.isoformat()}
            with database.transaction() as db:
                for tenant in (first, second):
                    db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional renewal fairness store')", (tenant, 'fair-' + tenant.hex))
                    db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                        VALUES (%s,%s,'production',%s,%s)''', (tenant, 'fictional-fairness-first' if tenant == first else tokens['merchant_id'],
                        'fictional-invalid-ciphertext' if tenant == first else square.cipher.encrypt(json.dumps(tokens).encode()).decode(), expires))
            self.transport.post.side_effect = lambda *args, **kwargs: Response(tokens | {
                'access_token': 'fictional-fairness-renewed-private', 'expires_at': (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()})
            worker = SquareRenewal(square, enabled=True)
            failed = worker.run_once(limit=1)
            self.assertEqual(failed['failed'], 1); self.assertEqual(failed['results'][0]['tenant_id'], str(first))
            success = worker.run_once(limit=1)
            self.assertEqual(success['renewed'], 1); self.assertEqual(success['results'][0]['tenant_id'], str(second))
            self.assertEqual(worker.run_once(limit=1)['attempted'], 0)
            self.assertEqual(self.transport.post.call_count, 1)
            self.assertNotIn(tokens['refresh_token'], json.dumps((failed, success)))

    def test_actual_http_status_exposes_unknown_due_stale_and_app_mismatch_without_secrets(self):
        token = 'fictional-renewal-status-session-' + uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                SELECT %s,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at FROM ss_sessions WHERE token_hash=%s''',
                (digest(token), self.actor['session_hash']))
        app = create_app(self.config | {'TESTING': True}, database=self.database, square_transport=self.transport)
        client = app.test_client()
        for days, expected in ((0, 'fresh'), (6, 'due'), (9, 'stale'), (None, 'unknown')):
            with self.subTest(age=expected):
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_square_connections SET token_obtained_at=CASE WHEN %s::INTEGER IS NULL THEN NULL ELSE clock_timestamp()-(%s::INTEGER * interval '1 day') END WHERE tenant_id=%s", (days, days, self.tenant))
                response = client.get('/api/square/status', base_url=self.config['PUBLIC_BASE_URL'], headers={'Authorization': 'Bearer ' + token})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['renewal']['age_status'], expected)
                self.assertFalse(response.json['renewal']['automatic_renewal_active'])
                self.assertFalse(response.json['grants_verified']); self.assertFalse(response.json['write_checkout_enabled'])
                for secret in (self.read_saved_tokens()['access_token'], self.read_saved_tokens()['refresh_token'], self.config['SQUARE_APPLICATION_SECRET']):
                    self.assertNotIn(secret, response.get_data(as_text=True))
        with self.database.transaction() as db:
            db.execute("UPDATE ss_square_connections SET oauth_application_id='fictional-other-app' WHERE tenant_id=%s", (self.tenant,))
        mismatch = client.get('/api/square/status', base_url=self.config['PUBLIC_BASE_URL'], headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(mismatch.status_code, 200); self.assertEqual(mismatch.json['connection_status'], 'application_mismatch')
        self.assertFalse(mismatch.json['connected']); self.assertIsNone(mismatch.json['merchant_id'])
        self.assertEqual(mismatch.json['renewal']['application_binding'], 'mismatch')
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        boundary = renewal_health({'token_obtained_at': now - timedelta(days=8), 'expires_at': now + timedelta(days=20), 'oauth_application_id': 'fictional-app'}, 'fictional-app', now=now)
        self.assertEqual(boundary['age_status'], 'due'); self.assertFalse(boundary['stale'])
        self.transport.post.assert_not_called()

    def test_schema7_upgrade_to11_and_repeat_migration_leave_legacy_age_and_app_unknown(self):
        raw = (Path(__file__).resolve().parent / 'tests/fixtures/schema7.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), 'c5a10046808fe3a3b8ba7c668dafd4b01af3165e4b523143f219b23dfe82e903')
        with self.namespace() as database:
            tenant = uuid.uuid4()
            with database.transaction() as db:
                db.execute(raw.decode('utf-8'), prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional legacy renewal store')", (tenant, 'legacy-' + tenant.hex))
                db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                    VALUES (%s,%s,'production','fictional-legacy-ciphertext',%s)''', (tenant, 'legacy-' + tenant.hex, self.expiry))
                old = db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s', (tenant,)).fetchone()
                db.execute('INSERT INTO ss_schema_versions(version) VALUES (10)')
            self.assertFalse(database.ready())
            database.migrate(); self.assertTrue(database.ready())
            with database.transaction() as db:
                current = db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s', (tenant,)).fetchone()
                self.assertEqual({key: current[key] for key in old}, old)
                for key in ('oauth_application_id', 'token_obtained_at', 'renewal_lease_token', 'renewal_lease_until', 'renewal_attempted_at', 'renewal_retry_after', 'renewal_error_code'):
                    self.assertIsNone(current[key])
            database.migrate()
            with database.transaction() as db:
                self.assertEqual(db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s', (tenant,)).fetchone(), current)
