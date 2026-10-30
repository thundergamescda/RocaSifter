"""Isolated receipt/CLI tests for real multi-game scanner and manual intake paths."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

import roca_to_square as core
from station_service import StationService
from station_store import StationError


def fixture(headers, *rows):
    output = io.StringIO(newline='');writer = csv.writer(output)
    writer.writerow(headers);writer.writerows(rows)
    return output.getvalue()


class IntakeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.root = Path(self.temp.name)
        self.patches = [patch.object(core, 'SETTINGS_FILE', str(self.root / 'settings.json')),
                        patch.object(core, 'OUTPUT_DIR', str(self.root / 'outputs')),
                        patch.object(core.requests, 'get', side_effect=AssertionError('Live network forbidden')),
                        patch.object(core.requests, 'post', side_effect=AssertionError('Live network forbidden'))]
        for item in self.patches:item.start()
        self.service = StationService(core)
        self.service.store.put('preferences', self.service.preferences() | {'pricingEnabled': False})

    def tearDown(self):
        for item in reversed(self.patches):item.stop()
        self.temp.cleanup()

    def test_mixed_game_file_survives_receipt_save_and_duplicate_retry(self):
        games = ['Pokémon', 'Magic: The Gathering', 'Riftbound', 'One Piece', 'Union Arena', 'Gundam', 'Sports', 'Flesh and Blood']
        text = fixture(['Card Name', 'Game', 'Set', 'Card Number', 'Price', 'Quantity'],
                       *[['Same', game, 'Same Set', '001', '3.13', 1] for game in games])
        receipt = self.service.import_csv(text, 'multi-scanner.csv', 'labels')['receipt']
        self.assertTrue(receipt['mixed_games']);self.assertEqual(receipt['pricing_game'], '')
        self.assertEqual(len({c['sku'] for c in receipt['cards']}), len(games))
        self.assertEqual(receipt['cards'][0]['final_price'], 3.13)
        source_ids = [c['source_ids'] for c in receipt['cards']]
        receipt = self.service.save_receipt(receipt['id'], receipt)
        self.assertEqual([c['source_ids'] for c in receipt['cards']], source_ids)
        replay = self.service.import_csv(text, 'renamed.csv', 'labels')
        self.assertTrue(replay['duplicate']);self.assertEqual(replay['receipt']['id'], receipt['id'])

    def test_market_only_rows_cannot_be_reviewed_labeled_received_or_traded_as_zero(self):
        text = 'Name,Game,Market Price,Quantity\nA,Gundam,9.99,1\n'
        receipt = self.service.import_csv(text, 'unpriced.csv', 'trade')['receipt']
        card = receipt['cards'][0]
        self.assertTrue(card['price_missing']);self.assertEqual(card['market_price_cents'], 999)
        card['reviewed'] = True
        with self.assertRaisesRegex(StationError, 'selling price'):
            self.service.save_receipt(receipt['id'], receipt)
        # Even a persisted legacy/bad review flag cannot bypass action boundaries.
        receipt['cards'][0]['reviewed'] = True;receipt['inventory_intent'] = 'new'
        self.service.store.save_receipt(receipt)
        for action in (lambda:self.service.labels(receipt['id']),
                       lambda:self.service.receive(receipt['id']),
                       lambda:self.service.accept_buy(receipt['id'], 'credit')):
            with self.assertRaises(StationError):action()

    def test_explicit_positive_price_or_deliberate_zero_confirmation_unlocks_review(self):
        text = 'Name,Game,Market Price,Quantity\nA,Gundam,9.99,1\n'
        first = self.service.import_csv(text, 'unpriced.csv', 'labels')['receipt']
        first['cards'][0].update(final_price=8.13, reviewed=True)
        first = self.service.save_receipt(first['id'], first)
        self.assertFalse(first['cards'][0]['price_missing'])
        self.assertEqual(self.service.approved_cards(first)[0]['final_price'], 8.13)
        second = self.service.import_csv(text, 'second.csv', 'labels', new_delivery=True)['receipt']
        second['cards'][0].update(price_confirmed=True, final_price=0, reviewed=True)
        second = self.service.save_receipt(second['id'], second)
        self.assertFalse(second['cards'][0]['price_missing'])
        self.assertEqual(self.service.approved_cards(second)[0]['final_price'], 0)

    def test_stock_snapshot_is_explicit_and_never_automatically_received(self):
        text = 'Product Name,Product Line,Total Quantity,TCG Marketplace Price\nA,Magic,20,1\n'
        with self.assertRaisesRegex(StationError, 'snapshot'):
            self.service.import_csv(text, 'inventory.csv', 'labels')
        self.assertEqual(self.service.store.summaries(), [])
        receipt = self.service.import_csv(text, 'inventory.csv', 'labels',
            intake_options={'confirm_snapshot': True})['receipt']
        self.assertEqual(receipt['cards'][0]['qty'], 20)
        self.assertTrue(receipt['intake_summary']['requires_quantity_confirmation'])
        self.assertEqual(receipt['inventory_status'], 'not_received')

    def test_manual_required_games_and_slab_metadata_survive_save_and_lock(self):
        for game in ('pokemon', 'magic', 'riftbound', 'onepiece', 'unionarena', 'gundam', 'sports'):
            data = {'requestId': str(uuid.uuid4()), 'mode': 'labels', 'game': game,
                    'item': {'card_name': 'Fictional Player', 'final_price': '85.13', 'qty': 1,
                             'condition': 'Graded', 'printing': 'Gold Refractor', 'grader': 'PSA', 'grade': '10',
                             'cert_number': '0000123456', 'serial_number': '03/10', 'year': '2024', 'manufacturer': 'Fixture'}}
            receipt = self.service.add_manual_item(data)
            card = receipt['cards'][0]
            self.assertEqual(card['condition'], 'PSA 10')
            self.assertEqual(card['game'], game)
            self.assertEqual(card['cert_number'], '0000123456')
            old_sku = card['sku'];card['cert_number'] = '0000123457'
            card['price_approval'] = {'retail': {'test': True}}
            self.service.store.save_receipt(receipt)
            receipt['cards'][0]['cert_number'] = '0000123458'
            receipt = self.service.save_receipt(receipt['id'], receipt)
            self.assertNotEqual(receipt['cards'][0]['sku'], old_sku)
            self.assertNotIn('price_approval', receipt['cards'][0])
            self.assertEqual(receipt['cards'][0]['serial_number'], '03/10')
            receipt['inventory_status'] = 'received';self.service.store.save_receipt(receipt)
            receipt['cards'][0]['cert_number'] = '0000123459'
            with self.assertRaisesRegex(StationError, 'locked'):
                self.service.save_receipt(receipt['id'], receipt)

    def test_marketplace_ids_never_become_square_overrides_after_save(self):
        text = 'SKU,Name,Product ID,Language,Printing,Condition,Quantity,Price\n000001234567,A,00042,English,Foil,NM,2,1.13\n'
        receipt = self.service.import_csv(text, 'app.csv', 'labels')['receipt']
        expected = receipt['cards'][0]['source_ids']
        self.assertEqual(expected['tcgplayer_sku'], '000001234567')
        self.assertEqual(receipt['cards'][0]['sku_override'], '')
        receipt = self.service.save_receipt(receipt['id'], receipt)
        self.assertEqual(receipt['cards'][0]['source_ids'], expected)
        self.assertEqual(receipt['cards'][0]['sku_override'], '')

    def test_card_game_edits_keep_batch_scope_consistent_and_cannot_erase_identity(self):
        text = 'Name,Game,Price,Quantity\nA,Gundam,1,1\nB,Gundam,2,1\n'
        receipt = self.service.import_csv(text, 'games.csv', 'labels')['receipt']
        receipt['cards'][0]['game'] = 'riftbound';receipt['cards'][0]['game_label'] = 'Riftbound'
        receipt = self.service.save_receipt(receipt['id'], receipt)
        self.assertTrue(receipt['mixed_games']);self.assertEqual(receipt['pricing_game'], '')
        receipt['cards'][0]['game'] = ''
        with self.assertRaisesRegex(StationError, 'explicit game'):
            self.service.save_receipt(receipt['id'], receipt)
    def test_legacy_sku_and_explicit_square_override_are_preserved(self):
        legacy = {'card_name': 'Card', 'card_num': '001', 'card_set': 'Set', 'condition': 'NM',
                  'printing': 'Normal', 'language': 'English'}
        expected = core.generate_sku('Card', '001', 'NM', 'Normal', 0, 'English', 'Set')
        self.assertEqual(core.generate_card_sku(legacy), expected)
        text = 'Name,Game,Price,Quantity,Square SKU\nCard,Gundam,1,1,000001234567\n'
        receipt = self.service.import_csv(text, 'mapped.csv', 'labels')['receipt']
        receipt = self.service.save_receipt(receipt['id'], receipt)
        self.assertEqual(receipt['cards'][0]['sku'], '000001234567')

    def test_cli_strictness_and_legacy_quarter_price_behavior(self):
        path = self.root / 'scan.csv'
        path.write_text('Card Name,Price,Bin,Qty\nA,1.13,1,1\n')
        self.assertEqual(core.parse_roca_csv(str(path))[0]['final_price'], 1.25)
        for text in ('Card Name,Price,Bin,Qty\nA,-12.00,1,1\n',
                     'Card Name,Market Price,Bin,Qty\nA,12.00,1,1\n'):
            path.write_text(text)
            with self.assertRaises(ValueError):core.parse_roca_csv(str(path))

    def test_malformed_import_is_atomic_and_source_errors_are_actionable(self):
        text = 'Name,Price,Quantity\nGood,1,1\nBad,-1,1\n'
        with self.assertRaisesRegex(StationError, 'row 3'):
            self.service.import_csv(text, 'bad.csv', 'labels')
        self.assertEqual(self.service.store.summaries(), [])


if __name__ == '__main__':unittest.main(verbosity=2)
