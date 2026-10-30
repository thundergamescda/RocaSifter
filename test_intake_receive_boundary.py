"""Claimed/hosted batches cannot fall back to local Square or local credit.

All profiles, staff sessions, cards and tokens are fictional. Provider methods
are mocks; the old unclaimed operation journal remains an explicit baseline.
"""
import json
import uuid
import unittest
from unittest.mock import Mock, patch

import roca_to_square as core
from station_store import StationError
import test_intake_claim as intake_fixtures


class ClaimedLegacyReceiveBoundaryTests(unittest.TestCase):
    setUp = intake_fixtures.OriginalIntakeCaptureTests.setUp
    connect = intake_fixtures.CentralIntakeClaimTests.connect
    prepare = intake_fixtures.CentralIntakeClaimTests.prepare

    def reviewed(self, mode='labels'):
        receipt = self.service.import_csv('Name,Game,Price,Quantity\nFictional boundary card,Magic,10,2\n',
                                          'boundary.csv', mode)['receipt']
        receipt['inventory_intent'] = 'new'
        for card in receipt['cards']:card['reviewed'] = True
        return self.service.save_receipt(receipt['id'], receipt)

    def local_config(self):
        self.service.store.put('preferences', self.service.preferences() |
                               {'squareEnvironment': 'sandbox', 'squareLocationId': 'fictional-location'})

    def assert_no_local_receive(self, receipt):
        self.local_config()
        before = self.service.store.receipt(receipt['id'])
        with (patch.object(self.service.vault, 'read', return_value='FICTIONAL-LOCAL-ONLY-TOKEN') as vault,
              patch.object(core, 'upload_to_square', return_value={'uploaded_count': 1, 'inventory_count': 2, 'errors': []}) as upload):
            error = None
            try:self.service.receive(receipt['id'])
            except StationError as caught:error = caught
            print(json.dumps({'fixture': 'claimed_or_hosted_batch_local_receive_boundary',
                              'local_vault_reads': vault.call_count, 'provider_mock_calls': upload.call_count,
                              'blocked': error is not None, 'live_square_contacted': False}), flush=True)
            self.assertIsNotNone(error, 'A centrally bound batch reached the legacy local Square receiving path.')
            vault.assert_not_called();upload.assert_not_called()
        self.assertEqual(self.service.store.receipt(receipt['id']), before)

    def test_confirmed_central_claim_blocks_legacy_vault_and_provider_before_any_intent(self):
        self.connect();receipt = self.reviewed()
        confirmed = self.prepare(receipt)['receipt']
        self.assertEqual(confirmed['intake_claim']['state'], 'confirmed')
        self.assert_no_local_receive(confirmed)

    def test_old_hosted_purchase_without_new_claim_fields_still_blocks_local_receiving(self):
        receipt = self.reviewed('trade')
        receipt['purchase'] = {'scope': 'hosted', 'type': 'credit', 'amount': 10, 'at': '2026-10-29T00:00:00Z'}
        receipt['shared_trade'] = {'state': 'confirmed', 'trade_id': str(uuid.uuid4())}
        self.service.store.save_receipt(receipt)
        self.assert_no_local_receive(receipt)

    def test_claim_with_inconsistent_existing_legacy_journal_blocks_without_rekey_or_mutation(self):
        self.connect();receipt = self.prepare(self.reviewed())['receipt']
        saved = json.dumps({'endpoint': 'https://connect.squareupsandbox.com/v2/inventory/changes/batch-create',
                            'payload': {'idempotency_key': 'FICTIONAL-ORIGINAL-KEY', 'changes': []}})
        with self.service.store.db() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,NULL)', (receipt['id'], 'stock:0', saved))
        self.assert_no_local_receive(receipt)
        with self.service.store.db() as db:
            rows = db.execute('SELECT request,response FROM operations WHERE receipt_id=?', (receipt['id'],)).fetchall()
        self.assertEqual([(row['request'], row['response']) for row in rows], [(saved, None)])

    def test_unclaimed_legacy_retry_retains_original_provider_body_key_and_algorithm(self):
        receipt = self.reviewed();self.local_config()
        receipt.update(inventory_status='needs_attention', catalog_mode='legacy',
                       square_connection={'squareEnvironment': 'sandbox', 'squareLocationId': 'fictional-location'})
        self.service.store.save_receipt(receipt)
        endpoint = 'https://connect.squareupsandbox.com/v2/inventory/changes/batch-create'
        payload = {'idempotency_key': 'FICTIONAL-ORIGINAL-KEY', 'changes': [{'quantity': '2'}]}
        saved = json.dumps({'endpoint': endpoint, 'payload': payload})
        with self.service.store.db() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,NULL)', (receipt['id'], 'stock:0', saved))
        response = Mock();response.json.return_value = {'counts': []};response.raise_for_status.return_value = None
        def legacy_upload(cards, token, **kwargs):
            self.assertFalse(kwargs['price_variations'])
            kwargs['request_log']('stock:0', endpoint, {'idempotency_key': 'DO-NOT-USE-NEW-KEY', 'changes': []}, {})
            return {'uploaded_count': 1, 'inventory_count': 2, 'errors': []}
        with (patch.object(self.service.vault, 'read', return_value='FICTIONAL-ORIGINAL-LOCAL-TOKEN'),
              patch.object(core, 'upload_to_square', side_effect=legacy_upload) as upload,
              patch.object(core.requests, 'post', return_value=response) as provider):
            result = self.service.receive(receipt['id'])
        self.assertEqual(result['inventory_status'], 'received')
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(provider.call_args.kwargs['json'], payload)
        with self.service.store.db() as db:
            row = db.execute('SELECT request FROM operations WHERE receipt_id=? AND key=?', (receipt['id'], 'stock:0')).fetchone()
        self.assertEqual(row['request'], saved)

    def test_confirmed_claim_cannot_record_a_parallel_local_buy(self):
        self.connect();receipt = self.prepare(self.reviewed('trade'))['receipt']
        before = self.service.store.receipt(receipt['id'])
        with patch.object(self.service, 'buy_receipt') as output:
            with self.assertRaises(StationError):self.service.accept_buy(receipt['id'], 'credit')
            output.assert_not_called()
        self.assertEqual(self.service.store.receipt(receipt['id']), before)

    def test_confirmed_claim_cannot_mint_local_wallet_credit_from_a_local_purchase(self):
        self.connect();receipt = self.prepare(self.reviewed('trade'))['receipt']
        receipt['purchase'] = {'type': 'credit', 'amount': 10, 'at': '2026-10-29T00:00:00Z'}
        self.service.store.save_receipt(receipt)
        customer = self.service.credit.add_customer({'requestId': 'fictional-customer', 'name': 'Fictional local customer'})
        before = self.service.credit.account(customer['id'])
        with self.assertRaises(StationError):
            self.service.credit.transaction({'requestId': 'fictional-claimed-local-credit', 'customerId': customer['id'],
                'kind': 'trade', 'receiptId': receipt['id'], 'reference': 'FIXTURE', 'note': '', 'version': customer['version']})
        self.assertEqual(self.service.credit.account(customer['id']), before)
        self.assertNotIn('tracked_credit', self.service.store.receipt(receipt['id']))

    def test_unclaimed_legacy_local_buy_and_trade_credit_replay_remain_available(self):
        receipt = self.reviewed('trade')
        with patch.object(self.service, 'buy_receipt'):
            purchased = self.service.accept_buy(receipt['id'], 'credit')
        customer = self.service.credit.add_customer({'requestId': 'fictional-customer', 'name': 'Fictional local customer'})
        data = {'requestId': 'fictional-original-credit', 'customerId': customer['id'], 'kind': 'trade',
                'receiptId': receipt['id'], 'reference': 'FIXTURE', 'note': '', 'version': customer['version']}
        first = self.service.credit.transaction(data);second = self.service.credit.transaction(data)
        self.assertEqual(first, second)
        self.assertEqual(len(first['entries']), 1)
        self.assertEqual(first['customer']['balance'], purchased['purchase']['amount'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
