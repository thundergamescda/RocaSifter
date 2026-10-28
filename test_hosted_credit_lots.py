"""Origin-policy eligibility and allocation tests using fictional server rows."""
import unittest
from hosted.credit_lots import choose_allocations, eligible
from hosted.security import ServiceError


def binding():
    return {'currency': 'USD', 'discount_id': 'credit-discount', 'discount_catalog_version': 123,
            'discount_tax_basis': 'DO_NOT_MODIFY_TAX_BASIS'}


def lot(ident='lot-a', cents=2000):
    return {'id': ident, 'source_entry_id': 'source-' + ident, 'available_cents': cents,
            'origin_code': 'fixture-verified-origin', 'policy_origin_code': 'fixture-verified-origin',
            'policy_id': 'approved-fixture', 'policy_version': 1, 'policy_reference': 'FICTIONAL POLICY ONLY',
            'policy_approved': True, 'policy_revoked': False, 'approver_role': 'owner',
            'approved_at': '2026-10-28T00:00:00Z', 'approval_reference': 'EXPLICIT FIXTURE APPROVAL',
            'policy_currency': 'USD', 'policy_discount_id': 'credit-discount',
            'policy_catalog_version': 123, 'policy_tax_basis': 'DO_NOT_MODIFY_TAX_BASIS'}


class CreditOriginAllocatorTests(unittest.TestCase):
    def test_approved_compatible_policy_is_eligible(self):
        self.assertTrue(eligible(lot(), binding()))

    def test_unknown_opening_and_correction_origins_never_become_approved_implicitly(self):
        for origin in ('unknown', 'legacy_unknown', 'opening_unknown', 'adjustment_unknown'):
            value = lot();value['origin_code'] = value['policy_origin_code'] = origin
            with self.subTest(origin=origin):self.assertFalse(eligible(value, binding()))

    def test_owner_approval_evidence_is_required(self):
        for patch in ({'policy_approved': False}, {'policy_approved': 1}, {'approved_at': None},
                      {'approver_role': 'manager'}, {'approval_reference': ''}, {'policy_reference': ''}):
            with self.subTest(patch=patch):self.assertFalse(eligible(lot() | patch, binding()))

    def test_revocation_or_origin_mismatch_prevents_new_allocation(self):
        self.assertFalse(eligible(lot() | {'policy_revoked': True}, binding()))
        self.assertFalse(eligible(lot() | {'policy_origin_code': 'different-origin'}, binding()))

    def test_currency_catalog_version_discount_and_tax_basis_must_match(self):
        for patch in ({'currency': 'CAD'}, {'discount_id': 'other'}, {'discount_catalog_version': 124},
                      {'discount_tax_basis': 'MODIFY_TAX_BASIS'}, {'discount_tax_basis': None}):
            with self.subTest(patch=patch):self.assertFalse(eligible(lot(), binding() | patch))

    def test_fifo_allocation_uses_only_available_eligible_origins(self):
        values = [lot('a', 700), lot('b', 500) | {'policy_approved': False}, lot('c', 1500)]
        selected = choose_allocations(values, 2000, binding())
        self.assertEqual([(item['lot_id'], item['amount_cents']) for item in selected], [('a', 700), ('c', 1300)])
        self.assertEqual(sum(item['amount_cents'] for item in selected), 2000)

    def test_aggregate_unknown_balance_does_not_cover_insufficient_eligible_balance(self):
        values = [lot('known', 500), lot('unknown', 10000) | {'policy_approved': False}]
        with self.assertRaises(ServiceError):choose_allocations(values, 600, binding())

    def test_zero_available_lots_are_skipped_without_overallocation(self):
        selected = choose_allocations([lot('held', 0), lot('free', 100)], 100, binding())
        self.assertEqual([item['lot_id'] for item in selected], ['free'])

    def test_invalid_amount_or_corrupt_availability_is_rejected(self):
        for amount in (True, 0, -1, 1.5, '100', 100000001):
            with self.subTest(amount=amount), self.assertRaises(ServiceError):choose_allocations([lot()], amount, binding())
        for available in (True, -1, '500'):
            with self.subTest(available=available), self.assertRaises(ServiceError):
                choose_allocations([lot() | {'available_cents': available}], 100, binding())

    def test_allocation_preserves_source_and_policy_provenance(self):
        selected = choose_allocations([lot()], 123, binding())[0]
        self.assertEqual((selected['source_entry_id'], selected['origin_code'], selected['policy_id'], selected['policy_version']),
                         ('source-lot-a', 'fixture-verified-origin', 'approved-fixture', 1))
        self.assertNotIn('approval_reference', selected)


if __name__ == '__main__':unittest.main(verbosity=2)
