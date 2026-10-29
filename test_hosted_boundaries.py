"""Hosted HTTP/security boundary tests without a PostgreSQL or Square connection."""
import base64
import hashlib
import hmac
import unittest
from unittest.mock import Mock, patch
from hosted.database import Database
import uuid
from cryptography.fernet import Fernet
from hosted.app import create_app
from hosted.security import ServiceError, amount, identifier, password_hash, password_valid, webhook_valid


class HostedSecurityTests(unittest.TestCase):
    def test_pool_is_lazy_and_recreated_after_process_change(self):
        with patch('hosted.database.ConnectionPool') as factory,patch('hosted.database.os.getpid',return_value=100) as pid:
            first,second=Mock(),Mock();factory.side_effect=[first,second]
            db=Database('fictional-private-url')
            factory.assert_not_called()
            self.assertIs(db.pool,first);self.assertIs(db.pool,first)
            first.open.assert_called_once_with(wait=True,timeout=10)
            self.assertEqual(factory.call_count,1)
            pid.return_value=101
            self.assertIs(db.pool,second)
            self.assertEqual(factory.call_count,2)
            first.close.assert_not_called()
            db.close();second.close.assert_called_once()

    def test_password_hash_random_salt_and_validation(self):
        a=password_hash('valid-fixture-password');b=password_hash('valid-fixture-password')
        self.assertNotEqual(a,b);self.assertNotIn('valid-fixture-password',a)
        self.assertTrue(password_valid('valid-fixture-password',a));self.assertFalse(password_valid('wrong',a))
        for invalid in ('short','x'*129,None):
            with self.assertRaises(ServiceError):password_hash(invalid)
        self.assertFalse(password_valid('fixture','broken'))

    def test_money_ids_and_webhook_constant_input_validation(self):
        for invalid in (True,1.5,0,-1,'100',100000001):
            with self.subTest(value=invalid),self.assertRaises(ServiceError):amount(invalid)
        for invalid in ('../order','a/b','x?token=secret',''):
            with self.assertRaises(ServiceError):identifier(invalid)
        raw=b'{"event_id":"fixture"}';url='https://example.test/webhooks/square';key='fixture-signature-key'
        signature=base64.b64encode(hmac.new(key.encode(),url.encode()+raw,hashlib.sha256).digest()).decode()
        self.assertTrue(webhook_valid(signature,url,raw,key))
        self.assertFalse(webhook_valid(signature,url,raw+b' ',key))
        self.assertFalse(webhook_valid(signature,url+'/',raw,key))
        self.assertFalse(webhook_valid('\u2603'*44,url,raw,key))


class HostedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.db=Mock();self.db.ready.return_value=True
        self.key=Fernet.generate_key().decode()
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://cloud.example.test','APP_ENCRYPTION_KEYS':self.key},database=self.db)
        self.client=self.app.test_client();self.auth=self.app.extensions['ss_auth']
        self.actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fixture-store','session_hash':'fixture-hash'}
        self.auth.resolve=Mock(return_value=self.actor)

    def get(self,path,**kwargs):return self.client.get(path,base_url='https://cloud.example.test',**kwargs)
    def post(self,path,**kwargs):return self.client.post(path,base_url='https://cloud.example.test',**kwargs)

    def test_health_readiness_headers_and_no_credential_echo(self):
        response=self.get('/healthz');self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['X-Frame-Options'],'DENY')
        self.assertIn("script-src 'self'",response.headers['Content-Security-Policy'])
        self.assertNotIn(self.key,response.text)
        ready=self.get('/readyz').json;self.assertTrue(ready['database_ready']);self.assertFalse(ready['commercial_release'])

    def test_json_origin_host_and_payload_boundaries(self):
        self.assertEqual(self.post('/api/auth/login',data='{}').status_code,415)
        self.assertEqual(self.post('/api/auth/login',json={},headers={'Origin':'https://attacker.test'}).status_code,403)
        self.assertEqual(self.client.get('/healthz',base_url='https://attacker.test').status_code,400)
        self.assertEqual(self.post('/api/auth/login',json={'tenant_id':'other'}).status_code,400)
        self.assertEqual(self.post('/api/auth/login',data=b'x'*530000,content_type='application/json').status_code,413)

    def test_browser_csrf_and_logout_and_bearer_scope(self):
        self.auth.login=Mock(return_value=('fixture-session',{'store':'fixture-store','role':'owner'}))
        result=self.post('/api/auth/login',json={'store_slug':'fixture','email':'test@example.test','password':'not-used'})
        self.assertEqual(result.status_code,200);self.assertIn('HttpOnly',result.headers['Set-Cookie'])
        self.assertEqual(self.post('/api/auth/logout',json={}).status_code,403)
        self.auth.logout=Mock()
        self.assertEqual(self.post('/api/auth/logout',json={},headers={'X-SS-CSRF':result.json['csrf_token']}).status_code,200)
        self.auth.logout.assert_called_once_with('fixture-session')
        self.assertEqual(self.post('/api/auth/logout',json={},headers={'Authorization':'Bearer fixture-desktop'}).status_code,200)

    def test_unauthorized_and_no_tenant_scope_from_body(self):
        self.auth.resolve=Mock(side_effect=ServiceError('Sign in.',401))
        self.assertEqual(self.get('/api/credit/customers').status_code,401)
        self.auth.resolve=Mock(return_value=self.actor)
        response=self.post('/api/credit/customers',json={'tenant_id':str(uuid.uuid4())},headers={'Authorization':'Bearer fixture'})
        self.assertEqual(response.status_code,400)

    def test_oauth_disabled_and_webhooks_fail_closed_without_configuration(self):
        response=self.post('/api/square/connect',json={},headers={'Authorization':'Bearer fixture'})
        self.assertEqual(response.status_code,503)
        self.assertNotIn('secret',response.text)
        self.assertEqual(self.post('/webhooks/square',data='{}',content_type='application/json').status_code,503)

    def test_portal_is_invitation_only_and_has_no_third_party_scripts(self):
        page=self.get('/')
        self.assertEqual(page.status_code,200)
        self.assertIn('invitation-only',page.text)
        self.assertNotIn('https://',page.text)
        script=self.get('/static/portal.js');self.assertEqual(script.status_code,200)
        # Only a random, non-PII epoch may cross tabs; credentials stay in memory.
        storage=[line.strip() for line in script.text.splitlines() if 'localStorage' in line]
        self.assertEqual(storage,["try{localStorage.setItem('ss-auth-epoch',crypto.randomUUID())}catch{}"])
        mfa_script=self.get('/static/mfa.js')
        self.assertNotIn('localStorage',mfa_script.text)
        self.assertNotIn('sessionStorage.setItem',mfa_script.text.replace("sessionStorage.setItem('ss-signed-out','1')",''))
        mfa_script.close()
        self.assertIn('textContent',script.text)
        script.close();page.close()


if __name__=='__main__':unittest.main()
