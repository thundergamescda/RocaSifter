"""Independent receiving admission/recovery regressions in disposable schemas.

The existing receiving setup is reused explicitly, without inheriting its test
methods. All provider effects are injected fictional fixtures; HTTP is forbidden.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import patch
import uuid

import psycopg
import test_hosted_receiving_postgres as receiving_fixtures
import test_hosted_settlement_postgres as postgres_fixtures
from hosted.receiving_plan import ReceivingError
from hosted.security import ServiceError
from test_hosted_trades import acceptance_data


class _ProcessInterrupted(BaseException):
    pass


class PostgreSQLReceivingAdversarialTests(postgres_fixtures._PostgreSQLSettlementFixture):
    setUp = receiving_fixtures.PostgreSQLReceivingAcceptance.setUp
    approve = receiving_fixtures.PostgreSQLReceivingAcceptance.approve
    plan = receiving_fixtures.PostgreSQLReceivingAcceptance.plan
    terms = receiving_fixtures.PostgreSQLReceivingAcceptance.terms
    commit = receiving_fixtures.PostgreSQLReceivingAcceptance.commit
    recover = receiving_fixtures.PostgreSQLReceivingAcceptance.recover
    wallet = receiving_fixtures.PostgreSQLReceivingAcceptance.wallet
    trade = receiving_fixtures.PostgreSQLReceivingAcceptance.trade

    def state(self, plan):
        with self.database.transaction() as db:
            row = db.execute('SELECT * FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',
                             (self.tenant, plan['receiving_id'])).fetchone()
            step = db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s',
                              (self.tenant, plan['receiving_id'])).fetchone()
            lane = db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE tenant_id=%s AND operation_id=%s',
                              (self.tenant, plan['receiving_id'])).fetchone()
        return row, step, lane

    def expire_lease(self, plan):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",
                       (self.tenant, plan['receiving_id']))

    def assert_financial_acceptance_blocked(self, trade):
        for offer, amount in (('cash', 700), ('credit', 1000)):
            data = acceptance_data(self.customer, request_key='blocked-'+uuid.uuid4().hex,
                                   version=trade['version'], offer=offer, cents=amount)
            data['claim_reviewed'] = True
            with self.subTest(offer=offer), self.assertRaisesRegex(ServiceError, 'admitted inventory-only') as caught:
                self.trades.accept(self.actor, trade['trade_id'], data)
            self.assertEqual(caught.exception.status,409)
            self.assertEqual(caught.exception.code,'PHYSICAL_INTAKE_CONFLICT')
        self.assertEqual(self.wallet(), (0, 0))
        with self.database.transaction() as db:
            saved = db.execute('SELECT state,receipt_hash,accepted_receipt FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s',
                               (self.tenant, trade['trade_id'])).fetchone()
            self.assertEqual(saved, {'state':'draft', 'receipt_hash':None, 'accepted_receipt':None})
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_credit_lots WHERE tenant_id=%s',
                                        (self.tenant,)).fetchone()['n'], 0)

    def test_process_interruption_after_admission_before_effect_keeps_exact_step_and_no_new_payment(self):
        trade = self.trade(); plan = self.plan()
        with patch.object(self.provider, 'dispatch_inventory', side_effect=_ProcessInterrupted):
            with self.assertRaises(_ProcessInterrupted):self.commit(plan)
        row, step, lane = self.state(plan)
        self.assertEqual((row['state'], lane['state']), ('inventory_pending', 'occupied'))
        self.assertIsNotNone(row['first_admitted_at'])
        saved = self.service._open(self.tenant, plan['receiving_id'], step)
        self.assertEqual(saved['idempotency_key'], str(step['provider_key']))
        self.assertEqual(self.provider.calls, [])
        self.assert_financial_acceptance_blocked(trade)
        self.expire_lease(plan)
        self.assertEqual(self.recover(plan)['state'], 'outcome_unknown')
        self.assertEqual(self.provider.calls, [])
        _, after, lane = self.state(plan)
        self.assertEqual(after, step)
        self.assertEqual(lane['state'], 'unknown')
        self.assert_financial_acceptance_blocked(trade)

    def test_completed_inventory_also_blocks_new_pc_cash_and_credit_acceptances(self):
        plan = self.plan(); result = self.commit(plan)
        self.assertEqual(result['state'], 'received')
        # A different PC can create a fresh local source reference/draft, but it
        # cannot turn the already admitted physical claim into another payment.
        self.assert_financial_acceptance_blocked(self.trade())
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.service.get(self.actor, plan['receiving_id'])['receipt'], result['receipt'])

    def test_database_guard_still_rejects_older_acceptance_code_without_domain_check(self):
        trade=self.trade();plan=self.plan();self.provider.behavior='before_effect'
        self.assertEqual(self.commit(plan)['state'],'outcome_unknown')
        for state in ('accepted_cash','accepted_credit'):
            with self.subTest(state=state),self.assertRaisesRegex(psycopg.errors.RaiseException,'permanently blocks'):
                with self.database.transaction() as db:
                    db.execute('UPDATE ss_trade_receipts SET state=%s WHERE tenant_id=%s AND id=%s',
                               (state,self.tenant,trade['trade_id']))
        self.assert_financial_acceptance_blocked(trade)

    def test_staff_revocation_during_catalog_preflight_prevents_durable_admission(self):
        plan = self.plan(); real_catalog = self.provider.catalog
        def revoke_after_read(access, ids):
            result = real_catalog(access, ids)
            with self.database.transaction() as db:
                db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',
                           (self.tenant, self.actor['staff_id']))
            return result
        with patch.object(self.provider, 'catalog', side_effect=revoke_after_read), self.assertRaises(ServiceError):
            self.commit(plan)
        row, step, lane = self.state(plan)
        self.assertEqual(row['state'], 'ready');self.assertIsNone(row['first_admitted_at'])
        self.assertIsNone(step);self.assertIsNone(lane);self.assertEqual(self.provider.calls, [])

    def test_session_expiry_during_catalog_preflight_prevents_durable_admission(self):
        plan = self.plan(); real_catalog = self.provider.catalog
        def expire_after_read(access, ids):
            result = real_catalog(access, ids)
            with self.database.transaction() as db:
                db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",
                           (self.actor['session_hash'],))
            return result
        with patch.object(self.provider, 'catalog', side_effect=expire_after_read), self.assertRaises(ServiceError):
            self.commit(plan)
        row, step, lane = self.state(plan)
        self.assertIsNone(row['first_admitted_at']);self.assertIsNone(step);self.assertIsNone(lane)
        self.assertEqual(self.provider.calls, [])

    def test_changed_provider_application_environment_or_merchant_never_admits_saved_plan(self):
        plan = self.plan(); original = deepcopy(self.provider.proof)
        for field, value in (('application_id','other-application'),('environment','production'),('merchant_id','other-merchant')):
            self.provider.proof = original | {field:value}
            with self.subTest(field=field),self.assertRaisesRegex(ReceivingError, 'different Square principal'):
                self.commit(plan)
        self.provider.proof = original
        row, step, lane = self.state(plan)
        self.assertIsNone(row['first_admitted_at']);self.assertIsNone(step);self.assertIsNone(lane)
        self.assertEqual(self.provider.calls, [])

    def test_changed_identity_anchor_price_tracking_or_location_never_admits(self):
        plan = self.plan(); original = deepcopy(self.provider.objects)
        changes = [lambda data:data.update(user_data='Another physical identity'),
                   lambda data:data.update(price_money={'amount':1250,'currency':'USD'}),
                   lambda data:data.update(track_inventory=False),
                   lambda data:data.update(location_overrides=[{'location_id':'fixture-location','track_inventory':False}])]
        for change in changes:
            self.provider.objects = deepcopy(original)
            change(self.provider.objects['fixture-variation']['item_variation_data'])
            with self.subTest(change=changes.index(change)),self.assertRaises(ReceivingError):self.commit(plan)
        self.provider.objects = original
        row, step, lane = self.state(plan)
        self.assertIsNone(row['first_admitted_at']);self.assertIsNone(step);self.assertIsNone(lane)
        self.assertEqual(self.provider.calls, [])

    def test_archive_before_admission_blocks_and_archive_after_effect_keeps_proof_without_labels(self):
        plan = self.plan(); parent = self.provider.objects['fixture-parent']['item_data']
        parent['is_archived'] = True
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.provider.calls, []);self.assertIsNone(self.state(plan)[1])
        parent['is_archived'] = False
        self.provider.after_dispatch = lambda:parent.update(is_archived=True)
        result = self.commit(plan)
        self.assertEqual((result['state'], result['inventory_confirmed'], result['receipt']), ('needs_review', True, None))
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.recover(plan)['state'], 'needs_review')
        parent['is_archived'] = False
        self.assertEqual(self.recover(plan)['state'], 'received')
        self.assertEqual(len(self.provider.calls), 1)

    def test_global_duplicate_sku_before_admission_blocks_and_after_effect_holds_final_receipt(self):
        plan = self.plan(); duplicate = deepcopy(self.provider.objects['fixture-variation'])
        duplicate['id'] = 'other-existing-variation'
        self.provider.objects[duplicate['id']] = duplicate
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.provider.calls, []);self.assertIsNone(self.state(plan)[1])
        del self.provider.objects[duplicate['id']]
        self.provider.after_dispatch = lambda:self.provider.objects.update({duplicate['id']:deepcopy(duplicate)})
        result = self.commit(plan)
        self.assertEqual((result['state'], result['inventory_confirmed'], result['receipt']), ('needs_review', True, None))
        self.assertEqual(self.recover(plan)['state'], 'needs_review')
        del self.provider.objects[duplicate['id']]
        self.assertEqual(self.recover(plan)['state'], 'received')
        self.assertEqual(len(self.provider.calls), 1)

    def test_wrong_application_or_duplicate_history_preserves_unknown_lane_without_reissue(self):
        self.provider.behavior = 'lost_response'; plan = self.plan()
        self.assertEqual(self.commit(plan)['state'], 'outcome_unknown')
        exact = deepcopy(self.provider.history_rows)
        wrong = deepcopy(exact);wrong[0]['adjustment']['source']['application_id'] = 'different-application'
        duplicate = deepcopy(exact);duplicate.append(deepcopy(exact[0]));duplicate[-1]['adjustment']['id'] = 'different-adjustment'
        for rows in (wrong, duplicate, []):
            self.provider.history_rows = rows
            with self.subTest(rows=len(rows)):
                self.assertEqual(self.recover(plan)['state'], 'outcome_unknown')
                row, step, lane = self.state(plan)
                self.assertIsNone(row['receipt']);self.assertIsNone(row['inventory_proof'])
                self.assertEqual(lane['state'], 'unknown');self.assertEqual(len(self.provider.calls), 1)
        self.provider.history_rows = exact
        self.assertEqual(self.recover(plan)['state'], 'received')
        self.assertEqual(len(self.provider.calls), 1)

    def test_stale_original_ack_after_expired_lease_recovery_cannot_replace_receipt_or_redispatch(self):
        plan = self.plan(); entered = threading.Event(); release = threading.Event()
        def hold_after_effect():
            entered.set()
            if not release.wait(10):raise AssertionError('Fictional release timeout')
        self.provider.after_dispatch = hold_after_effect
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.commit, plan)
            try:
                self.assertTrue(entered.wait(5));self.expire_lease(plan)
                recovered = self.recover(plan)
                self.assertEqual(recovered['state'], 'received')
                receipt = deepcopy(recovered['receipt'])
            finally:release.set()
            final = pending.result(5)
        self.assertEqual(final['receipt'], receipt)
        self.assertEqual(self.service.get(self.actor, plan['receiving_id'])['receipt'], receipt)
        self.assertEqual(len(self.provider.calls), 1)
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_steps WHERE tenant_id=%s',
                                        (self.tenant,)).fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_seller_lanes WHERE tenant_id=%s',
                                        (self.tenant,)).fetchone()['n'], 0)


if __name__ == '__main__':unittest.main(verbosity=2)
