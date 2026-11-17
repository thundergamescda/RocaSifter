"""Observer race/rollback acceptance using fictional Square and isolated PostgreSQL.

Source definitions only until the independently recorded hosted CI run. The
helper-only fixture does not introduce inherited acceptance cases.
"""
from copy import deepcopy

import psycopg

from hosted.cleanup_observer import CleanupObserverError
import test_hosted_cleanup_observer_postgres as fixture


class PostgreSQLCleanupObserverAdversarial(fixture._CleanupObserverFixture):
    def _assert_no_admitted_read(self):
        collections = self.read_collections()
        self.assertTrue(collections)
        for row in collections:
            self.assertEqual(row['outcome'], 'fenced')
            self.assertFalse(row['coverage_complete'])
            self.assertEqual((row['count_rows'], row['nonzero_count'], row['context']), (0, 0, {}))
            self.assertIsNone(row['before_context_hash'])
            self.assertIsNone(row['after_context_hash'])
        with self.database.transaction() as db:
            count = db.execute('''SELECT count(*) AS n FROM ss_cleanup_collection_counts c
                JOIN ss_cleanup_collections f ON f.id=c.collection_id WHERE f.tenant_id=%s''',
                (self.tenant,)).fetchone()['n']
        self.assertEqual(count, 0)
        projection = self.read_projection()
        self.assertTrue(projection is None or projection['last_complete_id'] is None)

    def test_inventory_notification_during_collection_fences_read_but_retains_positive_fact(self):
        self.owner_configure()
        work = self.enqueue_manual()
        positive = deepcopy(self.reader.result['counts'])
        positive[0]['quantity'] = '7'
        self.reader.on_collect = lambda access: self.deliver_inventory(positive)
        result = self.observer.run_once(work_id=work['id'])
        self.assertEqual(result['status'], 'fenced')
        self._assert_no_admitted_read()
        with self.database.transaction() as db:
            facts = db.execute('''SELECT f.quantity FROM ss_cleanup_inventory_facts f
                JOIN ss_cleanup_inventory_receipts r ON r.receipt_cursor=f.receipt_cursor
                WHERE r.tenant_id=%s ORDER BY f.receipt_cursor,f.ordinal''', (self.tenant,)).fetchall()
        self.assertEqual([row['quantity'] for row in facts], ['7'])
        current = self.observer.status(self.actor)
        self.assertFalse(current['coverage_current'])
        self.assertEqual(current['pending_notification_count'], 1)

    def test_expired_lease_takeover_and_late_old_finish_cannot_replace_new_coverage(self):
        self.owner_configure()
        work = self.enqueue_manual()
        old = self.observer.claim(work['id'])
        self.assertIsNotNone(old)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_cleanup_collection_work SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (work['id'],))
        new = self.observer.claim(work['id'])
        self.assertIsNotNone(new)
        self.assertNotEqual(old.attempt['id'], new.attempt['id'])
        self.assertGreater(new.attempt['attempt_cursor'], old.attempt['attempt_cursor'])
        value = deepcopy(self.reader.result)
        access = self.reader.access(self.tenant)
        self.assertEqual(self.observer.finish(new, value, access)['status'], 'complete')
        projection, finished = self.read_projection(), self.read_work(work['id'])
        history = self.read_collections()
        self.assertEqual([row['outcome'] for row in history], ['fenced', 'complete'])
        with self.assertRaises(CleanupObserverError) as rejected:
            self.observer.finish(old, value, access)
        self.assertEqual((rejected.exception.status, rejected.exception.code), (409, 'OBSERVER_REQUEST_CONFLICT'))
        self.assertEqual(self.read_projection(), projection)
        self.assertEqual(self.read_work(work['id']), finished)
        self.assertEqual(self.read_collections(), history)
        self.assertTrue(self.observer.status(self.actor)['coverage_current'])

    def test_owner_can_withdraw_while_worker_off_and_inflight_read_is_fenced(self):
        self.owner_configure(background=True)
        work = self.enqueue_manual()

        def withdraw(access):
            self.observer.enabled = False
            before = self.reader.access_calls
            withdrawn = self.owner_configure(enabled=False, background=False)
            self.assertFalse(withdrawn['enabled'])
            self.assertFalse(withdrawn['background_enabled'])
            self.assertEqual(self.reader.access_calls, before)

        self.reader.on_collect = withdraw
        result = self.observer.run_once(work_id=work['id'])
        self.assertEqual(result['status'], 'fenced')
        self._assert_no_admitted_read()
        self.assertEqual(self.observer.status(self.actor)['state'], 'disabled')

    def test_logout_during_manual_collection_prevents_evidence_publication(self):
        self.owner_configure()
        work = self.enqueue_manual()

        def logout(access):
            with self.database.transaction() as db:
                db.execute('DELETE FROM ss_sessions WHERE tenant_id=%s AND token_hash=%s',
                    (self.tenant, self.actor['session_hash']))

        self.reader.on_collect = logout
        self.assertEqual(self.observer.run_once(work_id=work['id'])['status'], 'fenced')
        self._assert_no_admitted_read()
        self.assertEqual(self.read_work(work['id'])['state'], 'blocked')

    def test_projection_write_failure_rolls_back_counts_acknowledgment_and_job_finish(self):
        self.owner_configure()
        self.deliver_inventory()
        work = self.enqueue_manual()
        claim = self.observer.claim(work['id'])
        self.assertIsNotNone(claim)
        original_work, original_projection = self.read_work(work['id']), self.read_projection()
        original_collections = self.read_collections()
        value, access = deepcopy(self.reader.result), self.reader.access(self.tenant)
        # This fixture-owned trigger adds a failure; no production evidence guard
        # is disabled or removed. Its schema is the isolated ss_test namespace.
        with self.database.transaction() as db:
            db.execute('''CREATE FUNCTION ss_test_observer_projection_failure() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'fictional projection failure'; END $$''')
            db.execute('''CREATE TRIGGER ss_test_observer_projection_failure BEFORE INSERT OR UPDATE
                ON ss_cleanup_current_projection FOR EACH ROW EXECUTE FUNCTION ss_test_observer_projection_failure()''')
        try:
            with self.assertRaises(psycopg.Error):
                self.observer.finish(claim, value, access)
            self.assertEqual(self.read_work(work['id']), original_work)
            self.assertEqual(self.read_projection(), original_projection)
            self.assertEqual(self.read_collections(), original_collections)
            with self.database.transaction() as db:
                count = db.execute('''SELECT count(*) AS n FROM ss_cleanup_collection_counts c
                    JOIN ss_cleanup_collections f ON f.id=c.collection_id WHERE f.tenant_id=%s''',
                    (self.tenant,)).fetchone()['n']
            self.assertEqual(count, 0)
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER ss_test_observer_projection_failure ON ss_cleanup_current_projection')
                db.execute('DROP FUNCTION ss_test_observer_projection_failure()')
        self.assertEqual(self.observer.finish(claim, value, self.reader.access(self.tenant))['status'], 'complete')
        self.assertEqual(self.read_work(work['id'])['state'], 'done')
        self.assertEqual(self.read_projection()['acknowledged_hwm'], work['receipt_hwm'])

    def test_exact_finish_replay_is_idempotent_and_changed_evidence_conflicts(self):
        self.owner_configure()
        work = self.enqueue_manual()
        claim = self.observer.claim(work['id'])
        self.assertIsNotNone(claim)
        value, access = deepcopy(self.reader.result), self.reader.access(self.tenant)
        first = self.observer.finish(claim, value, access)
        projection, finished, history = self.read_projection(), self.read_work(work['id']), self.read_collections()
        self.assertEqual((first['status'], first['duplicate'], len(history)), ('complete', False, 1))
        repeat = self.observer.finish(claim, deepcopy(value), access)
        self.assertEqual((repeat['status'], repeat['duplicate']), ('complete', True))
        changed = deepcopy(value)
        changed['counts'][0]['quantity'] = '7'
        with self.assertRaises(CleanupObserverError) as rejected:
            self.observer.finish(claim, changed, access)
        self.assertEqual((rejected.exception.status, rejected.exception.code), (409, 'OBSERVER_REQUEST_CONFLICT'))
        self.assertEqual(self.read_projection(), projection)
        self.assertEqual(self.read_work(work['id']), finished)
        self.assertEqual(self.read_collections(), history)
