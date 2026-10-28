"""Real PostgreSQL reconciliation acceptance, only in an isolated fixture schema.

Run on the private hosting network with DATABASE_URL set. A skipped run is not
cloud evidence. No Square requests; no application tables outside the temporary
ss_test_<uuid> namespace are queried or modified.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import os
import unittest
import uuid
import psycopg
from psycopg import sql
from hosted.database import Database
from hosted.ledger import Ledger
from hosted.security import ServiceError
from hosted.settlement import Settlement
from hosted.settlement import record_reservation_terms
from hosted.test_fixtures import approved_checkout_fixture
from test_hosted_settlement import terms, order, returned_order, payment_refunds


class _PostgreSQLSettlementFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = os.environ.get('DATABASE_URL')
        if not cls.url:
            raise unittest.SkipTest('DATABASE_URL is required for private-network PostgreSQL acceptance.')
        cls.schema = 'ss_test_' + uuid.uuid4().hex
        with psycopg.connect(cls.url, sslmode='require') as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        try:
            cls.database = Database(cls.url, schema=cls.schema)
            cls.database.migrate()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'database'):
            cls.database.close()
        if hasattr(cls, 'schema'):
            with psycopg.connect(cls.url, sslmode='require') as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.tenant, self.customer, self.reservation = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',
                       (self.tenant, 'fixture-' + self.tenant.hex, 'Fictional reconciliation store'))
            db.execute('''INSERT INTO ss_customers(id,tenant_id,display_name,square_customer_id,privacy_basis,balance_cents)
                          VALUES (%s,%s,'Fictional customer','customer-1','fixture-only',5000)''',
                       (self.customer, self.tenant))
            db.execute('''INSERT INTO ss_reservations(id,tenant_id,customer_id,amount_cents,state,order_id,request_key)
                          VALUES (%s,%s,%s,2000,'reserved','sale-1','fixture-reserve')''',
                       (self.reservation, self.tenant, self.customer))
            self.binding = approved_checkout_fixture(db, self.tenant, self.customer)
            record_reservation_terms(db, self.tenant, self.reservation, self.binding,
                                     reserved_cents=2000, customer_id=self.customer)
        self.settlement = Settlement(self.database)

    def snapshot(self):
        with self.database.transaction() as db:
            customer = db.execute('SELECT balance_cents,version FROM ss_customers WHERE tenant_id=%s AND id=%s',
                                  (self.tenant, self.customer)).fetchone()
            reservation = db.execute('SELECT state FROM ss_reservations WHERE tenant_id=%s AND id=%s',
                                     (self.tenant, self.reservation)).fetchone()
            entries = db.execute("SELECT amount_cents,source_key FROM ss_credit_entries WHERE tenant_id=%s AND kind='discount_redeem' ORDER BY at",
                                 (self.tenant,)).fetchall()
        return customer, reservation['state'], entries

    def reconcile(self, value=None):
        return self.settlement.reconcile_order(self.tenant, self.reservation, order() if value is None else value)


class PostgreSQLSettlementAcceptance(_PostgreSQLSettlementFixture):
    def test_partial_applied_amount_consumes_once_and_releases_unused_hold(self):
        first = self.reconcile(); second = self.reconcile()
        self.assertFalse(first['duplicate']); self.assertTrue(second['duplicate'])
        customer, state, entries = self.snapshot()
        self.assertEqual((customer['balance_cents'], customer['version'], state), (3500, 1, 'consumed'))
        self.assertEqual(entries, [{'amount_cents': -1500, 'source_key': 'order:sale-1'}])
        account = Ledger(self.database).account({'tenant_id': self.tenant}, self.customer)['customer']
        self.assertEqual((account['reserved_cents'], account['available_cents']), (0, 3500))

    def test_cancel_releases_without_debit(self):
        value = order(); value['state'] = 'CANCELED'
        self.assertEqual(self.reconcile(value)['state'], 'released')
        customer, state, entries = self.snapshot()
        self.assertEqual((customer['balance_cents'], state, entries), (5000, 'released', []))

    def test_bad_authoritative_order_keeps_hold_as_exception_then_can_reconcile(self):
        value = order(); value['customer_id'] = 'another-customer'
        self.assertTrue(self.reconcile(value)['needs_review'])
        account = Ledger(self.database).account({'tenant_id': self.tenant}, self.customer)['customer']
        self.assertEqual((account['balance_cents'], account['reserved_cents'], account['available_cents']), (5000, 2000, 3000))
        self.assertEqual(self.reconcile()['state'], 'consumed')

    def test_amount_over_hold_never_debits_or_releases_credit(self):
        value = order(); value['discounts'][0]['applied_money']['amount'] = 2001
        self.assertEqual(self.reconcile(value)['code'], 'invalid_applied_credit')
        customer, state, entries = self.snapshot()
        self.assertEqual((customer['balance_cents'], state, entries), (5000, 'exception', []))

    def test_another_tenant_cannot_settle_reservation(self):
        with self.assertRaises(ServiceError) as error:
            self.settlement.reconcile_order(uuid.uuid4(), self.reservation, order(), terms=terms())
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def test_duplicate_concurrent_reconciliation_debits_exactly_once(self):
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.reconcile(), range(2)))
        self.assertEqual(sum(result['duplicate'] for result in results), 1)
        customer, state, entries = self.snapshot()
        self.assertEqual((customer['balance_cents'], state, len(entries)), (3500, 'consumed', 1))

    def test_late_open_notification_cannot_undo_consumed_state(self):
        self.reconcile(); value = order(); value['state'] = 'OPEN'; value['version'] = 1
        result = self.reconcile(value)
        self.assertEqual((result['state'], result['code'], result['needs_review']), ('consumed', 'already_consumed', False))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_conflicting_terminal_notification_cannot_restore_or_spend_again(self):
        self.reconcile(); value = order(); value['state'] = 'CANCELED'
        result = self.reconcile(value)
        self.assertEqual((result['state'], result['needs_review']), ('consumed', True))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_changed_completed_amount_is_audited_without_rewriting_consumption(self):
        self.reconcile(); value = order(); value['version'] = 4
        value['discounts'][0]['applied_money']['amount'] = 1000
        result = self.reconcile(value)
        self.assertEqual((result['state'], result['code'], result['needs_review']), ('consumed', 'terminal_order_conflict', True))
        customer, state, entries = self.snapshot()
        self.assertEqual((customer['balance_cents'], state, len(entries)), (3500, 'consumed', 1))
        with self.database.transaction() as db:
            count = db.execute("SELECT count(*) AS count FROM ss_audit WHERE tenant_id=%s AND action='credit-terminal-reconciliation-exception'", (self.tenant,)).fetchone()['count']
        self.assertEqual(count, 1)

    def test_stale_completed_snapshot_cannot_override_already_consumed_result(self):
        self.reconcile(); value = order(); value['version'] = 1
        value['discounts'][0]['applied_money']['amount'] = 500
        result = self.settlement.reconcile_order(self.tenant, self.reservation, value)
        self.assertEqual((result['state'], result['code'], result['needs_review']), ('consumed', 'already_consumed', False))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_released_order_completing_late_requires_review_without_new_debit(self):
        value = order(); value['state'] = 'CANCELED'; self.reconcile(value)
        result = self.reconcile()
        self.assertEqual((result['state'], result['code'], result['needs_review']), ('released', 'released_order_completed', True))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def test_other_reservations_remain_held_when_this_one_settles(self):
        with self.database.transaction() as db:
            second = uuid.uuid4()
            db.execute('''INSERT INTO ss_reservations(id,tenant_id,customer_id,amount_cents,state,order_id,request_key)
                          VALUES (%s,%s,%s,3000,'reserved','sale-2','other-hold')''',
                       (second, self.tenant, self.customer))
            record_reservation_terms(db, self.tenant, second, self.binding | {'order_id': 'sale-2'},
                                     reserved_cents=3000, customer_id=self.customer)
        self.reconcile()
        account = Ledger(self.database).account({'tenant_id': self.tenant}, self.customer)['customer']
        self.assertEqual((account['balance_cents'], account['reserved_cents'], account['available_cents']), (3500, 3000, 500))

    def test_simultaneous_exception_and_other_settlement_preserve_all_holds(self):
        second = uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_reservations(id,tenant_id,customer_id,amount_cents,state,order_id,request_key)
                          VALUES (%s,%s,%s,3000,'reserved','sale-2','other-hold')''',
                       (second, self.tenant, self.customer))
            record_reservation_terms(db, self.tenant, second, self.binding | {'order_id': 'sale-2'},
                                     reserved_cents=3000, customer_id=self.customer)
        bad = order();bad['discounts'][0]['applied_money']['amount'] = 2001
        valid = order();valid['id'] = 'sale-2';valid['discounts'][0]['applied_money']['amount'] = 3000
        def reconcile(index):
            if index == 0:return self.reconcile(bad)
            return self.settlement.reconcile_order(self.tenant, second, valid)
        with ThreadPoolExecutor(max_workers=2) as workers:results = list(workers.map(reconcile, (0, 1)))
        self.assertEqual({result['state'] for result in results}, {'exception', 'consumed'})
        account = Ledger(self.database).account({'tenant_id': self.tenant}, self.customer)['customer']
        self.assertEqual((account['balance_cents'], account['reserved_cents'], account['available_cents']), (2000, 2000, 0))

    def test_wrong_immutable_customer_or_amount_is_rejected_before_mutation(self):
        for patch in ({'square_customer_id': 'another-customer'}, {'reserved_cents': 1999}, {'order_id': 'another-order'}):
            with self.subTest(patch=patch), self.assertRaises(ServiceError):
                self.settlement.reconcile_order(self.tenant, self.reservation, order(), terms=replace(terms(), discount_uid='credit-uid', **patch))
        self.assertEqual((self.snapshot()[0]['balance_cents'], self.snapshot()[1]), (5000, 'reserved'))

    def test_error_at_final_audit_rolls_back_balance_entry_and_reservation_together(self):
        # Trigger exists only on this disposable schema's audit table.
        with self.database.transaction() as db:
            db.execute("""CREATE OR REPLACE FUNCTION fixture_fail_settlement_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                          BEGIN IF NEW.action='credit-reservation-consumed' THEN RAISE EXCEPTION 'fixture audit failure'; END IF;
                          RETURN NEW; END $$""")
            db.execute('CREATE TRIGGER fixture_fail_settlement BEFORE INSERT ON ss_audit FOR EACH ROW EXECUTE FUNCTION fixture_fail_settlement_audit()')
        try:
            with self.assertRaises(psycopg.errors.RaiseException):self.reconcile()
            customer, state, entries = self.snapshot()
            self.assertEqual((customer['balance_cents'], customer['version'], state, entries), (5000, 0, 'reserved', []))
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER fixture_fail_settlement ON ss_audit')
                db.execute('DROP FUNCTION fixture_fail_settlement_audit()')

    def test_missing_legacy_snapshot_is_durable_review_and_preserves_hold(self):
        legacy = uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_reservations(id,tenant_id,customer_id,amount_cents,state,order_id,request_key)
                          VALUES (%s,%s,%s,1000,'reserved','legacy-order','legacy-hold')''', (legacy, self.tenant, self.customer))
        result = self.settlement.reconcile_order(self.tenant, legacy, order() | {'id': 'legacy-order'})
        self.assertEqual((result['state'], result['code'], result['needs_review']), ('exception', 'reservation_terms_missing', True))
        with self.database.transaction() as db:
            count = db.execute("SELECT count(*) AS count FROM ss_reconciliation_exceptions WHERE tenant_id=%s AND code='reservation_terms_missing' AND status='open'", (self.tenant,)).fetchone()['count']
        self.assertEqual(count, 1);self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def test_immutable_terms_cannot_be_rewritten_or_deleted(self):
        for statement in ('UPDATE ss_reservation_terms SET terms_hash=%s WHERE tenant_id=%s AND reservation_id=%s',
                          'DELETE FROM ss_reservation_terms WHERE terms_hash<>%s AND tenant_id=%s AND reservation_id=%s'):
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.RaiseException):
                with self.database.transaction() as db:db.execute(statement, ('0'*64, self.tenant, self.reservation))
        self.assertEqual(self.reconcile()['state'], 'consumed')

    def test_current_store_policy_changes_do_not_rewrite_bound_order(self):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_tenants SET discount_id='new-discount',tax_policy_version=2 WHERE id=%s", (self.tenant,))
        self.assertEqual(self.reconcile()['state'], 'consumed')

    def test_revoked_square_authorization_preserves_hold_and_creates_review(self):
        with self.database.transaction() as db:
            db.execute('UPDATE ss_square_connections SET revoked_at=now() WHERE tenant_id=%s', (self.tenant,))
        result = self.reconcile()
        self.assertEqual((result['state'], result['code']), ('exception', 'square_connection_binding_changed'))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def owner(self):
        with self.database.transaction() as db:
            owner = db.execute("SELECT id FROM ss_staff WHERE tenant_id=%s AND role='owner' LIMIT 1", (self.tenant,)).fetchone()
        return {'tenant_id': self.tenant, 'staff_id': owner['id'], 'role': 'owner'}

    def test_new_unclassified_credit_does_not_cover_insufficient_approved_credit(self):
        actor = self.owner()
        Ledger(self.database).adjust(actor, self.customer, {'request_key': 'unknown-add', 'amount_cents': 1000,
             'kind': 'adjust_add', 'reason': 'Fictional unknown-origin correction', 'reference': 'TEST', 'version': 0})
        with self.assertRaises(ServiceError):
            Ledger(self.database).reserve(actor, self.customer, {'request_key': 'cannot-use-unknown',
                'amount_cents': 3500, 'order_id': 'sale-2'}, checkout_binding=self.binding | {'order_id': 'sale-2'})
        account = Ledger(self.database).account(actor, self.customer)['customer']
        self.assertEqual((account['balance_cents'], account['reserved_cents']), (6000, 2000))

    def test_manual_removal_burns_only_available_lots_without_touching_hold(self):
        actor = self.owner()
        Ledger(self.database).adjust(actor, self.customer, {'request_key': 'remove', 'amount_cents': 1000,
            'kind': 'adjust_remove', 'reason': 'Fictional correction', 'reference': 'TEST', 'version': 0})
        with self.database.transaction() as db:
            total = db.execute('SELECT SUM(remaining_cents) AS amount FROM ss_credit_lots WHERE tenant_id=%s AND customer_id=%s', (self.tenant, self.customer)).fetchone()['amount']
        self.assertEqual(total, 4000)
        account = Ledger(self.database).account(actor, self.customer)['customer']
        self.assertEqual((account['balance_cents'], account['reserved_cents'], account['available_cents']), (4000, 2000, 2000))

    def test_policy_revocation_blocks_new_reservations_but_preserves_existing_commitment(self):
        actor = self.owner()
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_credit_policy_revocations(tenant_id,policy_id,policy_version,revoked_by,reason) VALUES (%s,'fixture-origin-policy',1,%s,'Fictional revocation')", (self.tenant, actor['staff_id']))
        self.assertEqual(self.reconcile()['state'], 'consumed')
        with self.assertRaises(ServiceError):
            Ledger(self.database).reserve(actor, self.customer, {'request_key': 'revoked-origin', 'amount_cents': 100,
                'order_id': 'sale-2'}, checkout_binding=self.binding | {'order_id': 'sale-2'})

    @staticmethod
    def return_fixture(ident=1, amount=500):
        value = returned_order();proof = payment_refunds()
        value['id'] = 'return-order-' + str(ident)
        value['returns'][0]['uid'] = 'return-' + str(ident)
        value['returns'][0]['return_discounts'][0]['applied_money']['amount'] = amount
        value['refunds'][0]['id'] = 'refund-' + str(ident)
        proof[0]['id'] = 'payment-1_refund-' + str(ident)
        return value, proof

    def test_itemized_credit_return_restores_once_and_preserves_origin_lot(self):
        self.reconcile();value, proof = self.return_fixture()
        first = self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
        second = self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
        self.assertEqual((first['state'], first['amount_cents'], second['duplicate']), ('restored', 500, True))
        with self.database.transaction() as db:
            lots = db.execute('SELECT origin_code,policy_id,policy_version,remaining_cents,parent_lot_id FROM ss_credit_lots WHERE tenant_id=%s AND customer_id=%s ORDER BY created_at,id', (self.tenant, self.customer)).fetchall()
            restored = db.execute('SELECT SUM(restored_cents) AS amount FROM ss_credit_lot_reservations WHERE tenant_id=%s AND reservation_id=%s', (self.tenant, self.reservation)).fetchone()['amount']
        self.assertEqual(sum(lot['remaining_cents'] for lot in lots), 4000)
        self.assertEqual(restored, 500)
        self.assertEqual(len(lots), 2)
        self.assertTrue(all(lot['origin_code'] == 'fixture-verified-origin' and lot['policy_version'] == 1 for lot in lots))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 4000)

    def test_concurrent_return_replay_restores_exactly_once(self):
        self.reconcile();value, proof = self.return_fixture()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof), range(2)))
        self.assertEqual(sum(result['duplicate'] for result in results), 1)
        self.assertEqual(self.snapshot()[0]['balance_cents'], 4000)

    def test_concurrent_distinct_returns_cannot_exceed_original_credit(self):
        self.reconcile()
        def restore(ident):
            value, proof = self.return_fixture(ident, 1000)
            return self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
        with ThreadPoolExecutor(max_workers=2) as workers:results = list(workers.map(restore, (1, 2)))
        self.assertEqual({result['state'] for result in results}, {'restored', 'review'})
        self.assertEqual(self.snapshot()[0]['balance_cents'], 4500)

    def test_custom_cash_refund_is_review_only_and_never_restores_credit(self):
        self.reconcile();value, proof = self.return_fixture()
        value['returns'][0]['return_line_items'][0]['item_type'] = 'CUSTOM_AMOUNT'
        result = self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
        self.assertEqual((result['state'], result['needs_review']), ('review', True))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_another_tenant_cannot_restore_original_settlement(self):
        self.reconcile();value, proof = self.return_fixture()
        with self.assertRaises(ServiceError):self.settlement.reconcile_return(uuid.uuid4(), 'sale-1', value, proof)
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_changed_completed_return_amount_never_rewrites_prior_credit(self):
        self.reconcile();value, proof = self.return_fixture()
        self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
        value['version'] = 2;value['returns'][0]['return_discounts'][0]['applied_money']['amount'] = 600
        result = self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
        self.assertEqual((result['state'], result['code']), ('review', 'terminal_return_conflict'))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 4000)

    def test_error_at_final_return_audit_rolls_back_ledger_lots_and_return_evidence(self):
        self.reconcile();value, proof = self.return_fixture()
        with self.database.transaction() as db:
            db.execute("""CREATE OR REPLACE FUNCTION fixture_fail_return_audit() RETURNS trigger LANGUAGE plpgsql AS $$
                          BEGIN IF NEW.action='credit-return-restored' THEN RAISE EXCEPTION 'fixture return audit failure'; END IF;
                          RETURN NEW; END $$""")
            db.execute('CREATE TRIGGER fixture_fail_return BEFORE INSERT ON ss_audit FOR EACH ROW EXECUTE FUNCTION fixture_fail_return_audit()')
        try:
            with self.assertRaises(psycopg.errors.RaiseException):self.settlement.reconcile_return(self.tenant, 'sale-1', value, proof)
            self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)
            with self.database.transaction() as db:
                restored = db.execute('SELECT SUM(restored_cents) AS amount FROM ss_credit_lot_reservations WHERE tenant_id=%s AND reservation_id=%s', (self.tenant, self.reservation)).fetchone()['amount']
                count = db.execute('SELECT count(*) AS count FROM ss_credit_returns WHERE tenant_id=%s', (self.tenant,)).fetchone()['count']
            self.assertEqual((restored, count), (0, 0))
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER fixture_fail_return ON ss_audit');db.execute('DROP FUNCTION fixture_fail_return_audit()')


if __name__ == '__main__':unittest.main(verbosity=2)
