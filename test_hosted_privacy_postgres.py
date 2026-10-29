"""Actual PostgreSQL export checks in disposable ss_test_<uuid> fixture schemas.

No application/merchant tables outside that schema and no provider requests.
"""
from datetime import timedelta
import json
import threading
import unittest
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb
from hosted.auth import Auth
from hosted.ledger import Ledger
from hosted.privacy import Privacy, DATASET_NAMES
from hosted.privacy_test_fixtures import request
from hosted.security import ServiceError,password_hash
from hosted.trades import Trades
from hosted.credit_lots import record_credit_lot
from test_hosted_privacy import decode
from test_hosted_settlement import order,returned_order,payment_refunds
from test_hosted_settlement_postgres import _PostgreSQLSettlementFixture
from test_hosted_trades import draft_data,acceptance_data


class PostgreSQLPrivacyAcceptance(_PostgreSQLSettlementFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fixture_password='Fictional-export-password-1234'
        cls.fixture_hash=password_hash(cls.fixture_password)

    def setUp(self):
        super().setUp()
        self.reconcile(order())
        self.settlement.reconcile_return(self.tenant,'sale-1',returned_order(),payment_refunds())
        with self.database.transaction() as db:
            owner=db.execute("SELECT id FROM ss_staff WHERE tenant_id=%s AND role='owner'",(self.tenant,)).fetchone()
            self.staff=owner['id'];self.email='fixture-owner-'+self.tenant.hex+'@example.invalid'
            db.execute('UPDATE ss_staff SET email=%s,password_hash=%s WHERE tenant_id=%s AND id=%s',
                       (self.email,self.fixture_hash,self.tenant,self.staff))
            self.slug=db.execute('SELECT slug FROM ss_tenants WHERE id=%s',(self.tenant,)).fetchone()['slug']
            db.execute("UPDATE ss_customers SET display_name='Fictional rights customer',contact='own-export@example.invalid' WHERE tenant_id=%s AND id=%s",(self.tenant,self.customer))
            for index in range(250):
                entry=uuid.uuid4();key='fixture-history-'+str(index)
                db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason,staff_id)
                              VALUES (%s,%s,%s,1,%s,'adjust_add',%s,%s,'Fictional export history',%s)''',
                           (entry,self.tenant,self.customer,4001+index,'request:'+key,'FICTIONAL-'+str(index),self.staff))
                db.execute('UPDATE ss_customers SET balance_cents=%s,version=version+1 WHERE tenant_id=%s AND id=%s',
                           (4001+index,self.tenant,self.customer))
                record_credit_lot(db,self.tenant,self.customer,entry)
                db.execute("INSERT INTO ss_requests(tenant_id,key,body_hash,result,operation) VALUES (%s,%s,%s,%s,'ledger')",
                           (self.tenant,key,'0'*64,Jsonb({'entry_id':str(entry),'balance_cents':4001+index})))
            self.other_customer=uuid.uuid4()
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,contact,square_customer_id,privacy_basis,balance_cents) VALUES (%s,%s,'OTHER-CUSTOMER-NAME','OTHER-CUSTOMER-CONTACT','other-square-customer','fixture-only',123)",
                       (self.other_customer,self.tenant))
            other_entry=uuid.uuid4()
            db.execute("INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason) VALUES (%s,%s,%s,123,123,'opening','other-entry','OTHER-CUSTOMER-REFERENCE','OTHER-CUSTOMER-REASON')",
                       (other_entry,self.tenant,self.other_customer))
            self.other_entry=other_entry
            foreign=uuid.uuid4();self.foreign_customer=uuid.uuid4()
            db.execute("INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,'OTHER-TENANT-NAME')",(foreign,'foreign-'+foreign.hex))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,contact,privacy_basis) VALUES (%s,%s,'FOREIGN-CUSTOMER-NAME','FOREIGN-CUSTOMER-CONTACT','fixture-only')",
                       (self.foreign_customer,foreign))
            db.execute("INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details) VALUES (%s,%s,'fixture-customer-note',%s,%s)",
                       (self.tenant,self.staff,str(self.customer),Jsonb({'kind':'fixture','access_token':'DO-NOT-EXPORT-SECRET'})))
            db.execute("INSERT INTO ss_audit(tenant_id,action,entity_id,details) VALUES (%s,'fixture-other-note',%s,%s)",
                       (self.tenant,str(self.other_customer),Jsonb({'customer_id':str(self.other_customer),'reference':'OTHER-CUSTOMER-AUDIT'})))
        self.auth=Auth(self.database,encryption_keys=Fernet.generate_key().decode())
        self.token,_=self.auth.login({'store_slug':self.slug,'email':self.email,'password':self.fixture_password},'127.0.0.1')
        self.actor=self.auth.resolve(self.token)
        self.service=Privacy(self.database,authorize_session=self.auth.resolve,proof_key='fictional-export-proof-key')
        trades=Trades(self.database)
        created=trades.create(self.actor,draft_data(customer_id=self.customer,request_key='fictional-trade-create'))
        accepted=trades.accept(self.actor,created['trade_id'],acceptance_data(self.customer,request_key='fictional-trade-accept'))
        self.trade_id=created['trade_id'];self.trade_hash=accepted['receipt_hash']
        no_network=patch('requests.sessions.Session.request',side_effect=AssertionError('Provider HTTP is forbidden in privacy acceptance.'))
        no_network.start();self.addCleanup(no_network.stop)

    def begin(self,actor=None,customer=None):
        selected=customer or self.customer
        return self.service.begin(actor or self.actor,selected,request(actor or self.actor,selected),self.token)

    def business_snapshot(self):
        with self.database.transaction() as db:
            customer=db.execute('SELECT display_name,contact,balance_cents,version FROM ss_customers WHERE tenant_id=%s AND id=%s',(self.tenant,self.customer)).fetchone()
            counts={table:db.execute('SELECT count(*) AS count FROM '+table+' WHERE tenant_id=%s',(self.tenant,)).fetchone()['count']
                    for table in ('ss_credit_entries','ss_credit_lots','ss_credit_lot_reservations','ss_reservations','ss_reservation_terms','ss_square_settlements','ss_credit_returns','ss_trade_receipts','ss_trade_receipt_lines','ss_requests')}
        return customer,counts

    def test_complete_all_linked_records_counts_receipts_and_no_credential_or_other_customer_leak(self):
        before=self.business_snapshot();output=self.begin();lines=list(output.iter_bytes())
        manifest,records,footer=decode(lines)
        self.assertEqual(footer['record_counts']['customer'],1)
        self.assertGreater(footer['record_counts']['credit_entries'],250)
        self.assertEqual(footer['record_counts']['idempotency_records'],252)
        for collection in ('credit_lots','lot_mutations','reservations','lot_reservations','reservation_terms','square_settlements','credit_returns','trade_receipts','trade_lines','origin_policies','audit'):
            self.assertGreater(footer['record_counts'][collection],0,collection)
        accepted=records['trade_receipts'][0]['accepted_receipt']
        self.assertEqual(accepted['customer']['id'],str(self.customer))
        self.assertEqual(records['trade_receipts'][0]['receipt_hash'],self.trade_hash)
        self.assertEqual(accepted['lines'][0]['identity']['name'],'Fictional card')
        body=b''.join(lines)
        for marker in ('OTHER-CUSTOMER','FOREIGN-CUSTOMER','DO-NOT-EXPORT-SECRET',self.fixture_hash,self.token):
            self.assertNotIn(marker.encode(),body)
        self.assertIn(b'own-export@example.invalid',body)
        self.assertEqual(self.business_snapshot(),before)
        with self.database.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) AS count FROM ss_audit WHERE tenant_id=%s AND action='customer-export-client-verified'",(self.tenant,)).fetchone()['count'],0)
        data={key:footer[key] for key in ('start_audit_id','content_chain_sha256','record_counts')}
        self.assertTrue(self.service.confirm(self.actor,output.id,data,self.token)['verified'])
        self.assertTrue(self.service.confirm(self.actor,output.id,data,self.token)['already_verified'])
        self.assertEqual(self.business_snapshot(),before)

    def test_repeatable_read_snapshot_excludes_concurrent_committed_credit(self):
        before=self.business_snapshot();output=self.begin();errors=[]
        def mutate():
            try:Ledger(self.database).adjust(self.actor,self.customer,{'request_key':'concurrent-export-write','amount_cents':1,'kind':'adjust_add',
                 'reference':'LATER-WRITE','reason':'Fictional concurrent snapshot test','version':before[0]['version']})
            except Exception as error:errors.append(error)
        worker=threading.Thread(target=mutate);worker.start();worker.join(timeout=10)
        self.assertFalse(worker.is_alive());self.assertFalse(errors)
        manifest,records,footer=decode(list(output.iter_bytes()))
        self.assertEqual(records['customer'][0]['balance_cents'],before[0]['balance_cents'])
        self.assertEqual(len(records['credit_entries']),before[1]['ss_credit_entries']-1)  # one other customer's entry excluded
        self.assertNotIn('LATER-WRITE',json.dumps(records))
        self.assertEqual(self.business_snapshot()[0]['balance_cents'],before[0]['balance_cents']+1)

    def test_logout_and_staff_revocation_during_stream_close_snapshot_and_never_confirm(self):
        for revoke in ('logout','disable'):
            if revoke=='disable':
                self.token,_=self.auth.login({'store_slug':self.slug,'email':self.email,'password':self.fixture_password},'127.0.0.1')
                self.actor=self.auth.resolve(self.token)
            output=self.begin();iterator=output.iter_bytes();header=next(iterator)
            if revoke=='logout':self.auth.logout(self.token)
            else:
                with self.database.transaction() as db:db.execute('UPDATE ss_staff SET enabled=FALSE WHERE tenant_id=%s AND id=%s',(self.tenant,self.staff))
            remaining=list(iterator)
            self.assertEqual(json.loads(remaining[-1])['kind'],'error')
            self.assertNotIn(b'own-export@example.invalid',b''.join(remaining))
            with self.database.transaction() as db:
                ended=db.execute("SELECT details FROM ss_audit WHERE tenant_id=%s AND entity_id=%s AND action='customer-export-stream-ended'",(self.tenant,str(output.id))).fetchone()
                self.assertEqual(ended['details']['outcome'],'interrupted')
                self.assertEqual(db.execute("SELECT count(*) AS count FROM ss_audit WHERE tenant_id=%s AND action='customer-export-client-verified'",(self.tenant,)).fetchone()['count'],0)

    def test_cancel_releases_snapshot_pool_and_export_slot_without_touching_records(self):
        before=self.business_snapshot();output=self.begin();iterator=output.iter_bytes();next(iterator);next(iterator);iterator.close()
        self.assertTrue(output.closed);self.assertIsNone(output.db)
        replacement=self.begin();replacement.close()
        self.assertEqual(self.business_snapshot(),before)
        with self.database.transaction() as db:
            self.assertTrue(db.execute('SELECT 1 AS ok').fetchone()['ok'])

    def test_role_tenant_recency_and_cross_customer_corruption_fail_without_data(self):
        with self.assertRaises(ServiceError):self.begin(actor=self.actor|{'role':'manager'})
        for customer in (self.other_customer,self.foreign_customer):
            if customer==self.other_customer:
                # A store owner can export a separately verified same-store
                # account, but cannot make it masquerade as the selected account.
                with self.assertRaises(ServiceError):self.service.begin(self.actor,customer,request(self.actor,self.customer),self.token)
            else:
                with self.assertRaises(ServiceError) as error:self.begin(customer=customer)
                self.assertEqual(error.exception.status,404)
        actual_clock=self.service.wall_clock
        self.service.wall_clock=lambda:self.actor['session_created_at']+timedelta(minutes=16)
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,401)
        self.service.wall_clock=actual_clock
        with self.database.transaction() as db:
            db.execute('''INSERT INTO ss_credit_lots(id,tenant_id,customer_id,source_entry_id,issued_cents,remaining_cents)
                          VALUES (%s,%s,%s,%s,123,123)''',(uuid.uuid4(),self.tenant,self.customer,self.other_entry))
        self.actor=self.auth.resolve(self.token)
        with self.assertRaises(ServiceError) as error:self.begin()
        self.assertEqual(error.exception.status,409)


if __name__=='__main__':unittest.main()
