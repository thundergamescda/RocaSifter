"""Independent physical-intake regression in a disposable PostgreSQL namespace.

The first regression intentionally fails on the pre-claim implementation. It
models two PCs using the actual desktop source-reference construction for one
fictional CSV. No existing customer, provider account or Square call is used.
"""
import hashlib
import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from hosted.security import ServiceError
from hosted.trades import Trades
from hosted.intake import IntakeClaims, IntakeError
from intake_manifest import capture_manifest, source_line_id
from test_intake_fixtures import fresh_mfa_actor
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import acceptance_data, draft_data


class PhysicalIntakeClaimAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        self.tenant, self.customer, self.staff = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.actor = {'tenant_id': self.tenant, 'staff_id': self.staff, 'role': 'owner'}
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',
                       (self.tenant, 'fixture-' + self.tenant.hex, 'Fictional physical intake store'))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,'owner')",
                       (self.staff, self.tenant, 'owner@example.invalid', 'fixture-invalid-password-hash'))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,%s,'fixture-only')",
                       (self.customer, self.tenant, 'Fictional customer'))
            self.actor = fresh_mfa_actor(db, self.actor)
        self.trades = Trades(self.database)
        self.claims = IntakeClaims(self.database)
        forbid_provider = patch('requests.sessions.Session.request', side_effect=AssertionError('No external/provider HTTP allowed.'))
        forbid_provider.start()
        self.addCleanup(forbid_provider.stop)

    def manifest(self, *, extra=False):
        data = draft_data(customer_id=self.customer)
        rows = [{'identity': data['lines'][0]['identity'], 'quantity': 2, 'source_row': 2}]
        if extra:
            rows.append({'identity': rows[0]['identity'] | {'name': 'Another fictional card'}, 'quantity': 1, 'source_row': 3})
        return capture_manifest('csv', rows, raw_csv_sha256='a' * 64)

    def claim(self, *, manifest=None, key='claim-base', reference='fictional-delivery-base', actor=None, **extra):
        return self.claims.create(actor or self.actor, {'request_key': key, 'physical_reference': reference,
            'source_manifest': manifest or self.manifest(), 'physical_delivery_verified': True, **extra})

    def draft(self, claim, *, key='draft', actor=None, quantity=2):
        data = draft_data(customer_id=self.customer, request_key=key)
        data['source_reference'] = 'R-20261028-' + key
        data.update(physical_intake_id=claim['physical_intake_id'], physical_manifest_hash=claim['manifest_hash'])
        data['lines'][0].update(quantity=quantity, source_line_key=source_line_id(data['lines'][0]['identity']))
        return self.trades.create(actor or self.actor, data)

    def accept(self, draft, *, key='accept', actor=None, reviewed=True, offer='credit'):
        data = acceptance_data(self.customer, request_key=key, version=draft['version'],
                               offer=offer, cents=1000 if offer == 'credit' else 700)
        if reviewed is not None:data['claim_reviewed'] = reviewed
        return self.trades.accept(actor or self.actor, draft['trade_id'], data)

    def aged_actor(self):
        actor = dict(self.actor) | {'session_hash': uuid.uuid4().hex + uuid.uuid4().hex}
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,created_at,expires_at,mfa_version,mfa_authenticated_at)
                          VALUES (%s,%s,%s,now()-interval '16 minutes',now()+interval '1 hour',1,now()-interval '16 minutes')''',
                       (actor['session_hash'], self.tenant, self.staff))
        return actor

    def wallet(self):
        with self.database.transaction() as db:
            customer = db.execute('SELECT balance_cents FROM ss_customers WHERE tenant_id=%s AND id=%s',
                                  (self.tenant, self.customer)).fetchone()
            entries = db.execute("SELECT count(*) AS n FROM ss_credit_entries WHERE tenant_id=%s AND customer_id=%s AND kind='trade_credit'",
                                 (self.tenant, self.customer)).fetchone()
            lots = db.execute('SELECT count(*) AS n FROM ss_credit_lots WHERE tenant_id=%s AND customer_id=%s',
                              (self.tenant, self.customer)).fetchone()
        return {'balance_cents': customer['balance_cents'], 'credit_entries': entries['n'], 'credit_lots': lots['n']}

    def test_unclaimed_copied_csv_cannot_issue_two_credits(self):
        # Match station_service.import_csv's raw-file hash normalization, then
        # station_shared_trade.open's local receipt ID + hashed source reference.
        csv = 'Name,Game,Set,Number,Quantity,Price\r\nFictional card,Pokemon,Fictional set,001,2,10.00\r\n'
        raw_digest = hashlib.sha256(csv.lstrip('\ufeff').replace('\r\n', '\n').encode()).hexdigest()
        digest_suffix = hashlib.sha256(str(raw_digest).encode()).hexdigest()
        receipts = ('R-20261028-AAAAAA', 'R-20261028-BBBBBB')
        drafts = []
        rejected = []
        for index, receipt in enumerate(receipts, 1):
            data = draft_data(customer_id=self.customer, request_key='fictional-pc-create-' + str(index))
            data['source_reference'] = receipt + ':' + digest_suffix
            try:
                drafts.append(self.trades.create(self.actor, data))
            except ServiceError as error:
                rejected.append({'phase': 'create', 'status': error.status})
        for index, draft in enumerate(drafts, 1):
            data = acceptance_data(self.customer, request_key='fictional-pc-accept-' + str(index), version=draft['version'])
            try:
                self.trades.accept(self.actor, draft['trade_id'], data)
            except ServiceError as error:
                rejected.append({'phase': 'accept', 'status': error.status})
        observed = self.wallet()
        print(json.dumps({'fixture': 'one CSV / two local receipt IDs / one tenant and customer',
                          'drafts_created': len(drafts), 'rejected': rejected, 'observed': observed,
                          'live_square_contacted': False}, sort_keys=True), flush=True)
        self.assertLessEqual(observed['credit_entries'], 1,
                             'One physical CSV imported on two PCs issued two separate trade credits.')
        # New unclaimed acceptances must be explicitly upgraded/claimed. An old
        # accepted receipt's replay is a different compatibility contract.
        self.assertEqual(observed, {'balance_cents': 0, 'credit_entries': 0, 'credit_lots': 0},
                         'New trade acceptance without a central physical claim must not issue credit.')
        self.assertTrue(rejected, 'The unclaimed path needs an explicit upgrade/claim rejection.')

    def test_two_pc_claim_and_acceptance_races_issue_exactly_one_credit(self):
        def create_claim(index):
            return self.claim(key='claim-pc-' + str(index), reference='different-local-pc-' + str(index))
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(create_claim, (1, 2)))
        self.assertEqual(claims[0]['physical_intake_id'], claims[1]['physical_intake_id'])
        drafts = [self.draft(claims[index], key='pc-' + str(index)) for index in (0, 1)]
        def commit(index):
            try:return self.accept(drafts[index], key='pc-accept-' + str(index))
            except ServiceError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, (0, 1)))
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.wallet(), {'balance_cents': 1000, 'credit_entries': 1, 'credit_lots': 1})
        winner = next(value for value in results if value)
        claim = self.claims.get(self.actor, claims[0]['physical_intake_id'])
        self.assertEqual((claim['state'], claim['bound_trade_id']), ('consumed', winner['trade_id']))

    def test_shuffled_equivalent_source_never_gets_a_second_claim_or_wrong_row_binding(self):
        original = self.manifest(extra=True)
        claim = self.claim(manifest=original)
        shuffled = capture_manifest('csv', list(reversed(original['rows'])), raw_csv_sha256='b' * 64)
        with self.assertRaises(IntakeError) as caught:
            self.claim(manifest=shuffled, key='shuffled', reference='same-stack-reexport')
        self.assertEqual(caught.exception.physical_intake_id, claim['physical_intake_id'])
        recovered = self.claims.get(self.actor, caught.exception.physical_intake_id)
        self.assertEqual(recovered['source_manifest'], original)
        self.assertNotEqual(shuffled['sha256'], original['sha256'])
        self.assertEqual(self.wallet()['credit_entries'], 0)

    def test_linked_actual_additional_delivery_requires_fresh_mfa_then_replays_one_claim(self):
        base = self.claim()
        extra = {'additional_delivery_of': base['physical_intake_id'], 'additional_delivery_confirmed': True,
                 'additional_delivery_reason': 'Another fictional physical delivery was checked.'}
        with self.assertRaises(IntakeError) as caught:
            self.claim(key='extra-stale', reference='another-physical-stack', actor=self.aged_actor(), **extra)
        self.assertEqual(caught.exception.code, 'MFA_STEP_UP_REQUIRED')
        one = self.claim(key='extra-fresh', reference='another-physical-stack', **extra)
        two = self.claim(key='extra-fresh', reference='another-physical-stack', **extra)
        self.assertEqual(one, two)
        self.assertNotEqual(one['physical_intake_id'], base['physical_intake_id'])
        self.assertEqual(one['additional_delivery_of'], base['physical_intake_id'])
        for index, value in enumerate((base, one)):
            self.accept(self.draft(value, key='actual-delivery-' + str(index)), key='actual-accept-' + str(index))
        self.assertEqual(self.wallet(), {'balance_cents': 2000, 'credit_entries': 2, 'credit_lots': 2})

    def test_original_quantity_budget_and_explicit_claim_review_are_enforced(self):
        claim = self.claim()
        with self.assertRaises(IntakeError):self.draft(claim, key='too-many', quantity=3)
        draft = self.draft(claim)
        with self.assertRaises(IntakeError) as caught:self.accept(draft, reviewed=False)
        self.assertEqual(caught.exception.code, 'PHYSICAL_INTAKE_REVIEW_REQUIRED')
        self.assertEqual(self.wallet()['credit_entries'], 0)
        self.accept(draft, key='reviewed-accept')
        self.assertEqual(self.wallet()['credit_entries'], 1)

    def test_expired_mfa_blocks_new_cash_and_credit_but_not_accepted_result_recovery(self):
        claim = self.claim();draft = self.draft(claim)
        stale = self.aged_actor()
        for offer in ('credit', 'cash'):
            with self.subTest(offer=offer), self.assertRaises(IntakeError) as caught:
                self.accept(draft, key='stale-' + offer, actor=stale, offer=offer)
            self.assertEqual(caught.exception.code, 'MFA_STEP_UP_REQUIRED')
        accepted = self.accept(draft, key='fresh-accept')
        replay = self.accept(draft, key='recovered-accepted', actor=stale, reviewed=None)
        self.assertEqual(accepted, replay)
        self.assertEqual(self.wallet(), {'balance_cents': 1000, 'credit_entries': 1, 'credit_lots': 1})

    def test_cash_acceptance_consumes_claim_without_issuing_credit_or_a_second_agreement(self):
        claim = self.claim();one = self.draft(claim, key='cash-one');two = self.draft(claim, key='cash-two')
        result = self.accept(one, key='cash-accept', offer='cash')
        with self.assertRaises(IntakeError):self.accept(two, key='second-cash', offer='cash')
        self.assertEqual(result['state'], 'accepted_cash')
        self.assertFalse(result['cash_payment_executed'])
        self.assertEqual(self.claims.get(self.actor, claim['physical_intake_id'])['bound_trade_id'], one['trade_id'])
        self.assertEqual(self.wallet(), {'balance_cents': 0, 'credit_entries': 0, 'credit_lots': 0})

    def test_consumed_original_evidence_cannot_be_rebound_deleted_or_changed(self):
        import psycopg
        claim = self.claim();self.accept(self.draft(claim))
        for query, args in (
            ('UPDATE ss_intake_claims SET physical_reference=%s WHERE tenant_id=%s AND id=%s', ('new-ref', self.tenant, claim['physical_intake_id'])),
            ('DELETE FROM ss_intake_claims WHERE tenant_id=%s AND id=%s', (self.tenant, claim['physical_intake_id'])),
            ("UPDATE ss_intake_claims SET state='open',version=0,bound_trade_id=NULL,consumed_at=NULL,consumed_by=NULL WHERE tenant_id=%s AND id=%s", (self.tenant, claim['physical_intake_id']))):
            with self.subTest(query=query), self.assertRaises(psycopg.Error):
                with self.database.transaction() as db:db.execute(query, args)
        self.assertEqual(self.wallet()['credit_entries'], 1)

    def test_claim_tenant_request_key_and_manifest_conflicts_cannot_redirect_a_delivery(self):
        base = self.claim()
        with self.assertRaises(IntakeError):self.claim(reference='changed-reference')
        foreign = self.actor | {'tenant_id': uuid.uuid4()}
        with self.assertRaises(ServiceError):self.claims.get(foreign, base['physical_intake_id'])
        with self.assertRaises(ServiceError):self.claims.request_status(foreign, 'claim-base')
        status = self.claims.request_status(self.actor, 'claim-base')
        self.assertTrue(status['found']);self.assertEqual(status['result'], base)
        self.assertEqual(self.wallet()['credit_entries'], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
