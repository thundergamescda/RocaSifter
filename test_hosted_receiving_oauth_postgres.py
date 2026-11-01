"""Actual PostgreSQL receiving OAuth state/role/proof checks; no OAuth HTTP."""
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, parse_qs
import json
import unittest
import uuid
from unittest.mock import Mock, patch
from cryptography.fernet import Fernet
from hosted.square import Square
from hosted.security import ServiceError, digest
from test_intake_fixtures import fresh_mfa_actor
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_square_authority import Response


class PostgreSQLReceivingOAuthAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        self.tenant=uuid.uuid4();self.actor={'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':'owner'}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional OAuth store')",(self.tenant,'fixture-'+self.tenant.hex))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'owner@example.invalid','fictional-invalid','owner')",(self.actor['staff_id'],self.tenant))
            self.actor=fresh_mfa_actor(db,self.actor)
        self.transport=Mock();self.config={'PUBLIC_BASE_URL':'https://fixture.invalid','SQUARE_ENVIRONMENT':'production',
            'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode(),'SQUARE_APPLICATION_ID':'fictional-app','SQUARE_APPLICATION_SECRET':'fictional-server-secret'}
        self.square=Square(self.database,self.config,self.transport)
        self.expiry=datetime.now(timezone.utc)+timedelta(days=30)
        self.tokens={'access_token':'fictional-access-private','refresh_token':'fictional-refresh-private','expires_at':self.expiry.isoformat(),'merchant_id':'fixture-merchant-'+self.tenant.hex}
        self.square._token=Mock(return_value=(self.tokens,self.expiry))
        guard=patch('requests.sessions.Session.request',side_effect=AssertionError('No real OAuth/provider HTTP allowed.'));guard.start();self.addCleanup(guard.stop)

    def begin(self,profile='receiving',actor=None):
        url=self.square.begin(actor or self.actor,profile=profile);query=parse_qs(urlsplit(url).query)
        return query['state'][0],query

    def state(self,value):
        with self.database.transaction() as db:return db.execute('SELECT * FROM ss_oauth_states WHERE state_hash=%s',(digest(value),)).fetchone()

    def connection(self):
        with self.database.transaction() as db:return db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(self.tenant,)).fetchone()

    def test_standard_default_read_scopes_remain_available_without_enrollment(self):
        with self.database.transaction() as db:db.execute('DELETE FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',(self.tenant,self.actor['staff_id']))
        state,query=self.begin(profile='standard')
        self.assertEqual(query['scope'][0].split(),list(Square.SCOPES));self.assertNotIn('ITEMS_WRITE',query['scope'][0])
        self.assertEqual(self.state(state)['requested_profile'],'standard');self.transport.post.assert_not_called()

    def test_receiving_state_pins_exact_app_environment_session_purpose_and_scope_list(self):
        state,query=self.begin();row=self.state(state)
        self.assertEqual(query['scope'][0].split(),list(Square.RECEIVING_SCOPES))
        self.assertEqual((row['application_id'],row['environment'],row['session_hash'],row['requested_profile'],row['requested_scopes']),
                         ('fictional-app','production',self.actor['session_hash'],'receiving',list(Square.RECEIVING_SCOPES)))
        self.assertNotIn(self.config['SQUARE_APPLICATION_SECRET'],json.dumps(query));self.transport.post.assert_not_called()

    def test_receiving_begin_rejects_unenrolled_stale_forged_or_revoked_owner(self):
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.begin()
        with self.assertRaises(ServiceError):self.begin(actor=self.actor|{'role':'manager'})
        with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.begin()
        self.assertIsNone(self.connection());self.transport.post.assert_not_called()

    def test_unenrolled_or_demoted_owner_cannot_start_receiving_consent(self):
        with self.database.transaction() as db:db.execute('DELETE FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',(self.tenant,self.actor['staff_id']))
        with self.assertRaises(ServiceError) as caught:self.begin()
        self.assertEqual(caught.exception.status,403)
        with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
        with self.assertRaises(ServiceError):self.begin()
        self.square._token.assert_not_called();self.assertIsNone(self.connection())

    def test_callback_context_changes_and_legacy_unpinned_state_fail_before_exchange(self):
        for column,value in (('application_id','another-app'),('environment','sandbox'),('requested_profile','standard'),('application_id',None)):
            state,_=self.begin()
            with self.database.transaction() as db:db.execute('UPDATE ss_oauth_states SET '+column+'=%s WHERE state_hash=%s',(value,digest(state)))
            with self.subTest(column=column,value=value),self.assertRaises(ServiceError):self.square.complete(self.actor,state,'fictional-code')
        self.square._token.assert_not_called();self.assertIsNone(self.connection())

    def test_callback_other_session_tenant_expiry_decline_and_replay_cannot_store_credentials(self):
        state,_=self.begin()
        for actor in (self.actor|{'session_hash':'f'*64},self.actor|{'tenant_id':uuid.uuid4()}):
            with self.assertRaises(ServiceError):self.square.complete(actor,state,'fictional-code')
        with self.database.transaction() as db:db.execute("UPDATE ss_oauth_states SET expires_at=clock_timestamp()-interval '1 second' WHERE state_hash=%s",(digest(state),))
        with self.assertRaises(ServiceError):self.square.complete(self.actor,state,'fictional-code')
        state,_=self.begin()
        with self.assertRaises(ServiceError):self.square.complete(self.actor,state,'',error='fictional-denied')
        self.assertIsNone(self.state(state));self.square._token.assert_not_called();self.assertIsNone(self.connection())

    def test_final_transaction_rechecks_owner_after_exchange_before_encrypted_save(self):
        state,_=self.begin()
        def exchange(payload):
            with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
            return self.tokens,self.expiry
        self.square._token.side_effect=exchange
        with self.assertRaises(ServiceError):self.square.complete(self.actor,state,'fictional-code')
        self.assertIsNone(self.connection());self.assertIsNone(self.state(state));self.assertEqual(self.square._token.call_count,1)
        with self.assertRaises(ServiceError):self.square.complete(self.actor,state,'fictional-code')
        self.assertEqual(self.square._token.call_count,1)

    def test_completed_receiving_request_is_encrypted_but_never_grants_proof_or_enablement(self):
        state,_=self.begin();result=self.square.complete(self.actor,state,'fictional-code');row=self.connection()
        self.assertTrue(result['connected']);self.assertFalse(result['grants_verified']);self.assertFalse(result['write_checkout_enabled'])
        self.assertEqual(result['requested_profile'],'receiving');self.assertEqual(result['requested_scopes'],list(Square.RECEIVING_SCOPES))
        self.assertNotIn(self.tokens['access_token'],row['encrypted_tokens']);self.assertEqual(json.loads(self.square.cipher.decrypt(row['encrypted_tokens'].encode())),self.tokens)
        with self.database.transaction() as db:self.assertIsNone(db.execute('SELECT enabled FROM ss_receiving_settings WHERE tenant_id=%s',(self.tenant,)).fetchone())
        with self.assertRaises(ServiceError):self.square.complete(self.actor,state,'fictional-code')
        self.assertEqual(self.square._token.call_count,1)
        payload={'merchant_id':self.tokens['merchant_id'],'client_id':'fictional-app','expires_at':self.expiry.isoformat(),'scopes':list(Square.SCOPES)}
        self.transport.post.return_value=Response(payload)
        with self.assertRaises(ServiceError):self.square.verified_access(self.tenant,('ITEMS_WRITE','INVENTORY_WRITE'))
