"""Fictional bounded-cursor fixtures for export tests and loopback Chrome only."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import re
import uuid
from .privacy import DATASET_NAMES
from .security import ServiceError


class Result:
    def __init__(self,row=None):self.row=row
    def fetchone(self):return self.row
    def fetchall(self):raise AssertionError('Export must not fetch all result rows into memory.')


class Cursor:
    def __init__(self,connection,name):self.connection=connection;self.name=name;self.rows=[];self.offset=0;self.itersize=None;self.closed=False
    def __enter__(self):self.connection.database.open_cursors+=1;return self
    def __exit__(self,*_):self.closed=True;self.connection.database.open_cursors-=1
    def execute(self,query,params):
        self.dataset=re.search(r'/\* privacy:([a-z_]+) \*/',query)[1]
        self.rows=deepcopy(self.connection.snapshot[self.dataset]);self.offset=0
    def fetchmany(self,size):
        self.connection.database.fetch_sizes.append(size)
        if self.connection.database.on_fetch:self.connection.database.on_fetch(self.dataset,self.offset)
        result=self.rows[self.offset:self.offset+size];self.offset+=len(result)
        if self.connection.database.drop_last and self.dataset=='credit_entries' and self.offset>=len(self.rows) and result:
            result=result[:-1]
        return result


class Connection:
    def __init__(self,database):self.database=database;self.snapshot=None
    def cursor(self,name):return Cursor(self,name)
    def execute(self,query,params=None):
        db=self.database;db.statements.append(query)
        if query.startswith('SET TRANSACTION'):
            self.snapshot=deepcopy(db.rows);return Result()
        if query.startswith('SET LOCAL') or 'pg_advisory_xact_lock' in query:return Result()
        if 'transaction_timestamp() AS snapshot_at' in query:
            return Result({'snapshot_at':datetime(2026,10,28,tzinfo=timezone.utc),'schema_version':4})
        if query.startswith('SELECT id,square_customer_id FROM ss_customers'):
            if str(params[0])!=str(db.actor['tenant_id']) or str(params[1])!=str(db.customer):return Result()
            return Result({'id':db.customer,'square_customer_id':'fixture-square-customer'})
        if query.startswith('/* privacy-integrity */'):return Result({'inconsistent':1} if db.integrity_failure else None)
        if query.startswith('/* privacy-count:'):
            name=re.search(r'privacy-count:([a-z_]+)',query)[1]
            return Result({'count':len(self.snapshot[name])})
        if query.startswith('INSERT INTO ss_audit'):
            tenant,staff,action,entity,details=params
            if db.fail_audit==action:raise RuntimeError('fictional audit failure')
            ident=len(db.audits)+1
            db.audits.append({'id':ident,'tenant_id':tenant,'staff_id':staff,'action':action,
                              'entity_id':entity,'details':deepcopy(details.obj)})
            return Result({'id':ident})
        if query.startswith('SELECT role,enabled FROM ss_staff'):
            return Result({'role':db.current_role,'enabled':db.session_active})
        if query.startswith('SELECT token_hash FROM ss_sessions'):
            return Result({'token_hash':db.actor['session_hash']}) if db.session_active and params[2]==db.actor['session_hash'] else Result()
        if 'FROM ss_audit WHERE' in query:
            ident,tenant,staff,entity=params
            action=re.search(r"action='([^']+)'",query)[1]
            matches=[r for r in db.audits if str(r['tenant_id'])==str(tenant) and str(r['staff_id'])==str(staff)
                     and r['entity_id']==entity and r['action']==action and (r['id']==ident if "action='customer-export-started'" in query else r['id']>ident)]
            return Result(matches[-1] if matches else None)
        raise AssertionError('Unexpected fixture SQL: '+query)


class Database:
    def __init__(self,entries=275):
        self._max_size=5;self.customer=uuid.uuid4()
        self.actor={'tenant_id':uuid.uuid4(),'staff_id':uuid.uuid4(),'role':'owner','slug':'fictional-store',
                    'session_hash':'fictional-session-hash','mfa_enabled':False,'mfa_authenticated':False,
                    'session_created_at':datetime.now(timezone.utc),'mfa_authenticated_at':datetime.now(timezone.utc)}
        self.session_active=True;self.current_role='owner';self.audits=[];self.statements=[];self.fetch_sizes=[]
        self.active_transactions=0;self.peak_transactions=0;self.open_cursors=0
        self.on_fetch=None;self.drop_last=False;self.integrity_failure=False;self.fail_audit=None
        self.rows={name:[] for name in DATASET_NAMES}
        self.rows['customer']=[{'id':self.customer,'display_name':'Fictional rights customer','contact':'fictional-rights@example.invalid',
                                'square_customer_id':'fixture-square-customer','privacy_basis':'fictional-purpose','balance_cents':entries,'version':entries}]
        self.rows['credit_entries']=[{'id':uuid.uuid4(),'customer_id':self.customer,'amount_cents':1,
                                      'balance_after_cents':index+1,'kind':'opening' if index==0 else 'adjust_add',
                                      'reference':'FICTIONAL-'+str(index),'reason':'Fictional export fixture'} for index in range(entries)]

    @contextmanager
    def transaction(self):
        self.active_transactions+=1;self.peak_transactions=max(self.peak_transactions,self.active_transactions)
        try:yield Connection(self)
        finally:self.active_transactions-=1

    def resolve(self,token):
        # Exercise a distinct short connection while the read snapshot is held.
        with self.transaction():
            if not self.session_active:raise ServiceError('Your session expired.',401)
            return self.actor|{'role':self.current_role}


def request(actor,customer):
    return {'expected_store':actor['slug'],'confirmed_customer_id':str(customer),'purpose':'customer_access',
            'verification_method':'existing_verified_channel','case_reference':'FICTIONAL-CASE-001',
            'identity_verified':True,'authority_confirmed':True,'scope_reviewed':True,'copy_handling_confirmed':True,
            'agent_authority_verified':False}
