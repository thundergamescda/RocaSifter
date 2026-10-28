"""Guarded worker dispatch/retry tests. All Square resources are fictional."""
from copy import deepcopy
from unittest.mock import Mock
import unittest
import uuid
from hosted.reconciliation import Reconciler, ResourceError, LeaseLost
from hosted.security import ServiceError
from test_hosted_settlement import order, returned_order, payment_refunds


class FakeSquare:
    def __init__(self, resources=None):
        self.resources = resources or {}; self.calls = []
    def configured(self):return True
    def retrieve(self, tenant, kind, ident):
        self.calls.append((tenant, kind, ident))
        return deepcopy(self.resources[(kind, ident)])


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tenant = uuid.uuid4()
        self.job = {'event_id': 'event-1', 'tenant_id': self.tenant, 'event_type': 'order.updated',
                    'resource_id': 'sale-1', 'lease_token': uuid.uuid4(), 'attempts': 1}
        self.square = FakeSquare({('orders', 'sale-1'): {'order': order()}})
        self.db = Mock(); self.worker = Reconciler(self.db, self.square, enabled=True)
        self.worker._claim = Mock(return_value=self.job)
        self.worker._heartbeat = Mock()
        self.worker._finish = Mock(side_effect=lambda job, state, code, **kw: {'status': state, 'code': code, **kw})
        self.worker._order = Mock(return_value={'state': 'consumed', 'code': 'verified_completed_order'})

    def test_disabled_by_default_never_queries_database_or_provider(self):
        worker = Reconciler(self.db, self.square)
        self.assertEqual(worker.run_once(), {'status': 'disabled', 'processed': False})
        self.db.transaction.assert_not_called();self.assertEqual(self.square.calls, [])

    def test_missing_oauth_configuration_never_claims_a_job(self):
        self.square.configured = lambda: False
        self.assertEqual(self.worker.run_once()['status'], 'not_configured')
        self.worker._claim.assert_not_called()

    def test_worker_settings_require_explicit_boolean_and_bounded_limits(self):
        for options in ({'enabled': 1}, {'lease_seconds': 1}, {'lease_seconds': 601}, {'max_attempts': True}, {'max_attempts': 21}):
            with self.subTest(options=options), self.assertRaises(ValueError):Reconciler(self.db, self.square, **options)

    def test_tenant_filter_is_validated_before_claim(self):
        with self.assertRaises(ValueError):self.worker.run_once(tenant_id='not-a-uuid')
        self.worker._claim.assert_not_called()

    def test_order_event_uses_current_tenant_provider_resource_not_webhook_object(self):
        self.job['object'] = {'order': order() | {'customer_id': 'forged'}}
        self.assertEqual(self.worker.run_once()['status'], 'done')
        self.assertEqual(self.square.calls, [(self.tenant, 'orders', 'sale-1')])
        self.assertEqual(self.worker._order.call_args.args[1]['customer_id'], 'customer-1')
        self.assertEqual(self.worker._heartbeat.call_count, 2)

    def test_mismatched_retrieved_id_or_response_shape_goes_to_review(self):
        for response in ({'order': order() | {'id': 'wrong'}}, {'order': []}, {}, []):
            self.square.resources[('orders', 'sale-1')] = response
            self.assertEqual(self.worker.run_once()['status'], 'exception')
            self.worker._order.assert_not_called()

    def test_payment_event_follows_retrieved_payment_order_reference(self):
        self.job.update(event_type='payment.updated', resource_id='payment-1')
        self.square.resources[('payments', 'payment-1')] = {'payment': {'id': 'payment-1', 'order_id': 'sale-1'}}
        self.assertEqual(self.worker.run_once()['status'], 'done')
        self.assertEqual([call[1:] for call in self.square.calls], [('payments', 'payment-1'), ('orders', 'sale-1')])

    def test_unassociated_payment_is_ignored_without_credit_mutation(self):
        self.job.update(event_type='payment.updated', resource_id='payment-1')
        self.square.resources[('payments', 'payment-1')] = {'payment': {'id': 'payment-1'}}
        result = self.worker.run_once()
        self.assertEqual((result['status'], result['code']), ('done', 'payment_without_order'))
        self.worker._order.assert_not_called()

    def test_provider_timeout_retries_with_bounded_backoff_without_error_text(self):
        self.square.retrieve = Mock(side_effect=ServiceError('sensitive-vendor-text', 502))
        result = self.worker.run_once()
        self.assertEqual((result['status'], result['delay']), ('pending', 5))
        self.assertNotIn('sensitive-vendor-text', str(result))

    def test_pending_order_never_releases_credit_on_timeout(self):
        self.worker._order.return_value = {'state': 'reserved', 'action': 'pending', 'code': 'order_not_terminal'}
        self.assertEqual(self.worker.run_once()['status'], 'pending')
        self.worker._finish.assert_called_once_with(self.job, 'pending', 'order_not_terminal', delay=5)

    def test_retry_budget_exhaustion_is_review_not_release(self):
        self.job['attempts'] = 8
        self.worker._order.return_value = {'state': 'reserved', 'action': 'pending', 'code': 'order_not_terminal'}
        self.assertEqual(self.worker.run_once()['status'], 'exception')

    def test_authorization_failure_is_durable_review(self):
        self.square.retrieve = Mock(side_effect=ServiceError('not permitted', 403))
        self.assertEqual(self.worker.run_once()['status'], 'exception')

    def test_lease_loss_before_retrieval_prevents_provider_and_ledger_calls(self):
        self.worker._heartbeat.side_effect = LeaseLost()
        self.assertEqual(self.worker.run_once()['status'], 'lease_lost')
        self.assertEqual(self.square.calls, []);self.worker._order.assert_not_called();self.worker._finish.assert_not_called()

    def test_lease_loss_after_retrieval_prevents_ledger_calls(self):
        self.worker._heartbeat.side_effect = [None, LeaseLost()]
        self.assertEqual(self.worker.run_once()['status'], 'lease_lost')
        self.assertEqual(len(self.square.calls), 1);self.worker._order.assert_not_called()

    def test_unknown_event_requires_review_without_provider_call(self):
        self.job['event_type'] = 'unknown-event'
        self.assertEqual(self.worker.run_once()['code'], 'unsupported_reconciliation_event')
        self.assertEqual(self.square.calls, [])

    def prepare_refund(self):
        self.job.update(event_type='refund.updated', resource_id='payment-1_refund-1')
        self.square.resources.update({('refunds', 'payment-1_refund-1'): {'refund': payment_refunds()[0] | {'payment_id': 'payment-1', 'order_id': 'return-order-1'}},
                                      ('payments', 'payment-1'): {'payment': {'id': 'payment-1', 'order_id': 'sale-1'}},
                                      ('orders', 'return-order-1'): {'order': returned_order()}})
        self.worker.settlement.reconcile_return = Mock(return_value={'state': 'restored', 'action': 'restore', 'code': 'verified_itemized_credit_return'})

    def test_refund_retrieves_sale_return_and_all_completed_proofs_before_restoration(self):
        self.prepare_refund()
        self.assertEqual(self.worker.run_once()['status'], 'done')
        args = self.worker.settlement.reconcile_return.call_args.args
        self.assertEqual((args[0], args[1], args[2]['id']), (self.tenant, 'sale-1', 'return-order-1'))
        self.assertEqual(args[3][0]['id'], 'payment-1_refund-1')

    def test_pending_refund_retries_before_any_original_credit_mutation(self):
        self.prepare_refund();self.square.resources[('refunds', 'payment-1_refund-1')]['refund']['status'] = 'PENDING'
        self.assertEqual(self.worker.run_once()['status'], 'pending')
        self.worker._order.assert_not_called();self.worker.settlement.reconcile_return.assert_not_called()

    def test_unlinked_refund_never_restores(self):
        self.prepare_refund();self.square.resources[('refunds', 'payment-1_refund-1')]['refund']['unlinked'] = True
        self.assertEqual(self.worker.run_once()['status'], 'exception')
        self.worker.settlement.reconcile_return.assert_not_called()

    def test_refund_missing_return_allocation_or_proof_is_review_only(self):
        self.prepare_refund();self.square.resources[('orders', 'return-order-1')]['order']['returns'] = []
        self.assertEqual(self.worker.run_once()['code'], 'refund_return_allocation_missing')
        self.worker.settlement.reconcile_return.assert_not_called()

    def test_duplicate_refund_reference_is_not_processed_twice(self):
        self.prepare_refund();self.square.resources[('orders', 'return-order-1')]['order']['refunds'] *= 2
        self.assertEqual(self.worker.run_once()['code'], 'refund_reference_unproven')
        self.worker.settlement.reconcile_return.assert_not_called()


if __name__ == '__main__':unittest.main(verbosity=2)
