"""Explicit original pending-key resolution against a real private PostgreSQL schema.

Pending A is a fictional exact wire body prepared before B, not a desktop SQLite
or DPAPI journal integration. All provider acknowledgements are injected test
evidence; this proves no new provider admission, never real Square acceptance.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
import uuid
from unittest.mock import patch

from psycopg import sql
from hosted.receiving_plan import canonical, fingerprint, ReceivingError
from hosted.security import ServiceError
import test_hosted_settlement_postgres as settlement_fixture
import test_hosted_receiving_postgres as receiving_fixture
import test_hosted_receiving_pipeline_postgres as catalog_fixture
from test_hosted_receiving import plan_data


PROOFS=[]


class PostgreSQLPendingCommitAcceptance(settlement_fixture._PostgreSQLSettlementFixture):
    def setUp(self):
        # The inherited class only owns disposable-schema lifecycle. Compose
        # existing case factories without inheriting/discovering their tests.
        pass

    def fixture(self,kind):
        cls=catalog_fixture.PostgreSQLReceivingPipelineAcceptance if kind=='catalog' else receiving_fixture.PostgreSQLReceivingAcceptance
        fixture=cls();fixture.database=self.database
        self.addCleanup(fixture.doCleanups);fixture.setUp()
        return fixture

    def plan(self,fixture,*,accepted=True,new_price=False):
        body=plan_data(fixture.claim);body['request_key']='plan-'+uuid.uuid4().hex
        if accepted:
            trade=fixture.trade();fixture.accept_trade(trade)
            accepted_trade=fixture.trades.get(fixture.actor,trade['trade_id'])
            body.pop('external_ownership_confirmed')
            body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['trade_id'],
                        accepted_receipt_hash=accepted_trade['receipt_hash'])
            body['lines'][0].pop('identity')
            body['lines'][0]['accepted_line_id']=accepted_trade['accepted_receipt']['lines'][0]['id']
            self.assertEqual(fixture.wallet(),(1000,1))
        if new_price:body['lines'][0]['selling_cents']=1250
        return fixture.service.create_plan(fixture.actor,body)

    def snapshot(self,*,exclude=None):
        """Retain only per-table digests/counts, never fixture auth/secret rows."""
        result={}
        with self.database.transaction() as db:
            tables=db.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",(self.schema,)).fetchall()
            for entry in tables:
                name=entry['table_name'];query=sql.SQL('SELECT to_jsonb(t)::text AS body FROM {}.{} t').format(sql.Identifier(self.schema),sql.Identifier(name));params=()
                if name=='ss_receiving_requests' and exclude:
                    query+=sql.SQL(' WHERE NOT (tenant_id=%s AND request_key=%s)');params=exclude
                query+=sql.SQL(' ORDER BY to_jsonb(t)::text')
                rows=[row['body'] for row in db.execute(query,params).fetchall()]
                result[name]={'rows':len(rows),'sha256':hashlib.sha256('\n'.join(rows).encode()).hexdigest()}
        return result

    @staticmethod
    def provider_snapshot(fixture):
        provider=fixture.provider
        return {'objectsHash':fingerprint(provider.objects),'inventoryBodyHashes':[hashlib.sha256(body).hexdigest() for body in provider.calls],
                'catalogBodyHashes':[hashlib.sha256(body).hexdigest() for body in provider.catalog_calls],
                'accessCalls':list(provider.access_calls),'historyHash':fingerprint(provider.history_rows)}

    def no_provider(self,fixture):
        stack=ExitStack()
        for name in ('access','assert_current','locations','location','catalog','assert_skus','dispatch_inventory','dispatch_catalog','history'):
            mock=stack.enter_context(patch.object(fixture.provider,name,side_effect=AssertionError('No provider method belongs in original admitted-key resolution.')))
            stack.callback(mock.assert_not_called)
        return stack

    def key_absent(self,fixture,key):
        with self.database.transaction() as db:
            row=db.execute('SELECT count(*) AS n FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',(fixture.tenant,key)).fetchone()
        self.assertEqual(row['n'],0)

    def expire_and_disable_new_writes(self,fixture,actor):
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '20 minutes' WHERE token_hash=%s",(actor['session_hash'],))
            db.execute('UPDATE ss_receiving_settings SET enabled=FALSE,new_price_enabled=FALSE WHERE tenant_id=%s',(fixture.tenant,))
        fixture.service.writes_enabled=False

    def check_resolution(self,*,kind,state,accepted=True,manager=False):
        fixture=self.fixture(kind);plan=self.plan(fixture,accepted=accepted,new_price=kind=='catalog')
        pending=fixture.terms(plan,confirmed=True,excluded_not_received_ack=True)
        original_wire=canonical(pending);key=pending['request_key']
        self.assertFalse(fixture.service.request_status(fixture.actor,key)['found']);self.key_absent(fixture,key)
        admitted=fixture.commit(plan)
        if state=='received' and kind=='catalog':admitted=fixture.authorize(plan)
        self.assertEqual(admitted['state'],state)
        actor=fixture.actors['manager'] if manager else fixture.actor
        self.expire_and_disable_new_writes(fixture,actor)
        before=self.snapshot();provider_before=self.provider_snapshot(fixture)
        current=fixture.service.get(actor,plan['receiving_id'])
        with self.no_provider(fixture):
            result=fixture.service.commit(actor,plan['receiving_id'],pending)
            self.assertEqual(result,current)
            self.assertEqual(fixture.service.request_status(actor,key),{'found':True,'result':result})
            after=self.snapshot(exclude=(fixture.tenant,key))
            self.assertEqual(after,before,'Only the new A receiving-request row may change any fixture table.')
            with self.database.transaction() as db:
                rows=db.execute('SELECT action,intent_hash,operation_id,result FROM ss_receiving_requests WHERE tenant_id=%s AND request_key=%s',(fixture.tenant,key)).fetchall()
                outcomes=db.execute('SELECT count(*) AS n FROM ss_receiving_request_outcomes WHERE tenant_id=%s AND request_key=%s',(fixture.tenant,key)).fetchone()['n']
            self.assertEqual(len(rows),1);self.assertEqual(outcomes,0)
            intent={'receiving_id':plan['receiving_id'],'revision':plan['revision'],'plan_hash':plan['plan_hash'],
                    'confirmed':True,'excluded_not_received_ack':True}
            self.assertEqual(rows[0]['action'],'commit');self.assertEqual(rows[0]['intent_hash'],fingerprint({'action':'commit','intent':intent}))
            self.assertEqual(str(rows[0]['operation_id']),plan['receiving_id']);self.assertEqual(rows[0]['result'],result)
            complete_after=self.snapshot()
            self.assertEqual(complete_after['ss_receiving_requests']['rows'],before['ss_receiving_requests']['rows']+1)
            self.assertEqual(fixture.service.commit(actor,plan['receiving_id'],pending),result)
            self.assertEqual(self.snapshot(),complete_after,'An exact repeat must not insert or modify any row.')
        self.assertEqual(canonical(pending),original_wire)
        self.assertEqual(self.provider_snapshot(fixture),provider_before)
        self.assertEqual((result['receiving_id'],result['revision'],result['plan_hash']),(plan['receiving_id'],plan['revision'],plan['plan_hash']))
        self.assertEqual(fixture.wallet(),(1000,1) if accepted else (0,0))
        PROOFS.append({'case':self.id(),'state':state,'workflow':'accepted_trade' if accepted else 'inventory_only_backend_control',
                       'actorRole':actor['role'],'pendingMeaning':'fictional exact wire body/key before B; no desktop journal claim',
                       'pendingBodyHash':hashlib.sha256(original_wire).hexdigest(),'requestKey':key,'receivingId':plan['receiving_id'],
                       'revision':plan['revision'],'planHash':plan['plan_hash'],'actualARequestRows':1,'actualAOutcomeRows':0,
                       'allTablesUnchangedExceptARequest':True,'repeatAllTablesUnchanged':True,'newProviderMethods':0,
                       'providerSnapshot':provider_before,'tableSnapshotBeforeA':before,'wallet':fixture.wallet(),'freshWriteMfaAndWritesDisabledAfterB':True})

    def test_accepted_same_price_received_resolves_exact_pending_a_without_new_step(self):
        self.check_resolution(kind='ordinary',state='received')

    def test_accepted_catalog_confirmed_resolves_exact_pending_a_without_stock_admission(self):
        self.check_resolution(kind='catalog',state='catalog_confirmed')

    def test_accepted_catalog_received_manager_resolves_exact_pending_a_without_new_step(self):
        self.check_resolution(kind='catalog',state='received',manager=True)

    def test_inventory_only_received_backend_control_preserves_claim_without_financial_effect(self):
        self.check_resolution(kind='ordinary',state='received',accepted=False)

    def test_pending_cancel_after_other_key_admission_stays_rejected_and_unrecorded(self):
        fixture=self.fixture('ordinary');plan=self.plan(fixture)
        pending=fixture.terms(plan,reason='Fictional explicitly reviewed cancellation before another admission')
        original=canonical(pending);self.key_absent(fixture,pending['request_key'])
        self.assertEqual(fixture.commit(plan)['state'],'received')
        before=self.snapshot();provider=self.provider_snapshot(fixture)
        with self.no_provider(fixture),self.assertRaises(ReceivingError) as caught:
            fixture.service.cancel(fixture.actor,plan['receiving_id'],pending)
        self.assertEqual(caught.exception.code,'PROVIDER_OUTCOME_UNKNOWN')
        self.key_absent(fixture,pending['request_key']);self.assertEqual(self.snapshot(),before)
        self.assertEqual(canonical(pending),original);self.assertEqual(self.provider_snapshot(fixture),provider)
        self.assertFalse(fixture.service.request_status(fixture.actor,pending['request_key'])['found'])
        PROOFS.append({'case':self.id(),'deniedCode':caught.exception.code,'allTablesAndProviderUnchanged':True,'pendingKeyRecorded':False})

    def test_unadmitted_ready_stale_mfa_cannot_use_admitted_resolution_branch(self):
        for kind in ('ordinary','catalog'):
            with self.subTest(kind=kind):
                fixture=self.fixture(kind);plan=self.plan(fixture,new_price=kind=='catalog')
                pending=fixture.terms(plan,confirmed=True,excluded_not_received_ack=True)
                with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '20 minutes' WHERE token_hash=%s",(fixture.actor['session_hash'],))
                before=self.snapshot();provider=self.provider_snapshot(fixture)
                with self.no_provider(fixture),self.assertRaises(ServiceError) as caught:
                    fixture.service.commit(fixture.actor,plan['receiving_id'],pending)
                self.assertEqual(getattr(caught.exception,'code',None),'MFA_STEP_UP_REQUIRED')
                self.key_absent(fixture,pending['request_key']);self.assertEqual(self.snapshot(),before)
                self.assertEqual(self.provider_snapshot(fixture),provider)
                PROOFS.append({'case':self.id(),'kind':kind,'deniedCode':'MFA_STEP_UP_REQUIRED','allTablesAndProviderUnchanged':True,'pendingKeyRecorded':False})

    def test_admitted_replay_keeps_current_authority_and_exact_revision_hash_guards(self):
        for change in ('role','session','tenant','revision','plan_hash'):
            with self.subTest(change=change):
                fixture=self.fixture('ordinary');plan=self.plan(fixture)
                pending=fixture.terms(plan,confirmed=True,excluded_not_received_ack=True)
                self.assertEqual(fixture.commit(plan)['state'],'received');actor=deepcopy(fixture.actor)
                if change=='role':
                    with self.database.transaction() as db:db.execute("UPDATE ss_staff SET role='buyer' WHERE tenant_id=%s AND id=%s",(fixture.tenant,actor['staff_id']))
                elif change=='session':
                    with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(actor['session_hash'],))
                elif change=='tenant':actor['tenant_id']=uuid.uuid4()
                elif change=='revision':pending['revision']+=1
                else:pending['plan_hash']='f'*64
                before=self.snapshot();provider=self.provider_snapshot(fixture)
                with self.no_provider(fixture),self.assertRaises(ServiceError) as caught:
                    fixture.service.commit(actor,plan['receiving_id'],pending)
                if change in ('revision','plan_hash'):self.assertEqual(getattr(caught.exception,'code',None),'PLAN_CHANGED')
                self.key_absent(fixture,pending['request_key']);self.assertEqual(self.snapshot(),before)
                self.assertEqual(self.provider_snapshot(fixture),provider)
                PROOFS.append({'case':self.id(),'change':change,'denied':True,'allTablesAndProviderUnchanged':True,'pendingKeyRecorded':False})
