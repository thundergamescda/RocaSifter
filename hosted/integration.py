"""Real PostgreSQL acceptance tests in a disposable, isolated schema.

Run only on the private hosting network with DATABASE_URL set. No Square calls.
The exact ss_test_<uuid> schema created here is dropped after testing; application
tables in the normal schema are not queried or modified.
"""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest
import uuid
from datetime import datetime, timezone, timedelta
import psycopg
from psycopg import sql
from .database import Database
from .auth import Auth
from .ledger import Ledger
from .security import ServiceError, password_hash


class PostgreSQLAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url=os.environ.get('DATABASE_URL')
        if not cls.url:raise unittest.SkipTest('DATABASE_URL is required for real private-network PostgreSQL tests.')
        cls.schema='ss_test_'+uuid.uuid4().hex
        with psycopg.connect(cls.url,sslmode='require') as db:
            db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
        cls.database=Database(cls.url,schema=cls.schema)
        cls.database.migrate()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls,'database'):cls.database.close()
        if hasattr(cls,'schema'):
            with psycopg.connect(cls.url,sslmode='require') as db:
                db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))

    def setUp(self):
        self.tenant,self.staff=uuid.uuid4(),uuid.uuid4()
        self.slug='fixture-'+self.tenant.hex[:12]
        self.actor={'tenant_id':self.tenant,'staff_id':self.staff,'role':'owner','slug':self.slug}
        with self.database.transaction() as db:
            db.execute('INSERT INTO ss_tenants(id,slug,name) VALUES (%s,%s,%s)',(self.tenant,self.slug,'Fictional store'))
            db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,%s,%s,'owner')",(self.staff,self.tenant,'owner@example.invalid',password_hash('fixture-password-1234')))
        self.auth=Auth(self.database);self.ledger=Ledger(self.database)
        created=self.ledger.create_customer(self.actor,{'request_key':'customer','display_name':'Fictional customer','permission_confirmed':True,'square_customer_id':'fixture-square-customer'})
        self.customer=created['customer_id']
        self.ledger.adjust(self.actor,self.customer,{'request_key':'opening','amount_cents':5000,'kind':'opening','reference':'FIXTURE OPENING','reason':'Fictional test opening','version':0})

    def test_schema_ready_tls_and_no_public_data_dependency(self):
        self.assertTrue(self.database.ready())
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT current_schema() AS name').fetchone()['name'],self.schema)
            encrypted=db.execute('SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
            self.assertTrue(encrypted['ssl'])

    def test_customer_permission_tenant_isolation_and_adjustment_replay(self):
        stranger=self.actor|{'tenant_id':uuid.uuid4()}
        with self.assertRaises(ServiceError):self.ledger.account(stranger,self.customer)
        with self.assertRaises(ServiceError):self.ledger.create_customer(self.actor,{'request_key':'no-consent','display_name':'Not permitted'})
        request={'request_key':'add1','amount_cents':250,'kind':'adjust_add','reference':'TEST','reason':'Test correction','version':1}
        one=self.ledger.adjust(self.actor,self.customer,request)
        two=self.ledger.adjust(self.actor,self.customer,request)
        self.assertEqual(one,two);self.assertEqual(self.ledger.account(self.actor,self.customer)['customer']['balance_cents'],5250)
        with self.assertRaises(ServiceError):self.ledger.adjust(self.actor,self.customer,request|{'amount_cents':251})
        with self.assertRaises(ServiceError):self.ledger.adjust(self.actor,self.customer,request|{'request_key':'stale'})

    def test_two_pool_connections_prevent_double_reservation(self):
        with self.database.transaction() as db:
            from .test_fixtures import approved_checkout_fixture
            binding=approved_checkout_fixture(db,self.tenant,self.customer)
        def reserve(key):
            try:
                order='order-'+key
                return self.ledger.reserve(self.actor,self.customer,{'amount_cents':4000,'order_id':order,'request_key':key},checkout_binding=binding|{'order_id':order})
            except ServiceError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,['till-a','till-b']))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.ledger.account(self.actor,self.customer)['customer']['available_cents'],1000)
        with self.assertRaises(ServiceError):
            self.ledger.adjust(self.actor,self.customer,{'request_key':'remove','amount_cents':2000,'kind':'adjust_remove','reference':'TEST','reason':'Must not consume reserved value','version':1})

    def test_checkout_disabled_until_approval_and_cashier_role_limited(self):
        with self.assertRaises(ServiceError):self.ledger.reserve(self.actor,self.customer,{'amount_cents':100,'request_key':'hold'})
        cashier=self.actor|{'role':'cashier'}
        with self.assertRaises(ServiceError):
            self.ledger.adjust(cashier,self.customer,{'amount_cents':100,'kind':'adjust_add','reference':'TEST','reason':'Unauthorized','request_key':'unauthorized','version':1})

    def test_customer_search_finds_matches_beyond_first_hundred_and_stays_tenant_scoped(self):
        target=uuid.uuid4()
        with self.database.transaction() as db:
            for index in range(115):
                db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,privacy_basis) VALUES (%s,%s,%s,'fixture')",
                           (uuid.uuid4(),self.tenant,f'Fixture {index:03d}'))
            db.execute("INSERT INTO ss_customers(id,tenant_id,display_name,contact,privacy_basis) VALUES (%s,%s,%s,%s,'fixture')",
                       (target,self.tenant,'Zebra fictional customer','literal %_ reference'))
        self.assertEqual(len(self.ledger.customers(self.actor)),100)
        matches=self.ledger.customers(self.actor,'ZEBRA')
        self.assertEqual([c['id'] for c in matches],[target])
        self.assertEqual(len(self.ledger.customers(self.actor,'%_')),1)
        self.assertEqual(self.ledger.customers(self.actor|{'tenant_id':uuid.uuid4()},'ZEBRA'),[])
        with self.assertRaises(ServiceError):self.ledger.customers(self.actor,'x'*201)

    def test_unverified_trade_amount_cannot_be_issued_through_adjustment(self):
        with self.assertRaises(ServiceError):
            self.ledger.adjust(self.actor,self.customer,{'request_key':'unverified-trade','amount_cents':500,'kind':'trade_credit',
                'reference':'client-declared-trade','reason':'Not an accepted hosted receipt','version':1})
        self.assertEqual(self.ledger.account(self.actor,self.customer)['customer']['balance_cents'],5000)

    def test_login_invite_expiry_replay_and_session_revocation(self):
        token,info=self.auth.login({'store_slug':self.slug,'email':'owner@example.invalid','password':'fixture-password-1234'},'fixture-address')
        actor=self.auth.resolve(token)
        self.assertEqual(actor['tenant_id'],self.tenant)
        self.assertEqual(info['role'],'owner')
        invite=self.auth.invite(actor,{'email':'cashier@example.invalid','role':'cashier'})
        self.auth.accept_invite({'invite_token':invite['invite_token'],'email':'cashier@example.invalid','password':'fixture-cashier-password'})
        with self.assertRaises(ServiceError):self.auth.accept_invite({'invite_token':invite['invite_token'],'email':'cashier@example.invalid','password':'fixture-cashier-password'})
        staff_token,_=self.auth.login({'store_slug':self.slug,'email':'cashier@example.invalid','password':'fixture-cashier-password'},'fixture-address')
        staff=self.auth.resolve(staff_token)
        staff_rows=self.auth.list_staff(actor)
        self.assertEqual({row['email'] for row in staff_rows},{'owner@example.invalid','cashier@example.invalid'})
        self.assertTrue(all('password_hash' not in row for row in staff_rows))
        self.assertEqual(self.auth.list_staff(actor|{'tenant_id':uuid.uuid4()}),[])
        with self.assertRaises(ServiceError):self.auth.list_staff(staff)
        with self.assertRaises(ServiceError):self.auth.invite(staff,{'email':'other@example.invalid','role':'manager'})
        self.auth.disable_staff(actor,staff['staff_id'])
        with self.assertRaises(ServiceError):self.auth.resolve(staff_token)
        self.auth.logout(token)
        with self.assertRaises(ServiceError):self.auth.resolve(token)

    def test_failed_login_throttles_and_never_creates_sessions(self):
        for _ in range(5):
            with self.assertRaises(ServiceError):self.auth.login({'store_slug':self.slug,'email':'owner@example.invalid','password':'wrong'},'fixture-address')
        with self.assertRaises(ServiceError) as error:
            self.auth.login({'store_slug':self.slug,'email':'owner@example.invalid','password':'fixture-password-1234'},'fixture-address')
        self.assertEqual(error.exception.status,429)
        with self.database.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) AS count FROM ss_sessions WHERE tenant_id=%s',(self.tenant,)).fetchone()['count'],0)


if __name__=='__main__':unittest.main(verbosity=2)
