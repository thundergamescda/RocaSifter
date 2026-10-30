"""Fictional OAuth introspection responses; no Square or network contact."""
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, Mock
import uuid

from hosted.security import ServiceError
from hosted.square import Square


class Response:
    def __init__(self, payload, *, status=200, headers=None, raw=None):
        self.raw=json.dumps(payload).encode('utf-8') if raw is None else raw
        self.status_code=status
        self.headers=headers or {}
        self.closed=False

    def iter_content(self,chunk_size):
        for offset in range(0,len(self.raw),chunk_size):yield self.raw[offset:offset+chunk_size]

    def close(self):self.closed=True


class SquareAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.db=MagicMock();self.cursor=self.db.transaction.return_value.__enter__.return_value
        self.transport=Mock();self.tenant=uuid.uuid4()
        self.config={'PUBLIC_BASE_URL':'https://fixture.invalid','SQUARE_ENVIRONMENT':'production',
                     'APP_ENCRYPTION_KEYS':'fictional-introspection-encryption-key-1234',
                     'SQUARE_APPLICATION_ID':'fixture-app','SQUARE_APPLICATION_SECRET':'fixture-secret'}
        self.square=Square(self.db,self.config,self.transport)
        self.square.access_token=Mock(return_value='fictional-access-token')
        self.expiry=datetime.now(timezone.utc)+timedelta(days=20)
        sealed=self.square.cipher.encrypt(json.dumps({'access_token':'fictional-access-token'}).encode()).decode()
        self.row={'merchant_id':'fixture-merchant','environment':'production','encrypted_tokens':sealed,
                  'expires_at':self.expiry,'revoked_at':None}
        self.cursor.execute.return_value.fetchone.return_value=self.row
        self.payload={'merchant_id':'fixture-merchant','client_id':'fixture-app',
                      'expires_at':self.expiry.isoformat(),
                      'scopes':['MERCHANT_PROFILE_READ','ITEMS_READ','ITEMS_WRITE','INVENTORY_READ','INVENTORY_WRITE']}
        self.required=('ITEMS_WRITE','INVENTORY_WRITE')

    def respond(self,payload=None,**kwargs):
        response=Response(self.payload if payload is None else payload,**kwargs)
        self.transport.post.return_value=response
        return response

    def rejected(self,payload=None,**kwargs):
        response=self.respond(payload,**kwargs)
        with self.assertRaises(ServiceError) as caught:self.square.verified_access(self.tenant,self.required)
        self.assertTrue(response.closed)
        self.assertNotIn('fictional-access-token',str(caught.exception))
        self.assertNotIn('fixture-secret',str(caught.exception))
        return caught.exception

    def test_exact_identity_actual_grants_and_server_only_token(self):
        response=self.respond()
        result=self.square.verified_access(self.tenant,self.required)
        self.assertTrue(response.closed)
        self.assertEqual(result.token,'fictional-access-token')
        self.assertNotIn(result.token,repr(result))
        self.assertNotIn(result.token,json.dumps(result.proof))
        self.assertTrue(result.proof['grants_verified'])
        self.assertEqual(result.proof['merchant_id'],self.row['merchant_id'])
        self.assertEqual(result.proof['application_id'],'fixture-app')
        self.assertEqual(result.proof['environment'],'production')
        self.assertEqual(result.proof['scopes'],sorted(self.payload['scopes']))
        args,kwargs=self.transport.post.call_args
        self.assertEqual(args,('https://connect.squareup.com/oauth2/token/status',))
        self.assertFalse(kwargs['allow_redirects']);self.assertTrue(kwargs['stream'])
        self.assertEqual(kwargs['timeout'],20)
        self.assertEqual(kwargs['headers']['Square-Version'],'2026-09-16')
        self.assertNotIn('client_secret',kwargs)
        self.assertFalse(self.transport.trust_env)

    def test_projection_contains_proof_without_credentials(self):
        self.respond()
        proof=self.square.verified_grants(self.tenant,self.required)
        self.assertIsInstance(proof,dict)
        self.assertNotIn('token',proof)
        self.assertNotIn('fixture-secret',str(proof))

    def test_requested_scopes_cannot_substitute_missing_actual_grant(self):
        error=self.rejected(self.payload|{'scopes':['ITEMS_READ','INVENTORY_READ']})
        self.assertEqual(error.status,409)
        self.assertIn('reconnect',str(error))

    def test_wrong_merchant_or_application_never_yields_authority(self):
        for key in ('merchant_id','client_id'):
            with self.subTest(key=key):self.assertEqual(self.rejected(self.payload|{key:'another-principal'}).status,502)

    def test_nonexpiring_personal_token_expired_near_expiry_or_naive_time_is_rejected(self):
        for expiry in ('',None,True,'2026-10-28T12:00:00','2020-01-01T00:00:00Z',
                       'not-a-date',(datetime.now(timezone.utc)+timedelta(seconds=30)).isoformat()):
            with self.subTest(expiry=expiry):self.assertEqual(self.rejected(self.payload|{'expires_at':expiry}).status,502)

    def test_invalid_scopes_and_provider_errors_are_not_grants(self):
        for scopes in (None,'ITEMS_WRITE',[],['ITEMS_WRITE','ITEMS_WRITE'],[23],['items_write'],['A'*101],['ITEMS_WRITE\n']):
            with self.subTest(scopes=scopes):self.assertEqual(self.rejected(self.payload|{'scopes':scopes}).status,502)
        self.assertEqual(self.rejected(self.payload|{'errors':[{'code':'UNAUTHORIZED'}]}).status,502)
        self.assertEqual(self.rejected([]).status,502)

    def test_redirect_http_error_or_oversized_and_malformed_body_is_rejected_and_closed(self):
        for status in (201,301,302,307,308,401,403,429,500):
            with self.subTest(status=status):self.assertEqual(self.rejected(status=status).status,502)
        for headers in ({'Content-Length':'65537'},{'Content-Length':'-1'},{'Content-Length':'invalid'}):
            with self.subTest(headers=headers):self.assertEqual(self.rejected(headers=headers).status,502)
        for raw in (b'x'*65537,b'{',b'\xff'):
            with self.subTest(raw_length=len(raw)):self.assertEqual(self.rejected(raw=raw).status,502)

    def test_sandbox_proof_is_explicitly_unaccepted_without_contact_or_token_refresh(self):
        sandbox=Square(self.db,self.config|{'SQUARE_ENVIRONMENT':'sandbox'},self.transport)
        sandbox.access_token=Mock()
        with self.assertRaises(ServiceError) as caught:sandbox.verified_access(self.tenant,self.required)
        self.assertEqual(caught.exception.status,409)
        self.assertIn('Sandbox',str(caught.exception))
        sandbox.access_token.assert_not_called();self.transport.post.assert_not_called()

    def test_unknown_client_scope_input_is_rejected_before_token_or_network(self):
        for required in (None,[], 'ITEMS_WRITE',['PAYMENTS_WRITE'],[True],['ITEMS_WRITE','ITEMS_WRITE\n']):
            with self.subTest(required=required),self.assertRaises(ServiceError):self.square.verified_access(self.tenant,required)
        self.square.access_token.assert_not_called();self.transport.post.assert_not_called()

    def test_saved_token_mismatch_revocation_environment_or_expiry_blocks_before_network(self):
        other=self.square.cipher.encrypt(b'{"access_token":"different-token"}').decode()
        for change in ({'encrypted_tokens':other},{'encrypted_tokens':'broken'}, {'revoked_at':datetime.now(timezone.utc)},
                       {'environment':'sandbox'},{'expires_at':datetime.now(timezone.utc)-timedelta(seconds=1)}):
            self.cursor.execute.return_value.fetchone.return_value=self.row|change
            with self.subTest(change=list(change)),self.assertRaises(ServiceError):self.square.verified_access(self.tenant,self.required)
        self.transport.post.assert_not_called()

    def test_connection_revoked_replaced_or_rotated_during_provider_call_blocks_result(self):
        rotated=self.square.cipher.encrypt(b'{"access_token":"fictional-access-token"}').decode()
        for change in ({'revoked_at':datetime.now(timezone.utc)},{'encrypted_tokens':rotated},{'merchant_id':'other-merchant'}):
            self.cursor.execute.return_value.fetchone.side_effect=[self.row,self.row|change]
            response=self.respond()
            with self.subTest(change=list(change)),self.assertRaises(ServiceError) as caught:self.square.verified_access(self.tenant,self.required)
            self.assertEqual(caught.exception.status,409);self.assertTrue(response.closed)

    def test_legacy_token_exchange_and_resource_read_never_follow_redirects(self):
        for code in (301,302,307,308):
            response=Mock(status_code=code)
            self.transport.post.return_value=response
            with self.subTest(code=code),self.assertRaises(ServiceError):self.square._token({'grant_type':'authorization_code','code':'fictional-code'})
            self.assertFalse(self.transport.post.call_args.kwargs['allow_redirects'])
            response.json.assert_not_called()
            self.transport.get.return_value=response
            with self.subTest(read_code=code),self.assertRaises(ServiceError):self.square.retrieve(self.tenant,'orders','fictional-order')
            self.assertFalse(self.transport.get.call_args.kwargs['allow_redirects'])


if __name__=='__main__':unittest.main(verbosity=2)
