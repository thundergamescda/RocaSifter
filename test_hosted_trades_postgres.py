"""Actual accepted-trade transactions in isolated private PostgreSQL schemas.

All people/cards/approvals are fictional. Tests never query application tables
outside ss_test_<uuid>, enable checkout, call Square or transform legacy data.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import patch
import unittest
import uuid
import psycopg
from hosted.ledger import Ledger
from hosted.security import ServiceError
from hosted.trades import Trades
from hosted.app import create_app
from hosted.security import password_hash, digest
from intake_manifest import source_line_id
from test_intake_fixtures import fresh_mfa_actor, claim_draft
from cryptography.fernet import Fernet
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import draft_data, acceptance_data, card_line


class PostgreSQLTradeAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        self.tenant, self.customer = uuid.uuid4(), uuid.uuid4()
        self.actors = {role: {'tenant_id': self.tenant, 'staff_id': uuid.uuid4(), 'role': role}
                       for role in ('owner', 'manager', 'buyer', 'cashier')}
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)', (self.tenant, 'fixture-' + self.tenant.hex, 'Fictional trade store'))
            for role, actor in self.actors.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',
                           (actor['staff_id'], self.tenant, role + '@example.invalid', 'fixture-invalid-password-hash', role))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Fictional trade customer','fixture-only')", (self.customer, self.tenant))
            for role in self.actors:
                self.actors[role] = fresh_mfa_actor(db, self.actors[role])
        self.trades = Trades(self.database)
        no_provider = patch('requests.sessions.Session.request', side_effect=AssertionError('Provider HTTP is forbidden in trade acceptance tests.'))
        no_provider.start();self.addCleanup(no_provider.stop)

    def create(self, *, actor=None, source=None, lines=None, offers=None, title=None):
        data = draft_data(customer_id=self.customer, request_key='create-' + uuid.uuid4().hex)
        data['source_reference'] = source or 'local-receipt-' + uuid.uuid4().hex
        if lines is not None:data['lines'] = lines
        if offers is not None:data['offer_terms'] = offers
        if title is not None:data['title'] = title
        data, _ = claim_draft(self.database, self.actors['owner'], data)
        return self.trades.create(actor or self.actors['buyer'], data), data

    def accept(self, trade, *, actor=None, key=None, offer='credit', cents=1000, version=None, customer=None):
        data = acceptance_data(customer or self.customer, request_key=key or 'accept-' + uuid.uuid4().hex,
                               version=trade['version'] if version is None else version, offer=offer, cents=cents)
        data['claim_reviewed'] = True
        return self.trades.accept(actor or self.actors['owner'], trade['trade_id'], data)

    def wallet(self, customer=None):
        with self.database.transaction() as db:
            person = db.execute('SELECT balance_cents,version FROM ss_customers WHERE tenant_id=%s AND id=%s', (self.tenant, customer or self.customer)).fetchone()
            entries = db.execute('SELECT id,kind,amount_cents,source_key,reference FROM ss_credit_entries WHERE tenant_id=%s AND customer_id=%s ORDER BY at,id', (self.tenant, customer or self.customer)).fetchall()
            lots = db.execute('SELECT origin_code,policy_id,policy_version,issued_cents,remaining_cents FROM ss_credit_lots WHERE tenant_id=%s AND customer_id=%s', (self.tenant, customer or self.customer)).fetchall()
        return person, entries, lots

    def test_draft_persists_reviewed_lines_and_formula_offers_without_issuing_credit(self):
        trade, _ = self.create()
        receipt = self.trades.get(self.actors['buyer'], trade['trade_id'])
        self.assertEqual((receipt['state'], receipt['intake_total_cents'], receipt['cash_offer_cents'], receipt['credit_offer_cents']), ('draft', 2000, 700, 1000))
        self.assertEqual((len(receipt['lines']), receipt['lines'][0]['identity']['game'], receipt['lines'][0]['reviewed']), (1, 'Pokémon', True))
        person, entries, lots = self.wallet()
        self.assertEqual((person['balance_cents'], entries, lots), (0, [], []))

    def test_accept_credit_creates_one_receipt_bound_entry_and_unreviewed_lot(self):
        trade, _ = self.create();result = self.accept(trade)
        person, entries, lots = self.wallet()
        self.assertEqual((result['state'], result['accepted_amount_cents'], result['credit_policy_state'], result['checkout_eligible']), ('accepted_credit', 1000, 'unreviewed', False))
        self.assertEqual((person['balance_cents'], person['version'], len(entries), len(lots)), (1000, 1, 1, 1))
        self.assertEqual((entries[0]['kind'], entries[0]['source_key'], entries[0]['reference']), ('trade_credit', 'trade:' + trade['trade_id'], trade['trade_id']))
        self.assertEqual((lots[0]['origin_code'], lots[0]['policy_id'], lots[0]['policy_version']), ('trade_unreviewed', None, None))
        receipt = self.trades.get(self.actors['owner'], trade['trade_id'])['accepted_receipt']
        self.assertEqual((receipt['accepted_by'], receipt['customer']['id'], receipt['accepted_amount_cents']), (str(self.actors['owner']['staff_id']), str(self.customer), 1000))

    def test_cash_acceptance_records_agreement_without_credit_or_payment_claim(self):
        trade, _ = self.create();result = self.accept(trade, offer='cash', cents=700)
        self.assertEqual((result['state'], result['credit_entry_id'], result['cash_payment_executed']), ('accepted_cash', None, False))
        person, entries, lots = self.wallet()
        self.assertEqual((person['balance_cents'], person['version'], entries, lots), (0, 0, [], []))
        self.assertFalse(self.trades.get(self.actors['owner'], trade['trade_id'])['accepted_receipt']['cash_payment_executed'])

    def test_lost_response_replay_returns_exact_acceptance_without_second_credit(self):
        trade, _ = self.create();key = 'lost-response'
        first = self.accept(trade, key=key)
        Ledger(self.database).adjust(self.actors['owner'], self.customer, {'request_key': 'later-correction', 'amount_cents': 25,
            'kind': 'adjust_add', 'version': 1, 'reference': 'FIXTURE', 'reason': 'Fictional later adjustment'})
        second = self.accept(trade, key=key)
        self.assertEqual(first, second);self.assertEqual(second['balance_after_cents'], 1000)
        self.assertEqual(self.wallet()[0]['balance_cents'], 1025)
        self.assertEqual(sum(entry['kind'] == 'trade_credit' for entry in self.wallet()[1]), 1)
        self.assertEqual(Ledger(self.database).request_status(self.actors['owner'], key)['result'], first)

    def test_new_request_key_with_same_frozen_terms_returns_original_acceptance(self):
        trade, _ = self.create();first = self.accept(trade, key='first-accept');second = self.accept(trade, key='retry-new-key')
        self.assertEqual(first, second);self.assertEqual(len(self.wallet()[1]), 1)

    def test_changed_post_acceptance_amount_or_offer_is_a_conflict_not_a_second_issue(self):
        trade, _ = self.create();self.accept(trade, key='accepted-original')
        for options in ({'cents': 999}, {'offer': 'cash', 'cents': 700}, {'key': 'accepted-original', 'cents': 999}):
            with self.subTest(options=options), self.assertRaises(ServiceError):self.accept(trade, **options)
        self.assertEqual((self.wallet()[0]['balance_cents'], len(self.wallet()[1])), (1000, 1))

    def test_concurrent_acceptance_issues_credit_exactly_once(self):
        trade, _ = self.create()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda index: self.accept(trade, key='till-' + str(index)), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual((self.wallet()[0]['balance_cents'], len(self.wallet()[1])), (1000, 1))

    def test_concurrent_cash_and_credit_agreements_cannot_both_be_accepted(self):
        trade, _ = self.create()
        def accept(offer):
            try:return self.accept(trade, offer=offer, cents=1000 if offer == 'credit' else 700)
            except ServiceError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(accept, ('credit', 'cash')))
        self.assertEqual(sum(result is not None for result in results), 1)
        selected = next(result for result in results if result)
        self.assertEqual(self.wallet()[0]['balance_cents'], 1000 if selected['offer_type'] == 'credit' else 0)

    def test_changed_amount_customer_or_version_cannot_redirect_or_mint_credit(self):
        trade, _ = self.create();other = uuid.uuid4()
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Other fictional customer','fixture-only')", (other, self.tenant))
        for change in ({'cents': 1001}, {'version': 1}, {'customer': other}):
            with self.subTest(change=change), self.assertRaises(ServiceError):self.accept(trade, **change)
        self.assertEqual((self.wallet()[0]['balance_cents'], self.wallet(other)[0]['balance_cents']), (0, 0))
        self.assertEqual(self.trades.get(self.actors['owner'], trade['trade_id'])['state'], 'draft')

    def test_unreviewed_or_all_excluded_cards_never_create_credit(self):
        for excluded in (False, True):
            line = card_line(reviewed=False)
            if excluded:line['included'] = False
            trade, _ = self.create(lines=[line])
            with self.assertRaises(ServiceError):self.accept(trade)
        self.assertEqual(self.wallet()[0]['balance_cents'], 0)

    def test_editing_draft_requires_fresh_acceptance_version_and_amount(self):
        trade, _ = self.create()
        line = card_line(cents=2000);line['source_line_key'] = source_line_id(line['identity'])
        saved = self.trades.update(self.actors['buyer'], trade['trade_id'], {'request_key': 'edit', 'version': 0, 'lines': [line]})
        self.assertEqual(saved['version'], 1)
        with self.assertRaises(ServiceError):self.accept(trade)
        self.assertEqual(self.accept(saved, cents=2000)['accepted_amount_cents'], 2000)

    def test_accepted_header_and_lines_are_immutable_in_domain_and_database(self):
        trade, _ = self.create();result = self.accept(trade)
        with self.assertRaises(ServiceError):self.trades.update(self.actors['owner'], trade['trade_id'], {'request_key': 'edit-final', 'version': 1, 'title': 'Changed'})
        statements = [('UPDATE ss_trade_receipts SET title=%s WHERE tenant_id=%s AND id=%s', ('Changed', self.tenant, trade['trade_id'])),
                      ('DELETE FROM ss_trade_receipts WHERE tenant_id=%s AND id=%s', (self.tenant, trade['trade_id'])),
                      ('UPDATE ss_trade_receipt_lines SET reviewed=FALSE WHERE tenant_id=%s AND trade_id=%s', (self.tenant, trade['trade_id'])),
                      ('DELETE FROM ss_trade_receipt_lines WHERE tenant_id=%s AND trade_id=%s', (self.tenant, trade['trade_id']))]
        for statement, parameters in statements:
            with self.subTest(statement=statement), self.assertRaises(psycopg.errors.RaiseException):
                with self.database.transaction() as db:db.execute(statement, parameters)
        self.assertEqual(self.trades.get(self.actors['owner'], trade['trade_id'])['receipt_hash'], result['receipt_hash'])

    def test_corrupted_cached_amount_or_lines_hash_is_not_trusted_for_issuance(self):
        for field, value in (('credit_offer_cents', 999), ('lines_hash', '0'*64)):
            trade, _ = self.create()
            with self.database.transaction() as db:
                if field == 'credit_offer_cents':db.execute('UPDATE ss_trade_receipts SET credit_offer_cents=%s WHERE tenant_id=%s AND id=%s', (value, self.tenant, trade['trade_id']))
                else:db.execute('UPDATE ss_trade_receipts SET lines_hash=%s WHERE tenant_id=%s AND id=%s', (value, self.tenant, trade['trade_id']))
            with self.assertRaises(ServiceError):self.accept(trade)
        self.assertEqual(self.wallet()[0]['balance_cents'], 0)

    def test_actual_staff_role_and_enablement_are_checked_inside_acceptance_transaction(self):
        trade, _ = self.create()
        with self.assertRaises(ServiceError):self.accept(trade, actor=self.actors['buyer'])
        forged = self.actors['buyer'] | {'role': 'owner'}
        with self.assertRaises(ServiceError):self.accept(trade, actor=forged)
        with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s', (self.tenant, self.actors['owner']['staff_id']))
        with self.assertRaises(ServiceError):self.accept(trade)
        accepted = self.accept(trade, actor=self.actors['manager'])
        self.assertEqual(accepted['accepted_amount_cents'], 1000)
        self.assertEqual(self.trades.get(self.actors['manager'], trade['trade_id'])['accepted_by'], str(self.actors['manager']['staff_id']))

    def test_another_tenant_cannot_read_or_accept_trade_or_use_its_customer(self):
        trade, _ = self.create();foreign = uuid.uuid4();staff = uuid.uuid4();customer = uuid.uuid4()
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)', (foreign, 'fixture-' + foreign.hex, 'Fictional other store'))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'foreign@example.invalid','fixture-invalid-password-hash','owner')", (staff, foreign))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Foreign fictional customer','fixture-only')", (customer, foreign))
        actor = {'tenant_id': foreign, 'staff_id': staff, 'role': 'owner'}
        with self.assertRaises(ServiceError):self.trades.get(actor, trade['trade_id'])
        with self.assertRaises(ServiceError):self.trades.accept(actor, trade['trade_id'], acceptance_data(customer))
        with self.assertRaises(ServiceError):self.trades.create(actor, draft_data(customer_id=self.customer))
        self.assertEqual(self.wallet()[0]['balance_cents'], 0)

    def test_source_receipt_and_request_replays_do_not_create_duplicate_trade(self):
        first, data = self.create(source='stable-local-receipt')
        replay = self.trades.create(self.actors['buyer'], data)
        self.assertEqual(first, replay)
        copied = deepcopy(data);copied['request_key'] = 'import-new-key'
        duplicate = self.trades.create(self.actors['buyer'], copied)
        self.assertEqual((duplicate['trade_id'], duplicate['source_deduplicated']), (first['trade_id'], True))
        copied['request_key'] = 'changed-import';copied['lines'][0]['unit_intake_cents'] = 999
        with self.assertRaises(ServiceError):self.trades.create(self.actors['buyer'], copied)
        with self.database.transaction() as db:
            count = db.execute('SELECT count(*) AS count FROM ss_trade_receipts WHERE tenant_id=%s', (self.tenant,)).fetchone()['count']
        self.assertEqual(count, 1)

    def test_concurrent_import_of_same_local_receipt_creates_one_durable_binding(self):
        payload = draft_data(customer_id=self.customer);payload['source_reference'] = 'same-local-source'
        def create(index):return self.trades.create(self.actors['buyer'], payload | {'request_key': 'source-' + str(index)})
        with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(create, (1, 2)))
        self.assertEqual(results[0]['trade_id'], results[1]['trade_id'])
        self.assertEqual(sum(result['source_deduplicated'] for result in results), 1)

    def test_manager_negotiated_offer_has_reason_and_fixed_receipt_amount(self):
        trade, _ = self.create(actor=self.actors['manager'], offers={'credit_override_cents': 1250, 'override_reason': 'Fictional negotiated offer'})
        result = self.accept(trade, cents=1250)
        receipt = self.trades.get(self.actors['owner'], trade['trade_id'])['accepted_receipt']
        self.assertEqual((result['accepted_amount_cents'], receipt['intake_total_cents'], receipt['offer_terms']['override_reason']), (1250, 2000, 'Fictional negotiated offer'))

    def test_accepted_slab_keeps_grade_qualifier_serial_and_manufacturer(self):
        line = card_line(quantity=1, cents=2000, game='Sports')
        line['identity'].update(kind='slab', grading_company='Fictional Grader', grade='10',
                                grade_qualifier='Pristine black label', certification='FIXTURE-CERT',
                                serial_number='07/25', manufacturer='Fictional manufacturer')
        trade, _ = self.create(lines=[line]);self.accept(trade)
        identity = self.trades.get(self.actors['owner'], trade['trade_id'])['accepted_receipt']['lines'][0]['identity']
        self.assertEqual((identity['grade_qualifier'], identity['serial_number'], identity['manufacturer']),
                         ('Pristine black label', '07/25', 'Fictional manufacturer'))

    def test_accepting_trade_does_not_enable_checkout_square_or_tax_policy(self):
        trade, _ = self.create();self.accept(trade)
        with self.database.transaction() as db:
            store = db.execute('SELECT credit_checkout_enabled,tax_policy_version FROM ss_tenants WHERE id=%s', (self.tenant,)).fetchone()
            connections = db.execute('SELECT count(*) AS count FROM ss_square_connections WHERE tenant_id=%s', (self.tenant,)).fetchone()['count']
            policies = db.execute('SELECT count(*) AS count FROM ss_credit_origin_policies WHERE tenant_id=%s', (self.tenant,)).fetchone()['count']
        self.assertEqual((store['credit_checkout_enabled'], store['tax_policy_version'], connections, policies), (False, 0, 0, 0))

    def test_final_audit_failure_rolls_back_receipt_credit_lot_and_request(self):
        trade, _ = self.create()
        with self.database.transaction() as db:
            db.execute("""CREATE OR REPLACE FUNCTION fixture_trade_audit_failure() RETURNS trigger LANGUAGE plpgsql AS $$
                          BEGIN IF NEW.action='trade-accepted' THEN RAISE EXCEPTION 'fixture trade audit failure'; END IF; RETURN NEW; END $$""")
            db.execute('CREATE TRIGGER fixture_trade_audit_failure BEFORE INSERT ON ss_audit FOR EACH ROW EXECUTE FUNCTION fixture_trade_audit_failure()')
        try:
            with self.assertRaises(psycopg.errors.RaiseException):self.accept(trade, key='failed-accept')
            self.assertEqual(self.trades.get(self.actors['owner'], trade['trade_id'])['state'], 'draft')
            self.assertEqual((self.wallet()[0]['balance_cents'], self.wallet()[1], self.wallet()[2]), (0, [], []))
            self.assertFalse(Ledger(self.database).request_status(self.actors['owner'], 'failed-accept')['found'])
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER fixture_trade_audit_failure ON ss_audit');db.execute('DROP FUNCTION fixture_trade_audit_failure()')

    def test_final_request_journal_failure_rolls_back_all_financial_state(self):
        trade, _ = self.create()
        with self.database.transaction() as db:
            db.execute("""CREATE OR REPLACE FUNCTION fixture_trade_request_failure() RETURNS trigger LANGUAGE plpgsql AS $$
                          BEGIN IF NEW.key='failed-journal' THEN RAISE EXCEPTION 'fixture trade journal failure'; END IF; RETURN NEW; END $$""")
            db.execute('CREATE TRIGGER fixture_trade_request_failure BEFORE INSERT ON ss_requests FOR EACH ROW EXECUTE FUNCTION fixture_trade_request_failure()')
        try:
            with self.assertRaises(psycopg.errors.RaiseException):self.accept(trade, key='failed-journal')
            self.assertEqual(self.trades.get(self.actors['owner'], trade['trade_id'])['state'], 'draft')
            self.assertEqual((self.wallet()[0]['balance_cents'], len(self.wallet()[1]), len(self.wallet()[2])), (0, 0, 0))
            with self.database.transaction() as db:
                count = db.execute("SELECT count(*) AS count FROM ss_audit WHERE tenant_id=%s AND action='trade-accepted'", (self.tenant,)).fetchone()['count']
            self.assertEqual(count, 0)
        finally:
            with self.database.transaction() as db:
                db.execute('DROP TRIGGER fixture_trade_request_failure ON ss_requests');db.execute('DROP FUNCTION fixture_trade_request_failure()')

    def test_void_retains_draft_evidence_without_financial_reversal(self):
        trade, _ = self.create()
        result = self.trades.void(self.actors['buyer'], trade['trade_id'], {'request_key': 'void', 'version': 0, 'reason': 'Fictional declined offer'})
        self.assertEqual(result['state'], 'voided')
        with self.assertRaises(ServiceError):self.accept(trade)
        with self.assertRaises(ServiceError):self.trades.update(self.actors['owner'], trade['trade_id'], {'request_key': 'edit-void', 'version': 1, 'title': 'Changed'})
        self.assertEqual((self.wallet()[0]['balance_cents'], self.wallet()[1]), (0, []))

    def test_confirmed_void_allows_one_explicit_replacement_with_same_source_family(self):
        trade, data = self.create(source='same-physical-local-receipt')
        self.trades.void(self.actors['buyer'], trade['trade_id'], {'request_key': 'void-before-replacement', 'version': 0, 'reason': 'Fictional revised offer'})
        with self.assertRaises(ServiceError):self.trades.create(self.actors['buyer'], data | {'request_key': 'unconfirmed-replacement'})
        replacement = self.trades.create(self.actors['buyer'], data | {'request_key': 'confirmed-replacement', 'replace_voided_source': True})
        self.assertNotEqual(replacement['trade_id'], trade['trade_id'])
        accepted = self.accept(replacement)
        replay = self.trades.create(self.actors['buyer'], data | {'request_key': 'other-computer', 'replace_voided_source': True})
        self.assertEqual((replay['trade_id'], replay['state']), (replacement['trade_id'], 'accepted_credit'))
        with self.assertRaises(ServiceError):self.trades.void(self.actors['owner'], replacement['trade_id'], {'request_key': 'cannot-void-final', 'version': accepted['version'], 'reason': 'No reversal'})
        self.assertEqual(self.wallet()[0]['balance_cents'], 1000)

    def test_concurrent_confirmed_replacements_share_one_new_active_draft(self):
        trade, data = self.create(source='replace-same-family')
        self.trades.void(self.actors['buyer'], trade['trade_id'], {'request_key': 'void-family', 'version': 0, 'reason': 'Fictional replacement'})
        def replace(index):
            return self.trades.create(self.actors['buyer'], data | {'request_key': 'replacement-' + str(index), 'replace_voided_source': True})
        with ThreadPoolExecutor(max_workers=2) as pool:results = list(pool.map(replace, (1, 2)))
        self.assertEqual(results[0]['trade_id'], results[1]['trade_id'])
        self.assertEqual(sum(result['source_deduplicated'] for result in results), 1)

    def test_trade_request_journals_do_not_expose_buyer_receipts_to_cashier(self):
        trade, data = self.create();self.accept(trade, key='private-trade-accept')
        for key in (data['request_key'], 'private-trade-accept'):
            with self.assertRaises(ServiceError) as error:Ledger(self.database).request_status(self.actors['cashier'], key)
            self.assertEqual(error.exception.status, 403)
            self.assertTrue(Ledger(self.database).request_status(self.actors['buyer'], key)['found'])

    def test_trade_list_pagination_search_and_staff_scope(self):
        created = [self.create(title=title)[0]['trade_id'] for title in ('Fictional first', 'Fictional Magic trade', 'Fictional third')]
        page = self.trades.list(self.actors['buyer'], limit=2)
        self.assertEqual(len(page['trades']), 2);self.assertTrue(page['next_cursor'])
        next_page = self.trades.list(self.actors['buyer'], limit=2, cursor=page['next_cursor'])
        self.assertEqual(len(next_page['trades']), 1)
        self.assertEqual({trade['id'] for trade in page['trades'] + next_page['trades']}, set(created))
        self.assertEqual(len(self.trades.list(self.actors['buyer'], 'magic')['trades']), 1)
        self.assertEqual(self.trades.list(self.actors['buyer'], '%')['trades'], [])
        with self.assertRaises(ServiceError):self.trades.list(self.actors['cashier'])

    def test_original_customer_name_and_amount_remain_in_immutable_receipt(self):
        trade, _ = self.create();self.accept(trade)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_customers SET display_name='Corrected fictional name' WHERE tenant_id=%s AND id=%s", (self.tenant, self.customer))
        receipt = self.trades.get(self.actors['owner'], trade['trade_id'])['accepted_receipt']
        self.assertEqual((receipt['customer']['display_name'], receipt['accepted_amount_cents']), ('Fictional trade customer', 1000))

    def test_unmatched_legacy_balance_stays_review_only_without_backfill_or_trade_issue(self):
        trade, _ = self.create()
        with self.database.transaction() as db:
            db.execute('UPDATE ss_customers SET balance_cents=50 WHERE tenant_id=%s AND id=%s', (self.tenant, self.customer))
        with self.assertRaises(ServiceError):self.accept(trade)
        self.assertEqual((self.wallet()[0]['balance_cents'], self.wallet()[1], self.wallet()[2]), (50, [], []))
        self.assertEqual(self.trades.get(self.actors['owner'], trade['trade_id'])['state'], 'draft')

    def test_manual_trade_credit_adjustment_cannot_bypass_accepted_receipt(self):
        with self.assertRaises(ServiceError):
            Ledger(self.database).adjust(self.actors['owner'], self.customer, {'request_key': 'fake-trade', 'amount_cents': 1000,
                'kind': 'trade_credit', 'version': 0, 'reference': 'unaccepted-receipt', 'reason': 'Must not issue'})
        self.assertEqual(self.wallet()[0]['balance_cents'], 0)

    def test_later_store_defaults_do_not_change_an_existing_customer_offer(self):
        trade, _ = self.create()
        with self.database.transaction() as db:db.execute('UPDATE ss_tenants SET trade_credit_bps=9000 WHERE id=%s', (self.tenant,))
        self.assertEqual(self.accept(trade)['accepted_amount_cents'], 1000)

    def test_customer_rebinding_requires_new_version_and_explicit_customer_confirmation(self):
        trade, _ = self.create();other = uuid.uuid4()
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Other fictional customer','fixture-only')", (other, self.tenant))
        updated = self.trades.update(self.actors['buyer'], trade['trade_id'], {'request_key': 'change-customer', 'version': 0, 'customer_id': str(other)})
        with self.assertRaises(ServiceError):self.accept(updated)
        accepted = self.accept(updated, customer=other)
        self.assertEqual((accepted['accepted_amount_cents'], self.wallet()[0]['balance_cents'], self.wallet(other)[0]['balance_cents']), (1000, 0, 1000))

    def test_real_http_session_csrf_trade_receipt_and_credit_transaction_work_together(self):
        password = 'fictional-owner-trade-password'
        with self.database.transaction() as db:
            # This fictional HTTP case starts unenrolled, signs in normally,
            # then establishes a fixture-only fresh enrolled proof below.
            db.execute('DELETE FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s', (self.tenant, self.actors['owner']['staff_id']))
            db.execute('UPDATE ss_staff SET password_hash=%s WHERE tenant_id=%s AND id=%s',
                       (password_hash(password), self.tenant, self.actors['owner']['staff_id']))
        app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': 'https://trade-fixture.example.test',
                          'APP_ENCRYPTION_KEYS': Fernet.generate_key().decode()}, database=self.database)
        client = app.test_client();base = 'https://trade-fixture.example.test'
        login = client.post('/api/auth/login', base_url=base, json={'store_slug': 'fixture-' + self.tenant.hex,
                             'email': 'owner@example.invalid', 'password': password})
        self.assertEqual(login.status_code, 200)
        csrf = {'X-SS-CSRF': login.json['csrf_token']}
        token = client.get_cookie('ss_session', domain='trade-fixture.example.test').value
        actor = self.actors['owner'] | {'session_hash': digest(token)}
        with self.database.transaction() as db:fresh_mfa_actor(db, actor, existing_session=True)
        bound, _ = claim_draft(self.database, actor, draft_data(customer_id=self.customer))
        created = client.post('/api/trades', base_url=base, json=bound, headers=csrf)
        self.assertEqual(created.status_code, 201)
        ident = created.json['trade_id'];path = '/api/trades/' + ident + '/accept'
        payload = acceptance_data(self.customer, request_key='http-accepted')
        payload['claim_reviewed'] = True
        self.assertEqual(client.post(path, base_url=base, json=payload).status_code, 403)
        accepted = client.post(path, base_url=base, json=payload, headers=csrf)
        self.assertEqual((accepted.status_code, accepted.json['accepted_amount_cents']), (200, 1000))
        replay = client.post(path, base_url=base, json=payload, headers=csrf)
        self.assertEqual(replay.json, accepted.json)
        receipt = client.get('/api/trades/' + ident, base_url=base)
        self.assertEqual((receipt.status_code, receipt.json['state'], receipt.json['accepted_receipt']['customer']['id']),
                         (200, 'accepted_credit', str(self.customer)))
        self.assertEqual((self.wallet()[0]['balance_cents'], len(self.wallet()[1])), (1000, 1))


if __name__ == '__main__':unittest.main(verbosity=2)
