"""Fictional resource tests for hosted Square settlement; no network calls."""
from copy import deepcopy
from dataclasses import replace
import unittest
from hosted.settlement import ReservationTerms, RedemptionTerms, plan_order, plan_return


def terms():
    return ReservationTerms('sale-1', 'customer-1', 'location-1', 'discount-credit', 2000, 'approved-fixture-policy',
                            discount_catalog_version=123,discount_tax_basis='DO_NOT_MODIFY_TAX_BASIS')


def catalog_discount():
    return {'id':'discount-credit','type':'DISCOUNT','version':123,'is_deleted':False,
            'discount_data':{'discount_type':'VARIABLE_AMOUNT','modify_tax_basis':'DO_NOT_MODIFY_TAX_BASIS'}}


def order():
    return {'id': 'sale-1', 'customer_id': 'customer-1', 'location_id': 'location-1',
            'version': 3, 'state': 'COMPLETED', 'total_money': {'amount': 3000, 'currency': 'USD'},
            'discounts': [{'uid': 'credit-uid', 'catalog_object_id': 'discount-credit','catalog_version':123,
                           'type': 'VARIABLE_AMOUNT', 'amount_money': {'amount': 2000, 'currency': 'USD'},
                           'applied_money': {'amount': 1500, 'currency': 'USD'}}]}


def redemption():
    return RedemptionTerms('sale-1', 'customer-1', 'location-1', 'discount-credit',
                           'credit-uid', 1500, 'approved-fixture-policy')


def returned_order():
    return {'id': 'return-order-1', 'location_id': 'location-1', 'version': 1,
            'state': 'COMPLETED',
            'returns': [{'uid': 'return-1', 'source_order_id': 'sale-1',
                         'return_line_items': [{'uid': 'returned-item', 'source_line_item_uid': 'sold-item',
                                                'quantity': '1', 'item_type': 'ITEM'}],
                         'return_discounts': [{'uid': 'returned-discount', 'source_discount_uid': 'credit-uid',
                                               'catalog_object_id': 'discount-credit',
                                               'applied_money': {'amount': 500, 'currency': 'USD'}}]}],
            'refunds': [{'id': 'refund-1', 'tender_id': 'payment-1',
                         'amount_money': {'amount': 2500, 'currency': 'USD'}}]}


def payment_refunds():
    return [{'id': 'payment-1_refund-1', 'status': 'COMPLETED', 'location_id': 'location-1',
             'payment_id': 'payment-1', 'amount_money': {'amount': 2500, 'currency': 'USD'}}]


class OrderSettlementPlannerTests(unittest.TestCase):
    def test_completed_credit_requires_exact_order_catalog_version(self):
        for version in (None,True,0,124,'123'):
            value=order();value['discounts'][0]['catalog_version']=version
            with self.subTest(version=version):
                self.assertEqual(plan_order(terms(),value,catalog_discount=catalog_discount()).action,'exception')

    def test_completed_credit_and_zero_use_require_retrieved_catalog_proof(self):
        for amount in (0,1500):
            value=order();value['discounts'][0]['applied_money']['amount']=amount
            self.assertEqual(plan_order(terms(),value).code,'catalog_discount_unverified')
        self.assertEqual(plan_order(replace(terms(),discount_catalog_version=None),order()).code,'catalog_terms_missing')

    def test_wrong_catalog_identity_version_object_type_and_deletion_fail_closed(self):
        for patch in ({'id':'other'},{'version':124},{'version':True},{'type':'ITEM'},{'is_deleted':True},{'is_deleted':0}):
            with self.subTest(patch=patch):
                self.assertEqual(plan_order(terms(),order(),catalog_discount=catalog_discount()|patch).action,'exception')

    def test_catalog_amount_type_and_explicit_tax_basis_must_match_approved_snapshot(self):
        for patch in ({'discount_type':'FIXED_PERCENTAGE'},{'discount_type':'FIXED_AMOUNT'},
                      {'modify_tax_basis':'MODIFY_TAX_BASIS'},{'modify_tax_basis':None},{'percentage':'5'}):
            value=catalog_discount();value['discount_data'].update(patch)
            with self.subTest(patch=patch):
                self.assertEqual(plan_order(terms(),order(),catalog_discount=value).action,'exception')

    def test_fixed_amount_catalog_currency_is_validated(self):
        value=order();value['discounts'][0]['type']='FIXED_AMOUNT'
        proof=catalog_discount();proof['discount_data'].update(discount_type='FIXED_AMOUNT',amount_money={'amount':2000,'currency':'USD'})
        self.assertEqual(plan_order(terms(),value,catalog_discount=proof).action,'consume')
        proof['discount_data']['amount_money']['currency']='CAD'
        self.assertEqual(plan_order(terms(),value,catalog_discount=proof).code,'catalog_discount_currency_mismatch')

    def test_open_and_canceled_orders_do_not_need_catalog_reads(self):
        value=order();value['state']='OPEN'
        self.assertEqual(plan_order(terms(),value).action,'pending')
        value['state']='CANCELED';value.pop('discounts')
        self.assertEqual(plan_order(terms(),value).action,'release')

    def test_applied_discount_not_declared_amount_or_order_total_is_consumed(self):
        decision = plan_order(terms(), order(),catalog_discount=catalog_discount())
        self.assertEqual((decision.action, decision.amount_cents), ('consume', 1500))
        self.assertEqual(decision.source_key, 'order:sale-1')
        self.assertEqual(decision.discount_uid, 'credit-uid')

    def test_name_match_does_not_authorize_a_different_catalog_discount(self):
        value = order();value['discounts'][0].update(name='Store Credit', catalog_object_id='untracked')
        self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).code, 'tracked_discount_not_unique')

    def test_other_discounts_do_not_change_tracked_credit(self):
        value = order();value['discounts'].append({'uid': 'coupon', 'catalog_object_id': 'sale-coupon',
                                                  'applied_money': {'amount': 800, 'currency': 'USD'}})
        self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).amount_cents, 1500)

    def test_duplicate_catalog_discount_or_uid_needs_review(self):
        for same_catalog in (True, False):
            with self.subTest(same_catalog=same_catalog):
                value = order();extra = deepcopy(value['discounts'][0])
                if not same_catalog:extra['catalog_object_id'] = 'other'
                value['discounts'].append(extra)
                self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'exception')

    def test_order_customer_location_and_uid_binding(self):
        for key in ('id', 'customer_id', 'location_id'):
            value = order();value[key] = 'other'
            with self.subTest(key=key):self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'exception')
        self.assertEqual(plan_order(replace(terms(), discount_uid='another'), order(),catalog_discount=catalog_discount()).code, 'discount_uid_mismatch')

    def test_open_draft_and_stale_order_hold_credit(self):
        for state in ('OPEN', 'DRAFT'):
            value = order();value['state'] = state
            self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'pending')
        self.assertEqual(plan_order(replace(terms(), minimum_order_version=4), order(),catalog_discount=catalog_discount()).code, 'stale_order_version')

    def test_cancellation_releases_without_creating_a_redemption(self):
        value = order();value['state'] = 'CANCELED';value.pop('discounts');value.pop('total_money')
        decision = plan_order(terms(), value,catalog_discount=catalog_discount())
        self.assertEqual((decision.action, decision.amount_cents, decision.source_key), ('release', 0, None))

    def test_completed_zero_credit_releases_hold(self):
        value = order();value['discounts'][0]['applied_money']['amount'] = 0
        self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).code, 'completed_without_credit_use')

    def test_money_is_strict_positive_integer_cents_not_more_than_reservation(self):
        for invalid in (True, '1500', 1.5, -1, 2001, None):
            with self.subTest(value=invalid):
                value = order();value['discounts'][0]['applied_money']['amount'] = invalid
                self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'exception')

    def test_currency_and_discount_type_fail_closed(self):
        value = order();value['discounts'][0]['applied_money']['currency'] = 'CAD'
        self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'exception')
        value = order();value['total_money']['currency'] = 'CAD'
        self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'exception')
        value = order();value['discounts'][0].update(type='FIXED_PERCENTAGE', percentage='50')
        self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).code, 'unapproved_discount_type')

    def test_malformed_resources_and_unknown_state_never_authorize_consumption(self):
        resources = [None, [], {}, order() | {'state': 'UNKNOWN'}, order() | {'version': True},
                     order() | {'version': 0}, order() | {'discounts': {}}, order() | {'discounts': [None]}]
        for value in resources:
            with self.subTest(value=value):self.assertEqual(plan_order(terms(), value,catalog_discount=catalog_discount()).action, 'exception')

    def test_terms_require_complete_approved_usd_bindings(self):
        for patch in ({'policy_reference': ''}, {'reserved_cents': True}, {'currency': 'CAD'},
                      {'minimum_order_version': True}, {'discount_uid': 'x' * 61}, {'order_id': '\nother'}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):replace(terms(), **patch)


class ReturnSettlementPlannerTests(unittest.TestCase):
    def test_credit_return_uses_source_discount_applied_amount_not_cash_refund(self):
        decision = plan_return(redemption(), returned_order(), payment_refunds())
        self.assertEqual((decision.action, decision.amount_cents), ('restore', 500))
        self.assertEqual(decision.source_key, 'return:return-order-1:order:sale-1')

    def test_multiple_payment_refunds_still_produce_one_credit_restore(self):
        value = returned_order();value['refunds'].append({'id': 'refund-2', 'tender_id': 'payment-2'})
        proof = payment_refunds() + [{'id': 'payment-2_refund-2', 'status': 'COMPLETED',
                                    'location_id': 'location-1', 'amount_money': {'amount': 100, 'currency': 'USD'}}]
        decision = plan_return(redemption(), value, proof)
        self.assertEqual((decision.action, decision.amount_cents), ('restore', 500))

    def test_total_restoration_is_capped_at_original_redeemed_credit(self):
        self.assertEqual(plan_return(redemption(), returned_order(), payment_refunds(), already_restored_cents=1000).action, 'restore')
        self.assertEqual(plan_return(redemption(), returned_order(), payment_refunds(), already_restored_cents=1001).code, 'return_exceeds_redemption')
        for invalid in (True, -1, 1501, '0'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                plan_return(redemption(), returned_order(), payment_refunds(), already_restored_cents=invalid)

    def test_missing_return_allocation_does_not_restore_cash_refund_dollars(self):
        value = returned_order();value['returns'][0].pop('return_discounts')
        decision = plan_return(redemption(), value, payment_refunds())
        self.assertEqual((decision.action, decision.amount_cents), ('none', 0))

    def test_wrong_sale_discount_or_location_cannot_restore_credit(self):
        mutations = [lambda v: v.update(location_id='other'), lambda v: v.update(customer_id='other'),
                     lambda v: v['returns'][0].update(source_order_id='other'),
                     lambda v: v['returns'][0]['return_discounts'][0].update(source_discount_uid='other'),
                     lambda v: v['returns'][0]['return_discounts'][0].update(catalog_object_id='other')]
        for mutate in mutations:
            value = returned_order();mutate(value)
            self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')

    def test_absent_return_customer_is_supported_by_original_sale_link(self):
        value = returned_order();self.assertNotIn('customer_id', value)
        self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'restore')

    def test_pending_refund_does_not_restore_and_failed_refund_needs_review(self):
        for status, expected in (('PENDING', 'pending'), ('FAILED', 'exception'), ('REJECTED', 'exception'), ('UNKNOWN', 'exception')):
            proof = payment_refunds();proof[0]['status'] = status
            with self.subTest(status=status):self.assertEqual(plan_return(redemption(), returned_order(), proof).action, expected)

    def test_missing_or_unlinked_refund_proof_fails_closed(self):
        for patch in ({'id': 'other'}, {'unlinked': True}, {'location_id': 'other'},
                      {'amount_money': {'amount': 2500, 'currency': 'CAD'}},
                      {'amount_money': {'amount': 0, 'currency': 'USD'}}):
            proof = payment_refunds();proof[0].update(patch)
            self.assertEqual(plan_return(redemption(), returned_order(), proof).action, 'exception')
        self.assertEqual(plan_return(redemption(), returned_order(), []).action, 'exception')

    def test_custom_unlinked_exchange_and_zero_payment_return_require_review(self):
        values = []
        value = returned_order();value['returns'][0]['return_line_items'][0]['item_type'] = 'CUSTOM_AMOUNT';values.append(value)
        value = returned_order();value['returns'][0]['return_line_items'][0].pop('source_line_item_uid');values.append(value)
        value = returned_order();value['line_items'] = [{'uid': 'new-sale'}];values.append(value)
        value = returned_order();value['refunds'] = [];values.append(value)
        for value in values:self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')

    def test_duplicate_return_discount_or_return_uid_is_rejected(self):
        value = returned_order();value['returns'][0]['return_discounts'] *= 2
        self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')
        value = returned_order();value['returns'] *= 2
        self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')

    def test_duplicate_payment_refund_reference_is_not_accepted_as_independent_proof(self):
        value = returned_order();value['refunds'] *= 2
        self.assertEqual(plan_return(redemption(), value, payment_refunds()).code, 'refund_reference_unproven')

    def test_malformed_or_negative_quantities_are_rejected(self):
        for quantity in (True, 1, 'NaN', 'Infinity', '-1', '0', '', '9' * 13):
            value = returned_order();value['returns'][0]['return_line_items'][0]['quantity'] = quantity
            with self.subTest(quantity=quantity):self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')

    def test_malformed_money_and_resources_fail_closed(self):
        for amount in (True, -1, '500', None):
            value = returned_order();value['returns'][0]['return_discounts'][0]['applied_money']['amount'] = amount
            self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')
        for value in (None, {}, [], returned_order() | {'returns': {}}, returned_order() | {'line_items': {}}, returned_order() | {'version': True}):
            self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, 'exception')

    def test_open_return_remains_pending_and_canceled_return_cannot_restore(self):
        for state, expected in (('OPEN', 'pending'), ('DRAFT', 'pending'), ('CANCELED', 'exception')):
            value = returned_order();value['state'] = state
            self.assertEqual(plan_return(redemption(), value, payment_refunds()).action, expected)


if __name__ == '__main__':unittest.main(verbosity=2)
