"""Authored real-PostgreSQL inventory ingress acceptance; UNRUN until admitted.

Owned ss_test_UUID schemas, fictional signed notifications and OAuth only. No
Square requests, cleanup execution, eligibility, stock or deletion is exercised.
Fixture module aliases avoid discovering inherited fixture TestCase classes.
"""
from concurrent.futures import ThreadPoolExecutor
import base64
import copy
import hashlib
import hmac
import json
from pathlib import Path
import threading
import uuid
from unittest.mock import patch

import psycopg
from psycopg import IsolationLevel, sql

from hosted.app import create_app
from hosted.database import Database
from hosted.security import ServiceError
from hosted.square import Square
import test_hosted_receiving_oauth_postgres as oauth_fixture
import test_hosted_settlement_postgres as settlement_fixture


class PostgreSQLCleanupInventoryAcceptance(settlement_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        oauth_fixture.PostgreSQLReceivingOAuthAcceptance.setUp(self)
        self.config['SQUARE_WEBHOOK_SIGNATURE_KEY'] = 'fictional-inventory-signature-key'

    def connect(self):
        state, _ = oauth_fixture.PostgreSQLReceivingOAuthAcceptance.begin(self)
        self.square.complete(self.actor, state, 'fictional-inventory-oauth-code')
        return oauth_fixture.PostgreSQLReceivingOAuthAcceptance.connection(self)

    def connection(self):
        return oauth_fixture.PostgreSQLReceivingOAuthAcceptance.connection(self)

    def fact(self, **changes):
        value = {'catalog_object_id': 'fictional-variation', 'catalog_object_type': 'ITEM_VARIATION',
                 'location_id': 'fictional-location', 'state': 'IN_STOCK', 'quantity': '0',
                 'calculated_at': '2020-08-14T15:51:00.246373287Z'}
        value.update(changes)
        return value

    def event(self, *, event=None, merchant=None, facts=None):
        return {'event_id': event or 'fictional-inventory-' + uuid.uuid4().hex,
                'merchant_id': merchant or self.tokens['merchant_id'], 'type': 'inventory.count.updated',
                'created_at': '2020-08-14T15:51:04.246373287Z',
                'data': {'type': 'inventory_counts', 'id': 'fictional-count-resource',
                         'object': {'inventory_counts': [self.fact()] if facts is None else facts}}}

    def signed(self, body, square=None):
        square = square or self.square
        raw = body if type(body) is bytes else json.dumps(body, separators=(',', ':')).encode('utf-8')
        signature = base64.b64encode(hmac.new(square.config['SQUARE_WEBHOOK_SIGNATURE_KEY'].encode(),
            square.webhook_url.encode() + raw, hashlib.sha256).digest()).decode()
        return raw, signature

    def receipt(self, event, square=None):
        square = square or self.square
        with self.database.transaction() as db:
            return db.execute('''SELECT * FROM ss_cleanup_inventory_receipts
                WHERE application_id=%s AND environment=%s AND event_id=%s''',
                (square.config['SQUARE_APPLICATION_ID'], square.environment, event)).fetchone()

    def facts(self, receipt):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_cleanup_inventory_facts WHERE receipt_cursor=%s ORDER BY ordinal',
                              (receipt['receipt_cursor'],)).fetchall()

    def intents(self):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_cleanup_refresh_queue WHERE tenant_id=%s ORDER BY receipt_cursor',
                              (self.tenant,)).fetchall()

    def assert_rejected(self, body, status=400):
        with self.assertRaises(ServiceError) as caught:
            self.square.ingest(*self.signed(body))
        self.assertEqual(caught.exception.status, status)

    def test_all_ordered_facts_keep_nonzero_states_item_estimates_and_wire_precision(self):
        connected = self.connect()
        rows = [self.fact(quantity='7.12500', is_estimated=False), self.fact(quantity='0'),
                self.fact(state='SOLD', quantity='-2', location_id='fictional-other-location'),
                self.fact(catalog_object_type='ITEM', catalog_object_id='fictional-parent', quantity='9'),
                self.fact(state='COMPOSED', quantity='1', is_estimated=True),
                self.fact(state='SUPPORTED_BY_NEWER_VERSION', quantity='3'),
                self.fact(quantity='5', calculated_at='2020-08-14T15:51:00.24637328+23:59')]
        event = self.event(facts=rows)
        event['created_at'] = '2020-08-14T15:51:04.246373287+23:59'
        raw, signature = self.signed(event)
        result = self.square.ingest(raw, signature)
        receipt = self.receipt(event['event_id'])
        self.assertEqual(result, {'accepted': True, 'duplicate': False, 'refresh_queued': True, 'refresh_executed': False})
        self.assertEqual(receipt['body_hash'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt['provider_created_at'], event['created_at'])
        self.assertGreater(receipt['received_at'].year, 2020)
        self.assertEqual(receipt['authorization_generation'], connected['authorization_generation'])
        self.assertEqual(receipt['binding_status'], 'active')
        self.assertEqual(receipt['fact_count'], len(rows))
        stored = self.facts(receipt)
        self.assertEqual(len(stored), len(rows))
        for ordinal, (actual, expected) in enumerate(zip(stored, rows)):
            self.assertEqual(actual['ordinal'], ordinal)
            for key in ('catalog_object_id', 'catalog_object_type', 'location_id', 'state', 'quantity'):
                self.assertEqual(actual[key], expected[key])
            self.assertEqual(actual['provider_calculated_at'], expected['calculated_at'])
            self.assertIs(actual['is_estimated'], expected.get('is_estimated'))
        intent = self.intents()[0]
        self.assertEqual(intent['receipt_cursor'], receipt['receipt_cursor'])
        self.assertEqual(intent['authorization_generation'], connected['authorization_generation'])
        self.assertEqual(intent['state'], 'queued_not_executed')
        self.transport.get.assert_not_called(); self.transport.post.assert_not_called()

    def test_absent_object_and_empty_counts_are_explicit_gaps_never_zero_facts(self):
        self.connect()
        for absent in (True, False):
            with self.subTest(object_absent=absent):
                event = self.event(facts=[])
                if absent:
                    del event['data']['object']
                self.square.ingest(*self.signed(event))
                receipt = self.receipt(event['event_id'])
                self.assertEqual(receipt['payload_status'], 'object_absent' if absent else 'empty_counts')
                self.assertEqual(receipt['fact_count'], 0)
                self.assertEqual(self.facts(receipt), [])
                self.assertTrue(receipt['refresh_queued'])
        self.assertTrue(all(row['state'] == 'queued_not_executed' for row in self.intents()))

    def test_exact_replay_keeps_old_receipt_generation_after_reconnect(self):
        old = self.connect(); event = self.event(); wire = self.signed(event)
        self.square.ingest(*wire)
        receipt = self.receipt(event['event_id']); facts = self.facts(receipt); intents = self.intents()
        current = self.connect()
        self.assertNotEqual(old['authorization_generation'], current['authorization_generation'])
        result = self.square.ingest(*wire)
        self.assertTrue(result['duplicate']); self.assertFalse(result['refresh_executed'])
        self.assertEqual(self.receipt(event['event_id']), receipt)
        self.assertEqual(self.facts(receipt), facts); self.assertEqual(self.intents(), intents)
        self.assertEqual(self.connection(), current)

    def test_unknown_receipt_is_never_rebound_when_first_connection_arrives(self):
        event = self.event(); wire = self.signed(event)
        self.assertFalse(self.square.ingest(*wire)['refresh_queued'])
        receipt = self.receipt(event['event_id'])
        self.assertEqual(receipt['binding_status'], 'unbound')
        self.assertIsNone(receipt['tenant_id']); self.assertIsNone(receipt['authorization_generation'])
        self.assertEqual(len(self.facts(receipt)), 1)
        self.connect()
        self.assertTrue(self.square.ingest(*wire)['duplicate'])
        self.assertEqual(self.receipt(event['event_id']), receipt); self.assertEqual(self.intents(), [])

    def test_inactive_binding_is_retained_once_without_work_or_later_rebinding(self):
        for condition in ('revoked', 'token_expired', 'environment_mismatch'):
            with self.subTest(condition=condition):
                before = self.connect()
                with self.database.transaction() as db:
                    if condition == 'revoked':
                        db.execute('UPDATE ss_square_connections SET revoked_at=clock_timestamp() WHERE tenant_id=%s', (self.tenant,))
                    elif condition == 'token_expired':
                        db.execute("UPDATE ss_square_connections SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s", (self.tenant,))
                    else:
                        db.execute("UPDATE ss_square_connections SET environment='sandbox' WHERE tenant_id=%s", (self.tenant,))
                event = self.event(); wire = self.signed(event)
                self.assertFalse(self.square.ingest(*wire)['refresh_queued'])
                receipt = self.receipt(event['event_id'])
                self.assertEqual(receipt['binding_status'], condition)
                self.assertEqual(receipt['tenant_id'], self.tenant)
                self.assertEqual(receipt['authorization_generation'], before['authorization_generation'])
                if condition == 'environment_mismatch':
                    with self.database.transaction() as db:
                        db.execute("UPDATE ss_square_connections SET environment='production' WHERE tenant_id=%s", (self.tenant,))
                self.connect(); self.assertTrue(self.square.ingest(*wire)['duplicate'])
                self.assertEqual(self.receipt(event['event_id']), receipt)
        self.assertEqual(self.intents(), [])

    def test_revocation_pending_records_positive_facts_but_creates_no_intent(self):
        old = self.connect()
        notice = {'event_id': 'fictional-revoke-' + uuid.uuid4().hex, 'merchant_id': self.tokens['merchant_id'],
                  'type': 'oauth.authorization.revoked'}
        self.square.ingest(*self.signed(notice))
        event = self.event(facts=[self.fact(quantity='4'), self.fact(quantity='0')])
        self.assertFalse(self.square.ingest(*self.signed(event))['refresh_queued'])
        receipt = self.receipt(event['event_id'])
        self.assertEqual(receipt['binding_status'], 'revocation_pending')
        self.assertEqual(receipt['authorization_generation'], old['authorization_generation'])
        self.assertEqual([row['quantity'] for row in self.facts(receipt)], ['4', '0'])
        self.assertEqual(self.intents(), [])
        self.connect(); self.assertTrue(self.square.ingest(*self.signed(event))['duplicate'])
        self.assertEqual(self.intents(), [])

    def test_changed_raw_bytes_merchant_or_nonzero_fact_conflict_without_overwrite(self):
        self.connect(); event = self.event(); raw, signature = self.signed(event)
        self.square.ingest(raw, signature)
        receipt = self.receipt(event['event_id']); facts = self.facts(receipt); intents = self.intents()
        positive = copy.deepcopy(event); positive['data']['object']['inventory_counts'][0]['quantity'] = '6'
        other = copy.deepcopy(event); other['merchant_id'] = 'fictional-other-merchant'
        for changed in (raw + b'\n', positive, other):
            with self.subTest(change=type(changed).__name__):
                self.assert_rejected(changed, 409)
                self.assertEqual(self.receipt(event['event_id']), receipt)
                self.assertEqual(self.facts(receipt), facts); self.assertEqual(self.intents(), intents)

    def test_application_and_environment_are_trusted_ingress_scope_not_event_fields(self):
        self.connect(); event = self.event()
        self.square.ingest(*self.signed(event))
        for changed_config in ({'SQUARE_APPLICATION_ID': 'fictional-other-app'}, {'SQUARE_ENVIRONMENT': 'sandbox'}):
            with self.subTest(config=changed_config):
                square = Square(self.database, self.config | changed_config, self.transport)
                result = square.ingest(*self.signed(event, square))
                self.assertFalse(result['duplicate'])
                receipt = self.receipt(event['event_id'], square)
                self.assertEqual(receipt['application_id'], square.config['SQUARE_APPLICATION_ID'])
                self.assertEqual(receipt['environment'], square.environment)
                self.assertEqual(receipt['refresh_queued'], square.environment == 'production')
        for field in ('application_id', 'environment', 'customer'):
            changed = copy.deepcopy(event); changed['event_id'] = 'fictional-forged-' + uuid.uuid4().hex; changed[field] = 'untrusted'
            self.assert_rejected(changed)
            self.assertIsNone(self.receipt(changed['event_id']))

    def test_new_receipt_after_rotation_uses_new_generation_without_rewriting_old_intent(self):
        old = self.connect(); first = self.event(); self.square.ingest(*self.signed(first)); before = self.intents()
        new = self.connect(); second = self.event(); self.square.ingest(*self.signed(second))
        after = self.intents()
        self.assertEqual(after[:1], before)
        self.assertEqual(after[0]['authorization_generation'], old['authorization_generation'])
        self.assertEqual(after[1]['authorization_generation'], new['authorization_generation'])
        self.assertEqual(self.receipt(second['event_id'])['provider_created_at'], second['created_at'])
        self.assertTrue(all(row['state'] == 'queued_not_executed' for row in after))

    def test_concurrent_exact_duplicates_commit_one_receipt_fact_set_and_intent(self):
        self.connect(); event = self.event(); wire = self.signed(event); barrier = threading.Barrier(2)
        def deliver():
            barrier.wait(timeout=10)
            return self.square.ingest(*wire)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(deliver) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sorted(row['duplicate'] for row in results), [False, True])
        self.assertEqual(len(self.facts(self.receipt(event['event_id']))), 1)
        self.assertEqual(len(self.intents()), 1)

    def test_concurrent_conflicting_signed_bodies_have_one_visible_conflict(self):
        self.connect(); event = self.event(); changed = copy.deepcopy(event)
        changed['data']['object']['inventory_counts'][0]['quantity'] = '9'
        barrier = threading.Barrier(2)
        def deliver(body):
            barrier.wait(timeout=10)
            try:
                return self.square.ingest(*self.signed(body))
            except ServiceError as error:
                return {'error_status': error.status}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(deliver, body) for body in (event, changed)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(row.get('error_status') == 409 for row in results), 1)
        self.assertEqual(sum(row.get('accepted') is True for row in results), 1)
        receipt = self.receipt(event['event_id'])
        expected = event if receipt['body_hash'] == hashlib.sha256(self.signed(event)[0]).hexdigest() else changed
        self.assertEqual(self.facts(receipt)[0]['quantity'], expected['data']['object']['inventory_counts'][0]['quantity'])
        self.assertEqual(len(self.intents()), 1)

    def test_concurrent_revocation_serializes_binding_and_leaves_only_inert_old_generation_work(self):
        before = self.connect(); event = self.event(facts=[self.fact(quantity='5')])
        notice = {'event_id': 'fictional-race-revoke-' + uuid.uuid4().hex,
                  'merchant_id': self.tokens['merchant_id'], 'type': 'oauth.authorization.revoked'}
        barrier = threading.Barrier(2)
        def deliver(body):
            barrier.wait(timeout=10)
            return self.square.ingest(*self.signed(body))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(deliver, body) for body in (event, notice)]
            for future in futures:
                self.assertTrue(future.result(timeout=20)['accepted'])
        receipt = self.receipt(event['event_id'])
        self.assertIn(receipt['binding_status'], ('active', 'revocation_pending'))
        self.assertEqual(receipt['authorization_generation'], before['authorization_generation'])
        self.assertEqual(self.facts(receipt)[0]['quantity'], '5')
        self.assertEqual(len(self.intents()), int(receipt['binding_status'] == 'active'))
        self.assertIsNotNone(self.connection()['pending_notice_cursor'])
        with self.assertRaises(ServiceError):
            self.square.access_token(self.tenant)
        self.assertTrue(all(row['state'] == 'queued_not_executed' for row in self.intents()))

    def test_invalid_signature_is_rejected_before_json_decode_or_persistence(self):
        with patch('hosted.square.signed_envelope', side_effect=AssertionError('Unauthenticated parser call')):
            with self.assertRaises(ServiceError) as caught:
                self.square.ingest(b'not-json', 'x' * 44)
        self.assertEqual(caught.exception.status, 403)
        self.assertEqual(self.intents(), [])

    def test_http_signed_inventory_dispatch_replay_signature_and_body_limit(self):
        self.connect(); event = self.event(facts=[self.fact(quantity='8'), self.fact(quantity='0')])
        raw, signature = self.signed(event)
        app = create_app(self.config | {'TESTING': True}, database=self.database, square_transport=self.transport)
        client = app.test_client()
        headers = {'x-square-hmacsha256-signature': signature}
        first = client.post('/webhooks/square', data=raw, content_type='application/json',
                            base_url=self.config['PUBLIC_BASE_URL'], headers=headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json, {'accepted': True, 'duplicate': False, 'refresh_queued': True, 'refresh_executed': False})
        receipt = self.receipt(event['event_id']); facts = self.facts(receipt); intents = self.intents()
        self.assertEqual(receipt['body_hash'], hashlib.sha256(raw).hexdigest())
        self.assertEqual([row['quantity'] for row in facts], ['8', '0'])
        duplicate = client.post('/webhooks/square', data=raw, content_type='application/json',
                                base_url=self.config['PUBLIC_BASE_URL'], headers=headers)
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(duplicate.json, first.json | {'duplicate': True})
        invalid = client.post('/webhooks/square', data=raw, content_type='application/json',
                              base_url=self.config['PUBLIC_BASE_URL'],
                              headers={'x-square-hmacsha256-signature': 'x' * 44})
        self.assertEqual(invalid.status_code, 403)
        oversized, oversized_signature = self.signed(raw + b' ' * (512 * 1024))
        refused = client.post('/webhooks/square', data=oversized, content_type='application/json',
                              base_url=self.config['PUBLIC_BASE_URL'],
                              headers={'x-square-hmacsha256-signature': oversized_signature})
        self.assertEqual(refused.status_code, 413)
        self.assertEqual(self.receipt(event['event_id']), receipt)
        self.assertEqual(self.facts(receipt), facts); self.assertEqual(self.intents(), intents)
        self.transport.get.assert_not_called(); self.transport.post.assert_not_called()

    def test_duplicate_keys_nonfinite_deep_oversized_and_invalid_utf8_json_fail_closed(self):
        self.connect(); event = self.event(); raw = self.signed(event)[0]
        cases = [raw.replace(b'"type":"inventory.count.updated"', b'"type":"payment.updated","type":"inventory.count.updated"'),
                 raw.replace(b'"quantity":"0"', b'"quantity":"8","quantity":"0"'),
                 raw.replace(b'"quantity":"0"', b'"quantity":NaN'),
                 raw.replace(b'"quantity":"0"', b'"quantity":1e999'),
                 b'{"type":"inventory.count.updated","nested":' + b'[' * 26 + b'0' + b']' * 26 + b'}',
                 raw + b' ' * (512 * 1024), b'\xff', b'[]']
        for index, body in enumerate(cases):
            with self.subTest(case=index):
                self.assert_rejected(body)
        self.assertIsNone(self.receipt(event['event_id'])); self.assertEqual(self.intents(), [])

    def test_malformed_typed_facts_and_unknown_fields_do_not_drop_partial_rows(self):
        self.connect()
        changes = ({'quantity': 0}, {'quantity': True}, {'quantity': 'NaN'}, {'quantity': '1e2'},
                   {'quantity': '0.000001'}, {'quantity': '1' * 27}, {'state': 'FUTURE_UNKNOWN'},
                   {'catalog_object_type': 'CUSTOMER'}, {'catalog_object_id': 'x' * 101},
                   {'location_id': 'bad\nlocation'}, {'calculated_at': '2020-02-30T00:00:00Z'},
                   {'calculated_at': '2020-01-01T00:00:00'}, {'calculated_at': '2020-01-01T00:00:00+01:99'},
                   {'calculated_at': '2020-01-01T00:00:00+24:00'}, {'calculated_at': '2020-01-01T24:00:00Z'},
                   {'calculated_at': '2020-01-01T00:60:00Z'}, {'calculated_at': '2020-01-01T00:00:60Z'},
                   {'is_estimated': 'false'}, {'customer_id': 'not-stored'})
        for change in changes:
            with self.subTest(change=change):
                event = self.event(facts=[self.fact(quantity='11'), self.fact(**change)])
                self.assert_rejected(event)
                self.assertIsNone(self.receipt(event['event_id']))
        missing = self.event(); del missing['data']['object']['inventory_counts'][0]['calculated_at']
        self.assert_rejected(missing)
        too_many = self.event(facts=[self.fact()] * 1001)
        self.assert_rejected(too_many)
        self.assertEqual(self.intents(), [])

    def test_receipts_facts_and_intents_are_immutable_and_cannot_gain_late_facts(self):
        self.connect(); event = self.event(); self.square.ingest(*self.signed(event))
        receipt = self.receipt(event['event_id']); facts = self.facts(receipt); intents = self.intents()
        self.database.migrate()
        self.assertEqual(self.receipt(event['event_id']), receipt)
        self.assertEqual(self.facts(receipt), facts); self.assertEqual(self.intents(), intents)
        for table in ('ss_cleanup_inventory_receipts', 'ss_cleanup_inventory_facts', 'ss_cleanup_refresh_queue'):
            for operation in ('UPDATE', 'DELETE'):
                with self.subTest(table=table, operation=operation):
                    with self.assertRaises(psycopg.Error):
                        with self.database.transaction() as db:
                            query = (sql.SQL('UPDATE {} SET receipt_cursor=receipt_cursor WHERE receipt_cursor=%s')
                                     if operation == 'UPDATE' else sql.SQL('DELETE FROM {} WHERE receipt_cursor=%s'))
                            db.execute(query.format(sql.Identifier(table)), (receipt['receipt_cursor'],))
        with self.assertRaises(psycopg.Error):
            with self.database.transaction() as db:
                db.execute('''INSERT INTO ss_cleanup_inventory_facts SELECT receipt_cursor,1,catalog_object_id,
                    catalog_object_type,location_id,state,quantity,provider_calculated_at,is_estimated
                    FROM ss_cleanup_inventory_facts WHERE receipt_cursor=%s''', (receipt['receipt_cursor'],))
        self.assertEqual(self.receipt(event['event_id']), receipt)
        self.assertEqual(self.facts(receipt), facts); self.assertEqual(self.intents(), intents)

    def test_queue_scope_cannot_upgrade_a_revoked_receipt_to_active_work(self):
        self.connect()
        with self.database.transaction() as db:
            db.execute('UPDATE ss_square_connections SET revoked_at=clock_timestamp() WHERE tenant_id=%s', (self.tenant,))
        event = self.event(); self.square.ingest(*self.signed(event)); receipt = self.receipt(event['event_id'])
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with self.database.transaction() as db:
                db.execute('''INSERT INTO ss_cleanup_refresh_queue
                    (receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation)
                    VALUES (%s,%s,%s,%s,%s,%s)''', (receipt['receipt_cursor'], receipt['application_id'],
                    receipt['environment'], receipt['merchant_id'], receipt['tenant_id'], receipt['authorization_generation']))
        self.assertEqual(self.intents(), [])

    def test_queue_insert_failure_rolls_back_receipt_and_all_count_facts(self):
        self.connect(); event = self.event(facts=[self.fact(quantity='12'), self.fact(quantity='0')])
        # An extra fictional failure trigger, not a disabled production guard.
        with self.database.transaction() as db:
            db.execute("""CREATE FUNCTION ss_fixture_reject_refresh() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'Fictional queue insert failure'; END $$;
                CREATE TRIGGER ss_fixture_reject_refresh BEFORE INSERT ON ss_cleanup_refresh_queue
                FOR EACH ROW EXECUTE FUNCTION ss_fixture_reject_refresh()""", prepare=False)
        try:
            with self.assertRaises(psycopg.Error):
                self.square.ingest(*self.signed(event))
            self.assertIsNone(self.receipt(event['event_id'])); self.assertEqual(self.intents(), [])
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER ss_fixture_reject_refresh ON ss_cleanup_refresh_queue')
                db.execute('DROP FUNCTION ss_fixture_reject_refresh()')
        self.assertFalse(self.square.ingest(*self.signed(event))['duplicate'])
        self.assertEqual(len(self.facts(self.receipt(event['event_id']))), 2)

    def test_first_inventory_sql_overrides_repeatable_read_before_receipt_snapshot(self):
        self.connect(); isolated = Database(self.url, schema=self.schema, max_size=1)
        self.addCleanup(isolated.close)
        with isolated.pool.connection() as connection:
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
        square = Square(isolated, self.config, self.transport); modes = []; original = square._notice_barrier
        def barrier(db):
            original(db)
            modes.append(db.execute('SHOW transaction_isolation').fetchone()['transaction_isolation'])
        with patch.object(square, '_notice_barrier', side_effect=barrier):
            square.ingest(*self.signed(self.event(), square))
        self.assertEqual(modes, ['read committed'])

    def test_inventory_ingress_preserves_financial_jobs_and_creates_no_cleanup_execution(self):
        self.connect(); identifier = 'fictional-financial-' + uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash)
                VALUES (%s,%s,'payment.updated','fictional-payment',%s)''', (identifier, self.tenant, '0' * 64))
            before = db.execute('SELECT * FROM ss_square_jobs WHERE event_id=%s', (identifier,)).fetchone()
        self.square.ingest(*self.signed(self.event()))
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT * FROM ss_square_jobs WHERE event_id=%s', (identifier,)).fetchone(), before)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_square_jobs WHERE tenant_id=%s', (self.tenant,)).fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_credit_entries WHERE tenant_id=%s', (self.tenant,)).fetchone()['n'], 0)
        self.assertEqual(self.intents()[0]['state'], 'queued_not_executed')
        self.transport.get.assert_not_called(); self.transport.post.assert_not_called()

    def test_schema7_to10_repeated_migration_preserves_legacy_and_backfills_no_observations(self):
        raw = (Path(__file__).resolve().parent / 'tests/fixtures/schema7.sql').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), 'c5a10046808fe3a3b8ba7c668dafd4b01af3165e4b523143f219b23dfe82e903')
        namespace = 'ss_test_' + uuid.uuid4().hex; created = False; upgraded = None
        try:
            with psycopg.connect(self.url, sslmode='require') as db:
                self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (namespace,)).fetchone())
                db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
            created = True; upgraded = Database(self.url, schema=namespace, max_size=1)
            tenant = uuid.uuid4()
            with upgraded.transaction() as db:
                db.execute(raw.decode('utf-8'), prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional inventory migration store')", (tenant, 'upgrade-' + tenant.hex))
                db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                    VALUES (%s,%s,'production','fictional-legacy-sealed',%s)''', (tenant, 'legacy-' + tenant.hex, self.expiry))
                db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash)
                    VALUES (%s,%s,'payment.updated','fictional-legacy-payment',%s)''', ('legacy-event-' + tenant.hex, tenant, '0' * 64))
                old_connection = db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s', (tenant,)).fetchone()
                old_job = db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s', (tenant,)).fetchone()
            self.assertFalse(upgraded.ready())
            # Version 9 alone must not advertise readiness for the new ingress.
            with upgraded.transaction() as db:
                db.execute('INSERT INTO ss_schema_versions(version) VALUES (9)')
            self.assertFalse(upgraded.ready())
            upgraded.migrate(); self.assertTrue(upgraded.ready())
            with upgraded.transaction() as db:
                connection = db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s', (tenant,)).fetchone()
                self.assertEqual({key: connection[key] for key in old_connection}, old_connection)
                self.assertIsNotNone(connection['authorization_generation'])
                self.assertEqual(db.execute('SELECT * FROM ss_square_jobs WHERE tenant_id=%s', (tenant,)).fetchone(), old_job)
            upgraded.migrate(); self.assertTrue(upgraded.ready())
            with upgraded.transaction() as db:
                self.assertEqual(db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s', (tenant,)).fetchone(), connection)
                self.assertIsNotNone(db.execute('SELECT 1 FROM ss_schema_versions WHERE version=10').fetchone())
                for table in ('ss_cleanup_inventory_receipts', 'ss_cleanup_inventory_facts', 'ss_cleanup_refresh_queue'):
                    self.assertEqual(db.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n'], 0)
        finally:
            if upgraded is not None:
                upgraded.close()
            if created:
                with psycopg.connect(self.url, sslmode='require') as db:
                    db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))
                with psycopg.connect(self.url, sslmode='require') as db:
                    self.assertIsNone(db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (namespace,)).fetchone())
