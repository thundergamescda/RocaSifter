"""Actual PostgreSQL physical-claim/financial boundaries in disposable schemas.

No provider requests, production migration, credentials or existing customer
records. Every test uses fictional staff, central references and original rows.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import unittest
import threading
import uuid
from unittest.mock import patch
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from intake_manifest import capture_manifest, source_line_id
from hosted.intake import IntakeClaims, IntakeError
from hosted.security import ServiceError
from hosted.trades import Trades
from hosted.database import Database
from hosted.ledger import Ledger
from hosted.credit_lots import record_credit_lot
from hosted.settlement import evidence_hash
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import card_line, draft_data, acceptance_data
from test_intake_fixtures import fresh_mfa_actor


class PostgreSQLIntakeAcceptance(_PostgreSQLSettlementFixture):
    def setUp(self):
        self.tenant, self.customer = uuid.uuid4(), uuid.uuid4()
        self.actors = {role:{'tenant_id':self.tenant,'staff_id':uuid.uuid4(),'role':role}
                       for role in ('owner','manager','buyer','cashier')}
        with self.database.transaction() as db:
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional intake store')",(self.tenant,'fixture-'+self.tenant.hex))
            for role, actor in self.actors.items():
                db.execute('INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,%s)',
                           (actor['staff_id'],self.tenant,role+'@example.invalid','fixture-invalid-password',role))
                self.actors[role] = fresh_mfa_actor(db,actor)
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,'Fictional intake customer','fixture-only')",(self.customer,self.tenant))
        self.actor=self.actors['owner'];self.claims=IntakeClaims(self.database);self.trades=Trades(self.database)
        self.line=card_line();self.line['source_line_key']=source_line_id(self.line['identity'])
        self.manifest=capture_manifest('csv',[{'identity':self.line['identity'],'quantity':2,'source_row':2}],raw_csv_sha256='a'*64)
        no_provider=patch('requests.sessions.Session.request',side_effect=AssertionError('Provider calls forbidden.'))
        no_provider.start();self.addCleanup(no_provider.stop)

    def claim_data(self, **changes):
        return {'request_key':'claim-'+uuid.uuid4().hex,'physical_reference':'batch-'+uuid.uuid4().hex,
                'source_manifest':deepcopy(self.manifest),'physical_delivery_verified':True} | changes

    def claim(self, **changes):return self.claims.create(self.actor,self.claim_data(**changes))

    def draft(self, claim=None, *, lines=None, customer=None):
        data=draft_data(customer_id=customer or self.customer,request_key='draft-'+uuid.uuid4().hex)
        data['source_reference']='PC-receipt-'+uuid.uuid4().hex
        data['lines']=deepcopy(lines if lines is not None else [self.line])
        if claim:data.update(physical_intake_id=claim['physical_intake_id'],physical_manifest_hash=claim['manifest_hash'])
        return self.trades.create(self.actor,data)

    def accept(self, draft, *, actor=None, customer=None, offer='credit', cents=1000, key=None, reviewed=True):
        data=acceptance_data(customer or self.customer,request_key=key or 'accept-'+uuid.uuid4().hex,
                             version=draft['version'],offer=offer,cents=cents)
        data['claim_reviewed']=reviewed
        return self.trades.accept(actor or self.actor,draft['trade_id'],data)

    def wallet(self):
        with self.database.transaction() as db:
            person=db.execute('SELECT balance_cents FROM ss_customers WHERE tenant_id=%s AND id=%s',(self.tenant,self.customer)).fetchone()
            entries=db.execute("SELECT count(*) AS n FROM ss_credit_entries WHERE tenant_id=%s AND kind='trade_credit'",(self.tenant,)).fetchone()['n']
            lots=db.execute('SELECT count(*) AS n FROM ss_credit_lots WHERE tenant_id=%s',(self.tenant,)).fetchone()['n']
        return person['balance_cents'],entries,lots

    def test_claim_request_lost_response_and_new_key_recover_exact_original(self):
        body=self.claim_data();first=self.claims.create(self.actor,body)
        self.assertEqual(self.claims.create(self.actor,body),first)
        self.assertEqual(self.claims.request_status(self.actor,body['request_key']),{'found':True,'result':first})
        copied=self.claims.create(self.actor,body|{'request_key':'other-PC','physical_reference':'different-PC-reference'})
        self.assertEqual(copied,first)
        self.assertEqual(self.claims.request_status(self.actor,'missing'),{'found':False,'result':None})
        self.accept(self.draft(first))
        self.assertEqual(self.claims.create(self.actor,body),first)
        self.assertEqual(self.claims.get(self.actor,first['physical_intake_id'])['state'],'consumed')

    def test_changed_request_key_payload_or_shared_reference_cannot_rebind(self):
        body=self.claim_data();first=self.claims.create(self.actor,body)
        for changed in ({'physical_reference':'another-ref'}, {'source_manifest':capture_manifest('manual',[{'identity':self.line['identity'],'quantity':2}])}):
            with self.subTest(changed=tuple(changed)),self.assertRaises(IntakeError):self.claims.create(self.actor,body|changed)
        with self.assertRaises(IntakeError):self.claims.create(self.actor,self.claim_data(physical_reference=body['physical_reference'],source_manifest=capture_manifest('manual',[{'identity':self.line['identity'],'quantity':1}])))
        self.assertEqual(self.claims.get(self.actor,first['physical_intake_id'])['manifest_hash'],first['manifest_hash'])

    def test_same_raw_csv_with_changed_mapping_cannot_create_new_claim(self):
        first=self.claim();other=deepcopy(self.line['identity']);other['name']='Edited mapping cannot create fresh stock'
        changed=capture_manifest('csv',[{'identity':other,'quantity':2}],raw_csv_sha256='a'*64)
        with self.assertRaises(IntakeError) as caught:self.claim(source_manifest=changed)
        self.assertEqual(caught.exception.physical_intake_id,first['physical_intake_id'])

    def test_equivalent_reordered_or_differently_encoded_source_requires_existing_claim(self):
        first=self.claim()
        changed=capture_manifest('csv',[{'identity':self.line['identity'],'quantity':1},{'identity':self.line['identity'],'quantity':1}],raw_csv_sha256='b'*64)
        with self.assertRaises(IntakeError) as caught:self.claim(source_manifest=changed)
        self.assertEqual(caught.exception.physical_intake_id,first['physical_intake_id'])

    def test_concurrent_different_pc_claim_creation_resolves_one_central_claim(self):
        bodies=[self.claim_data(),self.claim_data()]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda body:self.claims.create(self.actor,body),bodies))
        self.assertEqual(results[0],results[1])
        with self.database.transaction() as db:self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_intake_claims WHERE tenant_id=%s',(self.tenant,)).fetchone()['n'],1)

    def test_two_pc_receipts_sharing_claim_concurrently_issue_at_most_one_credit(self):
        claim=self.claim();drafts=[self.draft(claim),self.draft(claim)]
        def accept(draft):
            try:return self.accept(draft)
            except ServiceError as error:return error
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(accept,drafts))
        self.assertEqual(sum(isinstance(result,dict) for result in results),1)
        self.assertEqual(self.wallet(),(1000,1,1))
        final=self.claims.get(self.actor,claim['physical_intake_id'])
        self.assertEqual((final['state'],final['version']),('consumed',1))

    def test_unclaimed_new_cash_and_credit_acceptance_fail_before_any_money(self):
        for offer,cents in (('cash',700),('credit',1000)):
            draft=self.draft()
            with self.subTest(offer=offer),self.assertRaises(IntakeError) as caught:self.accept(draft,offer=offer,cents=cents)
            self.assertEqual((caught.exception.status,caught.exception.code),(428,'PHYSICAL_INTAKE_CLAIM_REQUIRED'))
        self.assertEqual(self.wallet(),(0,0,0))

    def test_legacy_pending_create_keeps_body_and_can_bind_only_via_versioned_update(self):
        data=draft_data(customer_id=self.customer,request_key='legacy-create-exact');draft=self.trades.create(self.actor,data)
        self.assertEqual(self.trades.create(self.actor,data),draft)
        claim=self.claim();line=deepcopy(self.line)
        body={'request_key':'explicit-claim-bind','version':0,'physical_intake_id':claim['physical_intake_id'],
              'physical_manifest_hash':claim['manifest_hash'],'lines':[line]}
        bound=self.trades.update(self.actor,draft['trade_id'],body)
        self.assertEqual(self.trades.update(self.actor,draft['trade_id'],body),bound)
        self.assertEqual(self.trades.create(self.actor,data),draft)
        self.assertEqual(self.accept(bound)['accepted_amount_cents'],1000)

    def test_forged_manifest_hash_source_line_and_quantity_inflation_fail(self):
        claim=self.claim()
        for changed in ('manifest','line','quantity'):
            line=deepcopy(self.line);bound=dict(claim)
            if changed=='manifest':bound['manifest_hash']='0'*64
            elif changed=='line':line['source_line_key']='0'*64
            else:line['quantity']=3
            with self.subTest(changed=changed),self.assertRaises(IntakeError):self.draft(bound,lines=[line])
        self.assertEqual(self.wallet(),(0,0,0))

    def test_reviewed_identity_correction_retains_original_source_and_caps_quantity(self):
        claim=self.claim();line=deepcopy(self.line);line['identity']['name']='Explicitly reviewed correction'
        result=self.accept(self.draft(claim,lines=[line]))
        receipt=self.trades.get(self.actor,result['trade_id'])['accepted_receipt']
        self.assertEqual(receipt['lines'][0]['identity']['name'],'Explicitly reviewed correction')
        self.assertEqual(receipt['physical_intake']['manifest_hash'],claim['manifest_hash'])

    def test_full_original_claim_consumed_even_when_some_units_excluded(self):
        claim=self.claim();selected=deepcopy(self.line);excluded=deepcopy(self.line)
        selected['quantity']=1;excluded.update(quantity=1,included=False,reviewed=False)
        first=self.draft(claim,lines=[selected,excluded]);second=self.draft(claim)
        accepted=self.accept(first,cents=500)
        self.assertEqual(accepted['accepted_amount_cents'],500)
        with self.assertRaises(IntakeError):self.accept(second)
        self.assertFalse(self.claims.get(self.actor,claim['physical_intake_id'])['policy']['excluded_rows_reusable'])
        self.assertEqual(self.wallet(),(500,1,1))

    def test_cash_agreement_consumes_claim_without_payment_or_credit(self):
        claim=self.claim();first=self.draft(claim);second=self.draft(claim)
        accepted=self.accept(first,offer='cash',cents=700)
        self.assertFalse(accepted['cash_payment_executed'])
        with self.assertRaises(IntakeError):self.accept(second)
        self.assertEqual(self.wallet(),(0,0,0))

    def test_missing_explicit_whole_manifest_review_cannot_accept(self):
        claim=self.claim();draft=self.draft(claim)
        with self.assertRaises(IntakeError) as caught:self.accept(draft,reviewed=False)
        self.assertEqual(caught.exception.code,'PHYSICAL_INTAKE_REVIEW_REQUIRED')
        self.assertEqual(self.claims.get(self.actor,claim['physical_intake_id'])['state'],'open')

    def test_stale_revoked_foreign_or_changed_mfa_session_cannot_accept(self):
        claim=self.claim();draft=self.draft(claim)
        with self.database.transaction() as db:
            db.execute("UPDATE ss_sessions SET mfa_authenticated_at=now()-interval '16 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        with self.assertRaises(IntakeError) as caught:self.accept(draft)
        self.assertEqual(caught.exception.code,'MFA_STEP_UP_REQUIRED')
        with self.assertRaises(ServiceError):self.accept(draft,actor=self.actor|{'session_hash':self.actors['manager']['session_hash']})
        with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
        with self.assertRaises(ServiceError):self.accept(draft)
        manager=self.actors['manager']
        with self.database.transaction() as db:db.execute('UPDATE ss_staff_mfa SET version=version+1 WHERE tenant_id=%s AND staff_id=%s',(self.tenant,manager['staff_id']))
        with self.assertRaises(ServiceError):self.accept(draft,actor=manager)
        self.assertEqual(self.wallet(),(0,0,0))

    def test_unenrolled_manager_may_create_claim_but_not_accept_or_attest_new_delivery(self):
        manager=self.actors['manager']
        with self.database.transaction() as db:db.execute('DELETE FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s',(self.tenant,manager['staff_id']))
        claim=self.claims.create(manager,self.claim_data());draft=self.draft(claim)
        with self.assertRaises(IntakeError) as caught:self.accept(draft,actor=manager)
        self.assertEqual(caught.exception.code,'MFA_STEP_UP_REQUIRED')
        body=self.claim_data(additional_delivery_of=claim['physical_intake_id'],additional_delivery_confirmed=True,
                             additional_delivery_reason='Fictional separate physical delivery attested')
        with self.assertRaises(IntakeError):self.claims.create(manager,body)

    def test_accepted_exact_recovery_works_after_proof_ages_without_second_credit(self):
        claim=self.claim();draft=self.draft(claim);first=self.accept(draft,key='accepted-key')
        with self.database.transaction() as db:db.execute("UPDATE ss_sessions SET mfa_authenticated_at=now()-interval '20 minutes' WHERE token_hash=%s",(self.actor['session_hash'],))
        self.assertEqual(self.accept(draft,key='accepted-key'),first)
        # New key and pre-guard body (no claim_reviewed) recover frozen evidence.
        old_body=acceptance_data(self.customer,request_key='old-client-recovery',version=draft['version'])
        self.assertEqual(self.trades.accept(self.actor,draft['trade_id'],old_body),first)
        self.assertEqual(self.wallet(),(1000,1,1))

    def test_additional_identical_physical_delivery_requires_link_attestation_and_fresh_mfa(self):
        first=self.claim();self.accept(self.draft(first))
        with self.assertRaises(ServiceError):self.claim(additional_delivery_of=first['physical_intake_id'])
        additional=self.claim(additional_delivery_of=first['physical_intake_id'],additional_delivery_confirmed=True,
                              additional_delivery_reason='Fictional second sealed physical delivery checked')
        self.assertNotEqual(additional['physical_intake_id'],first['physical_intake_id'])
        self.assertEqual(additional['additional_delivery_of'],first['physical_intake_id'])
        self.accept(self.draft(additional));self.assertEqual(self.wallet(),(2000,2,2))

    def test_void_replacement_reuses_open_claim_without_new_delivery_bypass(self):
        claim=self.claim();first=self.draft(claim)
        self.trades.void(self.actor,first['trade_id'],{'request_key':'void-original','version':0,'reason':'Fictional offer changed before agreement'})
        second=self.draft(claim);self.accept(second)
        self.assertEqual(self.claims.get(self.actor,claim['physical_intake_id'])['bound_trade_id'],second['trade_id'])

    def test_buyer_cashier_foreign_tenant_cannot_read_create_or_recover_claims(self):
        claim=self.claim()
        for role in ('buyer','cashier'):
            with self.subTest(role=role),self.assertRaises(ServiceError):self.claims.get(self.actors[role],claim['physical_intake_id'])
            with self.assertRaises(ServiceError):self.claims.create(self.actors[role],self.claim_data())
            with self.assertRaises(ServiceError):self.claims.request_status(self.actors[role],'unknown')
        foreign=self.actor|{'tenant_id':uuid.uuid4()}
        with self.assertRaises(ServiceError):self.claims.get(foreign,claim['physical_intake_id'])
        self.assertEqual(self.wallet(),(0,0,0))

    def test_schema_reapplication_and_immutable_evidence_and_old_server_guard(self):
        claim=self.claim();draft=self.draft(claim);self.database.migrate()
        self.assertTrue(self.database.ready())
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.database.transaction() as db:db.execute('UPDATE ss_intake_claims SET physical_reference=%s WHERE tenant_id=%s AND id=%s',('forged-ref',self.tenant,claim['physical_intake_id']))
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.database.transaction() as db:db.execute("UPDATE ss_trade_receipts SET state='accepted_cash' WHERE tenant_id=%s AND id=%s",(self.tenant,draft['trade_id']))
        self.accept(draft)
        with self.assertRaises(psycopg.errors.RaiseException):
            with self.database.transaction() as db:db.execute('DELETE FROM ss_intake_claims WHERE tenant_id=%s AND id=%s',(self.tenant,claim['physical_intake_id']))

    def test_failed_financial_audit_rolls_back_whole_claim_consumption(self):
        claim=self.claim();draft=self.draft(claim)
        with patch('hosted.trades.audit',side_effect=RuntimeError('Fictional audit failure')):
            with self.assertRaises(RuntimeError):self.accept(draft,key='rolled-back-accept')
        self.assertEqual(self.claims.get(self.actor,claim['physical_intake_id'])['state'],'open')
        self.assertEqual(self.wallet(),(0,0,0))
        self.assertEqual(self.accept(draft,key='rolled-back-accept')['accepted_amount_cents'],1000)

    def test_session_revocation_serializes_with_authorized_acceptance(self):
        from hosted.intake import current_authority
        claim=self.claim();draft=self.draft(claim)
        locked=threading.Event();release=threading.Event();revoking=threading.Event();revoked=threading.Event()
        def checked(*args,**kwargs):
            result=current_authority(*args,**kwargs);locked.set()
            if not release.wait(5):raise RuntimeError('Fixture authorization barrier expired.')
            return result
        def revoke():
            revoking.set()
            with self.database.transaction() as db:db.execute('DELETE FROM ss_sessions WHERE token_hash=%s',(self.actor['session_hash'],))
            revoked.set()
        with ThreadPoolExecutor(max_workers=2) as pool,patch('hosted.trades.current_authority',side_effect=checked):
            accepted=pool.submit(self.accept,draft)
            try:
                self.assertTrue(locked.wait(5));pending=pool.submit(revoke);self.assertTrue(revoking.wait(5))
                self.assertFalse(revoked.wait(.1),'Revocation bypassed the transaction session lock.')
            finally:release.set()
            self.assertEqual(accepted.result(5)['accepted_amount_cents'],1000);pending.result(5)
        self.assertTrue(revoked.is_set());self.assertEqual(self.wallet(),(1000,1,1))
        # A new acceptance cannot reuse the revoked staff session, even with a
        # dictionary claiming fresh MFA. Read-only recovery remains API scoped.
        with self.assertRaises(ServiceError):self.claims.get(self.actor,claim['physical_intake_id'])

    def test_changed_role_and_disabled_staff_cannot_consume_open_claim(self):
        claim=self.claim();draft=self.draft(claim)
        for change in ("role='buyer'", "role='owner',enabled=FALSE"):
            with self.database.transaction() as db:db.execute('UPDATE ss_staff SET '+change+' WHERE tenant_id=%s AND id=%s',(self.tenant,self.actor['staff_id']))
            with self.assertRaises(ServiceError):self.accept(draft)
        self.assertEqual(self.wallet(),(0,0,0))
        self.assertEqual(self.claims.get(self.actors['manager'],claim['physical_intake_id'])['state'],'open')

    def test_schema4_accepted_credit_and_exact_journal_survive_schema5_without_backfill(self):
        # Install the actual schema4 prefix in a SECOND disposable namespace,
        # create a fictional already-final receipt there, then run schema5.
        schema='ss_test_'+uuid.uuid4().hex
        with psycopg.connect(self.url,sslmode='require') as connection:
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        database=Database(self.url,schema=schema)
        try:
            old_schema=(Path(__file__).parent/'hosted'/'schema.sql').read_text().split('-- Version 5:')[0]
            tenant,staff,customer,trade,entry,lineid=[uuid.uuid4() for _ in range(6)]
            actor={'tenant_id':tenant,'staff_id':staff,'role':'owner'}
            line=deepcopy(self.line);line.pop('source_line_key');line.update(id=str(lineid),line_total_cents=2000)
            receipt={'trade_schema_version':1,'trade_id':str(trade),'version_before_acceptance':0,
                     'customer':{'id':str(customer),'display_name':'Fictional old customer'},'lines':[line],
                     'accepted_amount_cents':1000,'credit_entry_id':str(entry)}
            original={'trade_id':str(trade),'state':'accepted_credit','version':1,'accepted_amount_cents':1000,
                      'credit_entry_id':str(entry),'balance_after_cents':1000,'receipt_hash':evidence_hash(receipt)}
            ledger=Ledger(database)
            intent={'op':'trade_accept','trade_id':str(trade),'version':0,'customer_id':str(customer),
                    'offer_type':'credit','expected_offer_cents':1000,'customer_agreed':True,'items_received':True}
            with database.transaction() as db:
                db.execute(old_schema,prepare=False)
                db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'Fictional schema4 store')",(tenant,'old-'+tenant.hex))
                db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'old@example.invalid','fictional-invalid','owner')",(staff,tenant))
                db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis,balance_cents) VALUES (%s,%s,'Fictional old customer','fixture-only',1000)",(customer,tenant))
                db.execute('''INSERT INTO ss_trade_receipts(id,tenant_id,customer_id,title,creation_hash,cash_bps,credit_bps,
                              intake_total_cents,cash_offer_cents,credit_offer_cents,lines_hash,created_by,updated_by)
                              VALUES (%s,%s,%s,'Legacy accepted evidence',%s,3500,5000,2000,700,1000,%s,%s,%s)''',
                           (trade,tenant,customer,'0'*64,evidence_hash([line]),staff,staff))
                db.execute('''INSERT INTO ss_trade_receipt_lines(id,tenant_id,trade_id,ordinal,identity,quantity,unit_intake_cents,included,reviewed,line_total_cents)
                              VALUES (%s,%s,%s,0,%s,2,1000,TRUE,TRUE,2000)''',(lineid,tenant,trade,Jsonb(line['identity'])))
                db.execute("INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason,staff_id) VALUES (%s,%s,%s,1000,1000,'trade_credit',%s,%s,'Fictional preexisting receipt',%s)",
                           (entry,tenant,customer,'trade:'+str(trade),str(trade),staff))
                record_credit_lot(db,tenant,customer,entry,origin_code='trade_unreviewed')
                db.execute("UPDATE ss_trade_receipts SET state='accepted_credit',version=1,selected_offer='credit',accepted_amount_cents=1000,accepted_at=now(),accepted_by=%s,ledger_entry_id=%s,receipt_hash=%s,accepted_receipt=%s,acceptance_result=%s WHERE tenant_id=%s AND id=%s",
                           (staff,entry,original['receipt_hash'],Jsonb(receipt),Jsonb(original),tenant,trade))
                hashed,_=ledger._begin_request(db,tenant,'old-exact-acceptance',intent)
                ledger._finish(db,tenant,'old-exact-acceptance',hashed,original,operation='trade')
            database.migrate();database.migrate()
            service=Trades(database)
            for key in ('old-exact-acceptance','read-recovery-after-upgrade'):
                recovered=service.accept(actor,trade,acceptance_data(customer,request_key=key))
                self.assertEqual(recovered,original)
            final=service.get(actor,trade)
            self.assertEqual(final['accepted_receipt'],receipt)
            self.assertIsNone(final['physical_intake_id']);self.assertIsNone(final['physical_manifest_hash'])
            with database.transaction() as db:
                self.assertEqual(db.execute('SELECT balance_cents FROM ss_customers WHERE tenant_id=%s AND id=%s',(tenant,customer)).fetchone()['balance_cents'],1000)
                for table in ('ss_credit_entries','ss_credit_lots'):
                    self.assertEqual(db.execute('SELECT count(*) AS n FROM '+table+' WHERE tenant_id=%s',(tenant,)).fetchone()['n'],1)
                self.assertEqual(db.execute('SELECT count(*) AS n FROM ss_intake_claims WHERE tenant_id=%s',(tenant,)).fetchone()['n'],0)
        finally:
            database.close()
            with psycopg.connect(self.url,sslmode='require') as connection:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
