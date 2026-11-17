"""Source-authored observer HTTP acceptance; UNRUN until separately admitted.

Real SQL only in the existing disposable ss_test_UUID schema. Sessions and MFA
proof rows are fictional seeds, not MFA-crypto acceptance. OAuth is fictional;
HTTP status/config/queue requests must never perform a provider read or write.
No worker, browser, continuous-zero, eligibility or deletion acceptance.
"""
import json
import uuid
from unittest.mock import patch

from hosted.app import create_app
from hosted.security import digest
import test_hosted_receiving_oauth_postgres as oauth_fixture
import test_hosted_settlement_postgres as settlement_fixture


class PostgreSQLCleanupObserverHTTPAcceptance(settlement_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        # Explicit setup delegation avoids inheriting another suite's test IDs.
        oauth_fixture.PostgreSQLReceivingOAuthAcceptance.setUp(self)
        self.http_token='fictional-observer-http-session-'+uuid.uuid4().hex
        with self.database.transaction() as db:
            self.assertRegex(db.execute('SELECT current_schema() AS name').fetchone()['name'],r'^ss_test_[a-f0-9]{32}$')
            # Seed the known fictional bearer at INSERT; never reassign session identity.
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                          VALUES (%s,%s,%s,now()+interval '1 hour',1,now())''',
                       (digest(self.http_token),self.tenant,self.actor['staff_id']))
        self.actor['session_hash']=digest(self.http_token)
        self._observer_reconnect()
        self.http_app=create_app(self.config|{'TESTING':True,'CLEANUP_OBSERVER_ENABLED':True},
                                 database=self.database,square_transport=self.transport)
        self.http_client=self.http_app.test_client()
        self.http_square=self.http_app.extensions['ss_square']
        self.transport.get.reset_mock();self.transport.post.reset_mock()
        provider_guard=patch.object(self.http_square,'verified_access',side_effect=AssertionError('Provider proof must not run in observer HTTP.'))
        self.http_provider_guard=provider_guard.start();self.addCleanup(provider_guard.stop)
        self.addCleanup(self._assert_http_no_provider)

    def _assert_http_no_provider(self):
        self.transport.get.assert_not_called();self.transport.post.assert_not_called()
        self.http_provider_guard.assert_not_called()

    def _observer_reconnect(self):
        state,_=oauth_fixture.PostgreSQLReceivingOAuthAcceptance.begin(self)
        self.square.complete(self.actor,state,'fictional-observer-http-oauth-code')

    def _observer_get(self,path='/api/catalog-cleanup/status',*,client=None,headers=None):
        return (client or self.http_client).get(path,base_url=self.config['PUBLIC_BASE_URL'],
            headers={'Authorization':'Bearer '+self.http_token} if headers is None else headers)

    def _observer_post(self,path,values=None,*,raw=None,headers=None,client=None,content_type='application/json'):
        options={'base_url':self.config['PUBLIC_BASE_URL'],'content_type':content_type,
                 'headers':{'Authorization':'Bearer '+self.http_token} if headers is None else headers}
        if raw is None:options['data']=json.dumps(values)
        else:options['data']=raw
        return (client or self.http_client).post('/api/catalog-cleanup/'+path,**options)

    def _observer_manual_body(self,status=None,**changes):
        status=self._observer_get().json if status is None else status
        return {'request_key':'fictional-http-'+uuid.uuid4().hex,'expected_generation':status['generation'],
                'expected_config_version':status['config_version']}|changes

    def _observer_config_body(self,status=None,**changes):
        return self._observer_manual_body(status)|{'enabled':True,'background_enabled':False,
            'cadence_seconds':3600,'stale_after_seconds':7200}|changes

    def _observer_enable(self):
        result=self._observer_post('config',self._observer_config_body())
        self.assertEqual(result.status_code,200);self.assertEqual(result.json,{'accepted':True,'duplicate':False})
        return self._observer_get().json

    def test_status_is_minimal_default_off_and_never_provider_proof(self):
        response=self._observer_get();self.assertEqual(response.status_code,200)
        value=response.json
        self.assertEqual(set(value),{'generation','config_version','enabled','background_enabled','worker_enabled',
            'cadence_seconds','stale_after_seconds','state','reason_codes','coverage_current','last_attempt','last_complete',
            'observed_age_seconds','pending_notification_count','missing_count','missing','missing_truncated','next_due_at',
            'atomic_snapshot','continuous_zero_verified','deletion_enabled'})
        self.assertEqual(str(uuid.UUID(value['generation'])),value['generation'])
        self.assertEqual(value['config_version'],0);self.assertFalse(value['enabled']);self.assertFalse(value['background_enabled'])
        self.assertTrue(value['worker_enabled']);self.assertFalse(value['coverage_current'])
        self.assertIsNone(value['last_attempt']);self.assertIsNone(value['last_complete']);self.assertIsNone(value['missing_count'])
        for name in ('atomic_snapshot','continuous_zero_verified','deletion_enabled'):self.assertIs(value[name],False)
        self.assertEqual(response.headers['Cache-Control'],'no-store')
        for secret in (self.http_token,self.tokens['access_token'],self.tokens['refresh_token'],self.config['SQUARE_APPLICATION_SECRET']):
            self.assertNotIn(secret,response.get_data(as_text=True))

    def test_owner_can_store_consent_with_default_global_gate_off_but_cannot_enqueue(self):
        app=create_app(self.config|{'TESTING':True},database=self.database,square_transport=self.transport)
        client=app.test_client();initial=self._observer_get(client=client).json
        self.assertFalse(initial['worker_enabled'])
        response=self._observer_post('config',self._observer_config_body(initial,background_enabled=True),client=client)
        self.assertEqual(response.status_code,200)
        current=self._observer_get(client=client).json
        self.assertTrue(current['enabled']);self.assertTrue(current['background_enabled']);self.assertFalse(current['worker_enabled'])
        self.assertFalse(current['coverage_current']);self.assertIsNone(current['last_attempt'])
        blocked=self._observer_post('refresh',self._observer_manual_body(current),client=client)
        self.assertEqual(blocked.status_code,409);self.assertEqual(blocked.json['code'],'OBSERVER_DISABLED')
        withdrawn=self._observer_post('config',self._observer_config_body(current,enabled=False,background_enabled=False),client=client)
        self.assertEqual(withdrawn.status_code,200);self.assertEqual(withdrawn.json,{'accepted':True,'duplicate':False})
        after=self._observer_get(client=client).json
        self.assertFalse(after['enabled']);self.assertFalse(after['background_enabled']);self.assertFalse(after['worker_enabled'])

    def test_manual_enqueue_is_ack_only_and_same_key_replay_is_durable(self):
        current=self._observer_enable();values=self._observer_manual_body(current)
        first=self._observer_post('refresh',values);again=self._observer_post('refresh',values)
        self.assertEqual(first.status_code,200);self.assertEqual(first.json,{'accepted':True,'duplicate':False})
        self.assertEqual(again.status_code,200);self.assertEqual(again.json,{'accepted':True,'duplicate':True})
        status=self._observer_get().json
        self.assertEqual(status['state'],'queued');self.assertIsNone(status['last_attempt']);self.assertFalse(status['coverage_current'])

    def test_configure_replay_is_ack_only_and_conflicting_intent_is_rejected(self):
        values=self._observer_config_body()
        first=self._observer_post('config',values);again=self._observer_post('config',values)
        self.assertEqual(first.json,{'accepted':True,'duplicate':False});self.assertEqual(first.status_code,200)
        self.assertEqual(again.json,{'accepted':True,'duplicate':True});self.assertEqual(again.status_code,200)
        changed=self._observer_post('config',values|{'background_enabled':True})
        self.assertEqual(changed.status_code,409);self.assertEqual(changed.json['code'],'OBSERVER_REQUEST_CONFLICT')
        current=self._observer_get().json;self.assertEqual(current['config_version'],1);self.assertFalse(current['background_enabled'])

    def test_unknown_authority_and_principal_fields_are_rejected_without_mutation(self):
        before=self._observer_get().json
        for path in ('config','refresh'):
            for field in ('tenant_id','principal','staff_id','application_id','environment','merchant_id','lease','authorization_generation','unexpected'):
                with self.subTest(path=path,field=field):
                    body=self._observer_config_body(before) if path=='config' else self._observer_manual_body(before)
                    result=self._observer_post(path,body|{field:'fictional-untrusted'})
                    self.assertEqual(result.status_code,400);self.assertEqual(result.json['code'],'OBSERVER_INPUT_INVALID')
        self.assertEqual(self._observer_get().json,before)

    def test_duplicate_nonfinite_nested_wrong_type_and_missing_fields_fail_closed(self):
        before=self._observer_get().json;valid=self._observer_config_body(before)
        encoded=json.dumps(valid)
        malformed=[encoded[:-1]+',"enabled":false}',encoded.replace('3600','NaN'),encoded.replace('3600','Infinity'),
            encoded.replace('3600','-Infinity'),encoded.replace('3600','3600.0'),'[]','null',
            json.dumps(valid|{'expected_config_version':True}),json.dumps(valid|{'enabled':1}),
            json.dumps(valid|{'expected_generation':{'tenant_id':'fictional'}}),json.dumps(valid|{'request_key':None}),
            json.dumps({key:value for key,value in valid.items() if key!='background_enabled'})]
        for raw in malformed:
            with self.subTest(raw=raw):
                result=self._observer_post('config',raw=raw)
                self.assertEqual(result.status_code,400);self.assertEqual(result.json['code'],'OBSERVER_INPUT_INVALID')
        self.assertEqual(self._observer_get().json,before)

    def test_small_body_utf8_and_scalar_bounds_are_enforced(self):
        before=self._observer_get().json
        for raw in (b'\xff',b' '*4097,b'{'):
            with self.subTest(raw_length=len(raw)):
                response=self._observer_post('config',raw=raw)
                self.assertEqual(response.status_code,400);self.assertEqual(response.json['code'],'OBSERVER_INPUT_INVALID')
        for changes in ({'cadence_seconds':299},{'cadence_seconds':86401},{'stale_after_seconds':3599},
                        {'stale_after_seconds':604801},{'expected_config_version':-1},{'expected_generation':'not-a-uuid'},
                        {'request_key':''},{'enabled':False,'background_enabled':True}):
            with self.subTest(changes=changes):
                response=self._observer_post('config',self._observer_config_body(before,**changes))
                self.assertEqual(response.status_code,400);self.assertEqual(response.json['code'],'OBSERVER_INPUT_INVALID')
        self.assertEqual(self._observer_get().json,before)

    def test_config_version_cas_prevents_stale_tab_consent(self):
        before=self._observer_get().json;current=self._observer_enable()
        stale=self._observer_post('config',self._observer_config_body(before,enabled=False))
        self.assertEqual(stale.status_code,409);self.assertEqual(stale.json['code'],'OBSERVER_CONFIG_CHANGED')
        stale_manual=self._observer_post('refresh',self._observer_manual_body(before))
        self.assertEqual(stale_manual.status_code,409);self.assertEqual(stale_manual.json['code'],'OBSERVER_CONFIG_CHANGED')
        self.assertEqual(self._observer_get().json,current)

    def test_reconnect_rejects_original_generation_and_new_generation_starts_off(self):
        before=self._observer_enable();old_config=self._observer_config_body(before,enabled=False)
        old_manual=self._observer_manual_body(before)
        self._observer_reconnect();current=self._observer_get().json
        self.assertNotEqual(current['generation'],before['generation']);self.assertEqual(current['config_version'],0)
        self.assertFalse(current['enabled']);self.assertFalse(current['background_enabled']);self.assertFalse(current['coverage_current'])
        for path,body in (('config',old_config),('refresh',old_manual)):
            with self.subTest(path=path):
                result=self._observer_post(path,body)
                self.assertEqual(result.status_code,409);self.assertEqual(result.json['code'],'OBSERVER_GENERATION_CHANGED')
        self.assertEqual(self._observer_get().json,current)

    def test_current_db_roles_gate_view_configuration_and_manual_queue(self):
        current=self._observer_enable()
        for role in ('manager','buyer','cashier'):
            with self.subTest(role=role):
                with self.database.transaction() as db:
                    db.execute('UPDATE ss_staff SET role=%s WHERE tenant_id=%s AND id=%s',(role,self.tenant,self.actor['staff_id']))
                self.assertEqual(self._observer_get().status_code,403 if role=='cashier' else 200)
                config=self._observer_post('config',self._observer_config_body(current,enabled=False))
                self.assertEqual(config.status_code,403)
                manual=self._observer_post('refresh',self._observer_manual_body(current))
                self.assertEqual(manual.status_code,200 if role=='manager' else 403)

    def test_stale_mfa_blocks_configuration_and_accepted_replay_but_not_view_or_manual(self):
        values=self._observer_config_body();self.assertEqual(self._observer_post('config',values).status_code,200)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(digest(self.http_token),))
        current=self._observer_get();self.assertEqual(current.status_code,200)
        for body in (values,self._observer_config_body(current.json,enabled=False)):
            result=self._observer_post('config',body)
            self.assertEqual(result.status_code,403);self.assertEqual(result.json['code'],'MFA_STEP_UP_REQUIRED')
        self.assertEqual(self._observer_post('refresh',self._observer_manual_body(current.json)).status_code,200)

    def test_demotion_blocks_an_accepted_replay_without_rebinding_authority(self):
        values=self._observer_config_body();self.assertEqual(self._observer_post('config',values).status_code,200)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
        self.assertEqual(self._observer_post('config',values).status_code,403)
        current=self._observer_get().json;self.assertTrue(current['enabled']);self.assertEqual(current['config_version'],1)

    def test_expired_or_deleted_session_and_disabled_staff_cannot_replay_or_read(self):
        values=self._observer_config_body();self.assertEqual(self._observer_post('config',values).status_code,200)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",(digest(self.http_token),))
        self.assertEqual(self._observer_get().status_code,401);self.assertEqual(self._observer_post('config',values).status_code,401)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()+interval '1 hour' WHERE token_hash=%s",(digest(self.http_token),))
            db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,self.actor['staff_id']))
        self.assertEqual(self._observer_get().status_code,401);self.assertEqual(self._observer_post('config',values).status_code,401)
        with self.database.transaction() as db:
            db.execute('UPDATE ss_staff SET enabled=TRUE WHERE tenant_id=%s AND id=%s',(self.tenant,self.actor['staff_id']))
            db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(digest(self.http_token),))
        self.assertEqual(self._observer_get().status_code,401);self.assertEqual(self._observer_post('config',values).status_code,401)

    def test_cookie_csrf_is_required_and_cross_origin_or_wrong_mime_is_rejected(self):
        self.http_client.set_cookie('ss_session',self.http_token,domain='fixture.invalid',secure=True,httponly=True)
        me=self._observer_get('/api/me',headers={});self.assertEqual(me.status_code,200)
        values=self._observer_config_body()
        self.assertEqual(self._observer_post('config',values,headers={}).status_code,403)
        self.assertEqual(self._observer_post('config',values,headers={'X-SS-CSRF':'fictional-wrong'}).status_code,403)
        self.assertEqual(self._observer_post('config',values,headers={'X-SS-CSRF':me.json['csrf_token'],'Origin':'https://elsewhere.invalid'}).status_code,403)
        self.assertEqual(self._observer_post('config',values,content_type='text/plain').status_code,415)
        result=self._observer_post('config',values,headers={'X-SS-CSRF':me.json['csrf_token']})
        self.assertEqual(result.status_code,200);self.assertEqual(result.json,{'accepted':True,'duplicate':False})

    def test_missing_and_malformed_bearer_never_reach_store_status_or_queue(self):
        for headers in ({},{'Authorization':'Basic fictional'},{'Authorization':'Bearer fictional-not-a-session'}):
            with self.subTest(headers=headers):
                self.assertEqual(self._observer_get(headers=headers).status_code,401)
                self.assertEqual(self._observer_post('refresh',self._observer_manual_body(),headers=headers).status_code,401)

    def test_tenant_query_hint_cannot_select_another_connection(self):
        expected=self._observer_get().json
        response=self._observer_get('/api/catalog-cleanup/status?tenant_id='+str(uuid.uuid4()))
        self.assertEqual(response.status_code,200);self.assertEqual(response.json,expected)
