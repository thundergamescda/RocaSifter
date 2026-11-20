"""UNRUN staff stock SQL/HTTP acceptance with an owned fictional Square transport.

Real Auth/MFA, token exchange/introspection/renewal and authority checks execute.
No dependency, authority, clock or route mocks; no live provider/hardware proof.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb

from hosted.app import create_app
from hosted.mfa import secret_bytes, totp
from hosted.receiving_plan import API_VERSION, READ_SCOPES, fingerprint
from hosted.security import ServiceError, password_hash
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture


class _Response:
    def __init__(self, value):
        self.raw=json.dumps(value,separators=(',',':')).encode();self.status_code=200
        self.headers={'Content-Length':str(len(self.raw))};self.closed=False
    def iter_content(self,chunk_size):
        for offset in range(0,len(self.raw),chunk_size):yield self.raw[offset:offset+chunk_size]
    def close(self):self.closed=True


class _Transport:
    """No network implementation; only fixed fictional reads and normal OAuth."""
    def __init__(self, case):
        self.case=case;self.calls=[];self.responses=[];self.token='';self.refresh_token='';self.expiry=None
        self.exchanges=0;self.business_writes=0
        self.scopes=list(READ_SCOPES);self.hook=None;self.count_pages=None;self.search_override=None
        self.location='stock-location-'+uuid.uuid4().hex;self.merchant='stock-merchant-'+uuid.uuid4().hex
        self.variation='stock-variation-'+uuid.uuid4().hex;self.parent='stock-parent-'+uuid.uuid4().hex
        self.sku=' 000Ab^>~7 '
        self.locations=[{'id':self.location,'name':'Fictional <stock> location','status':'ACTIVE',
                         'merchant_id':self.merchant,'currency':'CAD'}]
        self.objects={self.variation:{'id':self.variation,'type':'ITEM_VARIATION','version':1,
            'item_variation_data':{'item_id':self.parent,'sku':self.sku,'name':'<script>literal variation</script>',
                                   'track_inventory':True,'pricing_type':'VARIABLE_PRICING'}},
            self.parent:{'id':self.parent,'type':'ITEM','version':1,
                         'item_data':{'name':'<img src=x>literal item','product_type':'REGULAR'}}}

    def count(self,quantity='3'):
        return {'catalog_object_id':self.variation,'catalog_object_type':'ITEM_VARIATION',
                'location_id':self.location,'state':'IN_STOCK','quantity':quantity,
                'calculated_at':'2026-11-01T12:00:00.123456789Z'}

    def get(self,url,**options):return self._request('GET',url,options)
    def post(self,url,**options):return self._request('POST',url,options)

    def _request(self,method,url,options):
        self.case.assertLess(len(self.calls),500)
        self.calls.append((method,url))
        self.case.assertTrue(url.startswith('https://connect.squareup.com/'))
        self.case.assertIs(options['allow_redirects'],False);self.case.assertIs(options['stream'],True)
        self.case.assertGreater(options['timeout'],0);self.case.assertLessEqual(options['timeout'],20)
        path=urlsplit(url).path
        if method=='POST' and path=='/oauth2/token':
            self.case.assertIs(options.get('verify'),True)
            self.case.assertEqual(options['headers'],{'Square-Version':API_VERSION})
            body=options['json'];self.case.assertEqual(body['client_id'],'fictional-stock-app')
            self.case.assertEqual(body['client_secret'],'fictional-stock-secret')
            self.case.assertIn(body['grant_type'],('authorization_code','refresh_token'))
            if body['grant_type']=='authorization_code':
                self.case.assertEqual(body['code'],'fictional-stock-code')
                self.case.assertEqual(body['redirect_uri'],'https://stock-fixture.invalid/oauth/square/callback')
                self.refresh_token='fictional-stock-refresh-'+str(self.exchanges+1)
            else:
                self.case.assertEqual(body['refresh_token'],self.refresh_token)
                self.case.assertIsNotNone(self.expiry)
            expires=datetime.now(timezone.utc)+timedelta(days=30)
            # PRIVATE refresh retains its refresh token, changes access and extends expiry.
            self.expiry=max(expires,self.expiry+timedelta(seconds=1)) if body['grant_type']=='refresh_token' else expires
            self.exchanges+=1;self.token='fictional-stock-access-'+str(self.exchanges)
            value={'access_token':self.token,'refresh_token':self.refresh_token,
                   'merchant_id':self.merchant,'client_id':'fictional-stock-app','token_type':'bearer',
                   'expires_at':self.expiry.isoformat()}
        else:
            self.case.assertEqual(options['headers'],{'Square-Version':API_VERSION,'Authorization':'Bearer '+self.token,
                                                       'Content-Type':'application/json'})
            if method=='POST' and path=='/oauth2/token/status':
                self.case.assertNotIn('data',options);self.case.assertNotIn('json',options)
                value={'client_id':'fictional-stock-app','merchant_id':self.merchant,'scopes':self.scopes,
                       'expires_at':self.expiry.isoformat()}
            elif method=='GET' and path=='/v2/locations':
                self.case.assertNotIn('data',options);value={'locations':deepcopy(self.locations)}
            elif method=='POST' and path=='/v2/catalog/search':
                query=json.loads(options['data'])
                self.case.assertEqual(set(query),{'object_types','include_deleted_objects','include_related_objects','limit','query'})
                self.case.assertEqual(query['object_types'],['ITEM_VARIATION']);self.case.assertIs(query['include_deleted_objects'],False)
                self.case.assertIs(query['include_related_objects'],False);self.case.assertEqual(query['limit'],20)
                exact=query['query']['set_query'];self.case.assertEqual(exact['attribute_name'],'sku')
                self.case.assertEqual(len(exact['attribute_values']),1);sku=exact['attribute_values'][0]
                rows=(self.search_override if self.search_override is not None else [row for row in self.objects.values()
                    if row['type']=='ITEM_VARIATION' and row['item_variation_data'].get('sku','').casefold()==sku.casefold()])
                value={'objects':deepcopy(rows)}
            elif method=='POST' and path=='/v2/catalog/batch-retrieve':
                query=json.loads(options['data']);self.case.assertEqual(set(query),{'object_ids','include_related_objects'})
                self.case.assertIs(query['include_related_objects'],True)
                self.case.assertTrue(set(query['object_ids'])<=set(self.objects));self.case.assertLessEqual(len(query['object_ids']),2)
                value={'objects':[deepcopy(self.objects[ident]) for ident in query['object_ids']]}
            elif method=='POST' and path=='/v2/inventory/counts/batch-retrieve':
                query=json.loads(options['data'])
                self.case.assertEqual(set(query)-{'cursor'},{'catalog_object_ids','location_ids','states','limit'})
                self.case.assertEqual(query['catalog_object_ids'],[self.variation]);self.case.assertEqual(query['location_ids'],[self.location])
                self.case.assertEqual(query['states'],['IN_STOCK']);self.case.assertEqual(query['limit'],1000)
                pages=self.count_pages if self.count_pages is not None else [{'counts':[self.count()]}]
                cursor=query.get('cursor');index=0 if cursor is None else {'next':1,'cycle':1,'p1':1,'p2':2,'p3':3}[cursor]
                self.case.assertLess(index,len(pages));value=deepcopy(pages[index])
                if self.hook is not None:
                    hook=self.hook;self.hook=None;hook()
            else:
                self.business_writes+=1
                raise AssertionError('Nonallowlisted provider request')
        response=_Response(value);self.responses.append(response);return response


class PostgreSQLStockLookupAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        self.tenant=uuid.uuid4();self.slug='stock-'+self.tenant.hex;self.password='Fictional stock password 2026!'
        self.ids={role:uuid.uuid4() for role in ('owner','manager','buyer','cashier')}
        hashed=password_hash(self.password)
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(self.tenant,self.slug,'Fictional stock store'))
            for role,staff in self.ids.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',
                           (staff,self.tenant,role+'@example.invalid',hashed,role))
        self.transport=_Transport(self)
        self.config={'TESTING':True,'PUBLIC_BASE_URL':'https://stock-fixture.invalid',
            'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode(),'SQUARE_APPLICATION_ID':'fictional-stock-app',
            'SQUARE_APPLICATION_SECRET':'fictional-stock-secret','SQUARE_ENVIRONMENT':'production'}
        self.app=create_app(self.config,database=self.database,square_transport=self.transport)
        self.auth=self.app.extensions['ss_auth'];self.square=self.app.extensions['ss_square'];self.service=self.app.extensions['ss_stock_lookup']
        self.tokens={};self.actors={}
        for role in self.ids:
            token,info=self.auth.login({'store_slug':self.slug,'email':role+'@example.invalid','password':self.password},'fixture-'+role)
            self.assertIsNotNone(token);self.tokens[role]=token;self.actors[role]=self.auth.resolve(token)
        self.recovery=self.enroll('owner');self.connect()

    def enroll(self,role):
        actor=self.actors[role];start=self.auth.mfa.begin(actor,{'password':self.password},'fixture-enroll-'+role)
        result=self.auth.mfa.confirm(actor,{'enrollment_token':start['enrollment_token'],
            'code':totp(secret_bytes(start['secret']),time.time())},'fixture-enroll-'+role)
        self.actors[role]=self.auth.resolve(self.tokens[role])
        return result['recovery_codes']

    def connect(self):
        url=self.square.begin(self.actors['owner'],profile='standard')
        state=parse_qs(urlsplit(url).query)['state'][0]
        self.square.complete(self.actors['owner'],state,'fictional-stock-code')

    def lookup(self,role='owner',sku=None):
        return self.service.lookup(self.actors[role],self.transport.sku if sku is None else sku,self.transport.location)

    def protected(self):
        result={}
        for table in ('ss_intake_claims','ss_receiving_bindings','ss_receiving_binding_previews','ss_receiving_operations',
                      'ss_receiving_steps','ss_receiving_price_templates','ss_trade_receipts','ss_credit_entries'):
            with self.database.transaction() as db:
                result[table]=db.execute('SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text),\'[]\'::jsonb) AS rows FROM '+
                    table+' t WHERE tenant_id=%s',(self.tenant,)).fetchone()['rows']
        return result

    def assert_safe(self,before):
        self.assertEqual(self.protected(),before);self.assertEqual(self.transport.business_writes,0)
        self.assertTrue(all(response.closed for response in self.transport.responses))

    def test_four_current_roles_read_without_business_mutation(self):
        before=self.protected()
        for role in self.actors:
            with self.subTest(role=role):
                self.assertEqual(self.service.locations(self.actors[role])['locations'][0]['id'],self.transport.location)
                value=self.lookup(role);self.assertEqual((value['catalog_state'],value['count_state'],value['availability']),('match','reported','positive'))
                self.assertEqual(value['requested_sku'],self.transport.sku);self.assertEqual(value['inventory']['quantity'],'3')
                self.assertEqual(value['reference']['item_name'],'<img src=x>literal item')
                self.assertEqual(value['reference']['variation_name'],'<script>literal variation</script>')
                self.assertIs(value['atomic_snapshot'],False);self.assertLess(len(json.dumps(value).encode()),16384)
                self.assertNotIn('access_token',json.dumps(value));self.assertNotIn('scopes',value)
        self.assert_safe(before)

    def test_raw_sku_case_variants_and_ambiguity_never_pick_another_reference(self):
        before=self.protected();original=deepcopy(self.transport.objects)
        for sku in ('','   ','x'*101,'sku\x00','sku\u200b'):
            count=len(self.transport.calls)
            with self.subTest(invalid=repr(sku)),self.assertRaises(ServiceError):self.lookup(sku=sku)
            self.assertEqual(len(self.transport.calls),count)
        for sku in (self.transport.sku.strip(),self.transport.sku.lower(),'000Ab^>~7',' 000Ab^>~7  '):
            with self.subTest(sku=sku):
                value=self.lookup(sku=sku);self.assertEqual(value['catalog_state'],'no_match');self.assertIsNone(value['reference'])
        for other_sku in (self.transport.sku,self.transport.sku.lower()):
            row=deepcopy(original[self.transport.variation]);row['id']='another-variation';row['item_variation_data']['sku']=other_sku
            self.transport.objects['another-variation']=row
            value=self.lookup();self.assertEqual(value['catalog_state'],'ambiguous');self.assertIsNone(value['inventory'])
        self.transport.objects=original
        self.assertEqual(self.lookup()['reference']['sku'],self.transport.sku)
        unicode_sku='\U0001f0a1'*100
        self.transport.objects[self.transport.variation]['item_variation_data']['sku']=unicode_sku
        self.assertEqual(self.lookup(sku=unicode_sku)['requested_sku'],unicode_sku)
        self.transport.search_override=[dict(original[self.transport.variation],merchant_id='foreign-merchant')]
        self.assertEqual(self.lookup(sku=unicode_sku)['catalog_state'],'incomplete')
        self.assert_safe(before)

    def test_decimal_positive_explicit_zero_negative_fraction_and_missing_are_distinct(self):
        before=self.protected()
        for quantity,availability in (('0','zero_reported'),('-0.00000','zero_reported'),('9007199254740993','positive'),
                                      ('-2','negative_reported'),('1.25000','fractional_review'),('-0.125','fractional_review')):
            with self.subTest(quantity=quantity):
                self.transport.count_pages=[{'counts':[self.transport.count(quantity)]}]
                result=self.lookup();self.assertEqual(result['availability'],availability);self.assertEqual(result['inventory']['quantity'],quantity)
        self.transport.count_pages=[{'counts':[]}]
        result=self.lookup();self.assertEqual(result['count_state'],'missing');self.assertEqual(result['availability'],'unknown');self.assertIsNone(result['inventory'])
        for row in (dict(self.transport.count(),is_estimated=True),{key:value for key,value in self.transport.count().items() if key!='calculated_at'}):
            self.transport.count_pages=[{'counts':[row]}]
            result=self.lookup();self.assertEqual(result['count_state'],'unknown');self.assertIsNone(result['inventory'])
        self.assert_safe(before)

    def test_bad_foreign_duplicate_or_partial_counts_never_publish_quantity(self):
        before=self.protected();count=self.transport.count()
        bad=[dict(count,location_id='foreign-location'),dict(count,catalog_object_id='foreign-variation'),dict(count,merchant_id='foreign-merchant'),
             dict(count,state='SOLD'),dict(count,catalog_object_type='ITEM'),dict(count,quantity=True),
             dict(count,quantity='NaN'),dict(count,quantity='1e3'),dict(count,quantity='0.123456'),dict(count,calculated_at='invalid')]
        pages=[[{'counts':[row]}] for row in bad]+[[{'counts':[count,count]}],[{'counts':[count],'cursor':False}],
            [{'counts':[count],'cursor':'next'},{'counts':[dict(count,quantity='9')]}],
            [{'counts':[],'cursor':'cycle'},{'counts':[],'cursor':'cycle'}],
            [{'counts':[],'cursor':'p1'},{'counts':[],'cursor':'p2'},{'counts':[],'cursor':'p3'},{'counts':[count]}], [{}]]
        for value in pages:
            with self.subTest(pages=value):
                self.transport.count_pages=value;result=self.lookup()
                self.assertEqual(result['catalog_state'],'incomplete');self.assertIsNone(result['reference']);self.assertIsNone(result['inventory'])
        self.assert_safe(before)

    def test_price_independent_measured_option_conversion_and_tracking_references(self):
        before=self.protected();original=deepcopy(self.transport.objects);data=self.transport.objects[self.transport.variation]['item_variation_data']
        data.update(price_money={'amount':999,'currency':'EUR'},pricing_type='VARIABLE_PRICING',
                    item_option_values=[{'item_option_id':'color','item_option_value_id':'red'}])
        self.assertEqual(self.lookup()['count_state'],'reported')
        data['track_inventory']=False;data['location_overrides']=[{'location_id':self.transport.location,'track_inventory':True}]
        self.assertEqual(self.lookup()['catalog_state'],'match')
        data['location_overrides'][0]['track_inventory']=False
        self.assertEqual(self.lookup()['reason_code'],'INVENTORY_NOT_TRACKED')
        data['track_inventory']=True;data['location_overrides']=[{'location_id':self.transport.location,'track_inventory':False}]
        self.assertEqual(self.lookup()['reason_code'],'INVENTORY_NOT_TRACKED')
        data['location_overrides']=[{'location_id':self.transport.location,'track_inventory':True}]*2
        self.assertEqual(self.lookup()['reason_code'],'TRACKING_SETTINGS_UNSUPPORTED')
        data.pop('location_overrides');data['measurement_unit_id']='measurement'
        self.transport.objects['measurement']={'id':'measurement','type':'MEASUREMENT_UNIT','measurement_unit_data':{
            'precision':3,'measurement_unit':{'custom_unit':{'name':'Custom <unit>','abbreviation':'u'}}}}
        result=self.lookup();self.assertEqual(result['reference']['unit']['kind'],'measured');self.assertEqual(result['reference']['unit']['precision'],3)
        measurement=self.transport.objects['measurement']['measurement_unit_data']
        custom=deepcopy(measurement['measurement_unit'])
        for unit in ({'type':'TYPE_WEIGHT','length_unit':'METRIC_METER'},
                     {'weight_unit':'INVENTED_UNIT'},{'type':'FUTURE_TYPE','weight_unit':'METRIC_KILOGRAM'},
                     {'weight_unit':'METRIC_KILOGRAM','length_unit':'METRIC_METER'}):
            measurement['measurement_unit']=unit
            self.assertEqual(self.lookup()['reason_code'],'MEASUREMENT_UNIT_UNSUPPORTED')
        measurement['measurement_unit']={'type':'TYPE_WEIGHT','weight_unit':'METRIC_KILOGRAM'}
        self.assertEqual(self.lookup()['reference']['unit']['kind'],'measured')
        measurement['measurement_unit']=custom
        stock=deepcopy(original[self.transport.variation]);stock['id']='stockable';stock['item_variation_data'].update(sku='STOCKABLE',stockable=True)
        self.transport.objects['stockable']=stock;data['stockable']=False
        data['stockable_conversion']={'stockable_item_variation_id':'stockable','stockable_quantity':'1','nonstockable_quantity':'5'}
        result=self.lookup();self.assertEqual(result['reference']['unit']['kind'],'measured_conversion')
        self.assertIn('CONVERSION',result['reference']['qualifications']);self.assertEqual(result['inventory']['quantity'],'3')
        data['stockable_conversion']['nonstockable_quantity']='0'
        self.assertEqual(self.lookup()['reason_code'],'CONVERSION_UNSUPPORTED')
        self.transport.objects=deepcopy(original)
        def change():self.transport.objects[self.transport.variation]['item_variation_data']['name']='Changed during count'
        self.transport.hook=change;result=self.lookup();self.assertEqual(result['catalog_state'],'mismatch');self.assertIsNone(result['inventory'])
        self.transport.objects=deepcopy(original)
        self.transport.hook=lambda:self.transport.locations[0].update(status='INACTIVE')
        result=self.lookup();self.assertEqual(result['catalog_state'],'mismatch');self.assertIsNone(result['reference'])
        self.assert_safe(before)

    def test_live_role_session_mfa_and_enabled_changes_during_io_deny(self):
        before=self.protected();self.enroll('cashier')
        changes={'owner':("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",self.ids['owner']),
                 'manager':('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',self.ids['manager']),
                 'buyer':('DELETE FROM ss_sessions WHERE tenant_id=%s AND staff_id=%s',self.ids['buyer']),
                 'cashier':('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',self.ids['cashier'])}
        for role,(query,ident) in changes.items():
            def change(query=query,ident=ident):
                with self.database.transaction() as db:db.execute(query,(self.tenant,ident))
            self.transport.hook=change
            with self.subTest(role=role),self.assertRaises(ServiceError):self.lookup(role)
        self.assert_safe(before)

    def test_grants_revocation_generation_merchant_and_config_fence_reads(self):
        before=self.protected();self.transport.scopes=['ITEMS_READ']
        with self.assertRaises(ServiceError):self.lookup()
        self.transport.scopes=list(READ_SCOPES)
        with self.database.transaction() as db:
            connection=db.execute('SELECT authorization_generation,merchant_id FROM ss_square_connections WHERE tenant_id=%s',(self.tenant,)).fetchone()
        changes=(('revoked_at=clock_timestamp()',()),('authorization_generation=%s',(uuid.uuid4(),)),
                 ('merchant_id=%s',('changed-merchant',)),('pending_notice_cursor',()))
        for clause,values in changes:
            committed={}
            def change(clause=clause,values=values):
                if clause=='pending_notice_cursor':
                    event='fictional-stock-pending-'+uuid.uuid4().hex
                    evidence={'event_id':event,'merchant_id':self.transport.merchant,'type':'oauth.authorization.revoked'}
                    with self.database.transaction() as db:
                        self.square._notice_barrier(db)  # First SQL; same receipt-allocation barrier as ingress.
                        receipt=db.execute('''INSERT INTO ss_square_revocation_receipts
                            (application_id,environment,event_id,merchant_id,body_hash,outcome)
                            VALUES (%s,%s,%s,%s,%s,%s) RETURNING notice_cursor''',
                            (self.app.config['SQUARE_APPLICATION_ID'],self.square.environment,event,self.transport.merchant,
                             fingerprint(evidence),Jsonb({'accepted':True,'revocation_pending':True}))).fetchone()
                        db.execute('UPDATE ss_square_connections SET pending_notice_cursor=%s WHERE tenant_id=%s',
                                   (receipt['notice_cursor'],self.tenant))
                    committed.update(cursor=receipt['notice_cursor'],event=event,body_hash=fingerprint(evidence))
                else:
                    with self.database.transaction() as db:db.execute('UPDATE ss_square_connections SET '+clause+' WHERE tenant_id=%s',values+(self.tenant,))
            self.transport.hook=change
            with self.subTest(clause=clause),self.assertRaises(ServiceError):self.lookup()
            if clause=='pending_notice_cursor':
                # Outside assertRaises: a swallowed hook/SQL error cannot satisfy the test.
                self.assertTrue(committed)
                with self.database.transaction() as db:
                    pending=db.execute('''SELECT c.pending_notice_cursor,c.revoked_at,r.application_id,r.environment,
                        r.event_id,r.merchant_id,r.body_hash,r.outcome FROM ss_square_connections c
                        JOIN ss_square_revocation_receipts r ON r.notice_cursor=c.pending_notice_cursor
                        WHERE c.tenant_id=%s''',(self.tenant,)).fetchone()
                self.assertIsNotNone(pending);self.assertEqual(pending['pending_notice_cursor'],committed['cursor'])
                self.assertEqual(pending['event_id'],committed['event']);self.assertEqual(pending['body_hash'],committed['body_hash'])
                self.assertEqual(pending['application_id'],self.app.config['SQUARE_APPLICATION_ID'])
                self.assertEqual(pending['environment'],self.square.environment);self.assertEqual(pending['merchant_id'],self.transport.merchant)
                self.assertEqual(pending['outcome'],{'accepted':True,'revocation_pending':True})
                self.assertIsNone(pending['revoked_at'])  # Isolate pending-notice denial from the revoked-at case.
            with self.database.transaction() as db:
                db.execute('''UPDATE ss_square_connections SET revoked_at=NULL,authorization_generation=%s,merchant_id=%s,
                    pending_notice_cursor=NULL WHERE tenant_id=%s''',(connection['authorization_generation'],connection['merchant_id'],self.tenant))
        self.transport.hook=lambda:self.app.config.update(SQUARE_APPLICATION_ID='changed-app')
        with self.assertRaises(ServiceError):self.lookup()
        self.assert_safe(before)

    def test_real_http_exact_query_contract_mfa_and_tenant_isolation(self):
        before=self.protected();client=self.app.test_client();base='https://stock-fixture.invalid'
        self.assertEqual(client.get('/api/stock/lookup?sku=x&location_id=y',base_url=base).status_code,401)
        self.assertEqual(client.get('/stock',base_url=base).status_code,200)
        login=client.post('/api/auth/login',base_url=base,json={'store_slug':self.slug,'email':'owner@example.invalid',
            'password':self.password,'mfa_supported':True}).get_json()
        self.assertTrue(login['mfa_required'])
        verified=client.post('/api/auth/mfa/challenge',base_url=base,json={'challenge_token':login['challenge_token'],'code':self.recovery[0]})
        self.assertEqual(verified.status_code,200)
        value=client.get('/api/stock/lookup',base_url=base,query_string={'sku':self.transport.sku,'location_id':self.transport.location})
        self.assertEqual(value.status_code,200);self.assertEqual(value.get_json()['inventory']['quantity'],'3')
        self.assertEqual(value.headers['Cache-Control'],'no-store');self.assertEqual(value.headers['Referrer-Policy'],'no-referrer')
        self.transport.objects[self.transport.parent]['item_data']['name']='\U0001f0a1'*512
        self.transport.objects[self.transport.variation]['item_variation_data']['name']='\U0001f0a1'*512
        unicode_result=client.get('/api/stock/lookup',base_url=base,query_string={'sku':self.transport.sku,'location_id':self.transport.location})
        self.assertEqual(unicode_result.status_code,200);self.assertLessEqual(len(unicode_result.data),16384)
        self.assertIn('\U0001f0a1'.encode(),unicode_result.data)
        self.assertEqual(client.get('/api/stock/locations',base_url=base).status_code,200)
        for query in ([('sku',self.transport.sku),('sku',self.transport.sku),('location_id',self.transport.location)],
                      {'sku':self.transport.sku},{'sku':self.transport.sku,'location_id':self.transport.location,'tenant_id':str(self.tenant)}):
            self.assertEqual(client.get('/api/stock/lookup',base_url=base,query_string=query).status_code,400)
        self.assertEqual(client.get('/api/stock/locations?tenant_id=x',base_url=base).status_code,400)
        other=uuid.uuid4();slug='stock-other-'+other.hex[:20]
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(other,slug,'Fictional other store'))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'cashier@example.invalid',%s,'cashier')",
                       (uuid.uuid4(),other,password_hash(self.password)))
        stranger=self.app.test_client();self.assertEqual(stranger.post('/api/auth/login',base_url=base,
            json={'store_slug':slug,'email':'cashier@example.invalid','password':self.password}).status_code,200)
        denied=stranger.get('/api/stock/lookup',base_url=base,query_string={'sku':self.transport.sku,'location_id':self.transport.location})
        self.assertEqual(denied.status_code,409);self.assertNotIn(self.transport.variation,denied.get_data(as_text=True))
        self.assert_safe(before)

    def test_normal_oauth_renewal_preserves_read_grants_and_zero_business_writes(self):
        before=self.protected();old=self.transport.exchanges
        with self.database.transaction() as db:
            db.execute("UPDATE ss_square_connections SET token_obtained_at=clock_timestamp()-interval '7 days' WHERE tenant_id=%s",(self.tenant,))
            previous=db.execute('SELECT encrypted_tokens,expires_at FROM ss_square_connections WHERE tenant_id=%s',(self.tenant,)).fetchone()
        previous_tokens=json.loads(self.square.cipher.decrypt(previous['encrypted_tokens'].encode()))
        result=self.lookup('cashier');self.assertEqual(result['count_state'],'reported');self.assertEqual(self.transport.exchanges,old+1)
        with self.database.transaction() as db:
            row=db.execute('SELECT requested_profile,token_obtained_at,encrypted_tokens,expires_at,clock_timestamp() AS checked_at FROM ss_square_connections WHERE tenant_id=%s',(self.tenant,)).fetchone()
        renewed_tokens=json.loads(self.square.cipher.decrypt(row['encrypted_tokens'].encode()))
        self.assertEqual(renewed_tokens['refresh_token'],previous_tokens['refresh_token'])
        self.assertNotEqual(renewed_tokens['access_token'],previous_tokens['access_token'])
        self.assertGreater(row['expires_at'],previous['expires_at'])
        self.assertEqual(row['requested_profile'],'standard');self.assertLess((row['checked_at']-row['token_obtained_at']).total_seconds(),60)
        self.assertEqual(set(self.transport.scopes),set(READ_SCOPES));self.assert_safe(before)
