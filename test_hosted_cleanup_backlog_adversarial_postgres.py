"""Large bounded-backlog acceptance in an isolated PostgreSQL fixture.

Bulk seed helpers create fictional typed receipt/fact/intent rows directly. They
exercise processing/storage boundaries, not signature verification or real Square
delivery. No immutable guard is disabled. Source definitions are UNRUN.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid
from unittest.mock import Mock

import psycopg
from psycopg.types.json import Jsonb

from hosted.cleanup_observer import ObserverClaim
from hosted.square import Square
import test_hosted_cleanup_observer_postgres as fixture


def seed_backlog(case, *, scope, receipts, facts_each=1, first_quantity='7'):
    """Share the fixture seed without importing/inheriting acceptance classes."""
    return PostgreSQLCleanupBacklogAdversarial._seed_backlog(
        case, scope=scope, receipts=receipts, facts_each=facts_each, first_quantity=first_quantity)


class PostgreSQLCleanupBacklogAdversarial(fixture._CleanupObserverFixture):
    def _drain_to_complete(self, *, max_steps=150):
        preparation_steps = 0
        identity = ('id', 'tenant_id', 'application_id', 'environment', 'merchant_id',
                    'authorization_generation', 'config_version', 'receipt_hwm', 'receipt_manifest',
                    'manifest_hash', 'trigger_kind', 'principal_kind', 'staff_id', 'session_hash',
                    'requested_role', 'receipt_basis_version', 'work_kind', 'receipt_basis_hash')
        for _ in range(max_steps):
            work = self.read_work()
            self.assertIn(work['state'], ('queued', 'reading'))
            before_calls, before_access = len(self.reader.calls), self.reader.access_calls
            projection = self.read_projection()
            result = self.observer.run_once(work_id=work['id'])
            saved = self.read_work(work['id'])
            self.assertEqual({name: saved[name] for name in identity}, {name: work[name] for name in identity})
            if work['work_kind'] == 'receipt_prepare':
                preparation_steps += 1
                self.assertEqual((len(self.reader.calls), self.reader.access_calls), (before_calls, before_access))
                self.assertEqual(saved['attempts'], 0)
                after = self.read_projection()
                for name in ('acknowledged_hwm', 'last_complete_id', 'latest_collection_id'):
                    self.assertEqual(after[name], projection[name])
            else:
                self.assertEqual(work['work_kind'], 'collection')
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(len(self.reader.calls), before_calls + 1)
                self.assertFalse(result['continuous_zero_verified'])
                self.assertFalse(result['deletion_enabled'])
                return preparation_steps
        self.fail('Bounded preparation did not reach one admitted read within the fixture budget.')

    def test_more_than_ten_thousand_receipts_resume_without_provider_calls_during_preparation(self):
        receipts = self._seed_backlog(receipts=10001)
        self.owner_configure()
        work = self.enqueue_manual()
        self.assertEqual((work['receipt_basis_version'], work['work_kind']), (2, 'receipt_prepare'))
        target = work['receipt_hwm']
        self.assertEqual(target, receipts[-1]['receipt_cursor'])
        status = self.observer.status(self.actor)
        self.assertEqual(status['receipt_processing_pending_count'], 1001)
        self.assertFalse(status['receipt_processing_pending_count_exact'])
        self.assertFalse(status['coverage_current'])
        steps = self._drain_to_complete()
        self.assertGreaterEqual(steps, 100)
        self.assertEqual(len(self.reader.calls), 1)
        status = self.observer.status(self.actor)
        self.assertTrue(status['coverage_current'])
        self.assertEqual((status['pending_notification_count'], status['receipt_processing_pending_count']), (0, 0))
        self.assertTrue(status['pending_notification_count_exact'])
        self.assertTrue(status['receipt_processing_pending_count_exact'])
        self.assertEqual(self.read_projection()['acknowledged_hwm'], target)
        self.assertIn('notification_positive', self.read_collections()[-1]['receipt_flags'])
        with self.database.transaction() as db:
            saved = db.execute('''SELECT receipt_cursor,body_hash,fact_count FROM ss_cleanup_inventory_receipts
                WHERE tenant_id=%s ORDER BY receipt_cursor''', (self.tenant,)).fetchall()
        self.assertEqual(saved, receipts)

    def test_automatic_worker_step_folds_only_one_batch_and_admits_another_store(self):
        receipts = self._seed_backlog(receipts=350)
        self.owner_configure(background=True)
        for expected in (100, 200):
            self.observer.run_once()
            with self.database.transaction() as db:
                head = db.execute('SELECT * FROM ss_cleanup_receipt_progress WHERE tenant_id=%s',
                                  (self.tenant,)).fetchone()
                batches = db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE tenant_id=%s AND NOT is_genesis ORDER BY high_inclusive',
                                     (self.tenant,)).fetchall()
            self.assertEqual(head['processed_hwm'], receipts[expected - 1]['receipt_cursor'])
            self.assertEqual(len(batches), expected // 100)
            self.assertTrue(all(len(row['receipt_manifest']) <= 100 for row in batches))
            self.assertEqual((self.reader.calls, self.reader.access_calls), ([], 0))
            self.assertEqual(self.read_projection()['acknowledged_hwm'], 0)
            self.assertIsNone(self.read_projection()['last_complete_id'])
        # A new store must be admitted while this store still has preparation
        # work. Reuse OAuth's helper-only setup, with the same server cipher key.
        # This object is not run as another acceptance case or class fixture.
        other = fixture._CleanupObserverFixture()
        other.database, other.schema, other.url = self.database, self.schema, self.url
        try:
            fixture.oauth_fixture.PostgreSQLReceivingOAuthAcceptance.setUp(other)
            other.config['APP_ENCRYPTION_KEYS'] = self.config['APP_ENCRYPTION_KEYS']
            other.square = Square(self.database, other.config, other.transport)
            other.square._token = Mock(return_value=(other.tokens, other.expiry))
            other.reconnect_owner()
            other.observer = self.observer
            other.owner_configure(background=True)
            other_receipts = seed_backlog(other, scope=other.scope_row(), receipts=10, first_quantity='0')
            self.observer.run_once()
            other_work = other.read_work()
            self.assertEqual(other_work['work_kind'], 'receipt_prepare')
            with self.database.transaction() as db:
                heads = db.execute('SELECT tenant_id,processed_hwm FROM ss_cleanup_receipt_progress WHERE tenant_id=ANY(%s)',
                                   ([self.tenant, other.tenant],)).fetchall()
            by_tenant = {row['tenant_id']: row['processed_hwm'] for row in heads}
            self.assertEqual(by_tenant, {self.tenant: receipts[299]['receipt_cursor'], other.tenant: 0})
            self.observer.run_once()
            with self.database.transaction() as db:
                heads = db.execute('SELECT tenant_id,processed_hwm FROM ss_cleanup_receipt_progress WHERE tenant_id=ANY(%s)',
                                   ([self.tenant, other.tenant],)).fetchall()
            by_tenant = {row['tenant_id']: row['processed_hwm'] for row in heads}
            self.assertEqual(by_tenant, {self.tenant: receipts[299]['receipt_cursor'],
                                       other.tenant: other_receipts[-1]['receipt_cursor']})
            self.assertEqual((self.reader.calls, self.reader.access_calls), ([], 0))
            other_child = other.read_work()
            self.assertEqual(other_child['work_kind'], 'collection')
            self.assertEqual(self.observer.run_once(work_id=other_child['id'])['status'], 'complete')
            self.assertTrue(self.observer.status(other.actor)['coverage_current'])
        finally:
            other.doCleanups()
        self._drain_to_complete()
        self.assertTrue(self.observer.status(self.actor)['coverage_current'])

    def test_more_than_one_hundred_thousand_facts_keep_positive_history_without_poisoning_next_interval(self):
        receipts = self._seed_backlog(receipts=101, facts_each=1000)
        self.owner_configure()
        self.enqueue_manual()
        self.assertGreater(self._drain_to_complete(), 0)
        first = self.read_collections()[-1]
        self.assertIn('notification_positive', first['receipt_flags'])
        self.enqueue_manual()
        self.assertEqual(self._drain_to_complete(), 0)
        history = self.read_collections()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0], first)
        self.assertNotIn('notification_positive', history[-1]['receipt_flags'])
        with self.database.transaction() as db:
            totals = db.execute('''SELECT count(*) AS facts,count(*) FILTER (WHERE f.quantity::NUMERIC>0) AS positives
                FROM ss_cleanup_inventory_facts f JOIN ss_cleanup_inventory_receipts r ON r.receipt_cursor=f.receipt_cursor
                WHERE r.tenant_id=%s''', (self.tenant,)).fetchone()
        self.assertEqual((totals['facts'], totals['positives']), (101000, 1))
        self.assertEqual(self.read_projection()['acknowledged_hwm'], receipts[-1]['receipt_cursor'])

    def test_owner_opt_out_mid_preparation_stops_progress_without_acknowledging_receipts(self):
        self._seed_backlog(receipts=250)
        self.owner_configure()
        work = self.enqueue_manual()
        self.observer.run_once(work_id=work['id'])
        before = self.observer.status(self.actor)
        self.assertEqual(before['receipt_processing_pending_count'], 50)
        self.assertTrue(before['receipt_processing_pending_count_exact'])
        self.owner_configure(enabled=False)
        self.observer.run_once(work_id=work['id'])
        after = self.observer.status(self.actor)
        self.assertEqual(after['receipt_processing_pending_count'], 50)
        self.assertEqual(after['state'], 'disabled')
        self.assertFalse(after['coverage_current'])
        self.assertEqual(self.read_work(work['id'])['state'], 'blocked')
        self.assertEqual(self.read_projection()['acknowledged_hwm'], 0)
        self.assertIsNone(self.read_projection()['last_complete_id'])
        self.assertEqual((self.reader.calls, self.reader.access_calls), ([], 0))

    def test_logged_out_manual_preparation_never_borrows_enabled_background_principal(self):
        self._seed_backlog(receipts=250)
        self.owner_configure(background=True)
        work = self.enqueue_manual()
        self.observer.run_once(work_id=work['id'])
        with self.database.transaction() as db:
            db.execute('DELETE FROM ss_sessions WHERE tenant_id=%s AND token_hash=%s',
                       (self.tenant, self.actor['session_hash']))
        self.observer.run_once(work_id=work['id'])
        buyer = self.add_actor('buyer')
        status = self.observer.status(buyer)
        self.assertTrue(status['background_enabled'])
        self.assertEqual(status['receipt_processing_pending_count'], 50)
        self.assertFalse(status['coverage_current'])
        saved = self.read_work(work['id'])
        self.assertEqual((saved['state'], saved['principal_kind'], saved['session_hash']),
                         ('blocked', 'manual', self.actor['session_hash']))
        self.assertEqual(self.read_projection()['acknowledged_hwm'], 0)
        self.assertEqual(self.read_collections(), [])
        self.assertEqual((self.reader.calls, self.reader.access_calls), ([], 0))
        with self.database.transaction() as db:
            count = db.execute('SELECT count(*) AS n FROM ss_cleanup_collection_work WHERE tenant_id=%s',
                               (self.tenant,)).fetchone()['n']
        self.assertEqual(count, 1)

    def test_progress_update_failure_rolls_back_new_batch_and_work_then_can_resume(self):
        self._seed_backlog(receipts=350)
        self.owner_configure()
        work = self.enqueue_manual()
        before_work = self.read_work(work['id'])
        with self.database.transaction() as db:
            before_head = db.execute('SELECT * FROM ss_cleanup_receipt_progress WHERE tenant_id=%s',
                                     (self.tenant,)).fetchone()
            before_batches = db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE tenant_id=%s ORDER BY high_inclusive,id',
                                        (self.tenant,)).fetchall()
            db.execute('''CREATE FUNCTION ss_test_backlog_head_failure() RETURNS trigger LANGUAGE plpgsql
                AS $$ BEGIN RAISE EXCEPTION 'fictional receipt head failure'; END $$''')
            db.execute('''CREATE TRIGGER ss_test_backlog_head_failure BEFORE UPDATE ON ss_cleanup_receipt_progress
                FOR EACH ROW EXECUTE FUNCTION ss_test_backlog_head_failure()''')
        try:
            with self.assertRaises(psycopg.Error):
                self.observer.run_once(work_id=work['id'])
            with self.database.transaction() as db:
                after_head = db.execute('SELECT * FROM ss_cleanup_receipt_progress WHERE tenant_id=%s',
                                        (self.tenant,)).fetchone()
                after_batches = db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE tenant_id=%s ORDER BY high_inclusive,id',
                                           (self.tenant,)).fetchall()
            self.assertEqual(after_head, before_head)
            self.assertEqual(after_batches, before_batches)
            self.assertEqual(self.read_work(work['id']), before_work)
            self.assertEqual(self.read_collections(), [])
            self.assertEqual(self.read_projection()['acknowledged_hwm'], 0)
            self.assertEqual((self.reader.calls, self.reader.access_calls), ([], 0))
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER ss_test_backlog_head_failure ON ss_cleanup_receipt_progress')
                db.execute('DROP FUNCTION ss_test_backlog_head_failure()')
        self._drain_to_complete()
        self.assertTrue(self.observer.status(self.actor)['coverage_current'])

    def test_concurrent_preparation_keeps_one_chain_and_one_child_without_provider_calls(self):
        receipts = self._seed_backlog(receipts=250)
        self.owner_configure()
        work = self.enqueue_manual()
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.observer.run_once, work_id=work['id']) for _ in range(2)]
            for call in calls:
                call.result(timeout=60)
        self.assertEqual((self.reader.calls, self.reader.access_calls), ([], 0))
        with self.database.transaction() as db:
            batches = db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE tenant_id=%s ORDER BY high_inclusive,id',
                                 (self.tenant,)).fetchall()
            children = db.execute('SELECT * FROM ss_cleanup_collection_work WHERE parent_work_id=%s',
                                  (work['id'],)).fetchall()
            progress = db.execute('SELECT * FROM ss_cleanup_receipt_progress WHERE tenant_id=%s',
                                  (self.tenant,)).fetchone()
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['principal_kind'], work['principal_kind'])
        self.assertEqual(children[0]['session_hash'], work['session_hash'])
        self.assertEqual(progress['processed_hwm'], receipts[-1]['receipt_cursor'])
        self.assertTrue(batches[0]['is_genesis'])
        self.assertEqual(batches[0]['receipt_manifest'], [])
        members = []
        for previous, current in zip(batches, batches[1:]):
            self.assertEqual((current['previous_id'], current['previous_hash']),
                             (previous['id'], previous['chain_hash']))
            self.assertEqual(current['low_exclusive'], previous['high_inclusive'])
            self.assertLessEqual(len(current['receipt_manifest']), 100)
            members.extend(current['receipt_manifest'])
        self.assertEqual(members, [{'receipt_cursor': row['receipt_cursor'], 'body_hash': row['body_hash']} for row in receipts])
        self.assertEqual(self._drain_to_complete(), 0)
        self.assertEqual(len(self.reader.calls), 1)

    def test_legacy_queued_fact_budget_recovers_into_new_work_without_rewriting_old_basis(self):
        self._seed_backlog(receipts=101, facts_each=1000)
        self.owner_configure(background=True)
        old, _ = self._seed_legacy_work()
        self.enqueue_manual()
        retired = self.read_work(old['id'])
        self.assertEqual(retired['state'], 'superseded')
        for name in ('authorization_generation', 'config_version', 'receipt_hwm', 'receipt_manifest',
                     'manifest_hash', 'receipt_basis_version', 'principal_kind', 'staff_id', 'session_hash'):
            self.assertEqual(retired[name], old[name])
        self.assertEqual(old['receipt_basis_version'], 1)
        self.assertEqual(self.read_work()['receipt_basis_version'], 2)
        self._drain_to_complete()
        self.assertTrue(self.observer.status(self.actor)['coverage_current'])

    def test_legacy_live_fact_budget_fences_own_lease_and_preserves_metadata_before_recovery(self):
        self._seed_backlog(receipts=101, facts_each=1000)
        self.owner_configure(background=True)
        old, claim = self._seed_legacy_work(reading=True)
        result = self.observer.finish(claim, deepcopy(self.reader.result), self.reader.access(self.tenant))
        self.assertEqual(result['status'], 'fenced')
        retired = self.read_work(old['id'])
        self.assertEqual(retired['state'], 'blocked')
        self.assertIsNone(retired['lease_token'])
        self.assertIsNone(retired['lease_until'])
        self.assertEqual(retired['receipt_manifest'], old['receipt_manifest'])
        self.assertEqual(retired['manifest_hash'], old['manifest_hash'])
        history = self.read_collections()
        self.assertEqual(len(history), 1)
        self.assertEqual((history[0]['outcome'], history[0]['count_rows'], history[0]['context']), ('fenced', 0, {}))
        self.assertIn('receipt_basis_upgrade_required', history[0]['reason_codes'])
        self.assertEqual(self.read_projection()['acknowledged_hwm'], 0)
        self.assertIsNone(self.read_projection()['last_complete_id'])
        self.enqueue_manual()
        self._drain_to_complete()
        self.assertEqual(self.read_collections()[0], history[0])
        self.assertTrue(self.observer.status(self.actor)['coverage_current'])

    def _seed_legacy_work(self, *, reading=False):
        """Register an authentic v12-shaped fixture header using v1 defaults."""
        scope = self.scope_row()
        context = self.square._token_context()
        access = self.reader.access(self.tenant) if reading else None
        lease = uuid.uuid4() if reading else None
        until = datetime.now(timezone.utc) + timedelta(minutes=2) if reading else None
        with self.database.transaction() as db:
            self.square._notice_barrier(db, configuration=context)
            self.assertRegex(db.execute('SELECT current_schema() AS name').fetchone()['name'],
                             r'^ss_test_[a-f0-9]{32}$')
            rows = db.execute('''SELECT receipt_cursor,body_hash FROM ss_cleanup_inventory_receipts
                WHERE tenant_id=%s AND authorization_generation=%s ORDER BY receipt_cursor''',
                (scope['tenant_id'], scope['authorization_generation'])).fetchall()
            manifest = [{'receipt_cursor': row['receipt_cursor'], 'body_hash': row['body_hash']} for row in rows]
            digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            config = db.execute('''SELECT config_version FROM ss_cleanup_observer_config
                WHERE tenant_id=%s AND authorization_generation=%s ORDER BY config_version DESC LIMIT 1''',
                (scope['tenant_id'], scope['authorization_generation'])).fetchone()
            values = dict(scope, id=uuid.uuid4(), config=config['config_version'], hwm=rows[-1]['receipt_cursor'],
                          manifest=Jsonb(manifest), digest=digest, state='reading' if reading else 'queued',
                          attempts=1 if reading else 0, lease=lease, until=until)
            work = db.execute('''INSERT INTO ss_cleanup_collection_work
                (id,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,receipt_hwm,
                 receipt_manifest,manifest_hash,trigger_kind,principal_kind,state,attempts,lease_token,lease_until)
                VALUES (%(id)s,%(tenant_id)s,%(application_id)s,%(environment)s,%(merchant_id)s,%(authorization_generation)s,
                    %(config)s,%(hwm)s,%(manifest)s,%(digest)s,'notification','platform',%(state)s,%(attempts)s,%(lease)s,%(until)s)
                RETURNING *''', values).fetchone()
            if not reading:
                return work, None
            notice = self.square._notice_cursor(db, scope['merchant_id'], configuration=context)
            values.update(work_id=work['id'], notice=notice, access=self.observer._stable_access(access, work))
            attempt = db.execute('''INSERT INTO ss_cleanup_collection_attempts
                (id,work_id,attempt,tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,
                 receipt_hwm,manifest_hash,notice_cursor,access_digest,lease_until)
                VALUES (%(lease)s,%(work_id)s,1,%(tenant_id)s,%(application_id)s,%(environment)s,%(merchant_id)s,
                    %(authorization_generation)s,%(config)s,%(hwm)s,%(digest)s,%(notice)s,%(access)s,%(until)s)
                RETURNING *''', values).fetchone()
        return work, ObserverClaim(dict(work), dict(attempt), access, context)

    def _seed_backlog(self, *, receipts, facts_each=1, first_quantity='7', scope=None):
        if (type(receipts) is not int or not 1 <= receipts <= 12000 or
                type(facts_each) is not int or not 1 <= facts_each <= 1000 or
                receipts * facts_each > 200000 or first_quantity not in ('0', '7', '-3')):
            raise ValueError('Invalid bounded fictional fixture size.')
        scope = self.scope_row() if scope is None else scope
        prefix = 'fictional-bulk-' + uuid.uuid4().hex + '-'
        rows = [{'ordinal': n, 'body_hash': hashlib.sha256((prefix + str(n)).encode()).hexdigest()}
                for n in range(1, receipts + 1)]
        with self.database.transaction() as db:
            self.assertRegex(db.execute('SELECT current_schema() AS name').fetchone()['name'],
                             r'^ss_test_[a-f0-9]{32}$')
            db.execute('''INSERT INTO ss_cleanup_inventory_receipts
                (application_id,environment,event_id,merchant_id,event_type,body_hash,provider_created_at,
                 resource_id,payload_status,fact_count,tenant_id,authorization_generation,binding_status,refresh_queued)
                SELECT %s,%s,%s||seed.ordinal::TEXT,%s,'inventory.count.updated',seed.body_hash,
                    '2020-08-14T15:51:00.246373287Z','fictional-bulk-counts','counts_present',%s,%s,%s,'active',TRUE
                FROM jsonb_to_recordset(%s) AS seed(ordinal INTEGER,body_hash TEXT) ORDER BY seed.ordinal''',
                (scope['application_id'], scope['environment'], prefix, scope['merchant_id'], facts_each,
                 scope['tenant_id'], scope['authorization_generation'], Jsonb(rows)))
            db.execute('''INSERT INTO ss_cleanup_inventory_facts
                (receipt_cursor,ordinal,catalog_object_id,catalog_object_type,location_id,state,quantity,provider_calculated_at)
                SELECT r.receipt_cursor,f.ordinal,'fictional-variation','ITEM_VARIATION','fictional-location','IN_STOCK',
                    CASE WHEN r.event_id=%s AND f.ordinal=0 THEN %s ELSE '0' END,
                    '2020-08-14T15:51:00.246373287Z'
                FROM ss_cleanup_inventory_receipts r CROSS JOIN LATERAL generate_series(0,r.fact_count-1) AS f(ordinal)
                WHERE r.tenant_id=%s AND r.authorization_generation=%s AND left(r.event_id,%s)=%s''',
                (prefix + '1', first_quantity, scope['tenant_id'], scope['authorization_generation'], len(prefix), prefix))
            db.execute('''INSERT INTO ss_cleanup_refresh_queue
                (receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,binding_status)
                SELECT receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,'active'
                FROM ss_cleanup_inventory_receipts WHERE tenant_id=%s AND authorization_generation=%s
                    AND left(event_id,%s)=%s''',
                (scope['tenant_id'], scope['authorization_generation'], len(prefix), prefix))
            saved = db.execute('''SELECT receipt_cursor,body_hash,fact_count FROM ss_cleanup_inventory_receipts
                WHERE tenant_id=%s AND authorization_generation=%s AND left(event_id,%s)=%s
                ORDER BY receipt_cursor''',
                (scope['tenant_id'], scope['authorization_generation'], len(prefix), prefix)).fetchall()
        self.assertEqual(len(saved), receipts)
        self.assertEqual(sum(row['fact_count'] for row in saved), receipts * facts_each)
        return saved
