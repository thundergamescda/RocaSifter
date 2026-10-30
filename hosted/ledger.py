"""Tenant-scoped credit operations serialized with PostgreSQL row locks."""
import hashlib
import json
import uuid
from psycopg.types.json import Jsonb
from .security import ServiceError, amount, identifier, provider_id
from .database import audit


def require_role(actor,*roles):
    if actor['role'] not in roles:raise ServiceError('Your store role cannot perform this action.',403)


class Ledger:
    def __init__(self,database):self.database=database

    def _customer(self,db,tenant,customer_id,lock=False):
        try:customer_id=uuid.UUID(str(customer_id))
        except ValueError:raise ServiceError('Customer ID is invalid.') from None
        row=db.execute('SELECT * FROM ss_customers WHERE tenant_id=%s AND id=%s'+(' FOR UPDATE' if lock else ''),(tenant,customer_id)).fetchone()
        if not row:raise ServiceError('Customer not found.',404)
        reserved=db.execute("SELECT COALESCE(SUM(amount_cents),0) AS held FROM ss_reservations WHERE tenant_id=%s AND customer_id=%s AND state IN ('reserved','exception')",(tenant,customer_id)).fetchone()['held']
        return dict(row)|{'reserved_cents':int(reserved),'available_cents':row['balance_cents']-int(reserved)}

    def _begin_request(self,db,tenant,key,body):
        identifier(key,'Request key')
        hashed=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        # Serialize identical request keys, including the gap before first insertion.
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(tenant)+':'+key,))
        old=db.execute('SELECT * FROM ss_requests WHERE tenant_id=%s AND key=%s',(tenant,key)).fetchone()
        if old and old['body_hash']!=hashed:raise ServiceError('Request key was reused with different details.',409)
        return hashed,old['result'] if old else None

    def _finish(self,db,tenant,key,hashed,result,*,operation='ledger'):
        if operation not in ('ledger','trade'):raise ValueError('Unsupported request operation.')
        db.execute('INSERT INTO ss_requests(tenant_id,key,body_hash,result,operation) VALUES (%s,%s,%s,%s,%s)',(tenant,key,hashed,Jsonb(result),operation))
        return result

    def customers(self,actor,query=''):
        if not isinstance(query,str) or len(query)>200:raise ServiceError('Keep customer searches under 200 characters.')
        with self.database.transaction() as db:
            rows=db.execute("SELECT id FROM ss_customers WHERE tenant_id=%s AND strpos(lower(display_name||' '||contact||' '||coalesce(square_customer_id,'')),lower(%s))>0 ORDER BY display_name,id LIMIT 100",(actor['tenant_id'],query)).fetchall()
            return [self._customer(db,actor['tenant_id'],row['id']) for row in rows]

    def account(self,actor,customer_id):
        with self.database.transaction() as db:
            customer=self._customer(db,actor['tenant_id'],customer_id)
            entries=db.execute('SELECT id,amount_cents,balance_after_cents,kind,reference,reason,at FROM ss_credit_entries WHERE tenant_id=%s AND customer_id=%s ORDER BY at DESC,id DESC LIMIT 100',
                               (actor['tenant_id'],customer['id'])).fetchall()
            return {'customer':customer,'entries':entries,'scope':'hosted'}

    def request_status(self,actor,key):
        identifier(key,'Request key')
        with self.database.transaction() as db:
            # Same lock as _begin_request: wait for an in-flight ledger commit
            # before telling a desktop whether its uncertain action completed.
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(str(actor['tenant_id'])+':'+key,))
            row=db.execute('SELECT result,operation FROM ss_requests WHERE tenant_id=%s AND key=%s',(actor['tenant_id'],key)).fetchone()
            if row and row['operation']=='trade':require_role(actor,'owner','manager','buyer')
            return {'found':bool(row),'result':row['result'] if row else None}

    def create_customer(self,actor,data):
        name=str(data.get('display_name','')).strip();contact=str(data.get('contact','')).strip()
        if not 1<=len(name)<=200 or len(contact)>320:raise ServiceError('Enter a name and keep contact details under 320 characters.')
        if data.get('permission_confirmed') is not True:raise ServiceError('Confirm authority to record this customer for store credit.')
        square=data.get('square_customer_id') or None
        if square:square=provider_id(square,'Square customer ID')
        ident=str(uuid.uuid4());tenant=actor['tenant_id']
        body={'name':name,'contact':contact,'square':square,'purpose':'store-credit-account'}
        with self.database.transaction() as db:
            hashed,old=self._begin_request(db,tenant,data.get('request_key'),body)
            if old:return old
            db.execute('INSERT INTO ss_customers(id,tenant_id,display_name,contact,square_customer_id,privacy_basis) VALUES (%s,%s,%s,%s,%s,%s)',
                       (ident,tenant,name,contact,square,'staff-confirmed-customer-credit-purpose'))
            audit(db,tenant,actor['staff_id'],'credit-customer-created',ident)
            return self._finish(db,tenant,data['request_key'],hashed,{'customer_id':ident})

    def adjust(self,actor,customer_id,data):
        require_role(actor,'owner','manager')
        value=amount(data.get('amount_cents'));kind=data.get('kind')
        if kind=='trade_credit':raise ServiceError('Trade credit must come from an explicitly accepted hosted trade receipt; it cannot be entered as a manual adjustment.',409)
        if kind not in ('opening','adjust_add','adjust_remove'):raise ServiceError('Select a supported credit entry.')
        reason=str(data.get('reason','')).strip();reference=str(data.get('reference','')).strip()
        if not 3<=len(reason)<=1000 or not 1<=len(reference)<=200:raise ServiceError('A reason and reference are required.')
        tenant=actor['tenant_id'];source='trade:'+reference if kind=='trade_credit' else 'request:'+str(data.get('request_key'))
        with self.database.transaction() as db:
            hashed,old=self._begin_request(db,tenant,data.get('request_key'),{'customer':str(customer_id),'amount':value,'kind':kind,'reason':reason,'reference':reference})
            if old:return old
            c=self._customer(db,tenant,customer_id,lock=True)
            if data.get('version')!=c['version']:raise ServiceError('The balance changed. Refresh before adjusting.',409)
            if kind=='opening' and db.execute('SELECT 1 FROM ss_credit_entries WHERE tenant_id=%s AND customer_id=%s',(tenant,c['id'])).fetchone():
                raise ServiceError('An opening balance can only be the first entry.',409)
            signed=-value if kind=='adjust_remove' else value
            if c['available_cents']+signed<0:raise ServiceError('Insufficient available credit, including reservations.',409)
            if db.execute('SELECT 1 FROM ss_credit_entries WHERE tenant_id=%s AND source_key=%s',(tenant,source)).fetchone():
                raise ServiceError('This source has already issued credit.',409)
            balance=c['balance_cents']+signed;entry=str(uuid.uuid4())
            db.execute('UPDATE ss_customers SET balance_cents=%s,version=version+1 WHERE tenant_id=%s AND id=%s',(balance,tenant,c['id']))
            db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason,staff_id)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',(entry,tenant,c['id'],signed,balance,kind,source,reference,reason,actor['staff_id']))
            from .credit_lots import record_credit_lot,debit_available_lots
            if signed>0:record_credit_lot(db,tenant,c['id'],entry)
            else:debit_available_lots(db,tenant,c['id'],entry)
            audit(db,tenant,actor['staff_id'],'credit-adjusted',c['id'],{'entry_id':entry,'amount_cents':signed,'kind':kind})
            return self._finish(db,tenant,data['request_key'],hashed,{'entry_id':entry,'balance_cents':balance,'version':c['version']+1})

    def reserve(self,actor,customer_id,data,*,checkout_binding=None):
        value=amount(data.get('amount_cents'));tenant=actor['tenant_id']
        order=data.get('order_id') or None
        if order:order=provider_id(order,'Square order ID')
        with self.database.transaction() as db:
            hashed,old=self._begin_request(db,tenant,data.get('request_key'),{'customer':str(customer_id),'amount':value,'order':order,'binding':checkout_binding})
            if old:return old
            store=db.execute('SELECT * FROM ss_tenants WHERE id=%s',(tenant,)).fetchone()
            if not store['credit_checkout_enabled']:raise ServiceError('Credit checkout is disabled until tax and Square device approval.',409)
            if not isinstance(checkout_binding,dict) or not order or checkout_binding.get('order_id')!=order:
                raise ServiceError('A verified server checkout binding is required. Browser-supplied credit terms are not accepted.',409)
            c=self._customer(db,tenant,customer_id,lock=True)
            if not c['square_customer_id']:raise ServiceError('Link this customer to Square before checkout.')
            if c['available_cents']<value:raise ServiceError('Insufficient available credit.',409)
            ident=str(uuid.uuid4())
            db.execute("INSERT INTO ss_reservations(id,tenant_id,customer_id,amount_cents,state,order_id,request_key,staff_id) VALUES (%s,%s,%s,%s,'reserved',%s,%s,%s)",
                       (ident,tenant,c['id'],value,order,data['request_key'],actor['staff_id']))
            from .settlement import record_reservation_terms
            record_reservation_terms(db,tenant,ident,checkout_binding,reserved_cents=value,customer_id=c['id'])
            audit(db,tenant,actor['staff_id'],'credit-reserved',ident,{'amount_cents':value})
            return self._finish(db,tenant,data['request_key'],hashed,{'reservation_id':ident,'state':'reserved','amount_cents':value})
