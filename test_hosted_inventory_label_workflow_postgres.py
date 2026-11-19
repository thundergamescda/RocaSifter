"""Scanner/manual -> real PostgreSQL/HTTP receiving -> original label data.

The existing disposable ss_test_<uuid> fixture and actual Auth.resolve own the
database/session boundaries. Square transport is fictional. Label preparation
uses the genuine saved source; no renderer, printer or scanner is exercised.
"""
from copy import deepcopy
import hashlib
import unittest
import uuid

from cryptography.fernet import Fernet
import intake_label_contract as labels
from intake_manifest import source_line_id
from intake_physical_identity import physical_facts_v2
from scanner_adapters import normalize_manual_card, parse_csv_strict
from station_intake_claim import capture_original
from hosted.app import create_app
from hosted.intake_label_context import capture, assert_current
from hosted.receiving_plan import fingerprint
from hosted.security import ServiceError, digest
import test_hosted_receiving_postgres as receiving_fixture
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture


class PostgreSQLInventoryLabelWorkflowAcceptance(_PostgreSQLSettlementFixture):
    setUp = receiving_fixture.PostgreSQLReceivingAcceptance.setUp
    approve = receiving_fixture.PostgreSQLReceivingAcceptance.approve
    wallet = receiving_fixture.PostgreSQLReceivingAcceptance.wallet

    def http(self, method, path, data=None, *, status=200):
        response = getattr(self.client, method)(path, base_url=self.base,
            headers=self.headers, **({'json': data} if data is not None else {}))
        self.assertEqual(response.status_code, status, response.get_json())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.get_json()

    def stock_step(self, operation):
        with self.database.transaction() as db:
            return db.execute('''SELECT encrypted_body,provider_key,body_hash,api_version
                FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s AND ordinal=1''',
                (self.tenant, operation)).fetchone()

    def workflow(self, card, kind, raw=None, *, exact_sku='000000001234'):
        self.assertIs(card['reviewed'], False)
        card = deepcopy(card)
        card['row_id'] = str(uuid.uuid4())
        local = {'cards': [card]}
        capture_original(local, kind,
            hashlib.sha256(raw.encode('utf-8')).hexdigest() if raw is not None else None)
        self.assertNotIn('source_manifest_error', local)
        manifest = deepcopy(local['source_manifest'])
        identity = manifest['rows'][0]['identity']

        token = 'fictional-inventory-label-session-' + uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                SELECT %s,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at
                FROM ss_sessions WHERE tenant_id=%s AND token_hash=%s''',
                (digest(token), self.tenant, self.actor['session_hash']))
        self.base = 'https://inventory-label.fixture.invalid'
        app = create_app({'TESTING': True, 'PUBLIC_BASE_URL': self.base,
            'APP_ENCRYPTION_KEYS': Fernet.generate_key().decode(),
            'RECEIVING_WRITES_ENABLED': True}, database=self.database)
        self.service = app.extensions['ss_receiving']
        self.service.provider = self.provider
        self.client = app.test_client()
        self.headers = {'Authorization': 'Bearer ' + token}
        actor = app.extensions['ss_auth'].resolve(token)

        self.http('post', '/api/receiving/catalog-bindings/' + self.binding['binding_id'] + '/revoke',
            {'request_key': 'retire-fixture-mapping', 'version': self.binding['version'],
             'reason': 'Fictional owner replaces the old fixture with complete source facts'})
        self.provider.objects['fixture-variation']['item_variation_data']['sku'] = exact_sku
        self.provider.objects['fixture-parent']['item_data']['variations'][0]['item_variation_data']['sku'] = exact_sku
        catalog_before = deepcopy(self.provider.objects)

        claim_body = {'request_key': 'source-delivery', 'physical_reference': 'fictional-source-delivery',
            'source_manifest': manifest, 'physical_delivery_verified': True}
        claim = self.http('post', '/api/intake-claims', claim_body, status=201)
        self.assertEqual(self.http('post', '/api/intake-claims', claim_body, status=201), claim)
        preview = self.http('post', '/api/receiving/catalog-bindings/previews',
            {'identity': identity, 'variation_id': 'fixture-variation', 'location_id': 'fixture-location',
             'physical_ownership_version': 2, 'physical_facts': physical_facts_v2(card)}, status=201)
        self.http('post', '/api/receiving/catalog-bindings/' + preview['preview_id'] + '/approve',
            {'request_key': 'approve-source-mapping', 'preview_hash': preview['preview_hash'],
             'identity_confirmed': True, 'reason': 'Fictional owner reviewed the complete physical card and exact SKU'})
        review_id = card['row_id']
        line = {'review_line_id': review_id, 'source_line_key': source_line_id(identity),
            'identity': identity, 'quantity': card['qty'], 'selling_cents': card['price_cents'],
            'zero_price_confirmed': False, 'reviewed': True,
            'physical_ownership': preview['facts']['physical_ownership']}
        body = {'request_key': 'reviewed-source-plan', 'physical_intake_id': claim['physical_intake_id'],
            'physical_manifest_hash': claim['manifest_hash'], 'source_snapshot_hash': fingerprint(local),
            'physical_ownership_version': 2, 'workflow_kind': 'inventory_only',
            'location_id': 'fixture-location', 'new_inventory_confirmed': True,
            'external_ownership_confirmed': True, 'excluded_not_received_ack': True,
            'client_label_profile': {'stock': '2x1', 'layout': 'standard', 'dpi': 203}, 'lines': [line]}
        unreviewed = deepcopy(body)
        unreviewed['lines'][0]['reviewed'] = False
        self.http('post', '/api/receiving/plans', unreviewed, status=400)
        self.assertEqual(self.provider.calls, [])
        plan = self.http('post', '/api/receiving/plans', body, status=201)
        self.assertEqual(plan['targets'][0]['target_sku'], exact_sku)
        operation = plan['receiving_id']
        path = '/api/receiving/' + operation
        terms = {'request_key': 'receive-source-once', 'revision': plan['revision'],
            'plan_hash': plan['plan_hash'], 'confirmed': True, 'excluded_not_received_ack': True}
        self.provider.behavior = 'lost_response'
        unknown = self.http('post', path + '/commit', terms)
        self.assertEqual((unknown['state'], unknown['inventory_confirmed'], unknown['receipt']),
            ('outcome_unknown', False, None))
        before = self.stock_step(operation)
        with self.database.transaction() as db:
            with self.assertRaises(ServiceError):
                self.service.capture_restoration_bundle(db, actor, operation)
        recovery = {'request_key': 'recover-source-stock', 'revision': plan['revision'], 'plan_hash': plan['plan_hash']}
        received = self.http('post', path + '/recover', recovery)
        self.assertEqual((received['state'], received['inventory_confirmed']), ('received', True))
        target = received['receipt']['confirmed_targets'][0]
        self.assertEqual((target['target_sku'], target['confirmed_quantity'], target['review_line_id']),
            (exact_sku, card['qty'], review_id))
        self.assertEqual(target['physical_ownership'], preview['facts']['physical_ownership'])
        self.assertEqual(self.stock_step(operation), before)
        self.assertEqual(self.provider.objects, catalog_before)

        provider_before = deepcopy((self.provider.calls, self.provider.catalog_calls, self.provider.access_calls))
        with self.database.transaction() as db:
            bundle = self.service.capture_restoration_bundle(db, actor, operation)
            value = labels.confirmed_input(bundle, service_origin=labels.SERVICE_ORIGIN, tenant_id=str(self.tenant))
            prepared = capture(db, self.service, actor, {'receivingId': operation,
                'serverRevision': received['revision'], 'planHash': received['plan_hash'],
                'expectedTerminalBasisHash': value['terminalBasisHash']})
            self.assertEqual(prepared.materialize_input(), value)
            self.assertEqual((prepared.row_count, prepared.total_copies), (1, card['qty']))
            row = value['confirmedRows'][0]
            self.assertEqual((row['reviewLineId'], row['sourceLineKey'], row['targetSku'], row['sellingCents']),
                (review_id, card['source_line_key'], exact_sku, card['price_cents']))
            self.assertEqual(row['printed']['name'], card['card_name'])
            self.assertEqual(row['game'], card['game'])
            full = labels.build_plan(value)
            reprint = labels.build_plan(value, copy_offset=card['qty'] - 1, copy_count=1)
            self.assertEqual((full['totalCopies'], full['copyCount'], reprint['copyCount']), (card['qty'], card['qty'], 1))
            self.assertEqual(labels.part(reprint, 0)['renderCounts'], {review_id: 1})
            self.assertNotEqual(labels.job_basis_hash(value, full, 'a' * 64),
                labels.job_basis_hash(value, reprint, 'a' * 64,
                    purpose='range_reprint', reprint_of_job_id=str(uuid.uuid4())))
            assert_current(db, self.service, prepared.expected_source, actor)
        self.assertEqual((self.provider.calls, self.provider.catalog_calls, self.provider.access_calls), provider_before)

        # Historical request replies retain their original outcome; recover/get
        # retrieve current stock. Replaying either request never dispatches stock.
        self.assertEqual(self.http('post', path + '/commit', terms), unknown)
        self.assertEqual(self.http('post', path + '/recover', recovery), received)
        self.assertEqual(self.http('get', path), received)
        self.assertEqual(self.http('get', '/api/intake-claims/' + claim['physical_intake_id'])['source_manifest'], manifest)
        duplicate = dict(body, request_key='same-delivery-from-another-computer')
        conflict = self.http('post', '/api/receiving/plans', duplicate, status=409)
        self.assertEqual((conflict['code'], conflict['receiving_id']), ('CLAIM_WORKFLOW_CONFLICT', operation))
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_operations WHERE tenant_id=%s AND claim_id=%s',
                (self.tenant, claim['physical_intake_id'])).fetchone()['n'], 1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s',
                (self.tenant, operation)).fetchone()['n'], 1)
        self.assertEqual((len(self.provider.calls), self.provider.catalog_calls, self.wallet()), (1, [], (0, 0)))
        self.assertEqual(self.stock_step(operation), before)

    def csv_workflow(self, text, adapter):
        preview = parse_csv_strict(text)
        self.assertEqual(preview.adapter, adapter)
        card = preview.cards[0]
        self.assertEqual(card['sku_override'], '')
        self.workflow(card, 'csv', text)
        return card

    def test_canonical_csv_keeps_leading_zero_sku_through_stock_and_range_reprint(self):
        self.csv_workflow('Name,Game,Set,Number,Condition,Printing,Language,Quantity,Price\n'
            'Fictional card,Gundam,Fixture set,001,NM,Normal,English,2,10.00\n', 'canonical')

    def test_roca_csv_keeps_scan_reference_and_original_source_binding(self):
        card = self.csv_workflow('Card Name,Game,Set Code,Card #,Condition,Printing,Language,Qty,Price,Bin,Sequence ID\n'
            'Fictional card,Riftbound,SET,001,NM,Normal,English,2,10.00,A1,00042\n', 'roca')
        self.assertEqual(card['source_ids']['source_sequence'], '00042')

    def test_tcgplayer_app_marketplace_id_never_replaces_approved_square_sku(self):
        card = self.csv_workflow('SKU,Name,Product ID,Language,Printing,Condition,Quantity,Price,Game\n'
            '000009876543,Fictional card,00042,English,Foil,NM,2,10.00,Magic\n', 'tcgplayer_app')
        self.assertEqual(card['source_ids']['tcgplayer_sku'], '000009876543')

    def test_tcgplayer_inventory_uses_add_quantity_and_approved_selling_price(self):
        card = self.csv_workflow('TCGplayer Id,Product Line,Product Name,Set Name,Number,Condition,Add to Quantity,TCG Marketplace Price,TCG Market Price\n'
            '00042,One Piece,Fictional card,SET,001,Near Mint,2,10.00,7.50\n', 'tcgplayer_inventory')
        self.assertEqual((card['quantity_semantics'], card['market_price_cents']), ('add', 750))

    def test_manual_card_keeps_exact_spaces_and_zpl_metacharacters_in_approved_sku(self):
        card = normalize_manual_card({'card_name': 'Fictional card', 'game': 'Union Arena',
            'condition': 'Near Mint', 'printing': 'Normal', 'qty': 2, 'final_price': '10.00'})
        self.workflow(card, 'manual', exact_sku=' S^>~7 ')

    def test_manual_slab_preserves_certificate_serial_and_sports_ownership(self):
        card = normalize_manual_card({'card_name': 'Fictional player', 'game': 'Sports',
            'condition': 'Graded', 'printing': 'Gold Refractor', 'grader': 'PSA', 'grade': '10',
            'cert_number': '0000123456', 'serial_number': '03/10', 'sport': 'Baseball',
            'league': 'Fictional league', 'qty': 1, 'final_price': '10.00'})
        self.workflow(card, 'manual')
        self.assertEqual((card['cert_number'], card['serial_number']), ('0000123456', '03/10'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
