"""Derived receipt-cache integrity against fictional isolated SQLite data."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from station_store import Store


class ReceiptSummaryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)

    @staticmethod
    def receipt(ident='R-1'):
        return {'id': ident, 'title': 'Fictional intake', 'source_name': 'fixture.csv', 'mode': 'trade',
                'pickup_pending': True, 'inventory_status': 'not_received', 'inventory_intent': 'new',
                'cards': [{'qty': 2, 'final_price': 1.125}, {'qty': 3, 'final_price': 2.1},
                          {'qty': 100, 'final_price': 99, 'include': False}],
                'purchase': {'type': 'credit', 'amount': 12.34, 'at': 'fixture-time', 'intake_total': 20.0, 'scope': 'hosted',
                             'items': [{'private_detail': 'not a public summary'}]},
                'intent': {'items': ['not public']}, 'customer_reference': 'not needed for history',
                'shared_trade': {'state': 'pending', 'trade_id': 'trade-fixture', 'message': 'Review needed',
                                 'body': {'customer_id': 'never-list'}, 'intent': {'items': ['never-list']}},
                'files': {'buy': 'fictional.pdf'}}

    def snapshot(self):
        with self.store.db() as db:
            existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY 1')]
                    for table in ('receipts', 'receipt_summaries', 'events', 'operations', 'settings',
                                  'credit_customers', 'credit_entries', 'credit_requests', 'credit_reservations') if table in existing}

    def test_saved_cache_counts_money_and_compact_payload(self):
        original = self.store.save_receipt(self.receipt(), ('created', 'fictional event'))
        public = self.store.summaries()[0]
        self.assertEqual((public['rows'], public['units'], public['retail']), (2, 5, 8.55))
        self.assertEqual(public['purchase'], {'type': 'credit', 'amount': 12.34, 'at': 'fixture-time', 'intake_total': 20.0, 'scope': 'hosted'})
        self.assertEqual(public['shared_trade'], {'state': 'pending', 'trade_id': 'trade-fixture', 'message': 'Review needed'})
        self.assertNotIn('cards', public)
        self.assertNotIn('intent', public)
        self.assertNotIn('customer_reference', public)
        self.assertEqual(self.store.receipt('R-1'), original)
        self.assertEqual(self.store.pickup_queue(), [{'id': 'R-1', 'title': 'Fictional intake', 'source_name': 'fixture.csv', 'mode': 'trade'}])
        public['purchase']['amount'] = 999
        self.assertEqual(self.store.summaries()[0]['purchase']['amount'], 12.34)

    def test_review_flag_uses_included_cards_missing_prices_and_nonempty_selection(self):
        receipt = self.receipt()
        receipt['cards'][0]['reviewed'] = True
        receipt['cards'][1]['reviewed'] = True
        receipt['cards'][2]['reviewed'] = False
        self.store.save_receipt(receipt)
        self.assertTrue(self.store.summaries()[0]['all_reviewed'])
        receipt['cards'][0]['price_missing'] = True
        self.store.save_receipt(receipt)
        self.assertFalse(self.store.summaries()[0]['all_reviewed'])
        receipt['cards'][0]['price_missing'] = False
        receipt['cards'][1]['reviewed'] = False
        self.store.save_receipt(receipt)
        self.assertFalse(self.store.summaries()[0]['all_reviewed'])
        for card in receipt['cards']:
            card['include'] = False
        receipt['purchase']['scope'] = 'local'
        self.store.save_receipt(receipt)
        public = self.store.summaries()[0]
        self.assertFalse(public['all_reviewed'])
        self.assertEqual((public['rows'], public['units'], public['retail']), (0, 0, 0))
        self.assertEqual(public['purchase']['scope'], 'local')
        self.assertNotIn('items', public['purchase'])

    def test_receipt_cache_and_event_rollback_together_on_either_failure(self):
        receipt = self.store.save_receipt(self.receipt(), ('created', 'fixture'))
        with self.store.db() as db:
            db.execute("INSERT INTO operations VALUES ('R-1','fixture-key','exact request','exact response')")
        for table in ('receipt_summaries', 'events'):
            with self.subTest(failure_table=table):
                before = self.snapshot()
                with self.store.db() as db:
                    db.execute(f"CREATE TRIGGER reject_fixture BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'fixture rollback'); END")
                receipt['title'] = 'Must not commit'
                receipt['cards'][0]['qty'] = 17
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'fixture rollback'):
                    self.store.save_receipt(receipt, ('changed', 'must roll back'))
                with self.store.db() as db:
                    db.execute('DROP TRIGGER reject_fixture')
                self.assertEqual(self.snapshot(), before)

    def test_stale_malformed_and_overbroad_cache_repair_only_derived_rows(self):
        self.store.save_receipt(self.receipt(), ('created', 'fixture'))
        from credit_tracker import Tracker
        tracker = Tracker(SimpleNamespace(store=self.store))
        customer = tracker.add_customer({'name': 'Fictional cache ledger', 'requestId': 'fixture-customer',
                                         'square_reference': 'fictional-square-customer'})
        tracker.transaction({'customerId': customer['id'], 'requestId': 'fixture-opening',
                             'kind': 'opening', 'amount': 12.34, 'reference': 'fictional-opening', 'version': 0})
        tracker.reserve(customer['id'], 2, 'fixture-reserve', 'fictional-order')
        with self.store.db() as db:
            db.execute("INSERT INTO operations VALUES ('R-1','fixture-key','exact request','exact response')")
            db.execute("INSERT INTO settings VALUES ('fixture-ledger-marker','12345')")
        base = self.snapshot()
        malformed = [None, {'invalid_json': True}, {'version': Store.SUMMARY_VERSION + 1}, {'stamp': 'stale'},
                     {'payload': {'id': 'R-1', 'rows': 1, 'units': 1, 'retail': 1, 'cards': ['leak']}},
                     {'payload': {'id': 'R-1', 'rows': 1, 'units': 1, 'retail': 1,
                                  'shared_trade': {'state': 'pending', 'intent': {'items': ['leak']}}}}]
        for corruption in malformed:
            with self.subTest(corruption=corruption):
                with self.store.db() as db:
                    if corruption is None:
                        db.execute('DELETE FROM receipt_summaries')
                    elif 'invalid_json' in corruption:
                        db.execute("UPDATE receipt_summaries SET body='not-json',digest='invalid'")
                    elif 'version' in corruption:
                        db.execute('UPDATE receipt_summaries SET format_version=?', (corruption['version'],))
                    elif 'stamp' in corruption:
                        db.execute('UPDATE receipt_summaries SET receipt_updated=?', (corruption['stamp'],))
                    else:
                        body = json.dumps(corruption['payload'])
                        db.execute('UPDATE receipt_summaries SET body=?,digest=?', (body, hashlib.sha256(body.encode()).hexdigest()))
                self.assertEqual(self.store.summaries()[0]['units'], 5)
                after = self.snapshot()
                for table in base:
                    if table != 'receipt_summaries':
                        self.assertEqual(after[table], base[table])
                self.assertEqual(after['receipt_summaries'], base['receipt_summaries'])

    def test_out_of_band_source_replacement_invalidates_cache_even_same_timestamp(self):
        receipt = self.store.save_receipt(self.receipt())
        for verb in ('UPDATE', 'REPLACE'):
            with self.subTest(verb=verb):
                receipt['cards'][0]['qty'] += 1
                with self.store.db() as db:
                    if verb == 'UPDATE':
                        db.execute('UPDATE receipts SET body=? WHERE id=?', (json.dumps(receipt), receipt['id']))
                    else:
                        db.execute('INSERT OR REPLACE INTO receipts(id,source_hash,updated,body,deleted_at,pickup_pending) VALUES (?,?,?,?,?,?)',
                                   (receipt['id'], '', receipt['updated'], json.dumps(receipt), None, 1))
                    self.assertEqual(db.execute('SELECT count(*) FROM receipt_summaries').fetchone()[0], 0)
                self.assertEqual(self.store.summaries()[0]['units'], receipt['cards'][0]['qty'] + 3)

    def test_delete_restore_limits_and_pickup_order_are_preserved(self):
        receipts = []
        timestamps = [f'2026-01-01T00:00:00.{index:06d}Z' for index in range(160)]
        with patch('station_store.now', side_effect=timestamps):
            for index in range(160):
                receipts.append(self.store.save_receipt(self.receipt(f'R-{index:03d}')))
        self.assertEqual([r['id'] for r in self.store.summaries()], [r['id'] for r in reversed(receipts[-150:])])
        self.assertEqual([r['id'] for r in self.store.pickup_queue()], [r['id'] for r in receipts[:150]])
        victim = receipts[0]
        victim['deleted_at'] = 'fixture-delete'
        self.store.save_receipt(victim)
        self.assertNotIn(victim['id'], [r['id'] for r in self.store.summaries()])
        self.assertNotIn(victim['id'], [r['id'] for r in self.store.pickup_queue()])
        victim.pop('deleted_at')
        self.store.save_receipt(victim)
        self.assertEqual(self.store.summaries()[0]['id'], victim['id'])
        self.assertEqual(len(self.store.pickup_queue()), 150)

    def test_legacy_repair_reads_one_original_at_a_time_and_warm_poll_reads_none(self):
        for index in range(6):
            self.store.save_receipt(self.receipt(f'R-{index}'))
        with self.store.db() as db:
            db.execute('DELETE FROM receipt_summaries')
        queries = []
        original_db = self.store.db

        @contextmanager
        def traced():
            with original_db() as db:
                db.set_trace_callback(queries.append)
                yield db

        with patch.object(self.store, 'db', traced):
            self.assertEqual(len(self.store.summaries()), 6)
            reads = [q for q in queries if q.startswith('SELECT body FROM receipts')]
            self.assertEqual(len(reads), 6)
            self.assertTrue(all('WHERE id=' in q for q in reads))
            queries.clear()
            self.store.summaries()
            self.store.pickup_queue()
            self.assertFalse(any(q.startswith('SELECT body FROM receipts') for q in queries))

    def test_legacy_schema_body_and_money_journals_survive_cache_creation(self):
        legacy = self.root / 'legacy'
        legacy.mkdir()
        record = self.receipt('legacy')
        raw = json.dumps(record)
        connection = sqlite3.connect(legacy / 'station.sqlite3')
        try:
            connection.execute('CREATE TABLE receipts(id TEXT PRIMARY KEY,source_hash TEXT,updated TEXT NOT NULL,body TEXT NOT NULL)')
            connection.execute('INSERT INTO receipts VALUES (?,?,?,?)', ('legacy', 'fixture-hash', '2026-01-01', raw))
            connection.commit()
        finally:
            connection.close()
        store = Store(legacy)
        self.assertEqual(store.summaries()[0]['retail'], 8.55)
        with store.db() as db:
            self.assertEqual(db.execute('SELECT body FROM receipts').fetchone()[0], raw)
            self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 0)
        self.assertEqual(store.find_source('fixture-hash'), 'legacy')

    def test_warm_poll_works_with_database_writes_disabled(self):
        self.store.save_receipt(self.receipt())
        original_db = self.store.db

        @contextmanager
        def readonly():
            with original_db() as db:
                db.execute('PRAGMA query_only=ON')
                yield db

        with patch.object(self.store, 'db', readonly):
            self.assertEqual(self.store.summaries()[0]['units'], 5)
            self.assertEqual(self.store.pickup_queue()[0]['id'], 'R-1')

    def test_two_store_instances_never_pair_new_body_with_old_cache(self):
        self.store.save_receipt(self.receipt())
        other = Store(self.root)
        errors = []

        def writer():
            try:
                for index in range(20):
                    receipt = other.receipt('R-1')
                    receipt['title'] = 'version-' + str(index)
                    receipt['cards'][0]['qty'] = index + 1
                    other.save_receipt(receipt, ('changed', 'fictional'))
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=writer)
        worker.start()
        for _ in range(20):
            public = self.store.summaries()[0]
            if public['title'].startswith('version-'):
                self.assertEqual(public['units'], int(public['title'].split('-')[1]) + 4)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(self.store.summaries()[0]['units'], 23)


if __name__ == '__main__':
    unittest.main()
