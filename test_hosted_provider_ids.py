"""Opaque Square IDs retain their symbols while URL paths stay on the fixed host."""
import base64
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import unittest
from unittest.mock import MagicMock, Mock
from urllib.parse import quote, urlsplit
import uuid
from hosted.security import ServiceError, identifier, provider_id
from hosted.square import Square


class ProviderIDTests(unittest.TestCase):
    def test_symbols_length_case_and_percent_text_are_preserved(self):
        for opaque in ('CaseSensitive:opaque/+id?x#fragment','id%2Fnot-decoded','a'*192,'r'*255,'Square ID with spaces'):
            with self.subTest(opaque=opaque):self.assertEqual(provider_id(opaque),opaque)
        self.assertNotEqual(provider_id('ID:Case'),provider_id('id:case'))

    def test_controls_empty_dot_segments_and_overlong_values_are_rejected(self):
        for invalid in (None,True,23,'','   ','.','..',' padded','padded ',' padded ','x'*256,'line\nbreak','\tleading','trailing\r','null\0byte','del\x7fbyte','c1\x85byte','surrogate\ud800'):
            with self.subTest(value=repr(invalid)),self.assertRaises(ServiceError):provider_id(invalid)

    def test_application_request_key_validator_remains_strict(self):
        self.assertEqual(identifier('application_key-123'),'application_key-123')
        for invalid in ('provider:opaque/id','a'*151,'key with spaces','%2F'):
            with self.subTest(value=invalid),self.assertRaises(ServiceError):identifier(invalid)


class SquareOpaqueIDTests(unittest.TestCase):
    def setUp(self):
        self.db=MagicMock();self.cursor=self.db.transaction.return_value.__enter__.return_value
        self.transport=Mock();self.tenant=uuid.uuid4()
        self.transport.get.return_value.status_code=200
        self.transport.post.return_value.status_code=200
        self.square=Square(self.db,{'PUBLIC_BASE_URL':'https://fixture.invalid','SQUARE_ENVIRONMENT':'sandbox',
                                   'APP_ENCRYPTION_KEYS':'fictional-provider-id-encryption-key-1234',
                                   'SQUARE_APPLICATION_ID':'fixture-app','SQUARE_APPLICATION_SECRET':'fixture-secret',
                                   'SQUARE_WEBHOOK_SIGNATURE_KEY':'fixture-signing-key'},self.transport)

    def signed(self,payload):
        raw=json.dumps(payload,separators=(',',':')).encode()
        signature=base64.b64encode(hmac.new(b'fixture-signing-key',self.square.webhook_url.encode()+raw,hashlib.sha256).digest()).decode()
        return raw,signature

    def test_retrieve_quotes_one_opaque_segment_without_decoding_or_changing_host(self):
        self.square.access_token=Mock(return_value='fixture-access-token')
        self.transport.get.return_value.json.return_value={'refund':{'id':'fixture'}}
        opaque='refund:/Case+suffix%2F?query#fragment'
        self.square.retrieve(self.tenant,'refunds',opaque)
        url=self.transport.get.call_args.args[0]
        self.assertEqual(url,self.square.base+'/v2/refunds/'+quote(opaque,safe=''))
        self.assertEqual(urlsplit(url).hostname,'connect.squareupsandbox.com')
        self.assertEqual(urlsplit(url).query,'');self.assertEqual(urlsplit(url).fragment,'')
        self.assertIn('%252F',url)

    def test_invalid_resource_id_never_obtains_a_token_or_calls_provider(self):
        self.square.access_token=Mock()
        for invalid in ('..',' padded-id ','bad\nvalue','x'*256):
            with self.subTest(value=invalid),self.assertRaises(ServiceError):self.square.retrieve(self.tenant,'orders',invalid)
        self.square.access_token.assert_not_called();self.transport.get.assert_not_called()

    def test_catalog_read_pins_exact_version_and_quotes_opaque_id(self):
        self.square.access_token=Mock(return_value='fixture-access-token')
        self.transport.get.return_value.json.return_value={'object':{'id':'fixture'}}
        opaque='discount:/ID%2F?x#fragment'
        self.square.retrieve(self.tenant,'catalog',opaque,catalog_version=123)
        url=self.transport.get.call_args.args[0]
        self.assertEqual(url,self.square.base+'/v2/catalog/object/'+quote(opaque,safe='')+'?catalog_version=123')
        self.assertEqual(urlsplit(url).hostname,'connect.squareupsandbox.com')
        self.transport.post.assert_not_called()

    def test_catalog_latest_or_invalid_versions_are_rejected_before_any_provider_call(self):
        self.square.access_token=Mock()
        for version in (None,True,0,-1,2**63,'123'):
            with self.subTest(version=version),self.assertRaises(ServiceError):
                self.square.retrieve(self.tenant,'catalog','discount',catalog_version=version)
        with self.assertRaises(ServiceError):self.square.retrieve(self.tenant,'orders','order',catalog_version=123)
        self.square.access_token.assert_not_called();self.transport.get.assert_not_called()

    def test_signed_webhook_preserves_opaque_event_merchant_and_resource_ids(self):
        merchant='merchant:Case/+ID';event='event:'+('e'*180);resource='refund:Opaque/+id%2F'
        lookup=Mock();lookup.fetchone.return_value={'tenant_id':self.tenant,'revoked_at':None}
        previous=Mock();previous.fetchone.return_value=None
        self.cursor.execute.side_effect=[lookup,previous,Mock()]
        result=self.square.ingest(*self.signed({'event_id':event,'merchant_id':merchant,'type':'refund.updated','data':{'id':resource}}))
        self.assertEqual(result,{'accepted':True,'duplicate':False})
        calls=self.cursor.execute.call_args_list
        self.assertEqual(calls[0].args[1],(merchant,))
        self.assertEqual(calls[2].args[1][:4],(event,self.tenant,'refund.updated',resource))

    def test_invalid_signed_event_ids_are_rejected_before_database_access(self):
        valid={'event_id':'event:fixture','merchant_id':'merchant:fixture','type':'payment.updated','data':{'id':'payment:fixture'}}
        for field in ('event_id','merchant_id','resource'):
            payload=valid|{field:'bad\nvalue'} if field!='resource' else valid|{'data':{'id':'..'}}
            with self.subTest(field=field),self.assertRaises(ServiceError):self.square.ingest(*self.signed(payload))
        self.cursor.execute.assert_not_called()

    def test_token_response_preserves_only_valid_opaque_merchant_id(self):
        expiry=(datetime.now(timezone.utc)+timedelta(days=30)).isoformat()
        result={'access_token':'fixture-access','refresh_token':'fixture-refresh','expires_at':expiry,'merchant_id':'merchant:Opaque/+ID'}
        self.transport.post.return_value.json.return_value=result
        tokens,_=self.square._token({'grant_type':'authorization_code','code':'fixture-code'})
        self.assertEqual(tokens['merchant_id'],'merchant:Opaque/+ID')
        for invalid in ('..',' merchant:Opaque/+ID ','bad\nmerchant','x'*256,None):
            self.transport.post.return_value.json.return_value=result|{'merchant_id':invalid}
            with self.subTest(value=repr(invalid)),self.assertRaises(ServiceError) as error:
                self.square._token({'grant_type':'refresh_token','refresh_token':'fixture-refresh'})
            self.assertEqual(error.exception.status,502)
        self.transport.post.return_value.json.return_value={key:value for key,value in result.items() if key!='merchant_id'}
        with self.assertRaises(ServiceError) as error:self.square._token({'grant_type':'authorization_code','code':'fixture-code'})
        self.assertEqual(error.exception.status,502)


if __name__=='__main__':unittest.main()
