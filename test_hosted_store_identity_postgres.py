"""Actual store identity through full sign-in, MFA and /me in disposable PG."""
from datetime import datetime,timedelta,timezone
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.mfa import secret_bytes,totp
from hosted.security import password_hash
import test_hosted_settlement_postgres as postgres_fixture


class PostgreSQLStoreIdentity(postgres_fixture._PostgreSQLSettlementFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.password='Fictional store identity password only!'
        cls.hashed=password_hash(cls.password)

    def setUp(self):
        self.tenant,self.staff=uuid.uuid4(),uuid.uuid4();self.slug='identity-'+self.tenant.hex
        self.address='fictional-'+self.tenant.hex;self.now=datetime.now(timezone.utc)
        key=Fernet.generate_key().decode()
        with self.database.transaction() as db:
            self.assertRegex(db.execute('SELECT current_schema() AS name').fetchone()['name'],r'^ss_test_[a-f0-9]{32}$')
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(self.tenant,self.slug,'Fictional identity store'))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'owner@example.invalid',%s,'owner')",(self.staff,self.tenant,self.hashed))
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://identity-pg.fixture.invalid',
                             'APP_ENCRYPTION_KEYS':key},database=self.database)
        self.auth=self.app.extensions['ss_auth'];self.auth.mfa.clock=lambda:self.now
        self.client=self.app.test_client();self.base='https://identity-pg.fixture.invalid'
        self.credentials={'store_slug':self.slug,'email':'owner@example.invalid','password':self.password,'mfa_supported':True}
        no_http=patch('requests.sessions.Session.request',side_effect=AssertionError('External HTTP forbidden in identity fixtures.'))
        no_http.start();self.addCleanup(no_http.stop)

    def post(self,path,body):
        return self.client.post(path,base_url=self.base,json=body,environ_base={'REMOTE_ADDR':self.address})

    def enroll(self):
        token,_=self.auth.login(self.credentials,self.address)
        actor=self.auth.resolve(token)
        self.setup=self.auth.mfa.begin(actor,{'password':self.password},self.address)
        code=totp(secret_bytes(self.setup['secret']),self.now.timestamp())
        confirmed=self.auth.mfa.confirm(actor,{'enrollment_token':self.setup['enrollment_token'],'code':code},self.address)
        self.now+=timedelta(seconds=30)
        return confirmed['recovery_codes']

    def assert_same_store(self,response,*,mfa):
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['tenant_id'],str(self.tenant))
        self.assertEqual(response.json['mfa_authenticated'],mfa)
        self.assertEqual(self.auth.resolve(response.json['access_token'])['tenant_id'],self.tenant)
        me=self.client.get('/api/me',base_url=self.base)
        self.assertEqual(me.status_code,200)
        self.assertEqual(me.json['tenant_id'],response.json['tenant_id'])
        self.assertEqual(me.json['mfa_authenticated'],mfa)
        self.assertNotIn('mfa_fresh',me.json)

    def test_normal_full_login_and_me_expose_exact_database_uuid(self):
        self.assert_same_store(self.post('/api/auth/login',self.credentials),mfa=False)

    def test_totp_challenge_has_no_identity_binding_then_full_completion_matches_me(self):
        self.enroll()
        challenge=self.post('/api/auth/login',self.credentials)
        self.assertEqual(challenge.status_code,200);self.assertTrue(challenge.json['mfa_required'])
        for key in ('tenant_id','access_token','csrf_token'):self.assertNotIn(key,challenge.json)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,self.staff))
        result=self.post('/api/auth/mfa/challenge',{'challenge_token':challenge.json['challenge_token'],
                         'code':totp(secret_bytes(self.setup['secret']),self.now.timestamp())})
        self.assert_same_store(result,mfa=True);self.assertEqual(result.json['role'],'manager')

    def test_recovery_full_completion_exposes_same_uuid_and_keeps_one_use_proof(self):
        codes=self.enroll();challenge=self.post('/api/auth/login',self.credentials)
        self.assertNotIn('tenant_id',challenge.json)
        body={'challenge_token':challenge.json['challenge_token'],'code':codes[0]}
        self.assert_same_store(self.post('/api/auth/mfa/challenge',body),mfa=True)
        replay=self.post('/api/auth/mfa/challenge',body)
        self.assertEqual(replay.status_code,401);self.assertNotIn('tenant_id',replay.json)
