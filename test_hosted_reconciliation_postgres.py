"""Real job leasing/reconciliation acceptance in a disposable private PG schema."""
from concurrent.futures import ThreadPoolExecutor
import uuid
from hosted.reconciliation import Reconciler
from hosted.security import ServiceError
from test_hosted_settlement import order, returned_order, payment_refunds
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_reconciliation import FakeSquare


class PostgreSQLReconciliationAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        super().setUp()
        self.event = 'fixture-event-' + uuid.uuid4().hex
        self.square = FakeSquare({('orders', 'sale-1'): {'order': order()}})
        self.worker = Reconciler(self.database, self.square, enabled=True)
        self.add_job(self.event)

    def add_job(self, event, *, tenant=None, kind='order.updated', resource='sale-1'):
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash) VALUES (%s,%s,%s,%s,%s)',
                       (event, self.tenant if tenant is None else tenant, kind, resource, '0'*64))

    def job(self, event=None):
        with self.database.transaction() as db:
            return db.execute('SELECT * FROM ss_square_jobs WHERE event_id=%s', (event or self.event,)).fetchone()

    def run_once(self, worker=None):
        return (worker or self.worker).run_once(tenant_id=self.tenant)

    def test_disabled_worker_preserves_pending_job_and_balance(self):
        result = self.run_once(Reconciler(self.database, self.square))
        self.assertEqual((result['status'], self.job()['state'], self.snapshot()[0]['balance_cents']), ('disabled', 'pending', 5000))
        self.assertEqual(self.square.calls, [])

    def test_minimal_order_job_fetches_provider_and_consumes_once(self):
        result = self.run_once()
        self.assertEqual((result['status'], self.job()['state'], self.snapshot()[0]['balance_cents']), ('done', 'done', 3500))
        self.assertEqual(self.square.calls, [(self.tenant, 'orders', 'sale-1')])
        self.assertEqual(self.run_once()['status'], 'idle')

    def test_duplicate_events_for_same_order_do_not_duplicate_credit_consumption(self):
        self.add_job('duplicate-' + self.event)
        self.assertEqual(self.run_once()['status'], 'done')
        self.assertEqual(self.run_once()['status'], 'done')
        self.assertEqual((self.snapshot()[0]['balance_cents'], len(self.snapshot()[2])), (3500, 1))

    def test_concurrent_workers_claim_one_job_only(self):
        workers = [Reconciler(self.database, self.square, enabled=True) for _ in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda worker: worker.run_once(tenant_id=self.tenant), workers))
        self.assertEqual(sorted(result['status'] for result in results), ['done', 'idle'])
        self.assertEqual((self.snapshot()[0]['balance_cents'], len(self.snapshot()[2])), (3500, 1))

    def test_expired_lease_is_reclaimed_and_old_worker_cannot_ack_new_owner(self):
        first = self.worker._claim(self.tenant)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_square_jobs SET lease_until=now()-interval '1 second' WHERE event_id=%s", (self.event,))
        other = Reconciler(self.database, self.square, enabled=True)
        second = other._claim(self.tenant)
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        self.assertEqual(second['attempts'], 2)
        self.assertEqual(self.worker._finish(first, 'done', 'old_ack')['status'], 'lease_lost')
        self.assertEqual(self.job()['lease_token'], second['lease_token'])
        result = other._process(second)
        self.assertEqual(other._finish(second, 'done', result['code'])['status'], 'done')
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_tenant_filter_cannot_claim_another_store_job(self):
        foreign = uuid.uuid4();foreign_event = 'foreign-' + self.event
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)', (foreign, 'fixture-' + foreign.hex, 'Fictional other tenant'))
        self.add_job(foreign_event, tenant=foreign)
        self.assertEqual(self.run_once()['status'], 'done')
        self.assertEqual(self.job(foreign_event)['state'], 'pending')
        self.assertTrue(all(call[0] == self.tenant for call in self.square.calls))

    def test_transient_provider_failure_defers_job_without_releasing_hold(self):
        self.square.retrieve = lambda *args: (_ for _ in ()).throw(ServiceError('fixture timeout', 502))
        self.assertEqual(self.run_once()['status'], 'pending')
        job = self.job()
        self.assertEqual((job['state'], job['attempts'], job['lease_token']), ('pending', 1, None))
        self.assertEqual((self.snapshot()[0]['balance_cents'], self.snapshot()[1]), (5000, 'reserved'))

    def test_exhausted_retry_budget_creates_durable_exception_without_spending(self):
        self.square.retrieve = lambda *args: (_ for _ in ()).throw(ServiceError('fixture timeout', 502))
        result = self.run_once(Reconciler(self.database, self.square, enabled=True, max_attempts=1))
        self.assertEqual(result['status'], 'exception')
        with self.database.transaction() as db:
            row = db.execute('SELECT code,status FROM ss_reconciliation_exceptions WHERE tenant_id=%s AND source_key=%s', (self.tenant, 'job:' + self.event)).fetchone()
        self.assertEqual((row['code'], row['status']), ('provider_or_service_unavailable', 'open'))
        self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def test_unreserved_tracked_discount_is_review_not_blind_customer_debit(self):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_square_jobs SET resource_id='unreserved' WHERE event_id=%s", (self.event,))
        self.square.resources[('orders', 'unreserved')] = {'order': order() | {'id': 'unreserved'}}
        self.assertEqual(self.run_once()['code'], 'unreserved_credit_discount')
        self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def test_unrelated_ordinary_order_is_ignored_without_credit_effect(self):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_square_jobs SET resource_id='ordinary' WHERE event_id=%s", (self.event,))
        value = order();value.update(id='ordinary', discounts=[])
        self.square.resources[('orders', 'ordinary')] = {'order': value}
        self.assertEqual(self.run_once()['status'], 'done')
        self.assertEqual(self.snapshot()[0]['balance_cents'], 5000)

    def prepare_refund(self):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_square_jobs SET event_type='refund.updated',resource_id='payment-1_refund-1' WHERE event_id=%s", (self.event,))
        self.square.resources.update({('refunds', 'payment-1_refund-1'): {'refund': payment_refunds()[0] | {'payment_id': 'payment-1', 'order_id': 'return-order-1'}},
                                      ('payments', 'payment-1'): {'payment': {'id': 'payment-1', 'order_id': 'sale-1'}},
                                      ('orders', 'return-order-1'): {'order': returned_order()}})

    def test_refund_arriving_before_order_event_settles_then_restores_proven_credit(self):
        self.prepare_refund()
        self.assertEqual(self.run_once()['status'], 'done')
        self.assertEqual(self.snapshot()[0]['balance_cents'], 4000)
        with self.database.transaction() as db:
            count = db.execute('SELECT count(*) AS count FROM ss_credit_returns WHERE tenant_id=%s', (self.tenant,)).fetchone()['count']
        self.assertEqual(count, 1)

    def test_retry_after_consumption_before_refund_restores_without_second_debit(self):
        self.prepare_refund();original = self.square.retrieve;failed = False
        def fail_once(tenant, kind, ident):
            nonlocal failed
            if kind == 'orders' and ident == 'return-order-1' and not failed:
                failed = True;raise ServiceError('fixture interruption', 502)
            return original(tenant, kind, ident)
        self.square.retrieve = fail_once
        self.assertEqual(self.run_once()['status'], 'pending')
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)
        with self.database.transaction() as db:db.execute('UPDATE ss_square_jobs SET available_at=now() WHERE event_id=%s', (self.event,))
        self.assertEqual(self.run_once()['status'], 'done')
        self.assertEqual((self.snapshot()[0]['balance_cents'], len(self.snapshot()[2])), (4000, 1))

    def test_ambiguous_custom_refund_creates_review_without_credit_restoration(self):
        self.prepare_refund()
        self.square.resources[('orders', 'return-order-1')]['order']['returns'][0]['return_line_items'][0]['item_type'] = 'CUSTOM_AMOUNT'
        self.assertEqual(self.run_once()['status'], 'exception')
        self.assertEqual(self.snapshot()[0]['balance_cents'], 3500)

    def test_authorization_revocation_during_read_cancels_job_before_ledger_write(self):
        original = self.square.retrieve
        def revoke_during_read(tenant, kind, ident):
            with self.database.transaction() as db:
                db.execute('UPDATE ss_square_connections SET revoked_at=now() WHERE tenant_id=%s', (self.tenant,))
                db.execute("UPDATE ss_square_jobs SET state='canceled',last_error_code='authorization_revoked' WHERE event_id=%s", (self.event,))
            return original(tenant, kind, ident)
        self.square.retrieve = revoke_during_read
        self.assertEqual(self.run_once()['status'], 'lease_lost')
        self.assertEqual((self.job()['state'], self.snapshot()[0]['balance_cents']), ('canceled', 5000))


if __name__ == '__main__':
    import unittest
    unittest.main(verbosity=2)
