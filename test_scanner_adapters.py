import csv
import io
import unittest
from unittest.mock import patch

from scanner_adapters import (IntakeValidationError, identity_key, normalize_game,
                              normalize_manual_card, parse_csv, parse_csv_strict,
                              spreadsheet_safe)


def fixture(headers, *rows):
    out = io.StringIO(newline='');writer = csv.writer(out)
    writer.writerow(headers);writer.writerows(rows)
    return out.getvalue()


class ScannerIntakeTests(unittest.TestCase):
    def test_existing_roca_fields_and_all_bins_are_preserved_without_price_rounding(self):
        text = fixture(['Card Name', 'Card #', 'Set Code', 'Condition', 'Printing', 'Language', 'Price', 'Bin', 'Qty'],
                       ['Pikachu', '006/165', 'MEW', 'NM', 'Reverse Holo', 'JP', '1.13', '2', '3'])
        result = parse_csv_strict(text, default_game='Pokémon')
        self.assertEqual(result.adapter, 'roca')
        card = result.cards[0]
        self.assertEqual((card['game'], card['card_num'], card['card_set']), ('pokemon', '006/165', 'MEW'))
        self.assertEqual((card['condition'], card['printing'], card['language']), ('Near Mint', 'Reverse Holofoil', 'Japanese'))
        self.assertEqual((card['price_cents'], card['final_price'], card['qty'], card['bin']), (113, 1.13, 3, '2'))
        self.assertFalse(card['reviewed'])

    def test_required_games_and_additional_games_remain_distinct(self):
        names = ['Pokémon', 'Magic: The Gathering', 'Riftbound', 'One Piece Card Game',
                 'Union Arena', 'Gundam Card Game', 'Sports Cards', 'Flesh and Blood', 'Yu-Gi-Oh!']
        expected = ['pokemon', 'magic', 'riftbound', 'onepiece', 'unionarena', 'gundam',
                    'sports', 'other:flesh-and-blood', 'other:yu-gi-oh']
        text = fixture(['Name', 'Game', 'Price', 'Quantity'], *[['A', name, '2', 1] for name in names])
        result = parse_csv_strict(text)
        self.assertEqual([c['game'] for c in result.cards], expected)
        self.assertEqual(len({c['intake_identity'] for c in result.cards}), len(names))
        self.assertEqual(result.cards[-2]['game_label'], 'Flesh and Blood')

    def test_missing_game_is_visible_and_not_relabelled_as_pokemon(self):
        result = parse_csv_strict('Name,Price\nA,1.00\n')
        self.assertEqual(result.cards[0]['game'], '')
        self.assertIn('missing_game', {i.code for i in result.issues})

    def test_tcgplayer_app_sku_cannot_become_square_sku(self):
        text = fixture(['SKU', 'Name', 'Product ID', 'Language', 'Printing', 'Condition', 'Quantity', 'Price'],
                       ['000001234567', 'Card', '00042', 'English', 'Surge Foil', 'LP', 2, '3.19'])
        result = parse_csv_strict(text, default_game='Magic')
        self.assertEqual(result.adapter, 'tcgplayer_app')
        self.assertEqual(result.cards[0]['source_ids'], {'tcgplayer_sku': '000001234567', 'source_product_id': '00042'})
        self.assertEqual(result.cards[0]['sku_override'], '')
        self.assertEqual(result.cards[0]['printing'], 'Surge Foil')

    def test_generic_sku_preserved_as_unconfirmed_source_identifier(self):
        result = parse_csv_strict('Name,SKU,Price\nCard,000001234567,1\n')
        self.assertEqual(result.cards[0]['source_ids']['source_sku'], '000001234567')
        self.assertEqual(result.cards[0]['sku_override'], '')
        self.assertIn('ambiguous_sku', {i.code for i in result.issues})

    def test_only_explicit_square_sku_is_validated_for_override(self):
        result = parse_csv_strict('Name,Square SKU,Price\nCard,000001234567,1\n')
        self.assertEqual(result.cards[0]['sku_override'], '000001234567')
        for value in ('123', '1E+11', '=123456789012', '１２３４５６７８９０１２'):
            with self.subTest(value=value), self.assertRaises(IntakeValidationError):
                parse_csv_strict(f'Name,Square SKU,Price\nCard,{value},1\n')

    def test_tcgplayer_inventory_condition_finish_and_price_roles(self):
        text = fixture(['TCGplayer ID', 'Product Line', 'Set Name', 'Product Name', 'Number', 'Condition',
                        'TCG Market Price', 'TCG Marketplace Price', 'Add to Quantity'],
                       ['0042', 'Magic: The Gathering', 'Set A', 'A', '001', 'Near Mint Foil', '5.99', '6.13', 3])
        result = parse_csv_strict(text)
        card = result.cards[0]
        self.assertEqual((result.adapter, result.quantity_semantics), ('tcgplayer_inventory', 'add'))
        self.assertEqual((card['condition'], card['printing']), ('Near Mint', 'Foil'))
        self.assertEqual((card['price_cents'], card['market_price_cents']), (613, 599))
        self.assertEqual(card['source_ids']['tcgplayer_sku'], '0042')

    def test_market_reference_is_never_implicitly_approved_label_price(self):
        result = parse_csv_strict('Name,Market Price\nA,9.99\n')
        self.assertIsNone(result.cards[0]['final_price'])
        self.assertEqual(result.cards[0]['market_price_cents'], 999)
        self.assertIn('missing_sale_price', {i.code for i in result.issues})
        explicit = parse_csv_strict('Name,Market Price\nA,9.99\n', price_column='Market Price')
        self.assertEqual(explicit.cards[0]['final_price'], 9.99)

    def test_snapshot_and_add_quantities_require_deliberate_column_choice(self):
        text = 'Product Name,Product Line,Add to Quantity,Total Quantity,TCG Marketplace Price\nA,Magic,2,20,1\n'
        result = parse_csv(text)
        self.assertFalse(result.can_import)
        self.assertEqual(result.issues[0].code, 'quantity_selection')
        add = parse_csv_strict(text, quantity_column='Add to Quantity')
        self.assertEqual(add.cards[0]['qty'], 2)
        self.assertFalse(add.requires_quantity_confirmation)
        snapshot = parse_csv_strict(text, quantity_column='Total Quantity')
        self.assertEqual(snapshot.cards[0]['qty'], 20)
        self.assertTrue(snapshot.requires_quantity_confirmation)
        self.assertEqual(snapshot.quantity_semantics, 'snapshot')

    def test_duplicate_quantity_aliases_must_agree_or_be_explicitly_mapped(self):
        with self.assertRaises(IntakeValidationError):
            parse_csv_strict('Name,Qty,Quantity,Price\nA,2,20,1\n')
        mapped = parse_csv_strict('Name,Qty,Quantity,Price\nA,2,20,1\n', quantity_column='Quantity')
        self.assertEqual(mapped.cards[0]['qty'], 20)
        fallback = parse_csv_strict('Name,Qty,Quantity,Price\nA,,2,1\n')
        self.assertEqual(fallback.cards[0]['qty'], 2)

    def test_conflicting_condition_finish_is_not_silently_repaired(self):
        with self.assertRaises(IntakeValidationError):
            parse_csv_strict('Name,Condition,Printing,Price\nA,Near Mint Foil,Normal,1\n')

    def test_malformed_or_dangerous_money_never_becomes_a_positive_price(self):
        for price in ('-12.00', '(12.00)', '=12+4', '=HYPERLINK("x")', '1e3', 'NaN', 'Infinity', 'USD 1',
                      '1.001', '$1,00.00', '1000000.01', '--5', '4-2'):
            with self.subTest(price=price), self.assertRaises(IntakeValidationError):
                parse_csv_strict(fixture(['Name', 'Price'], ['A', price]))
        result = parse_csv_strict(fixture(['Name', 'Price'], ['A', '$1,234.50']))
        self.assertEqual(result.cards[0]['price_cents'], 123450)

    def test_non_usd_cannot_silently_become_usd(self):
        with self.assertRaises(IntakeValidationError):
            parse_csv_strict('Name,Price,Currency\nA,1,EUR\n')

    def test_invalid_quantities_reject_entire_batch_instead_of_truncating_or_absorbing(self):
        for qty in ('0', '-1', '1.5', '1e2', '=2', '100001', ''):
            text = fixture(['Name', 'Price', 'Quantity'], ['Good', '1', 2], ['Bad', '1', qty])
            preview = parse_csv(text)
            self.assertEqual(len(preview.cards), 1)
            self.assertFalse(preview.can_import)
            with self.subTest(qty=qty), self.assertRaises(IntakeValidationError):preview.require_valid()

    def test_empty_names_extra_columns_duplicate_headers_and_bad_quotes_are_errors(self):
        fixtures = ['Name,Price\n,2\n', 'Name,Price\nA,2,Extra\n', 'Name, name ,Price\nA,A,2\n',
                    'Name,Price\n"unclosed,2\n', 'Name,,Price\nA,,2\n']
        for text in fixtures:
            with self.subTest(text=text), self.assertRaises(IntakeValidationError):parse_csv_strict(text)

    def test_unmapped_conflicting_aliases_do_not_silently_choose_identity(self):
        text = 'Name,Card Name,Set Code,Set Name,Price\nA,B,SET,Set Long Name,1\n'
        with self.assertRaises(IntakeValidationError):parse_csv_strict(text)
        mapped = parse_csv_strict(text, column_mapping={'card_name': 'Card Name', 'card_set': 'Set Name'})
        self.assertEqual((mapped.cards[0]['card_name'], mapped.cards[0]['card_set']), ('B', 'Set Long Name'))

    def test_mapping_validates_missing_unknown_and_explicit_price_sources(self):
        for options in ({'column_mapping': {'card_name': 'Missing'}}, {'column_mapping': {'password': 'Name'}},
                        {'quantity_column': 'Missing'}, {'price_column': 'Missing'}, {'adapter': 'imaginary-vendor'}):
            with self.subTest(options=options), self.assertRaises(IntakeValidationError):
                parse_csv_strict('Name,Price\nA,1\n', **options)

    def test_bom_utf8_tsv_and_semicolon_are_supported(self):
        for text in ('\ufeffName,Price,Quantity\nÉowyn,1,2\n', 'Name\tPrice\tQuantity\nÉowyn\t1\t2\n',
                     'Name;Price;Quantity\nÉowyn;1;2\n'):
            result = parse_csv_strict(text.encode('utf-8'))
            self.assertEqual(result.cards[0]['card_name'], 'Éowyn')

    def test_binary_encoding_control_characters_and_field_limits_fail_clearly(self):
        for text in (b'Name,Price\n\xff,1\n', 'Name,Price\nA\x00B,1\n', 'Name,Price\nA\x01B,1\n',
                     fixture(['Name', 'Price'], ['A' * 501, 1])):
            with self.subTest(text=str(text)[:30]), self.assertRaises(IntakeValidationError):parse_csv_strict(text)

    def test_row_limit_counts_invalid_rows_too(self):
        with patch('scanner_adapters.MAX_ROWS', 2):
            result = parse_csv('Name,Price\n,1\n,2\n,3\n')
        self.assertIn('row_limit', {i.code for i in result.issues})
        self.assertEqual(len([issue for issue in result.issues if issue.severity == 'error']), 3)

    def test_grade_certificate_and_serial_variants_stay_in_identity(self):
        base = {'card_name': 'Player A', 'card_num': '001', 'card_set': 'Set', 'condition': 'Graded',
                'printing': 'Gold Refractor', 'language': 'English', 'qty': 1, 'final_price': '85.13',
                'grader': 'PSA', 'grade': '10', 'cert_number': '0000123456', 'serial_number': '03/10',
                'year': '2024', 'manufacturer': 'Topps', 'team': 'Team A', 'player': 'Player A'}
        first = normalize_manual_card(base, game='Sports')
        self.assertEqual(first['grade'], '10')
        self.assertEqual(first['cert_number'], '0000123456')
        self.assertEqual(first['final_price'], 85.13)
        self.assertEqual(first['item_type'], 'slab')
        normalized_grade = normalize_manual_card(base | {'grade': '9.0'}, game='Sports')
        self.assertEqual((normalized_grade['grade'], normalized_grade['condition']), ('9', 'PSA 9'))
        for field, value in (('cert_number', '0000123457'), ('grade', '9'), ('serial_number', '04/10'), ('year', '2025')):
            other = normalize_manual_card(base | {field: value}, game='Sports')
            self.assertNotEqual(first['intake_identity'], other['intake_identity'])

    def test_scanner_sequence_and_image_reference_preserve_stack_traceability(self):
        card = parse_csv_strict('Card Name,Price,Bin,Import Sequence ID,Image Filename\nA,1,2,00007,00007-front.png\n').cards[0]
        self.assertEqual(card['source_ids']['source_sequence'], '00007')
        self.assertEqual(card['source_ids']['source_image'], '00007-front.png')

    def test_slab_conflicts_and_incomplete_grading_do_not_become_raw_nm_cards(self):
        for card in ({'grader': 'PSA'}, {'grade': '10'}, {'cert_number': '001'},
                     {'grader': 'PSA', 'grade': '10', 'condition': 'Near Mint'}):
            with self.subTest(card=card), self.assertRaises(IntakeValidationError):
                normalize_manual_card({'card_name': 'A', 'final_price': '1'} | card, game='Pokemon')
        with self.assertRaises(IntakeValidationError):
            normalize_manual_card({'card_name': 'A', 'final_price': '1', 'grader': 'PSA',
                                   'grade': '10', 'cert_number': '00012', 'qty': 2}, game='Pokemon')

    def test_manual_input_cannot_set_reviewed_or_inclusion_flags(self):
        card = normalize_manual_card({'card_name': 'A', 'final_price': '1.01', 'qty': 1,
                                      'reviewed': True, 'include': False}, game='Gundam')
        self.assertFalse(card['reviewed']);self.assertTrue(card['include'])
        self.assertEqual(card['entry_source'], 'manual')
        for value in (None, [], 'A'):
            with self.assertRaises(IntakeValidationError):normalize_manual_card(value)
        with self.assertRaises(IntakeValidationError):normalize_manual_card({'card_name': 'A'})

    def test_spreadsheet_injection_is_neutralized_only_at_export_boundary(self):
        for text in ('=HYPERLINK("x")', '+SUM(1)', '-SUM(1)', '@SUM(1)', '  =1', '\tHello', '\rHello', '\nHello'):
            self.assertTrue(spreadsheet_safe(text).startswith("'"))
        self.assertEqual(spreadsheet_safe('Normal Card'), 'Normal Card')
        card = parse_csv_strict(fixture(['Name', 'Price'], ['=Not a formula in stored identity', '1'])).cards[0]
        self.assertEqual(card['card_name'], '=Not a formula in stored identity')
        self.assertNotEqual(identity_key(card), identity_key(card | {'card_name': "'" + card['card_name']}))


if __name__ == '__main__':unittest.main(verbosity=2)
