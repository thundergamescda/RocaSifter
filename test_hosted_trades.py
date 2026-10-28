"""Accepted-trade parsing, money and HTTP boundaries; no provider/database calls."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
import uuid
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.security import ServiceError
from hosted.trades import Trades, normalize_lines, normalize_offer_changes, calculate_offer


def card_line(*, name='Fictional card', game='Pokémon', quantity=2, cents=1000, reviewed=True):
    return {'identity': {'name': name, 'game': game, 'kind': 'card', 'set': 'Fictional set', 'number': '001'},
            'quantity': quantity, 'unit_intake_cents': cents, 'included': True, 'reviewed': reviewed}


def draft_data(*, customer_id=None, request_key='fixture-create'):
    return {'request_key': request_key, 'customer_id': str(customer_id or uuid.uuid4()),
            'title': 'Fictional customer trade', 'source_reference': 'fictional-local-receipt', 'lines': [card_line()]}


def acceptance_data(customer_id, *, request_key='fixture-accept', version=0, offer='credit', cents=1000):
    return {'request_key': request_key, 'version': version, 'customer_id': str(customer_id),
            'offer_type': offer, 'expected_offer_cents': cents, 'customer_agreed': True, 'items_received': True}


class TradeLineAndOfferTests(unittest.TestCase):
    def test_supported_and_additional_game_names_are_preserved_without_invented_price_lookup(self):
        for game in ('Pokémon', 'Magic', 'Riftbound', 'One Piece', 'Union Arena', 'Gundam', 'Sports', 'Additional scanner game'):
            with self.subTest(game=game):self.assertEqual(normalize_lines([card_line(game=game)])[0]['identity']['game'], game)

    def test_slab_requires_complete_card_certification_facts(self):
        line = card_line(quantity=1);line['identity'].update(kind='slab', grading_company='Fictional Grader', grade='9.5', certification='SLAB-TEST-001')
        result = normalize_lines([line])[0]
        self.assertEqual((result['identity']['kind'], result['identity']['grade']), ('slab', '9.5'))
        for missing in ('grading_company', 'grade', 'certification'):
            value = deepcopy(line);value['identity'].pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ServiceError):normalize_lines([value])

    def test_grade_qualifier_serial_manufacturer_and_additional_game_label_are_preserved(self):
        line = card_line(quantity=1, game='other:fixture-game')
        line['identity'].update(kind='slab', game_label='Fictional additional game', grading_company='Fictional Grader',
                                grade='10', grade_qualifier='Pristine black label', certification='FIXTURE-CERT',
                                serial_number='07/25', manufacturer='Fictional manufacturer')
        identity = normalize_lines([line])[0]['identity']
        self.assertEqual((identity['game_label'], identity['grade_qualifier'], identity['serial_number'], identity['manufacturer']),
                         ('Fictional additional game', 'Pristine black label', '07/25', 'Fictional manufacturer'))

    def test_certified_slab_quantity_and_certificate_uniqueness_are_enforced(self):
        line = card_line(quantity=2);line['identity'].update(kind='slab', grading_company='Fictional Grader', grade='10', certification='FIXTURE-CERT')
        with self.assertRaises(ServiceError):normalize_lines([line])
        line['quantity'] = 1
        with self.assertRaises(ServiceError):normalize_lines([line, deepcopy(line)])

    def test_additional_game_key_requires_its_display_label(self):
        with self.assertRaises(ServiceError):normalize_lines([card_line(game='other:fixture-game')])

    def test_raw_card_cannot_hide_slab_certification_fields(self):
        line = card_line();line['identity']['grade'] = '10'
        with self.assertRaises(ServiceError):normalize_lines([line])

    def test_sensitive_unneeded_identity_fields_are_rejected(self):
        for field in ('government_id', 'date_of_birth', 'payment_card_number', 'customer_photo', 'address'):
            line = card_line();line['identity'][field] = 'not-collected'
            with self.subTest(field=field), self.assertRaises(ServiceError):normalize_lines([line])

    def test_names_games_and_item_types_are_validated(self):
        for patch in ({'name': ''}, {'game': ''}, {'name': 'x'*201}, {'name': 'line\nbreak'}, {'kind': 'unsupported'}):
            line = card_line();line['identity'].update(patch)
            with self.subTest(patch=patch), self.assertRaises(ServiceError):normalize_lines([line])

    def test_quantities_and_intake_money_are_integer_cents(self):
        for quantity in (True, 0, -1, 1.2, '2', 100001):
            with self.subTest(quantity=quantity), self.assertRaises(ServiceError):normalize_lines([card_line(quantity=quantity)])
        for cents in (True, -1, 1.5, '100', 100000001, None):
            with self.subTest(cents=cents), self.assertRaises(ServiceError):normalize_lines([card_line(cents=cents)])

    def test_line_and_aggregate_intake_caps_are_enforced(self):
        with self.assertRaises(ServiceError):normalize_lines([card_line(quantity=2, cents=100000000)])
        with self.assertRaises(ServiceError):normalize_lines([card_line(quantity=1, cents=60000000), card_line(quantity=1, cents=60000000)])

    def test_review_and_inclusion_flags_are_real_booleans(self):
        for field in ('reviewed', 'included'):
            for value in ('true', 1, None):
                line = card_line();line[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ServiceError):normalize_lines([line])

    def test_duplicate_line_ids_are_rejected(self):
        ident = str(uuid.uuid4());one = card_line();one['id'] = ident
        two = card_line();two['id'] = ident
        with self.assertRaises(ServiceError):normalize_lines([one, two])

    def test_normalization_does_not_generate_new_ids_before_request_fingerprinting(self):
        one = normalize_lines([card_line()]);two = normalize_lines([card_line()])
        self.assertEqual(one, two);self.assertIsNone(one[0]['id'])

    def test_empty_or_oversized_trades_fail_explicitly(self):
        for lines in (None, {}, [], [card_line()] * 1001):
            with self.subTest(size=len(lines) if hasattr(lines, '__len__') else None), self.assertRaises(ServiceError):normalize_lines(lines)

    def test_a_typical_thousand_line_trade_keeps_exact_money_without_blank_field_expansion(self):
        import json
        lines = normalize_lines([card_line()] * 1000)
        self.assertNotIn('manufacturer', lines[0]['identity'])
        self.assertLess(len(json.dumps(lines).encode()), 512 * 1024)
        totals = calculate_offer(lines, 3500, 5000)
        self.assertEqual((totals['intake_total_cents'], totals['credit_offer_cents'], totals['included_quantity']), (2000000, 1000000, 2000))

    def test_offers_round_half_up_once_at_the_whole_trade_total(self):
        values = calculate_offer(normalize_lines([card_line(quantity=1, cents=101)]), 3500, 5000)
        self.assertEqual((values['intake_total_cents'], values['cash_offer_cents'], values['credit_offer_cents']), (101, 35, 51))

    def test_excluded_cards_do_not_contribute_to_offer_or_block_included_review(self):
        included = card_line();excluded = card_line(cents=5000, reviewed=False);excluded['included'] = False
        values = calculate_offer(normalize_lines([included, excluded]), 3500, 5000)
        self.assertEqual((values['intake_total_cents'], values['credit_offer_cents'], values['all_included_reviewed']), (2000, 1000, True))

    def test_all_excluded_or_unreviewed_lines_cannot_be_accepted(self):
        line = card_line();line['included'] = False
        self.assertFalse(calculate_offer(normalize_lines([line]), 3500, 5000)['all_included_reviewed'])
        self.assertFalse(calculate_offer(normalize_lines([card_line(reviewed=False)]), 3500, 5000)['all_included_reviewed'])

    def test_negotiated_offer_is_explicit_and_preserves_formula_intake_total(self):
        lines = normalize_lines([card_line()]);values = calculate_offer(lines, 3500, 5000, credit_override_cents=1250)
        self.assertEqual((values['intake_total_cents'], values['cash_offer_cents'], values['credit_offer_cents']), (2000, 700, 1250))
        self.assertEqual(normalize_offer_changes({'credit_override_cents': 1250, 'override_reason': 'Fictional negotiated offer'})['credit_override_cents'], 1250)

    def test_custom_rates_and_overrides_require_valid_ranges_and_reason(self):
        for value in ({'credit_bps': 5001}, {'cash_override_cents': 100}, {'cash_bps': True, 'override_reason': 'test'},
                      {'credit_bps': 10001, 'override_reason': 'test'}, {'credit_override_cents': 1.5, 'override_reason': 'test'},
                      {'credit_override_cents': 100, 'override_reason': ''}):
            with self.subTest(value=value), self.assertRaises(ServiceError):normalize_offer_changes(value)


class TradeRoleAndIntentTests(unittest.TestCase):
    def setUp(self):
        self.db = Mock();self.trades = Trades(self.db)
        self.actor = {'tenant_id': uuid.uuid4(), 'staff_id': uuid.uuid4(), 'role': 'cashier'}
        self.ident = uuid.uuid4();self.customer = uuid.uuid4()

    def test_cashier_cannot_read_draft_costs_or_create_update_accept_void(self):
        calls = [lambda: self.trades.create(self.actor, {}), lambda: self.trades.get(self.actor, self.ident),
                 lambda: self.trades.list(self.actor), lambda: self.trades.update(self.actor, self.ident, {}),
                 lambda: self.trades.accept(self.actor, self.ident, {}), lambda: self.trades.void(self.actor, self.ident, {})]
        for call in calls:
            with self.assertRaises(ServiceError) as error:call()
            self.assertEqual(error.exception.status, 403)
        self.db.transaction.assert_not_called()

    def test_buyer_can_draft_but_cannot_accept_or_override_offer(self):
        actor = self.actor | {'role': 'buyer'}
        with self.assertRaises(ServiceError) as error:self.trades.accept(actor, self.ident, acceptance_data(self.customer))
        self.assertEqual(error.exception.status, 403)
        data = draft_data(customer_id=self.customer);data['offer_terms'] = {'credit_bps': 6000, 'override_reason': 'Fictional override'}
        with self.assertRaises(ServiceError) as error:self.trades.create(actor, data)
        self.assertEqual(error.exception.status, 403);self.db.transaction.assert_not_called()

    def test_voided_source_replacement_is_explicit_and_uses_stable_source_reference(self):
        actor = self.actor | {'role': 'buyer'}
        for patch in ({'replace_voided_source': 'true'}, {'replace_voided_source': 1},
                      {'replace_voided_source': True, 'source_reference': ''}):
            data = draft_data(customer_id=self.customer);data.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ServiceError):self.trades.create(actor, data)
        self.db.transaction.assert_not_called()

    def test_acceptance_rejects_submitted_amount_origin_or_policy_approval(self):
        actor = self.actor | {'role': 'manager'}
        for field, value in (('amount_cents', 999999), ('origin_code', 'approved-trade'), ('policy_approved', True)):
            data = acceptance_data(self.customer);data[field] = value
            with self.subTest(field=field), self.assertRaises(ServiceError):self.trades.accept(actor, self.ident, data)
        self.db.transaction.assert_not_called()

    def test_customer_agreement_and_physical_receipt_are_explicit(self):
        actor = self.actor | {'role': 'owner'}
        for field in ('customer_agreed', 'items_received'):
            for value in (False, 'true', 1, None):
                data = acceptance_data(self.customer);data[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ServiceError):self.trades.accept(actor, self.ident, data)
        self.db.transaction.assert_not_called()

    def test_acceptance_customer_version_and_expected_offer_are_validated(self):
        actor = self.actor | {'role': 'owner'}
        for patch in ({'version': True}, {'version': -1}, {'customer_id': 'bad'}, {'offer_type': 'gift_card'},
                      {'expected_offer_cents': 0}, {'expected_offer_cents': True}, {'expected_offer_cents': '1000'}):
            data = acceptance_data(self.customer);data.update(patch)
            with self.subTest(patch=patch), self.assertRaises(ServiceError):self.trades.accept(actor, self.ident, data)
        self.db.transaction.assert_not_called()


class TradeHTTPBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': 'https://cloud.example.test',
                               'APP_ENCRYPTION_KEYS': Fernet.generate_key().decode()}, database=Mock())
        self.client = self.app.test_client();self.auth = self.app.extensions['ss_auth'];self.trades = self.app.extensions['ss_trades']
        self.actor = {'tenant_id': uuid.uuid4(), 'staff_id': uuid.uuid4(), 'role': 'owner', 'slug': 'fixture-store'}
        self.auth.resolve = Mock(return_value=self.actor);self.ident = uuid.uuid4()

    def request(self, method, path, **kwargs):
        kwargs.setdefault('headers', {'Authorization': 'Bearer fixture'})
        return self.client.open(path, method=method, base_url='https://cloud.example.test', **kwargs)

    def test_trade_create_get_update_accept_void_use_authenticated_actor(self):
        for name, method, path in (('create', 'POST', '/api/trades'), ('get', 'GET', '/api/trades/' + str(self.ident)),
                ('update', 'PUT', '/api/trades/' + str(self.ident)), ('accept', 'POST', '/api/trades/' + str(self.ident) + '/accept'),
                ('void', 'POST', '/api/trades/' + str(self.ident) + '/void')):
            operation = Mock(return_value={'trade_id': str(self.ident)});setattr(self.trades, name, operation)
            result = self.request(method, path, **({} if method == 'GET' else {'json': {'request_key': 'fixture'}}))
            self.assertEqual(result.status_code, 201 if name == 'create' else 200)
            self.assertEqual(operation.call_args.args[0], self.actor)

    def test_unauthenticated_trade_routes_are_rejected(self):
        self.auth.resolve.side_effect = ServiceError('Sign in.', 401)
        for method, path in (('GET', '/api/trades'), ('POST', '/api/trades'), ('PUT', '/api/trades/' + str(self.ident)),
                             ('POST', '/api/trades/' + str(self.ident) + '/accept')):
            self.assertEqual(self.request(method, path, **({} if method == 'GET' else {'json': {}})).status_code, 401)

    def test_client_cannot_choose_tenant_in_trade_body(self):
        self.trades.create = Mock()
        result = self.request('POST', '/api/trades', json={'tenant_id': str(uuid.uuid4())})
        self.assertEqual(result.status_code, 400);self.trades.create.assert_not_called()

    def test_malformed_page_size_is_an_actionable_client_error(self):
        self.assertEqual(self.request('GET', '/api/trades?limit=oops').status_code, 400)

    def test_trade_list_queries_are_scoped_by_session_actor(self):
        self.trades.list = Mock(return_value={'trades': [], 'next_cursor': None})
        result = self.request('GET', '/api/trades?q=fixture&limit=25')
        self.assertEqual(result.status_code, 200)
        self.trades.list.assert_called_once_with(self.actor, 'fixture', limit=25, cursor=None)

    def test_trade_acceptance_preserves_cookie_csrf_protection(self):
        self.auth.login = Mock(return_value=('fixture-cookie', {'store': 'fixture-store'}))
        login = self.client.post('/api/auth/login', base_url='https://cloud.example.test', json={})
        self.trades.accept = Mock(return_value={'state': 'accepted_credit'})
        path = '/api/trades/' + str(self.ident) + '/accept'
        self.assertEqual(self.client.post(path, base_url='https://cloud.example.test', json={}).status_code, 403)
        self.assertEqual(self.client.post(path, base_url='https://cloud.example.test', json={}, headers={'X-SS-CSRF': login.json['csrf_token']}).status_code, 200)


if __name__ == '__main__':unittest.main(verbosity=2)
