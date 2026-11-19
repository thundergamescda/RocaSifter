"""Receiving saga transactions in disposable ss_test_<uuid> schemas only.

Injected fictional provider, no HTTP, merchant data or production migration.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
import uuid
from unittest.mock import Mock, patch
import psycopg
from cryptography.fernet import Fernet
from intake_manifest import capture_manifest, source_line_id
from catalog_physical_ownership import ownership_from_card
from intake_physical_identity import physical_facts_v2
from hosted.app import create_app
from hosted.intake import IntakeClaims
from hosted.receiving import Receiving
from hosted.receiving_square import ReceivingSquare
from hosted.receiving_plan import ReceivingError, fingerprint
from hosted.security import ServiceError, digest
from hosted.trades import Trades
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import card_line, draft_data, acceptance_data
from test_hosted_receiving import plan_data
from test_intake_fixtures import fresh_mfa_actor
from test_receiving_fixtures import FictionalReceivingProvider


class PostgreSQLReceivingAcceptance(_PostgreSQLSettlementFixture):
    def _physical_mapping(self, *, location='fixture-location', identity=None):
        identity=deepcopy(identity or self.original['identity'])
        fields={'game':'game','game_label':'game_label','card_name':'name','card_set':'set','card_num':'number',
            'condition':'condition','printing':'printing','language':'language','item_type':'kind','grader':'grading_company',
            'grade':'grade','grade_qualifier':'grade_qualifier','cert_number':'certification','serial_number':'serial_number',
            'year':'year','sport':'sport','league':'league','manufacturer':'manufacturer','team':'team','player':'player'}
        facts=physical_facts_v2({field:identity.get(source,'') for field,source in fields.items()})
        data={'identity':identity,'physical_ownership_version':2,'physical_facts':facts,
              'variation_id':'fixture-variation','location_id':location}
        preview=self.service.binding_preview(self.actor,data)
        body={'request_key':'physical-mapping-'+uuid.uuid4().hex,'preview_hash':preview['preview_hash'],
              'identity_confirmed':True,'reason':'Fictional owner explicitly reviewed all current physical facts'}
        return data,preview,body

    def _retire_original_mapping(self):
        return self.service.revoke_binding(self.actor,self.binding['binding_id'],{
            'request_key':'retire-before-physical-'+uuid.uuid4().hex,'version':self.binding['version'],
            'reason':'Fictional owner explicitly retires the earlier identity mapping'})

    def _approve_physical_mapping(self):
        self._retire_original_mapping()
        data,preview,body=self._physical_mapping()
        result=self.service.approve_binding(self.actor,preview['preview_id'],body)
        return data,preview,body,result

    def _physical_plan(self,ownership):
        body=plan_data(self.claim);body['physical_ownership_version']=2
        for line in body['lines']:line['physical_ownership']=deepcopy(ownership)
        return body

    def setUp(self):
        self.tenant,self.customer=uuid.uuid4(),uuid.uuid4()
        self.actors={role:{'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':role} for role in ('owner','manager','buyer','cashier')}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional receiving store')",(self.tenant,'fixture-'+self.tenant.hex))
            for role,actor in self.actors.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',(actor['staff_id'],self.tenant,role+'@example.invalid','fictional-invalid-password',role))
                self.actors[role]=fresh_mfa_actor(db,actor)
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Fictional receiving customer','fixture-only')",(self.customer,self.tenant))
        self.actor=self.actors['owner'];self.provider=FictionalReceivingProvider(self.database,self.tenant)
        self.cipher=Fernet(Fernet.generate_key());self.service=Receiving(self.database,self.provider,self.cipher,writes_enabled=True)
        self.claims=IntakeClaims(self.database);self.trades=Trades(self.database)
        self.original=card_line();self.manifest=capture_manifest('manual',[{'identity':self.original['identity'],'quantity':2}])
        self.claim=self.claims.create(self.actor,{'request_key':'claim-base','physical_reference':'fictional-batch-base',
             'source_manifest':self.manifest,'physical_delivery_verified':True})
        self.approve()
        with self.database.transaction() as db:db.execute('INSERT INTO ss_receiving_settings(tenant_id,enabled) VALUES (%s,TRUE)',(self.tenant,))
        no_http=patch('requests.sessions.Session.request',side_effect=AssertionError('External HTTP forbidden in saga fixtures.'))
        no_http.start();self.addCleanup(no_http.stop)

    def approve(self):
        self.preview=self.service.binding_preview(self.actor,{'identity':self.original['identity'],'variation_id':'fixture-variation','location_id':'fixture-location'})
        self.binding=self.service.approve_binding(self.actor,self.preview['preview_id'],{'request_key':'mapping-'+uuid.uuid4().hex,
            'preview_hash':self.preview['preview_hash'],'identity_confirmed':True,'reason':'Fictional owner reviewed the exact card identity'})

    def plan(self, body=None):
        return self.service.create_plan(self.actor,body or plan_data(self.claim))

    def terms(self, plan, **extra):
        return {'request_key':'request-'+uuid.uuid4().hex,'revision':plan['revision'],'plan_hash':plan['plan_hash']}|extra

    def commit(self, plan, *, actor=None, body=None):
        return self.service.commit(actor or self.actor,plan['receiving_id'],body or self.terms(plan,confirmed=True,excluded_not_received_ack=True))

    def recover(self, plan, *, actor=None):return self.service.recover(actor or self.actor,plan['receiving_id'],self.terms(plan))

    def wallet(self):
        with self.database.transaction() as db:
            balance=db.execute('SELECT balance_cents FROM ss_customers WHERE tenant_id=%s AND id=%s',(self.tenant,self.customer)).fetchone()['balance_cents']
            entries=db.execute('SELECT count(*) AS n FROM ss_credit_entries WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']
        return balance,entries

    def trade(self):
        body=draft_data(customer_id=self.customer,request_key='trade-'+uuid.uuid4().hex)
        body['source_reference']='pc-receipt-'+uuid.uuid4().hex
        body.update(physical_intake_id=self.claim['physical_intake_id'],physical_manifest_hash=self.claim['manifest_hash'])
        body['lines'][0]['source_line_key']=plan_data(self.claim)['lines'][0]['source_line_key']
        return self.trades.create(self.actor,body)

    def accept_trade(self, trade):
        body=acceptance_data(self.customer,request_key='trade-accept-'+uuid.uuid4().hex,version=trade['version']);body['claim_reviewed']=True
        return self.trades.accept(self.actor,trade['trade_id'],body)

    def test_owner_mapping_approval_is_explicit_idempotent_and_preserves_catalog(self):
        before=deepcopy(self.provider.objects)
        body={'request_key':'approval-repeat','preview_hash':self.preview['preview_hash'],'identity_confirmed':True,'reason':'Fictional explicitly reviewed duplicate approval'}
        first=self.service.approve_binding(self.actor,self.preview['preview_id'],body)
        self.assertEqual(self.service.approve_binding(self.actor,self.preview['preview_id'],body),first)
        self.assertEqual(first['binding_id'],self.binding['binding_id']);self.assertEqual(self.provider.objects,before)
        self.assertEqual(self.provider.calls,[])
        self.assertNotIn('token',json.dumps(first))

    def _extra_fixture_locations(self):
        # Additional locations belong only to the injected fictional provider.
        # Real database transactions, sessions, MFA and approval gates remain.
        original=self.provider.location
        def location(access,ident):
            if ident in ('fixture-location-two','fixture-location-three'):
                return {'id':ident,'name':'Fictional extra till','status':'ACTIVE','currency':'USD'}
            return original(access,ident)
        return patch.object(self.provider,'location',side_effect=location)

    def _mapping_at(self,identity,location,variation='fixture-variation'):
        preview=self.service.binding_preview(self.actor,{'identity':identity,
            'variation_id':variation,'location_id':location})
        body={'request_key':'mapping-'+uuid.uuid4().hex,'preview_hash':preview['preview_hash'],
            'identity_confirmed':True,'reason':'Fictional owner reviewed this exact physical card'}
        return preview,body

    def _mapping_review_snapshot(self):
        with self.database.transaction() as db:
            return {table:db.execute('SELECT * FROM '+table+' WHERE tenant_id=%s ORDER BY '+order,
                (self.tenant,)).fetchall() for table,order in (
                    ('ss_receiving_bindings','id'),('ss_receiving_binding_previews','id'),
                    ('ss_receiving_requests','request_key'),('ss_receiving_operations','id'),('ss_audit','id'))}

    def test_binding_review_same_identity_is_read_only_and_preserves_original_approval(self):
        # Real isolated PostgreSQL; fictional provider and seeded MFA only.
        body={'request_key':'review-original-approval','preview_hash':self.preview['preview_hash'],
              'identity_confirmed':True,'reason':'Fictional owner retains the original reviewed mapping'}
        saved=self.service.approve_binding(self.actor,self.preview['preview_id'],body)
        before=self._mapping_review_snapshot();provider_before=deepcopy(self.provider.objects)
        with patch.object(self.provider,'access',side_effect=AssertionError('Read-only mapping review accessed the provider.')):
            result=self.service.binding_review(self.actor,self.preview['preview_id'])
            self.assertEqual(self.service.approve_binding(self.actor,self.preview['preview_id'],body),saved)
        self.assertEqual(result,{'review_version':1,'preview_id':self.preview['preview_id'],
            'preview_hash':self.preview['preview_hash'],'conflict_exists':False,'conflicts':[],'next_cursor':None})
        self.assertEqual(self._mapping_review_snapshot(),before)
        self.assertEqual(fingerprint(self.preview['facts']),self.preview['preview_hash'])
        fresh_body=body|{'request_key':'review-fresh-original-approval'}
        self.assertEqual(self.service.approve_binding(self.actor,self.preview['preview_id'],fresh_body)['binding_id'],self.binding['binding_id'])
        self.assertEqual(self.provider.objects,provider_before);self.assertEqual(self.provider.calls,[])

    def test_binding_review_pages_cross_location_conflicts_and_keeps_global_predicate(self):
        with self._extra_fixture_locations():
            for location in ('fixture-location-two','fixture-location-three'):
                preview,body=self._mapping_at(self.original['identity'],location)
                self.service.approve_binding(self.actor,preview['preview_id'],body)
            identity=deepcopy(self.original['identity']);identity['name']='Different reviewed physical card'
            preview,_=self._mapping_at(identity,'fixture-location-two')
        before=self._mapping_review_snapshot();rows=[];cursor=None
        with patch.object(self.provider,'access',side_effect=AssertionError('Mapping review fetched provider details.')):
            for page in range(3):
                result=self.service.binding_review(self.actor,preview['preview_id'],limit=1,cursor=cursor)
                self.assertTrue(result['conflict_exists']);self.assertEqual(len(result['conflicts']),1)
                rows.extend(result['conflicts']);cursor=result['next_cursor']
                self.assertEqual(cursor is None,page==2)
            empty=self.service.binding_review(self.actor,preview['preview_id'],limit=1,
                cursor='ffffffff-ffff-ffff-ffff-ffffffffffff')
        self.assertTrue(empty['conflict_exists']);self.assertEqual(empty['conflicts'],[]);self.assertIsNone(empty['next_cursor'])
        self.assertEqual({row['location_id'] for row in rows},{'fixture-location','fixture-location-two','fixture-location-three'})
        self.assertEqual(len({row['binding_id'] for row in rows}),3)
        self.assertEqual([row['binding_id'] for row in rows],sorted(row['binding_id'] for row in rows))
        for row in rows:
            self.assertEqual(set(row),{'binding_id','version','identity','identity_hash','retained_sku','location_id','selling_cents'})
            self.assertEqual(row['identity'],self.preview['facts']['identity']);self.assertEqual(row['retained_sku'],'000000001234')
            self.assertEqual(row['version'],1);self.assertEqual(row['selling_cents'],self.preview['facts']['selling_cents'])
        self.assertEqual(self._mapping_review_snapshot(),before)
        self.service.revoke_binding(self.actor,rows[0]['binding_id'],{'request_key':'review-explicit-fixture-revoke',
            'version':rows[0]['version'],'reason':'Fictional owner explicitly retires this fixture mapping'})
        remaining=self.service.binding_review(self.actor,preview['preview_id'])
        self.assertTrue(remaining['conflict_exists']);self.assertEqual(len(remaining['conflicts']),2)
        self.assertNotIn(rows[0]['binding_id'],{row['binding_id'] for row in remaining['conflicts']})

    def test_binding_review_matches_preview_provider_and_variation_scope(self):
        identity=deepcopy(self.original['identity']);identity['name']='Different scoped card'
        preview,_=self._mapping_at(identity,'fixture-location')
        environment=self.preview['facts']['provider']['environment']
        decoys=({'environment':'production' if environment=='sandbox' else 'sandbox'},
                {'merchant_id':'other-fictional-merchant'}, {})
        with self.database.transaction() as db:
            for index,provider in enumerate(decoys):
                db.execute('''INSERT INTO ss_receiving_bindings
                    (tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,
                     identity_anchor_hash,evidence_hash,approved_by,approval_reason)
                    SELECT tenant_id,%s,identity,identity_hash,provider||%s::jsonb,%s,parent_id,sku,%s,selling_cents,
                           identity_anchor_hash,evidence_hash,approved_by,approval_reason
                    FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s''',
                    (uuid.uuid4(),json.dumps(provider),'other-fictional-variation' if index==2 else 'fixture-variation',
                     'fixture-scope-location-'+str(index),self.tenant,self.binding['binding_id']))
        result=self.service.binding_review(self.actor,preview['preview_id'])
        self.assertTrue(result['conflict_exists']);self.assertEqual([row['binding_id'] for row in result['conflicts']],[self.binding['binding_id']])

    def test_binding_review_requires_current_owner_and_tenant_owned_unexpired_preview(self):
        for role in ('manager','buyer','cashier'):
            with self.subTest(role=role),self.assertRaises(ServiceError) as caught:
                self.service.binding_review(self.actors[role],self.preview['preview_id'])
            self.assertEqual(caught.exception.status,403)
        foreign={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner'}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Other fictional review store')",
                (foreign['tenant_id'],'review-'+foreign['tenant_id'].hex))
            db.execute('''INSERT INTO ss_staff(id,tenant_id,email,password_hash,role)
                VALUES (%s,%s,'foreign-owner@example.invalid','fictional-invalid-password','owner')''',
                (foreign['staff_id'],foreign['tenant_id']))
            foreign=fresh_mfa_actor(db,foreign)
            foreign_identity=deepcopy(self.preview['facts']['identity']);foreign_identity['name']='Other tenant physical identity'
            db.execute('''INSERT INTO ss_receiving_bindings
                (tenant_id,id,identity,identity_hash,provider,variation_id,parent_id,sku,location_id,selling_cents,
                 identity_anchor_hash,evidence_hash,approved_by,approval_reason)
                SELECT %s,%s,%s::jsonb,%s,provider,variation_id,parent_id,sku,location_id,selling_cents,
                       identity_anchor_hash,evidence_hash,%s,'Fictional other tenant reviewed its own mapping'
                FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s''',
                (foreign['tenant_id'],uuid.uuid4(),json.dumps(foreign_identity),source_line_id(foreign_identity),
                 foreign['staff_id'],self.tenant,self.binding['binding_id']))
        own=self.service.binding_review(self.actor,self.preview['preview_id'])
        self.assertFalse(own['conflict_exists']);self.assertEqual(own['conflicts'],[])
        with self.assertRaises(ReceivingError) as foreign_error:self.service.binding_review(foreign,self.preview['preview_id'])
        with self.assertRaises(ReceivingError) as missing_error:self.service.binding_review(self.actor,uuid.uuid4())
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_binding_previews SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",
                (self.tenant,self.preview['preview_id']))
        with self.assertRaises(ReceivingError) as expired_error:self.service.binding_review(self.actor,self.preview['preview_id'])
        self.assertEqual({error.exception.code for error in (foreign_error,missing_error,expired_error)},{'CATALOG_REVIEW_UNAVAILABLE'})

    def test_binding_review_rejects_changed_preview_and_inactive_owner_session(self):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_binding_previews SET preview_hash=repeat('0',64) WHERE tenant_id=%s AND id=%s",
                (self.tenant,self.preview['preview_id']))
        with self.assertRaises(ReceivingError) as caught:self.service.binding_review(self.actor,self.preview['preview_id'])
        self.assertEqual(caught.exception.code,'CATALOG_REVIEW_INVALID')
        with self.database.transaction() as db:
            db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,self.actor['staff_id']))
        with self.assertRaises(ServiceError) as caught:self.service.binding_review(self.actor,self.preview['preview_id'])
        self.assertEqual(caught.exception.status,403)
        with self.database.transaction() as db:
            db.execute('UPDATE ss_staff SET enabled=TRUE WHERE tenant_id=%s AND id=%s',(self.tenant,self.actor['staff_id']))
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND token_hash=%s",
                (self.tenant,self.actor['session_hash']))
        with self.assertRaises(ServiceError) as caught:self.service.binding_review(self.actor,self.preview['preview_id'])
        self.assertEqual(caught.exception.status,401)

    def test_binding_review_get_uses_database_owner_authority_without_provider_and_exposes_freshness(self):
        # This seeded session checks the DB-clock read flag, not real MFA login.
        token='fictional-mapping-review-owner-session';http_session_hash=digest(token)
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                SELECT %s,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at
                FROM ss_sessions WHERE tenant_id=%s AND token_hash=%s''',
                (http_session_hash,self.tenant,self.actor['session_hash']))
        base='https://review.fixture.invalid'
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':base,'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.database)
        app.extensions['ss_receiving'].provider=self.provider;client=app.test_client();headers={'Authorization':'Bearer '+token}
        path='/api/receiving/catalog-bindings/previews/'+self.preview['preview_id']+'/review'
        with patch.object(self.provider,'access',side_effect=AssertionError('Review GET accessed the provider.')):
            for offset,fresh in ((-5,True),(-901,False),(60,False)):
                with self.subTest(offset=offset):
                    with self.database.transaction() as db:
                        db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()+(%s*interval '1 second') WHERE tenant_id=%s AND token_hash=%s",
                            (offset,self.tenant,http_session_hash))
                    me=client.get('/api/me',base_url=base,headers=headers)
                    self.assertEqual(me.status_code,200);self.assertTrue(me.json['mfa_authenticated'])
                    self.assertIs(me.json['mfa_fresh'],fresh);self.assertNotIn('mfa_authenticated_at',me.json)
                    result=client.get(path,base_url=base,headers=headers)
                    self.assertEqual(result.status_code,200);self.assertFalse(result.json['conflict_exists'])
                    self.assertEqual(result.json['preview_hash'],self.preview['preview_hash'])
        self.assertEqual(client.get(path,base_url=base).status_code,401)

    def test_owner_mapping_rejects_other_identity_across_locations(self):
        # UNRUN: database acceptance case, not live Square acceptance.
        identity=deepcopy(self.original['identity']);identity['name']='Different physical card'
        before=deepcopy(self.provider.objects)
        with self._extra_fixture_locations():
            preview,body=self._mapping_at(identity,'fixture-location-two')
            with self.assertRaises(ReceivingError) as caught:
                self.service.approve_binding(self.actor,preview['preview_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_IDENTITY_CONFLICT')
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_bindings WHERE tenant_id=%s',
                (self.tenant,)).fetchone()['n'],1)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',
                (self.tenant,body['request_key'])).fetchone()['n'],0)
        self.assertEqual(self.provider.objects,before);self.assertEqual(self.provider.calls,[])

    def test_owner_mapping_allows_same_identity_at_another_location(self):
        # One physical variation can be stocked at multiple store locations.
        before=deepcopy(self.provider.objects)
        with self._extra_fixture_locations():
            preview,body=self._mapping_at(self.original['identity'],'fixture-location-two')
            result=self.service.approve_binding(self.actor,preview['preview_id'],body)
            self.assertEqual(self.service.approve_binding(self.actor,preview['preview_id'],body),result)
        self.assertTrue(result['approved']);self.assertNotEqual(result['binding_id'],self.binding['binding_id'])
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_bindings WHERE tenant_id=%s',
                (self.tenant,)).fetchone()['n'],2)
        self.assertEqual(self.provider.objects,before);self.assertEqual(self.provider.calls,[])

    def test_owner_mapping_returns_identity_conflict_before_unique_constraint(self):
        identity=deepcopy(self.original['identity']);identity['name']='Another physical card'
        preview,body=self._mapping_at(identity,'fixture-location')
        with self.assertRaises(ReceivingError) as caught:
            self.service.approve_binding(self.actor,preview['preview_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_IDENTITY_CONFLICT')
        self.assertEqual(self.provider.calls,[])

    def test_saved_owner_approval_replays_after_reviewed_identity_replacement(self):
        body={'request_key':'approval-before-replacement','preview_hash':self.preview['preview_hash'],
            'identity_confirmed':True,'reason':'Fictional owner reviewed the original mapping'}
        first=self.service.approve_binding(self.actor,self.preview['preview_id'],body)
        self.service.revoke_binding(self.actor,first['binding_id'],{
            'request_key':'reviewed-mapping-revocation','version':first['version'],
            'reason':'Fictional owner explicitly retires the original mapping'})
        identity=deepcopy(self.original['identity']);identity['name']='Reviewed replacement identity'
        with self._extra_fixture_locations():
            preview,replacement_body=self._mapping_at(identity,'fixture-location-two')
            replacement=self.service.approve_binding(self.actor,preview['preview_id'],replacement_body)
        self.assertNotEqual(replacement['binding_id'],first['binding_id'])
        query='SELECT id,version,identity_hash,revoked_at,location_id FROM ss_receiving_bindings WHERE tenant_id=%s ORDER BY id'
        with self.database.transaction() as db:
            before=db.execute(query,(self.tenant,)).fetchall()
        self.assertEqual(len(before),2)
        self.assertEqual(sum(row['revoked_at'] is None for row in before),1)
        # This is saved outcome retrieval, not a fresh mapping approval. It must
        # neither reclassify identity nor touch provider access or replacement.
        with patch.object(self.provider,'access',side_effect=AssertionError('Saved approval re-entered provider access.')):
            self.assertEqual(self.service.approve_binding(self.actor,self.preview['preview_id'],body),first)
        with self.database.transaction() as db:
            self.assertEqual(db.execute(query,(self.tenant,)).fetchall(),before)
        self.assertEqual(self.provider.calls,[])

    def test_concurrent_owner_mappings_keep_one_identity_across_locations(self):
        # Fresh unbound fixture variation. Existing anchor mapping is untouched.
        variation=deepcopy(self.provider.objects['fixture-variation'])
        variation['id']='fixture-unbound-variation'
        variation['item_variation_data']['sku']='987654321098'
        self.provider.objects[variation['id']]=variation
        self.provider.objects[variation['item_variation_data']['item_id']]['item_data']['variations'].append(deepcopy(variation))
        before=deepcopy(self.provider.objects)
        identities=[deepcopy(self.original['identity']) for _ in range(2)]
        identities[0]['name']='First physical identity';identities[1]['name']='Second physical identity'
        with self._extra_fixture_locations():
            requests=[self._mapping_at(identity,location,variation['id']) for identity,location in
                zip(identities,('fixture-location-two','fixture-location-three'))]
            barrier=threading.Barrier(2);original=self.provider.assert_skus
            def checked(access,targets):
                original(access,targets);barrier.wait(timeout=10)
            def approve(value):
                preview,body=value
                try:return self.service.approve_binding(self.actor,preview['preview_id'],body)
                except ReceivingError as exc:return {'error':exc.code}
            with patch.object(self.provider,'assert_skus',side_effect=checked):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(approve,value) for value in requests]
                    results=[future.result(timeout=20) for future in futures]
        self.assertEqual(sum(result.get('approved') is True for result in results),1)
        self.assertEqual(sum(result.get('error')=='CATALOG_IDENTITY_CONFLICT' for result in results),1)
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_bindings WHERE tenant_id=%s AND variation_id=%s',
                (self.tenant,variation['id'])).fetchone()['n'],1)
        self.assertEqual(self.provider.objects,before);self.assertEqual(self.provider.calls,[])

    def test_global_and_tenant_write_switches_default_off_before_provider_admission(self):
        plan=self.plan();self.service.writes_enabled=False;access_before=len(self.provider.access_calls)
        with self.assertRaises(ReceivingError) as caught:self.commit(plan)
        self.assertEqual(caught.exception.code,'RECEIVING_DISABLED')
        self.assertEqual(len(self.provider.access_calls),access_before)
        self.service.writes_enabled=True
        with self.database.transaction() as db:db.execute('UPDATE ss_receiving_settings SET enabled=FALSE WHERE tenant_id=%s',(self.tenant,))
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])
        with self.database.transaction() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_steps WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],0)

    def test_plan_create_lost_response_and_different_pc_id_recover_permanent_operation(self):
        body=plan_data(self.claim);first=self.plan(body)
        self.assertEqual(self.plan(body),first)
        copied=deepcopy(body);copied['request_key']='other-PC';copied['lines'][0]['review_line_id']=str(uuid.uuid4())
        with self.assertRaises(ReceivingError) as caught:self.plan(copied)
        self.assertEqual(caught.exception.receiving_id,first['receiving_id'])
        self.assertEqual(self.service.request_status(self.actor,body['request_key']),{'found':True,'result':first})
        self.assertEqual(self.service.get(self.actor,first['receiving_id']),first)
        self.assertEqual(self.provider.calls,[])

    def test_successful_inventory_retains_old_sku_price_catalog_and_exact_receipt(self):
        before=deepcopy(self.provider.objects);plan=self.plan();terms=self.terms(plan,confirmed=True,excluded_not_received_ack=True)
        result=self.commit(plan,body=terms)
        self.assertEqual((result['state'],result['inventory_confirmed']),('received',True))
        receipt=result['receipt'];hashed=receipt.pop('receipt_hash');self.assertEqual(fingerprint(receipt),hashed);receipt['receipt_hash']=hashed
        self.assertEqual(receipt['confirmed_targets'][0]['target_sku'],'000000001234')
        self.assertEqual(receipt['confirmed_targets'][0]['confirmed_quantity'],2)
        self.assertFalse(receipt['catalog_write_performed']);self.assertEqual(self.provider.objects,before)
        self.assertEqual(self.commit(plan,body=terms),result);self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.service.request_status(self.actor,terms['request_key'])['result'],result)
        self.assertEqual(self.wallet(),(0,0))

    def test_lost_response_holds_same_encrypted_key_and_recovers_by_history_without_reissue(self):
        self.provider.behavior='lost_response';plan=self.plan();unknown=self.commit(plan)
        self.assertEqual((unknown['state'],unknown['inventory_confirmed']),('outcome_unknown',False));self.assertEqual(len(self.provider.calls),1)
        with self.database.transaction() as db:
            step=db.execute('SELECT * FROM ss_receiving_steps WHERE tenant_id=%s AND operation_id=%s',(self.tenant,plan['receiving_id'])).fetchone()
            lane=db.execute('SELECT * FROM ss_receiving_seller_lanes WHERE tenant_id=%s',(self.tenant,)).fetchone()
        self.assertEqual(lane['state'],'unknown');self.assertNotIn('fictional-server-token',step['encrypted_body'])
        self.assertEqual(self.service._open(self.tenant,plan['receiving_id'],step),json.loads(self.provider.calls[0]))
        result=self.recover(plan)
        self.assertEqual(result['state'],'received');self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(result['receipt']['confirmed_targets'][0]['confirmed_quantity'],2)

    def test_admission_before_effect_blocks_new_trade_even_when_request_was_lost(self):
        trade=self.trade();self.provider.behavior='before_effect';plan=self.plan();unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown');self.assertEqual(self.provider.history_rows,[])
        with self.assertRaises(ServiceError):self.accept_trade(trade)
        self.assertEqual(self.wallet(),(0,0))
        self.assertEqual(self.claims.get(self.actor,self.claim['physical_intake_id'])['state'],'open')
        self.assertEqual(self.recover(plan)['state'],'outcome_unknown');self.assertEqual(len(self.provider.calls),1)
        with self.assertRaises(ReceivingError):self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Must not cancel uncertain provider admission'))

    def test_first_admission_financial_guard_serializes_during_inflight_http(self):
        trade=self.trade();plan=self.plan();entered=threading.Event();release=threading.Event()
        def hold():entered.set();self.assertTrue(release.wait(5))
        self.provider.after_dispatch=hold
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(self.commit,plan)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ServiceError):self.accept_trade(trade)
                self.assertEqual(self.wallet(),(0,0))
            finally:release.set()
            self.assertEqual(pending.result(5)['state'],'received')

    def test_trade_first_receiving_uses_server_accepted_identity_quantity_and_copied_line_ids(self):
        trade=self.trade();self.accept_trade(trade);receipt=self.trades.get(self.actor,trade['trade_id'])['accepted_receipt']
        body=plan_data(self.claim);body.pop('external_ownership_confirmed');body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['trade_id'],accepted_receipt_hash=fingerprint({}))
        body['accepted_receipt_hash']=self.trades.get(self.actor,trade['trade_id'])['receipt_hash']
        row=body['lines'][0];row.pop('identity');row['accepted_line_id']=receipt['lines'][0]['id'];row['review_line_id']=str(uuid.uuid4())
        result=self.commit(self.plan(body))
        self.assertEqual(result['state'],'received');self.assertEqual(result['receipt']['accepted_trade_id'],trade['trade_id'])
        self.assertEqual(self.wallet(),(1000,1))
        bad=deepcopy(body);bad['request_key']='forged-identity';bad['lines'][0]['identity']={'name':'forged'}
        with self.assertRaises(ServiceError):self.plan(bad)

    def test_current_mfa_role_session_and_mapping_revocation_gate_new_steps(self):
        plan=self.plan()
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])
        manager=self.actors['manager']
        with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,manager['staff_id']))
        with self.assertRaises(ServiceError):self.commit(plan,actor=manager)
        with self.database.transaction() as db:db.execute('UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp() WHERE token_hash=%s',(self.actor['session_hash'],))
        self.service.revoke_binding(self.actor,self.binding['binding_id'],{'request_key':'revoke-mapping','version':1,'reason':'Fictional owner revoked before admission'})
        with self.assertRaises(ReceivingError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])

    def test_unknown_lane_survives_lease_expiry_and_blocks_different_claim(self):
        self.provider.behavior='lost_response';first=self.plan();self.commit(first)
        second_claim=self.claims.create(self.actor,{'request_key':'separate-physical-delivery','physical_reference':'another-actual-stack','source_manifest':self.manifest,
            'physical_delivery_verified':True,'additional_delivery_of':self.claim['physical_intake_id'],'additional_delivery_confirmed':True,
            'additional_delivery_reason':'Fictional separate physical stack arrived later'})
        second=self.plan(plan_data(second_claim))
        with self.database.transaction() as db:db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 day' WHERE tenant_id=%s AND id=%s",(self.tenant,first['receiving_id']))
        with self.assertRaises(ReceivingError) as caught:self.commit(second)
        self.assertEqual(caught.exception.code,'SELLER_OPERATION_PENDING');self.assertEqual(len(self.provider.calls),1)
        self.assertEqual(self.recover(first)['state'],'received')
        self.provider.behavior='success';self.assertEqual(self.commit(second)['state'],'received')

    def test_postwrite_catalog_change_preserves_proven_stock_but_blocks_final_labels(self):
        plan=self.plan()
        def changed():self.provider.objects['fixture-variation']['item_variation_data']['price_money']['amount']=999
        self.provider.after_dispatch=changed
        result=self.commit(plan)
        self.assertEqual((result['state'],result['inventory_confirmed'],result['receipt']),('needs_review',True,None))
        self.provider.objects['fixture-variation']['item_variation_data']['price_money']['amount']=1000
        self.assertEqual(self.recover(plan)['state'],'received');self.assertEqual(len(self.provider.calls),1)

    def test_prewrite_cancel_revision_preserves_same_operation_and_old_plan_history(self):
        body=plan_data(self.claim);plan=self.plan(body)
        self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Fictional receipt review before any request'))
        revised_body=deepcopy(body);revised_body.update(request_key='revise-before-admission',revision=0);revised_body['lines'][0]['quantity']=1
        revised=self.service.revise(self.actor,plan['receiving_id'],revised_body)
        self.assertEqual((revised['receiving_id'],revised['revision'],revised['excluded_quantities'][body['lines'][0]['source_line_key']]),(plan['receiving_id'],1,1))
        self.assertEqual(self.commit(revised)['state'],'received')
        with self.database.transaction() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_plan_revisions WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],2)
        with self.assertRaises(ReceivingError):self.service.revise(self.actor,plan['receiving_id'],revised_body|{'request_key':'bad-late-edit','revision':1})

    def test_concurrent_commits_never_repeat_provider_write(self):
        plan=self.plan()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:self.commit(plan),range(2)))
        self.assertEqual(len(self.provider.calls),1)
        self.assertTrue(any(result['state']=='received' for result in results))
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['state'],'received')

    def test_final_audit_failure_recovers_exact_provider_history_without_second_call(self):
        plan=self.plan()
        from hosted.database import audit
        def failing(db,tenant,staff,action,*args,**kwargs):
            if action=='receiving-completed':raise RuntimeError('Fictional outcome database failure')
            return audit(db,tenant,staff,action,*args,**kwargs)
        with patch('hosted.receiving.audit',side_effect=failing):
            with self.assertRaises(RuntimeError):self.commit(plan)
        self.assertEqual(len(self.provider.calls),1)
        with self.database.transaction() as db:db.execute("UPDATE ss_receiving_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE tenant_id=%s AND id=%s",(self.tenant,plan['receiving_id']))
        self.assertEqual(self.recover(plan)['state'],'received');self.assertEqual(len(self.provider.calls),1)

    def test_tenant_cashier_buyer_request_key_and_immutable_journal_boundaries(self):
        plan=self.plan()
        for role in ('buyer','cashier'):
            with self.subTest(role=role),self.assertRaises(ServiceError):self.service.get(self.actors[role],plan['receiving_id'])
            with self.assertRaises(ServiceError):self.service.request_status(self.actors[role],'anything')
        with self.assertRaises(ServiceError):self.service.get(self.actor|{'tenant_id':uuid.uuid4()},plan['receiving_id'])
        self.commit(plan)
        for statement in ("DELETE FROM ss_receiving_steps WHERE tenant_id=%s", "UPDATE ss_receiving_steps SET api_version='2024-09-19' WHERE tenant_id=%s", "UPDATE ss_receiving_operations SET plan_hash=repeat('0',64) WHERE tenant_id=%s"):
            with self.subTest(statement=statement),self.assertRaises(psycopg.errors.RaiseException):
                with self.database.transaction() as db:db.execute(statement,(self.tenant,))
        self.database.migrate();self.assertTrue(self.database.ready());self.assertEqual(len(self.provider.calls),1)

    def test_physical_ownership_preview_rejects_partial_unknown_or_inconsistent_facts(self):
        data,_,_=self._physical_mapping();before=len(self.provider.access_calls)
        invalid=[data|{'physical_ownership_version':value} for value in (None,True,1,2.0,3,'2')]
        invalid.extend((data|{'physical_facts':None},data|{'physical_facts':{'card_name':'Incomplete'}},
                        data|{'physical_facts':data['physical_facts']|{'card_name':'Other card'}},
                        data|{'physical_facts':data['physical_facts']|{'unknown':'Extra'}}))
        for body in invalid:
            with self.subTest(body=body),self.assertRaises(ReceivingError) as caught:
                self.service.binding_preview(self.actor,body)
            self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED')
        self.assertEqual(len(self.provider.access_calls),before);self.assertEqual(self.provider.calls,[])

    def _lookup_fixture(self,sku=' 000Ab^>~7 '):
        self.provider.objects['fixture-variation']['item_variation_data']['sku']=sku
        self.provider.objects['fixture-parent']['item_data']['variations'][0]['item_variation_data']['sku']=sku
        self.lookup_adapter=ReceivingSquare(None)
        self.lookup_adapter._call=Mock(return_value={'objects':[deepcopy(self.provider.objects['fixture-variation'])]})
        self.provider.lookup_sku=lambda access,value:self.lookup_adapter.lookup_sku(access,value)
        return sku

    def _lookup_records(self):
        with self.database.transaction() as db:
            return {name:db.execute('SELECT to_jsonb(t) AS value FROM '+name+' t WHERE tenant_id=%s ORDER BY to_jsonb(t)::text',
                (self.tenant,)).fetchall() for name in ('ss_receiving_bindings','ss_receiving_binding_previews',
                    'ss_receiving_requests','ss_receiving_operations','ss_receiving_steps','ss_intake_claims','ss_audit','ss_credit_entries')}

    def _lookup_http(self):
        token='fictional-lookup-owner-'+uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                SELECT %s,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at
                FROM ss_sessions WHERE tenant_id=%s AND token_hash=%s''',(digest(token),self.tenant,self.actor['session_hash']))
        base='https://sku-lookup.fixture.invalid'
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':base,'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.database)
        app.extensions['ss_receiving'].provider=self.provider
        return app.test_client(),base,{'Authorization':'Bearer '+token},digest(token)

    def test_sku_lookup_exact_raw_candidate_is_bounded_and_never_changes_catalog_stock_or_registry(self):
        sku=self._lookup_fixture()
        self.provider.objects['fixture-variation']['private-fixture-field']='FICTIONAL_RAW_BODY_NEVER_RETURN'
        self.lookup_adapter._call.return_value={'objects':[deepcopy(self.provider.objects['fixture-variation'])]}
        before=self._lookup_records();catalog=deepcopy(self.provider.objects)
        result=self.service.catalog_lookup(self.actor,sku,'fixture-location')
        self.assertEqual(set(result),{'lookup_version','requested_sku','location_id','state','lookup_complete','exact_match_count','case_variant_count','candidates'})
        self.assertEqual((result['state'],result['lookup_complete'],result['requested_sku']),('match',True,sku))
        self.assertEqual(result['candidates'],[{'variation_id':'fixture-variation','parent_id':'fixture-parent','sku':sku,
            'item_name':'Fictional parent','variation_name':'Retained old stock','selling_cents':1000,'currency':'USD'}])
        self.assertNotIn('FICTIONAL_RAW_BODY_NEVER_RETURN',json.dumps(result));self.assertNotIn('fictional-server-token',json.dumps(result))
        self.assertLess(len(json.dumps(result).encode()),4096)
        self.assertEqual(self._lookup_records(),before);self.assertEqual(self.provider.objects,catalog)
        self.assertEqual((self.provider.calls,self.provider.catalog_calls,self.wallet()),([],[],(0,0)))

    def test_sku_lookup_raw_case_spaces_and_leading_zeroes_never_become_normalized_matches(self):
        sku=self._lookup_fixture('Ab007')
        result=self.service.catalog_lookup(self.actor,'ab007','fixture-location')
        self.assertEqual((result['state'],result['exact_match_count'],result['case_variant_count'],result['candidates']),('no_match',0,1,[]))
        for query in (' Ab007','Ab007 ','007','000Ab007'):
            with self.subTest(query=query):
                self.lookup_adapter._call.return_value={'objects':[]}
                result=self.service.catalog_lookup(self.actor,query,'fixture-location')
                self.assertEqual((result['state'],result['lookup_complete'],result['requested_sku']),('no_match',True,query))
                self.assertEqual(self.lookup_adapter._call.call_args.args[2]['query']['set_query']['attribute_values'],[query])
        self.assertEqual(self.provider.calls,[])

    def test_sku_lookup_exact_duplicates_and_case_collisions_offer_no_selectable_candidate(self):
        sku=self._lookup_fixture();row=deepcopy(self.provider.objects['fixture-variation'])
        for other_sku in (sku,sku.swapcase()):
            with self.subTest(other_sku=other_sku):
                other=deepcopy(row);other['id']='other-variation';other['item_variation_data']['sku']=other_sku
                self.lookup_adapter._call.return_value={'objects':[row,other]}
                result=self.service.catalog_lookup(self.actor,sku,'fixture-location')
                self.assertEqual((result['state'],result['lookup_complete'],result['candidates']),('ambiguous',True,[]))
                self.assertEqual(result['exact_match_count']+result['case_variant_count'],2)
        self.assertEqual((self.provider.calls,self.provider.catalog_calls),([],[]))

    def test_sku_lookup_incomplete_later_page_and_advisory_limit_never_publish_partial_match(self):
        sku=self._lookup_fixture();row=deepcopy(self.provider.objects['fixture-variation']);before=self._lookup_records()
        for pages in ([{'objects':[row],'cursor':'last'},{'objects':[],'cursor':False}],
                      [{'objects':[row],'cursor':'last'},ReceivingError('Fictional provider failure',502)],
                      [{'objects':[row]*1001}]):
            with self.subTest(pages=len(pages)):
                self.lookup_adapter._call=Mock(side_effect=pages)
                result=self.service.catalog_lookup(self.actor,sku,'fixture-location')
                self.assertEqual(result,{'lookup_version':1,'requested_sku':sku,'location_id':'fixture-location',
                    'state':'incomplete','lookup_complete':False,'exact_match_count':0,'case_variant_count':0,'candidates':[]})
        self.assertEqual(self._lookup_records(),before);self.assertEqual(self.provider.calls,[])

    def test_sku_lookup_current_catalog_settings_and_changed_search_version_prevent_selection(self):
        sku=self._lookup_fixture();original=deepcopy(self.provider.objects)
        changes=(('fixture-parent','item_data','is_archived',True),('fixture-variation','item_variation_data','track_inventory',False))
        for ident,field,key,value in changes:
            with self.subTest(key=key):
                self.provider.objects=deepcopy(original);self.provider.objects[ident][field][key]=value
                self.lookup_adapter._call.return_value={'objects':[deepcopy(self.provider.objects['fixture-variation'])]}
                result=self.service.catalog_lookup(self.actor,sku,'fixture-location')
                self.assertEqual((result['state'],result['lookup_complete'],result['candidates']),('unavailable',True,[]))
        self.provider.objects=deepcopy(original)
        self.lookup_adapter._call.return_value={'objects':[deepcopy(original['fixture-variation'])]}
        self.provider.objects['fixture-variation']['version']+=1
        result=self.service.catalog_lookup(self.actor,sku,'fixture-location')
        self.assertEqual((result['state'],result['lookup_complete'],result['candidates']),('incomplete',False,[]))

    def test_sku_lookup_revalidates_role_session_merchant_and_location_after_blocking_reads(self):
        sku=self._lookup_fixture();row=deepcopy(self.provider.objects['fixture-variation'])
        with self.database.transaction() as db:
            expiry=db.execute('SELECT expires_at FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],)).fetchone()['expires_at']
        location=self.provider.location;merchant=self.provider.proof['merchant_id']
        for change in ('role','session','merchant','provider_revoked','location'):
            with self.subTest(change=change):
                def read(*args,**kwargs):
                    if change in ('role','session'):
                        with self.database.transaction() as db:
                            if change=='role':db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
                            else:db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE token_hash=%s",(self.actor['session_hash'],))
                    elif change=='merchant':self.provider.proof['merchant_id']='different-merchant'
                    elif change=='provider_revoked':self.provider.revoked=True
                    else:self.provider.location=lambda *args:(_ for _ in ()).throw(ReceivingError('Fictional location no longer active'))
                    return {'objects':[row]}
                self.lookup_adapter._call=Mock(side_effect=read)
                try:
                    if change=='location':
                        result=self.service.catalog_lookup(self.actor,sku,'fixture-location')
                        self.assertEqual((result['state'],result['lookup_complete'],result['candidates']),('incomplete',False,[]))
                    else:
                        with self.assertRaises(ServiceError):self.service.catalog_lookup(self.actor,sku,'fixture-location')
                finally:
                    with self.database.transaction() as db:
                        db.execute("UPDATE ss_staff SET role='owner' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
                        db.execute('UPDATE ss_sessions SET expires_at=%s WHERE token_hash=%s',(expiry,self.actor['session_hash']))
                    self.provider.location=location;self.provider.proof['merchant_id']=merchant;self.provider.revoked=False
        access=self.provider.access
        def changed_before_search(*args,**kwargs):
            value=access(*args,**kwargs)
            with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
            return value
        self.lookup_adapter._call.reset_mock()
        with patch.object(self.provider,'access',side_effect=changed_before_search):
            with self.assertRaises(ServiceError):self.service.catalog_lookup(self.actor,sku,'fixture-location')
        self.lookup_adapter._call.assert_not_called()
        with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='owner' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
        self.assertEqual((self.provider.calls,self.provider.catalog_calls),([],[]))

    def test_sku_lookup_http_actual_auth_exact_query_scope_and_no_store_override(self):
        sku=self._lookup_fixture();client,base,headers,session=self._lookup_http();before=self._lookup_records()
        path='/api/receiving/catalog-lookup';query={'sku':sku,'location_id':'fixture-location'}
        response=client.get(path,query_string=query,base_url=base,headers=headers)
        self.assertEqual(response.status_code,200);self.assertEqual(response.json['requested_sku'],sku)
        self.assertEqual(response.json['state'],'match');self.assertEqual(response.headers['Cache-Control'],'no-store')
        calls=self.lookup_adapter._call.call_count
        for bad in ([('sku',sku),('sku',sku),('location_id','fixture-location')],
                    [('sku',sku),('location_id','fixture-location'),('tenant_id',str(uuid.uuid4()))],
                    [('sku',sku)], [('sku',sku),('location_id','fixture-location'),('cursor','forged')],
                    [('sku',''),('location_id','fixture-location')], [('sku',' '),('location_id','fixture-location')],
                    [('sku','x'*101),('location_id','fixture-location')], [('sku','A\nB'),('location_id','fixture-location')],
                    [('sku',sku),('location_id','.')]):
            with self.subTest(query=bad):self.assertEqual(client.get(path,query_string=bad,base_url=base,headers=headers).status_code,400)
        self.assertEqual(client.get(path,query_string=query,base_url=base).status_code,401)
        self.assertEqual(self.lookup_adapter._call.call_count,calls)
        self.assertEqual(self._lookup_records(),before)

    def test_sku_lookup_http_denies_nonowner_foreign_location_and_session_revoked_during_read(self):
        sku=self._lookup_fixture();client,base,headers,session=self._lookup_http();path='/api/receiving/catalog-lookup'
        self.assertEqual(client.get(path,query_string={'sku':sku,'location_id':'foreign-location'},base_url=base,headers=headers).status_code,409)
        self.lookup_adapter._call.assert_not_called()
        with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
        self.assertEqual(client.get(path,query_string={'sku':sku,'location_id':'fixture-location'},base_url=base,headers=headers).status_code,403)
        with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='owner' WHERE tenant_id=%s AND id=%s",(self.tenant,self.actor['staff_id']))
        def revoke(*args,**kwargs):
            with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(session,))
            return {'objects':[deepcopy(self.provider.objects['fixture-variation'])]}
        self.lookup_adapter._call=Mock(side_effect=revoke)
        response=client.get(path,query_string={'sku':sku,'location_id':'fixture-location'},base_url=base,headers=headers)
        self.assertEqual(response.status_code,401);self.assertNotIn('candidates',response.json)
        self.assertEqual((self.provider.calls,self.provider.catalog_calls),([],[]))

    def test_physical_ownership_legacy_mapping_is_not_silently_upgraded(self):
        _,preview,body=self._physical_mapping()
        review=self.service.binding_review(self.actor,preview['preview_id'])
        self.assertTrue(review['conflict_exists']);self.assertEqual(review['conflicts'][0]['binding_id'],self.binding['binding_id'])
        self.assertEqual(review['conflicts'][0]['identity_hash'],preview['facts']['identity_hash'])
        with self.assertRaises(ReceivingError) as caught:self.service.approve_binding(self.actor,preview['preview_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_IDENTITY_CONFLICT')
        with self.database.transaction() as db:
            rows=db.execute('SELECT physical_ownership,revoked_at FROM ss_receiving_bindings WHERE tenant_id=%s',(self.tenant,)).fetchall()
        self.assertEqual(len(rows),1);self.assertIsNone(rows[0]['physical_ownership']);self.assertIsNone(rows[0]['revoked_at'])
        self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_explicit_replacement_keeps_original_sku_and_history(self):
        original_catalog=deepcopy(self.provider.objects)
        data,preview,body,result=self._approve_physical_mapping()
        self.assertEqual(preview['facts']['physical_ownership'],ownership_from_card(data['physical_facts']))
        self.assertEqual(self.service.approve_binding(self.actor,preview['preview_id'],body),result)
        self.assertFalse(self.service.binding_review(self.actor,preview['preview_id'])['conflict_exists'])
        with self.database.transaction() as db:
            old=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,self.binding['binding_id'])).fetchone()
            new=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,result['binding_id'])).fetchone()
        self.assertIsNotNone(old['revoked_at']);self.assertIsNone(old['physical_ownership'])
        self.assertEqual(new['physical_ownership'],preview['facts']['physical_ownership'])
        self.assertEqual(new['sku'],old['sku']);self.assertEqual(new['identity_hash'],old['identity_hash'])
        self.assertEqual(self.provider.objects,original_catalog);self.assertEqual(self.provider.calls,[])
        self.assertEqual(self.service.readiness(self.actor)['physical_ownership_versions'],[1,2]);self.assertTrue(self.database.ready())

    def test_physical_ownership_saved_legacy_approval_still_replays_after_replacement(self):
        body={'request_key':'legacy-approval-to-replay','preview_hash':self.preview['preview_hash'],
              'identity_confirmed':True,'reason':'Fictional saved original owner approval for compatibility'}
        original=self.service.approve_binding(self.actor,self.preview['preview_id'],body)
        self._approve_physical_mapping()
        with patch.object(self.provider,'access',side_effect=AssertionError('Saved approval re-read the provider.')):
            self.assertEqual(self.service.approve_binding(self.actor,self.preview['preview_id'],body),original)
        self.assertEqual(original['binding_id'],self.binding['binding_id']);self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_receiving_round_trip_keeps_source_budget_and_exact_sku(self):
        original_manifest=deepcopy(self.manifest);catalog=deepcopy(self.provider.objects)
        _,preview,_,binding=self._approve_physical_mapping();body=self._physical_plan(preview['facts']['physical_ownership'])
        plan=self.plan(body);self.assertEqual(plan['physical_ownership_version'],2)
        target=plan['targets'][0]
        self.assertEqual(target['physical_ownership'],body['lines'][0]['physical_ownership'])
        self.assertEqual(target['source_line_key'],body['lines'][0]['source_line_key'])
        self.assertEqual(target['identity_hash'],source_line_id(self.original['identity']))
        self.assertEqual(target['target_sku'],'000000001234');self.assertEqual(target['binding_id'],binding['binding_id'])
        result=self.commit(plan)
        self.assertEqual(result['state'],'received');self.assertEqual(result['receipt']['physical_ownership_version'],2)
        self.assertEqual(result['receipt']['confirmed_targets'][0]['physical_ownership'],target['physical_ownership'])
        self.assertEqual(result['receipt']['selected_quantities'],{body['lines'][0]['source_line_key']:2})
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(self.provider.objects,catalog);self.assertEqual(self.manifest,original_manifest)

    def test_physical_ownership_new_plan_requires_explicit_versioned_binding(self):
        data,_,_=self._physical_mapping();body=self._physical_plan(ownership_from_card(data['physical_facts']))
        with self.assertRaises(ReceivingError) as caught:self.plan(body)
        self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED');self.assertEqual(self.provider.calls,[])
        self._approve_physical_mapping()
        legacy=plan_data(self.claim);legacy['request_key']='new-unversioned-plan'
        with self.assertRaises(ReceivingError) as caught:self.plan(legacy)
        self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED');self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_completed_legacy_receiving_remains_exact_after_replacement(self):
        plan=self.plan();body=self.terms(plan,confirmed=True,excluded_not_received_ack=True)
        result=self.commit(plan,body=body);receipt=deepcopy(result['receipt'])
        self._approve_physical_mapping()
        with patch.object(self.provider,'access',side_effect=AssertionError('Completed replay accessed the provider.')):
            self.assertEqual(self.service.commit(self.actor,plan['receiving_id'],body),result)
            self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['receipt'],receipt)
        self.assertNotIn('physical_ownership_version',receipt);self.assertEqual(len(self.provider.calls),1)

    def test_physical_ownership_cross_location_review_compares_complete_record(self):
        _,preview,_,_=self._approve_physical_mapping()
        with self._extra_fixture_locations():
            _,second,body=self._physical_mapping(location='fixture-location-two')
            self.assertFalse(self.service.binding_review(self.actor,second['preview_id'])['conflict_exists'])
            self.service.approve_binding(self.actor,second['preview_id'],body)
            identity=deepcopy(self.original['identity']);identity['name']='Different physical card'
            _,other,body=self._physical_mapping(identity=identity)
            review=self.service.binding_review(self.actor,other['preview_id'],limit=1)
            self.assertTrue(review['conflict_exists']);self.assertIsNotNone(review['next_cursor'])
            self.assertEqual(review['conflicts'][0]['physical_ownership'],preview['facts']['physical_ownership'])
            with self.assertRaises(ReceivingError) as caught:self.service.approve_binding(self.actor,other['preview_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_IDENTITY_CONFLICT');self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_binding_is_immutable_and_changed_preview_is_rejected(self):
        _,preview,_,binding=self._approve_physical_mapping()
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.database.transaction() as db:
                db.execute('UPDATE ss_receiving_bindings SET physical_ownership=NULL WHERE tenant_id=%s AND id=%s',(self.tenant,binding['binding_id']))
        _,fresh,body=self._physical_mapping()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_binding_previews SET facts=jsonb_set(facts,'{snapshot_hash}',to_jsonb(repeat('0',64))) WHERE tenant_id=%s AND id=%s",(self.tenant,fresh['preview_id']))
        with self.assertRaises(ReceivingError) as caught:self.service.approve_binding(self.actor,fresh['preview_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_REVIEW_INVALID');self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_price_template_cannot_drop_to_legacy_semantics(self):
        from hosted.receiving_templates import PriceTemplates
        _,_,_,binding=self._approve_physical_mapping()
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,binding['binding_id'])).fetchone()
        templates=PriceTemplates(self.database,self.provider,self.service)
        facts,_=templates._facts(self.actor,row,'fixture-prefix')
        self.assertEqual(facts['physical_ownership'],row['physical_ownership'])
        self.assertEqual(facts['binding_evidence_hash'],row['evidence_hash'])
        self.assertEqual(facts['binding_version'],row['version'])
        self.assertFalse(facts['receiving_enabled']);self.assertFalse(facts['new_parent_creation'])
        changed=deepcopy(row);changed['physical_ownership']['facts']['printing']='unreviewed printing'
        with patch.object(self.provider,'access',side_effect=AssertionError('Malformed ownership accessed provider.')):
            with self.assertRaises(ReceivingError) as caught:templates._facts(self.actor,changed,'fixture-prefix')
        self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED');self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_removing_preview_record_cannot_downgrade_approval(self):
        _,preview,body=self._physical_mapping()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_receiving_binding_previews SET facts=facts-'physical_ownership' WHERE tenant_id=%s AND id=%s",(self.tenant,preview['preview_id']))
        with patch.object(self.provider,'access',side_effect=AssertionError('Inconsistent preview reached provider.')):
            with self.assertRaises(ReceivingError) as caught:self.service.approve_binding(self.actor,preview['preview_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_REVIEW_INVALID');self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_revision_cannot_downgrade_original_operation(self):
        _,preview,_,_=self._approve_physical_mapping();plan=self.plan(self._physical_plan(preview['facts']['physical_ownership']))
        body=plan_data(self.claim)|{'request_key':'downgrade-original-plan','revision':plan['revision']}
        with patch.object(self.provider,'access',side_effect=AssertionError('Protocol downgrade reached provider.')):
            with self.assertRaises(ReceivingError) as caught:self.service.revise(self.actor,plan['receiving_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED')
        self.assertEqual(self.service.get(self.actor,plan['receiving_id'])['plan_hash'],plan['plan_hash']);self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_revision_cannot_implicitly_upgrade_legacy_operation(self):
        plan=self.plan();_,preview,_,_=self._approve_physical_mapping()
        body=self._physical_plan(preview['facts']['physical_ownership'])|{'request_key':'upgrade-original-plan','revision':plan['revision']}
        with patch.object(self.provider,'access',side_effect=AssertionError('Implicit protocol upgrade reached provider.')):
            with self.assertRaises(ReceivingError) as caught:self.service.revise(self.actor,plan['receiving_id'],body)
        self.assertEqual(caught.exception.code,'CATALOG_PHYSICAL_OWNERSHIP_REVIEW_REQUIRED');self.assertEqual(self.provider.calls,[])

    def test_physical_ownership_same_protocol_revision_replays_saved_result(self):
        _,preview,_,_=self._approve_physical_mapping();body=self._physical_plan(preview['facts']['physical_ownership'])
        plan=self.plan(body);revised=deepcopy(body)|{'request_key':'physical-same-protocol-revision','revision':plan['revision']}
        result=self.service.revise(self.actor,plan['receiving_id'],revised)
        self.assertEqual(result['physical_ownership_version'],2);self.assertEqual(result['revision'],plan['revision']+1)
        with patch.object(self.provider,'access',side_effect=AssertionError('Saved revision replay reached provider.')):
            self.assertEqual(self.service.revise(self.actor,plan['receiving_id'],revised),result)
        self.assertEqual(self.provider.calls,[])

    def test_catalog_games_choices_are_owner_only_and_do_not_call_provider(self):
        before=len(self.provider.access_calls)
        with patch.object(self.provider,'access',side_effect=AssertionError('Game choices accessed Square.')):
            result=self.service.catalog_games(self.actor)
            self.assertEqual(result['catalog_games_version'],1);self.assertTrue(result['supports_additional_games'])
            self.assertEqual({row['id'] for row in result['games']},{'pokemon','magic','riftbound','onepiece','unionarena','gundam','sports'})
            self.assertEqual(next(row['label'] for row in result['games'] if row['id']=='onepiece'),'One Piece')
            for role in ('manager','buyer','cashier'):
                for actor in (self.actors[role],self.actors[role]|{'role':'owner'}):
                    with self.subTest(role=role),self.assertRaises(ServiceError) as caught:self.service.catalog_games(actor)
                    self.assertEqual(caught.exception.status,403)
        self.assertEqual(len(self.provider.access_calls),before);self.assertEqual(self.provider.calls,[])

    def test_catalog_games_resolve_intake_aliases_and_preserve_custom_labels(self):
        aliases={'Pokémon TCG':'pokemon','MTG':'magic','Riftbound TCG':'riftbound','One Piece':'onepiece',
                 'Union Arena':'unionarena','Gundam Card Game':'gundam','Sports Cards':'sports'}
        for name,key in aliases.items():
            with self.subTest(name=name):self.assertEqual(self.service.catalog_games(self.actor,name)['resolved']['game'],key)
        first=self.service.catalog_games(self.actor,'A&B')['resolved'];second=self.service.catalog_games(self.actor,'A B')['resolved']
        self.assertEqual(first['game'],'other:a-b');self.assertEqual(first['game'],second['game'])
        self.assertEqual(first['game_label'],'A&B');self.assertEqual(second['game_label'],'A B')
        self.assertNotEqual(ownership_from_card(first|{'card_name':'Same physical description'}),ownership_from_card(second|{'card_name':'Same physical description'}))
        decomposed=self.service.catalog_games(self.actor,'Cafe\u0301 Game')['resolved']
        self.assertEqual(decomposed['game_label'],'Café Game');self.assertEqual(self.provider.calls,[])

    def test_catalog_games_invalid_names_stop_without_creating_mapping_or_request(self):
        for name in ('','  ','bad\x00name','bad\nname','\ud800','x'*101,'x'*501,1,False,[],{}):
            with self.subTest(name_type=type(name).__name__),self.assertRaises(ServiceError) as caught:
                self.service.catalog_games(self.actor,name)
            self.assertEqual(caught.exception.status,400)
        with self.assertRaises(ServiceError):self.service.catalog_games(self.actor,'x'*100)
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_bindings WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],1)
            self.assertEqual(db.execute("SELECT count(*) AS n FROM ss_receiving_requests WHERE tenant_id=%s AND action='binding_revoke'",(self.tenant,)).fetchone()['n'],0)
        self.assertEqual(self.provider.calls,[])

    def test_owner_mapping_retirement_requires_fresh_mfa_without_provider_mutation(self):
        body={'request_key':'fresh-proof-retirement','version':self.binding['version'],'reason':'Fictional owner reviewed this mapping retirement'}
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE tenant_id=%s AND token_hash=%s",(self.tenant,self.actor['session_hash']))
        with self.assertRaises(ServiceError) as caught:self.service.revoke_binding(self.actor,self.binding['binding_id'],body)
        self.assertEqual(caught.exception.status,403)
        with self.database.transaction() as db:
            row=db.execute('SELECT revoked_at FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,self.binding['binding_id'])).fetchone()
            self.assertIsNone(row['revoked_at'])
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',(self.tenant,body['request_key'])).fetchone()['n'],0)
        self.assertEqual(self.provider.calls,[])

    def test_owner_mapping_retirement_retry_replays_same_result_after_proof_expires(self):
        body={'request_key':'same-original-retirement','version':self.binding['version'],'reason':'Fictional owner explicitly retires this old mapping'}
        before=len(self.provider.access_calls);result=self.service.revoke_binding(self.actor,self.binding['binding_id'],body)
        with self.database.transaction() as db:
            original_revocation=db.execute('SELECT revoked_at,revoke_reason FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,self.binding['binding_id'])).fetchone()
            self.assertIsNotNone(original_revocation['revoked_at']);self.assertIn(body['reason'],original_revocation['revoke_reason'])
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE tenant_id=%s AND token_hash=%s",(self.tenant,self.actor['session_hash']))
        self.assertEqual(self.service.revoke_binding(self.actor,self.binding['binding_id'],body),result)
        with self.database.transaction() as db:
            row=db.execute('SELECT revoked_at,revoke_reason FROM ss_receiving_bindings WHERE tenant_id=%s AND id=%s',(self.tenant,self.binding['binding_id'])).fetchone()
            self.assertEqual(row,original_revocation)
            self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',(self.tenant,body['request_key'])).fetchone()['n'],1)
        self.assertEqual(len(self.provider.access_calls),before);self.assertEqual(self.provider.calls,[])
