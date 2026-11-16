"""Read-only recovery history against disposable real TLS PostgreSQL only.

Uses isolated fictional domain-session seeds and actual current_authority and
HTTP Auth.resolve; separate acceptance proves password/TOTP cryptography.
"""
from copy import deepcopy
import hashlib
import json
import time
import unittest
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from hosted.app import create_app
from hosted.receiving_plan import ReceivingError, canonical, fingerprint
from hosted.security import ServiceError, digest
from test_hosted_receiving import plan_data
import test_hosted_receiving_postgres as receiving_fixture
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_intake_fixtures import fresh_mfa_actor


class PostgreSQLReceivingHistoryAcceptance(_PostgreSQLSettlementFixture):
    # Reuse reviewed fixture setup/helpers without inheriting or rediscovering
    # its unrelated test cases, or changing fixture authorization in product.
    setUp=receiving_fixture.PostgreSQLReceivingAcceptance.setUp
    approve=receiving_fixture.PostgreSQLReceivingAcceptance.approve
    plan=receiving_fixture.PostgreSQLReceivingAcceptance.plan
    terms=receiving_fixture.PostgreSQLReceivingAcceptance.terms
    commit=receiving_fixture.PostgreSQLReceivingAcceptance.commit
    trade=receiving_fixture.PostgreSQLReceivingAcceptance.trade
    accept_trade=receiving_fixture.PostgreSQLReceivingAcceptance.accept_trade
    wallet=receiving_fixture.PostgreSQLReceivingAcceptance.wallet

    def snapshot(self):
        with self.database.transaction() as db:
            names=db.execute("SELECT table_name FROM information_schema.columns WHERE table_schema=%s AND column_name='tenant_id' ORDER BY table_name",(self.schema,)).fetchall()
            payload={row['table_name']:db.execute(sql.SQL('SELECT to_jsonb(t) AS value FROM {} t WHERE tenant_id=%s ORDER BY to_jsonb(t)::text').format(sql.Identifier(row['table_name'])),(self.tenant,)).fetchall() for row in names}
        return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()

    def readonly_history(self, actor=None, ident=None):
        before=self.snapshot();provider=deepcopy((self.provider.calls,self.provider.catalog_calls,self.provider.access_calls,self.provider.history_rows,self.provider.objects))
        try:return self.service.intake_history(actor or self.actor,ident or self.claim['physical_intake_id'])
        finally:
            self.assertEqual(self.snapshot(),before,'History must not alter any tenant table.')
            self.assertEqual((self.provider.calls,self.provider.catalog_calls,self.provider.access_calls,self.provider.history_rows,self.provider.objects),provider,'History must not access or mutate the provider.')

    def test_owned_claim_without_operation_is_complete_authoritative_none(self):
        self.assertEqual(self.readonly_history(),{'version':1,'physical_intake_id':self.claim['physical_intake_id'],'operations':[],'lookup_complete':True})

    def test_owner_manager_current_operation_and_unknown_outcome_are_exact_and_read_only(self):
        plan=self.plan()
        for actor in (self.actor,self.actors['manager']):
            self.assertEqual(self.readonly_history(actor)['operations'],[plan])
        self.provider.behavior='lost_response';unknown=self.commit(plan)
        self.assertEqual(unknown['state'],'outcome_unknown')
        result=self.readonly_history();self.assertTrue(result['lookup_complete'])
        self.assertEqual(result['operations'],[unknown]);self.assertEqual(len(self.provider.calls),1)

    def test_received_full_original_receipt_is_not_recovered_or_reissued(self):
        result=self.commit(self.plan())
        self.assertEqual(self.readonly_history()['operations'],[result])
        self.assertEqual(self.readonly_history()['operations'][0]['receipt'],result['receipt'])
        self.assertEqual(len(self.provider.calls),1);self.assertEqual(self.wallet(),(0,0))

    def test_never_admitted_cancelled_plan_remains_visible_after_later_accepted_trade(self):
        plan=self.plan()
        cancelled=self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Fictional owner cancelled before any provider admission'))
        self.assertEqual(self.readonly_history()['operations'],[cancelled])
        self.accept_trade(self.trade())
        self.assertEqual(self.claims.get(self.actor,self.claim['physical_intake_id'])['state'],'consumed')
        self.assertEqual(self.readonly_history()['operations'],[cancelled])
        self.assertEqual(self.wallet(),(1000,1));self.assertEqual(self.provider.calls,[])

    def test_accepted_trade_history_preserves_original_financial_source_without_new_credit(self):
        trade=self.trade();self.accept_trade(trade);accepted=self.trades.get(self.actor,trade['trade_id'])
        body=plan_data(self.claim);body.pop('external_ownership_confirmed')
        body.update(workflow_kind='accepted_trade',accepted_trade_id=trade['trade_id'],accepted_receipt_hash=accepted['receipt_hash'])
        body['lines'][0].pop('identity');body['lines'][0]['accepted_line_id']=accepted['accepted_receipt']['lines'][0]['id']
        plan=self.plan(body)
        self.assertEqual(self.readonly_history()['operations'],[plan]);self.assertEqual(self.wallet(),(1000,1))
        self.assertEqual(self.provider.calls,[])
        # Unsupported legacy/corrupt plan data must not borrow the valid claim's
        # accepted financial authority merely because its plan hash is coherent.
        with self.database.transaction() as db:
            row=db.execute('SELECT plan FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',(self.tenant,plan['receiving_id'])).fetchone()
            corrupted=row['plan']|{'accepted_receipt_hash':'0'*64}
            db.execute('UPDATE ss_receiving_operations SET plan=%s,plan_hash=%s WHERE tenant_id=%s AND id=%s',
                       (Jsonb(corrupted),fingerprint(corrupted),self.tenant,plan['receiving_id']))
        with self.assertRaises(ReceivingError):self.readonly_history()
        self.assertEqual(self.wallet(),(1000,1));self.assertEqual(self.provider.calls,[])

    def test_corrupt_saved_operation_source_binding_fails_closed_and_changes_nothing(self):
        plan=self.plan()
        with self.database.transaction() as db:
            row=db.execute('SELECT plan FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s',(self.tenant,plan['receiving_id'])).fetchone()
            corrupted=row['plan']|{'physical_manifest_hash':'0'*64}
            db.execute('UPDATE ss_receiving_operations SET plan=%s,plan_hash=%s WHERE tenant_id=%s AND id=%s',
                       (Jsonb(corrupted),fingerprint(corrupted),self.tenant,plan['receiving_id']))
        with self.assertRaises(ReceivingError) as caught:self.readonly_history()
        self.assertEqual(caught.exception.code,'RECEIVING_HISTORY_INCONSISTENT')

    def test_tenant_isolation_missing_claim_and_buyer_cashier_denials_are_not_empty_success(self):
        self.plan()
        with self.assertRaises(ServiceError) as caught:self.readonly_history(ident=str(uuid.uuid4()))
        self.assertEqual(caught.exception.status,404)
        for role in ('buyer','cashier'):
            with self.subTest(role=role),self.assertRaises(ServiceError) as caught:self.readonly_history(self.actors[role])
            self.assertEqual(caught.exception.status,403)
        tenant=uuid.uuid4();foreign={'tenant_id':tenant,'staff_id':uuid.uuid4(),'role':'owner'}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional unrelated store')",(tenant,'foreign-'+tenant.hex))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'history@example.invalid','fictional-invalid-password','owner')",(foreign['staff_id'],tenant))
            foreign=fresh_mfa_actor(db,foreign)
        with self.assertRaises(ServiceError) as caught:self.readonly_history(foreign)
        self.assertEqual(caught.exception.status,404)

    def test_current_disabled_role_revoked_session_and_mfa_version_changes_are_denied(self):
        self.plan();manager=self.actors['manager']
        updates=[("UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s",(self.tenant,manager['staff_id']),403),
                 ("UPDATE ss_staff SET role='cashier' WHERE tenant_id=%s AND id=%s",(self.tenant,manager['staff_id']),403),
                 ("DELETE FROM ss_sessions WHERE token_hash=%s",(manager['session_hash'],),401),
                 ("UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s",(self.tenant,manager['staff_id']),401)]
        for statement,parameters,status in updates:
            # Commit each fictional authority change so the actual service sees
            # it through a different connection, then explicitly restore it.
            with self.subTest(statement=statement.split()[0:3]):
                with self.database.transaction() as db:db.execute(statement,parameters)
                with self.assertRaises(ServiceError) as caught:self.readonly_history(manager)
                self.assertEqual(caught.exception.status,status)
                with self.database.transaction() as db:
                    db.execute("UPDATE ss_staff SET enabled=TRUE,role='manager' WHERE tenant_id=%s AND id=%s",(self.tenant,manager['staff_id']))
                    db.execute('UPDATE ss_staff_mfa SET version=1 WHERE tenant_id=%s AND staff_id=%s',(self.tenant,manager['staff_id']))
                    fresh_mfa_actor(db,manager,existing_session=True)
                    # Recreate only the explicitly deleted fictional session.
                    db.execute("INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at) VALUES (%s,%s,%s,now()+interval '1 hour',1,now()) ON CONFLICT(token_hash) DO UPDATE SET expires_at=EXCLUDED.expires_at",(manager['session_hash'],self.tenant,manager['staff_id']))

    def test_stale_write_mfa_allows_history_but_cannot_authorize_new_provider_work(self):
        plan=self.plan()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=clock_timestamp()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        self.assertEqual(self.readonly_history()['operations'],[plan])
        with self.assertRaises(ServiceError):self.commit(plan)
        self.assertEqual(self.provider.calls,[])

    def test_session_expiring_during_projection_is_denied_by_second_actual_authority_check(self):
        self.plan()
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET expires_at=clock_timestamp()+interval '1 second' WHERE token_hash=%s",(self.actor['session_hash'],))
        from hosted.receiving_plan import public_response_bytes
        def delayed_projection(value):
            result=public_response_bytes(value);time.sleep(1.1);return result
        with patch('hosted.receiving.public_response_bytes',side_effect=delayed_projection):
            with self.assertRaises(ServiceError) as caught:self.readonly_history()
        self.assertEqual(caught.exception.status,401)

    def test_actual_unique_constraint_prevents_second_operation_including_after_cancellation(self):
        plan=self.plan();self.service.cancel(self.actor,plan['receiving_id'],self.terms(plan,reason='Fictional before-write cancellation retains permanent operation'))
        before=self.snapshot()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            with self.database.transaction() as db:
                db.execute('''INSERT INTO ss_receiving_operations(tenant_id,id,claim_id,workflow_kind,plan_hash,plan,created_by,updated_by)
                              SELECT tenant_id,%s,claim_id,workflow_kind,plan_hash,plan,created_by,updated_by FROM ss_receiving_operations WHERE tenant_id=%s AND id=%s''',(uuid.uuid4(),self.tenant,plan['receiving_id']))
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.readonly_history()['operations']),1)

    def test_domain_budget_failure_is_read_only_not_an_authoritative_empty_result(self):
        self.plan()
        with patch('hosted.receiving_plan.PUBLIC_RESPONSE_BYTES',32):
            with self.assertRaises(ReceivingError) as caught:self.readonly_history()
        self.assertEqual(caught.exception.code,'RECEIVING_PROJECTION_LIMIT')

    def test_actual_http_auth_resolve_route_returns_full_dto_and_denies_revoked_token(self):
        plan=self.plan();token='fictional-history-session-'+uuid.uuid4().hex
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                          SELECT %s,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at FROM ss_sessions WHERE token_hash=%s''',
                       (digest(token),self.actor['session_hash']))
        app=create_app({'TESTING':True,'PUBLIC_BASE_URL':'https://history.fixture.invalid',
                        'APP_ENCRYPTION_KEYS':Fernet.generate_key().decode()},database=self.database)
        client=app.test_client();url='/api/receiving/intakes/'+self.claim['physical_intake_id']+'/history'
        before=self.snapshot();response=client.get(url,base_url='https://history.fixture.invalid',headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.data,canonical({'version':1,'physical_intake_id':self.claim['physical_intake_id'],'operations':[plan],'lookup_complete':True}))
        self.assertEqual(self.snapshot(),before);self.assertEqual(response.headers['Cache-Control'],'no-store')
        with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(digest(token),))
        response=client.get(url,base_url='https://history.fixture.invalid',headers={'Authorization':'Bearer '+token})
        self.assertEqual(response.status_code,401);self.assertNotIn('operations',response.json)
        self.assertEqual(self.provider.calls,[])


if __name__=='__main__':unittest.main()
