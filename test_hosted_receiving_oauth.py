"""Explicit receiving consent HTTP contract, no provider or database calls."""
import unittest
import uuid
from unittest.mock import Mock
from cryptography.fernet import Fernet
from hosted.app import create_app


class ReceivingOAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-store'}
        self.app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://oauth.fixture.invalid','APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=Mock())
        self.app.extensions['ss_auth'].resolve=Mock(return_value=self.actor)
        self.square=self.app.extensions['ss_square'];self.square.begin=Mock(return_value='https://connect.squareup.com/oauth2/authorize?fictional')
        self.client=self.app.test_client();self.base='https://oauth.fixture.invalid';self.headers={'Authorization':'Bearer fictional-session'}

    def test_empty_body_retains_standard_read_only_begin_contract(self):
        response=self.client.post('/api/square/connect',base_url=self.base,headers=self.headers,json={})
        self.assertEqual(response.status_code,200);self.square.begin.assert_called_once_with(self.actor)

    def test_receiving_profile_requires_explicit_extra_permission_confirmation(self):
        for body in ({'profile':'receiving'},{'profile':'receiving','receiving_permissions_confirmed':False},
                     {'profile':'receiving','receiving_permissions_confirmed':1},{'profile':'other'},
                     {'profile':'standard','receiving_permissions_confirmed':True},{'profile':'receiving','receiving_permissions_confirmed':True,'scopes':['PAYMENTS_WRITE']}):
            with self.subTest(body=body):self.assertEqual(self.client.post('/api/square/connect',base_url=self.base,headers=self.headers,json=body).status_code,400)
        self.square.begin.assert_not_called()

    def test_requested_receiving_scopes_do_not_enable_or_prove_actual_grants(self):
        response=self.client.post('/api/square/connect',base_url=self.base,headers=self.headers,json={'profile':'receiving','receiving_permissions_confirmed':True})
        self.assertEqual(response.status_code,200);self.square.begin.assert_called_once_with(self.actor,profile='receiving')
        self.assertEqual((response.json['requested_profile'],response.json['grants_verified'],response.json['receiving_enabled']),('receiving',False,False))

    def test_cookie_receiving_scope_request_keeps_csrf_boundary(self):
        self.client.set_cookie('ss_session','fictional-cookie',domain='oauth.fixture.invalid')
        response=self.client.post('/api/square/connect',base_url=self.base,json={'profile':'receiving','receiving_permissions_confirmed':True})
        self.assertEqual(response.status_code,403);self.square.begin.assert_not_called()
