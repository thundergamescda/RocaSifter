"""Physical-intake binding and HTTP controls without a database/provider."""
from copy import deepcopy
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet
from intake_manifest import capture_manifest, source_line_id
from hosted.app import create_app
from hosted.intake import validate_claim_lines, IntakeError, IntakeClaims
from hosted.security import ServiceError
from hosted.trades import normalize_lines
from test_hosted_trades import card_line


class IntakeLineTests(unittest.TestCase):
    def setUp(self):
        self.line = card_line()
        self.line['source_line_key'] = source_line_id(self.line['identity'])
        self.claim = {'id': uuid.uuid4(), 'source_manifest': capture_manifest('manual', [{'identity': self.line['identity'], 'quantity': 2}])}

    def test_line_binding_preserved_without_changing_legacy_line_fingerprint(self):
        old = card_line();self.assertNotIn('source_line_key', normalize_lines([old])[0])
        self.assertEqual(normalize_lines([self.line])[0]['source_line_key'], self.line['source_line_key'])

    def test_corrected_review_facts_keep_original_key_and_quantity_cap(self):
        self.line['identity']['name'] = 'Reviewed corrected name'
        self.assertEqual(validate_claim_lines(self.claim, [self.line]), {self.line['source_line_key']: 2})
        self.line['quantity'] = 3
        with self.assertRaises(IntakeError):validate_claim_lines(self.claim, [self.line])

    def test_split_lines_cannot_multiply_original_quantity_even_if_excluded(self):
        other = deepcopy(self.line);other['included'] = False
        with self.assertRaises(IntakeError):validate_claim_lines(self.claim, [self.line, other])

    def test_missing_or_substituted_original_key_requires_review(self):
        for key in (None, '0'*64):
            self.line['source_line_key'] = key
            with self.assertRaises(IntakeError) as caught:validate_claim_lines(self.claim, [self.line])
            self.assertEqual(caught.exception.code, 'SOURCE_MANIFEST_REQUIRED')

    def test_invalid_original_manifest_is_actionable_before_database_io(self):
        db = Mock();claims = IntakeClaims(db)
        with self.assertRaises(IntakeError) as caught:
            claims.create({'tenant_id': uuid.uuid4()}, {'request_key':'claim', 'physical_reference':'batch-1',
                          'source_manifest':{}, 'physical_delivery_verified':True})
        self.assertEqual((caught.exception.status, caught.exception.code), (428, 'SOURCE_MANIFEST_REQUIRED'))
        db.transaction.assert_not_called()


class IntakeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.actor = {'tenant_id': uuid.uuid4(), 'staff_id': uuid.uuid4(), 'role':'owner','slug':'fictional-store'}
        self.app = create_app({'TESTING':True, 'PUBLIC_BASE_URL':'https://intake.fixture.invalid',
                              'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()}, database=Mock())
        self.app.extensions['ss_auth'].resolve = Mock(return_value=self.actor)
        self.claims = self.app.extensions['ss_intake']
        self.claims.create = Mock(return_value={'physical_intake_id':str(uuid.uuid4())})
        self.claims.get = Mock(return_value={'state':'open'})
        self.claims.request_status = Mock(return_value={'found':False,'result':None})
        self.client = self.app.test_client();self.base='https://intake.fixture.invalid'
        self.headers={'Authorization':'Bearer fictional-staff-token'}

    def test_typed_claim_routes_use_session_actor_and_never_client_tenant(self):
        result=self.client.post('/api/intake-claims',base_url=self.base,json={'request_key':'request'},headers=self.headers)
        self.assertEqual(result.status_code,201);self.claims.create.assert_called_once_with(self.actor,{'request_key':'request'})
        self.assertEqual(self.client.get('/api/intake-claims/'+str(uuid.uuid4()),base_url=self.base,headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/api/intake-claims/requests/unknown',base_url=self.base,headers=self.headers).json,{'found':False,'result':None})
        denied=self.client.post('/api/intake-claims',base_url=self.base,json={'tenant_id':str(uuid.uuid4())},headers=self.headers)
        self.assertEqual(denied.status_code,400)

    def test_machine_error_exposes_only_safe_claim_reference(self):
        ident=uuid.uuid4();self.claims.create.side_effect=IntakeError('Recover central claim.',physical_intake_id=ident)
        result=self.client.post('/api/intake-claims',base_url=self.base,json={},headers=self.headers)
        self.assertEqual(result.json,{'error':'Recover central claim.','code':'PHYSICAL_INTAKE_CONFLICT','physical_intake_id':str(ident)})

    def test_cookie_claim_mutation_requires_csrf_and_authentication(self):
        self.client.set_cookie('ss_session','fictional-cookie',domain='intake.fixture.invalid')
        result=self.client.post('/api/intake-claims',base_url=self.base,json={})
        self.assertEqual(result.status_code,403);self.claims.create.assert_not_called()
        self.app.extensions['ss_auth'].resolve.side_effect=ServiceError('Sign in.',401)
        self.assertEqual(self.client.get('/api/intake-claims/'+str(uuid.uuid4()),base_url=self.base).status_code,401)

    def test_capabilities_disclose_required_claim_and_stepup_without_enabling_receiving(self):
        result=self.client.get('/api/me',base_url=self.base,headers=self.headers)
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json['capabilities'],{'physical_intake_claims':1,'trade_claim_required':True,
                         'new_trade_acceptance_requires_fresh_mfa':True,'hosted_receiving':False})
