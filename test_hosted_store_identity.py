"""Server-confirmed store UUID projection; no provider or database calls."""
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.mfa import MFA,issue_session
from hosted.security import ServiceError


class StoreIdentityContract(unittest.TestCase):
    def setUp(self):
        self.tenant=uuid.uuid4()
        self.staff={'tenant_id':self.tenant,'id':uuid.uuid4(),'slug':'fictional-store',
                    'name':'Fictional store','role':'owner','password_hash':'fictional hash'}
        self.key=Fernet.generate_key().decode()

    def test_full_session_helper_exposes_same_canonical_uuid_for_both_auth_paths(self):
        for version in (None,7):
            with self.subTest(mfa_version=version):
                token,info=issue_session(Mock(),self.staff,mfa_version=version)
                self.assertEqual(info['tenant_id'],str(self.tenant))
                self.assertEqual(str(uuid.UUID(info['tenant_id'])),info['tenant_id'])
                self.assertEqual((info['store'],info['store_name'],info['role']),
                                 ('fictional-store','Fictional store','owner'))
                self.assertEqual(info['mfa_authenticated'],version is not None)
                self.assertEqual(info['expires_in'],28800)
                self.assertIsInstance(token,str)

    def test_password_only_mfa_challenge_has_no_tenant_or_session_authority(self):
        db=Mock();db.execute.return_value.fetchone.return_value={'enabled':True,'version':7}
        token,info=MFA(Mock(),self.key).begin_challenge(db,self.staff,'fictional-address')
        self.assertIsNone(token);self.assertTrue(info['mfa_required'])
        for field in ('tenant_id','access_token','csrf_token','mfa_authenticated'):
            self.assertNotIn(field,info)
        self.assertEqual(info['store'],'fictional-store')

    def app(self):
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://store-id.fixture.invalid',
                        'APP_ENCRYPTION_KEYS':self.key},database=Mock())
        return app,app.test_client()

    def test_authenticated_me_projects_resolved_actor_not_query_or_cookie_hints(self):
        app,client=self.app()
        app.extensions['ss_auth'].resolve=Mock(return_value={
            'tenant_id':self.tenant,'slug':'fictional-store','role':'manager',
            'mfa_enabled':True,'mfa_authenticated':True})
        response=client.get('/api/me?tenant_id='+str(uuid.uuid4())+'&mfa_fresh=true',
                            base_url='https://store-id.fixture.invalid',headers={'Authorization':'Bearer fictional-session'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['tenant_id'],str(self.tenant))
        self.assertEqual((response.json['store'],response.json['role']),('fictional-store','manager'))
        # The resolved actor omitted recent proof; a browser hint cannot add it.
        self.assertIs(response.json['mfa_fresh'],False)
        self.assertNotIn('mfa_authenticated_at',response.json)
        self.assertNotIn('Set-Cookie',response.headers)

    def test_unauthenticated_me_exposes_no_store_id(self):
        app,client=self.app();app.extensions['ss_auth'].resolve=Mock(side_effect=ServiceError('Sign in.',401))
        response=client.get('/api/me',base_url='https://store-id.fixture.invalid')
        self.assertEqual(response.status_code,401);self.assertNotIn('tenant_id',response.json)

    def test_http_full_session_and_challenge_envelopes_remain_distinct(self):
        app,client=self.app();base='https://store-id.fixture.invalid'
        token,info=issue_session(Mock(),self.staff,mfa_version=7)
        auth=app.extensions['ss_auth'];auth.mfa.verify_challenge=Mock(return_value=(token,info))
        response=client.post('/api/auth/mfa/challenge',base_url=base,json={})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['tenant_id'],str(self.tenant))
        self.assertTrue(response.json['mfa_authenticated']);self.assertIn('Set-Cookie',response.headers)
        auth.login=Mock(return_value=(None,{'mfa_required':True,'challenge_token':'fictional','expires_in':300,
                                          'methods':['totp','recovery_code'],'store':'fictional-store'}))
        challenge=client.post('/api/auth/login',base_url=base,json={})
        self.assertTrue(challenge.json['mfa_required'])
        for field in ('tenant_id','access_token','csrf_token'):self.assertNotIn(field,challenge.json)
