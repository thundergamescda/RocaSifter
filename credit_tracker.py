"""Transactional credit domain for the local pilot; no Square payment writes.

The ledger is deliberately separate from HTTP/UI so a hosted service can own it.
Local pilot balances are NOT shared across computers.
"""
import csv
from decimal import Decimal, InvalidOperation
import io
import json
import uuid
from station_store import StationError, now


def cents(value):
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value <= 0 or value > 1_000_000 or value * 100 != (value * 100).to_integral():
            raise ValueError()
        return int(value * 100)
    except (ValueError, InvalidOperation):
        raise StationError('Enter a positive amount with at most two decimal places.') from None


class Tracker:
    def __init__(self, service):
        self.service, self.store = service, service.store
        with self.store.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS credit_customers (
              id TEXT PRIMARY KEY, name TEXT, contact TEXT, square_reference TEXT,
              balance_cents INTEGER NOT NULL DEFAULT 0 CHECK(balance_cents>=0), version INTEGER NOT NULL DEFAULT 0);
            CREATE UNIQUE INDEX IF NOT EXISTS credit_square_customer ON credit_customers(square_reference) WHERE square_reference<>'';
            CREATE TABLE IF NOT EXISTS credit_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT, at TEXT, kind TEXT,
              cents INTEGER, balance_cents INTEGER, reference TEXT, note TEXT, source TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS credit_requests (key TEXT PRIMARY KEY, body TEXT, response TEXT);
            CREATE TABLE IF NOT EXISTS credit_reservations (
              id TEXT PRIMARY KEY, customer_id TEXT, cents INTEGER, state TEXT, order_id TEXT UNIQUE, at TEXT);
            ''')

    def _begin(self, db):
        db.execute('BEGIN IMMEDIATE')

    def _customer(self, db, ident):
        row = db.execute('SELECT * FROM credit_customers WHERE id=?', (ident,)).fetchone()
        if not row:
            raise StationError('Customer not found.')
        held = db.execute("SELECT COALESCE(SUM(cents),0) FROM credit_reservations WHERE customer_id=? AND state='reserved'", (ident,)).fetchone()[0]
        return dict(row) | {'balance': row['balance_cents']/100, 'reserved': held/100,
                            'available_cents':row['balance_cents']-held,
                            'available': (row['balance_cents']-held)/100}

    def _account(self, db, ident):
        entries = [dict(r) for r in db.execute('SELECT * FROM credit_entries WHERE customer_id=? ORDER BY id DESC LIMIT 100',(ident,))]
        for e in entries:
            e.update(amount=e.pop('cents')/100, balance_after=e.pop('balance_cents')/100)
        return {'customer':self._customer(db,ident), 'entries':entries, 'scope':'local-pilot'}

    def account(self, ident):
        with self.store.db() as db:
            return self._account(db,ident)

    def search(self, query=''):
        with self.store.db() as db:
            rows=db.execute('SELECT id FROM credit_customers WHERE instr(lower(name||contact||square_reference),?)>0 ORDER BY name LIMIT 100',(str(query).lower(),))
            return [self._customer(db,r['id']) for r in rows]

    def _request(self, db, key, body):
        if not isinstance(key,str) or not 1<=len(key)<=150:
            raise StationError('A request ID is required.')
        signature=json.dumps(body,sort_keys=True,separators=(',',':'))
        existing=db.execute('SELECT * FROM credit_requests WHERE key=?',(key,)).fetchone()
        if existing and existing['body']!=signature:
            raise StationError('This request ID was already used for a different operation.')
        return signature, json.loads(existing['response']) if existing else None

    def _save_request(self,db,key,signature,result):
        db.execute('INSERT INTO credit_requests VALUES (?,?,?)',(key,signature,json.dumps(result)))
        return result

    def add_customer(self,data):
        name=str(data.get('name','')).strip()
        if not name or len(name)>200:
            raise StationError('Enter a customer name, at most 200 characters.')
        body={k:str(data.get(k,'')).strip() for k in ('name','contact','square_reference')}
        if len(body['contact'])>320 or len(body['square_reference'])>150:
            raise StationError('Customer details are too long.')
        with self.store.db() as db:
            self._begin(db)
            sig,old=self._request(db,data.get('requestId'),body)
            if old:return old
            if body['square_reference'] and db.execute('SELECT 1 FROM credit_customers WHERE square_reference=?',(body['square_reference'],)).fetchone():
                raise StationError('This Square customer is already linked.')
            ident=uuid.uuid4().hex
            db.execute('INSERT INTO credit_customers(id,name,contact,square_reference) VALUES (?,?,?,?)',(ident,name,body['contact'],body['square_reference']))
            return self._save_request(db,data['requestId'],sig,self._customer(db,ident))

    def _entry(self,db,ident,amount,kind,reference,note,source):
        c=self._customer(db,ident)
        if c['available_cents'] + amount < 0:
            raise StationError('Insufficient available credit, including active reservations.')
        balance=c['balance_cents']+amount
        db.execute('UPDATE credit_customers SET balance_cents=?,version=version+1 WHERE id=?',(balance,ident))
        db.execute('INSERT INTO credit_entries(customer_id,at,kind,cents,balance_cents,reference,note,source) VALUES (?,?,?,?,?,?,?,?)',
                   (ident,now(),kind,amount,balance,reference,note,source))

    def transaction(self,data):
        # Password/session authorization belongs to the HTTP boundary, not persisted payloads.
        body={k:data.get(k) for k in ('customerId','kind','amount','receiptId','reference','note','version')}
        ident,kind=body['customerId'],body['kind']
        with self.store.db() as db:
            self._begin(db)
            sig,old=self._request(db,data.get('requestId'),body)
            if old:return self._account(db,ident)
            c=self._customer(db,ident)
            if body['version']!=c['version']:
                raise StationError('The balance changed. Reload the customer before continuing.')
            if kind not in ('opening','adjust_add','adjust_remove','trade','redeem'):
                raise StationError('Choose a supported credit operation.')
            reference=str(body['reference'] or '').strip()
            note=str(body['note'] or '').strip()
            if not reference or len(reference)>500 or len(note)>1000:
                raise StationError('A reference is required; keep notes under 1000 characters.')
            amount=cents(body['amount']) if kind!='trade' else 0
            source='request:'+data['requestId']
            if kind=='trade':
                receipt=self.service.store.receipt(str(body['receiptId']))
                purchase=receipt.get('purchase') or {}
                if receipt.get('shared_trade',{}).get('state') not in (None,'cancelled') or purchase.get('scope')=='hosted':
                    raise StationError('This trade belongs to shared credit. It cannot issue credit to a local wallet.')
                if purchase.get('type')!='credit':
                    raise StationError('Choose an accepted store-credit trade.')
                amount=cents(purchase['amount']); reference=receipt['id'];source='trade:'+receipt['id']
                if db.execute('SELECT 1 FROM credit_entries WHERE source=?',(source,)).fetchone():
                    raise StationError('This trade credit has already been issued.')
            if kind=='opening' and db.execute('SELECT 1 FROM credit_entries WHERE customer_id=?',(ident,)).fetchone():
                raise StationError('Opening balances are only allowed before the first entry.')
            if kind in ('adjust_add','adjust_remove') and not note:
                raise StationError('Enter a reason for the manager adjustment.')
            self._entry(db,ident,-amount if kind in ('redeem','adjust_remove') else amount,kind,reference,note,source)
            if kind=='trade':
                receipt['tracked_credit']={'customer_id':ident,'amount':amount/100}
                receipt['updated']=now();receipt['revision']=receipt.get('revision',0)+1
                db.execute('UPDATE receipts SET body=?,updated=? WHERE id=?',(json.dumps(receipt),receipt['updated'],receipt['id']))
            result=self._account(db,ident)
            return self._save_request(db,data['requestId'],sig,result)

    def reserve(self,ident,amount,key,order_id):
        amount=cents(amount)
        if not isinstance(order_id,str) or not order_id.strip():
            raise StationError('Link a checkout order before reserving credit.')
        with self.store.db() as db:
            self._begin(db)
            sig,old=self._request(db,key,{'customer':ident,'cents':amount,'order':order_id})
            if old:return old
            c=self._customer(db,ident)
            if amount>c['available_cents']:
                raise StationError('Insufficient available credit.')
            if not c['square_reference']:
                raise StationError('Link the Square customer before checkout.')
            if db.execute('SELECT 1 FROM credit_reservations WHERE order_id=?',(order_id,)).fetchone():
                raise StationError('This order already has a reservation.')
            db.execute("INSERT INTO credit_reservations VALUES (?,?,?,'reserved',?,?)",(key,ident,amount,order_id,now()))
            return self._save_request(db,key,sig,{'reservationId':key,'amount':amount/100,'state':'reserved'})

    def reconcile_order(self,order,*,location_id,discount_id):
        """Accept only authoritative adapter output, never an unauthenticated webhook body.

        Not exposed to browsers. A future signed hosted adapter must retrieve the order.
        """
        with self.store.db() as db:
            self._begin(db)
            row=db.execute('SELECT * FROM credit_reservations WHERE order_id=?',(order.get('id'),)).fetchone()
            if not row:raise StationError('No reservation for this order.')
            c=self._customer(db,row['customer_id'])
            if not location_id or not discount_id or order.get('location_id')!=location_id or order.get('customer_id')!=c['square_reference']:
                raise StationError('Order customer or location does not match the reservation.')
            if row['state']!='reserved':return self._account(db,c['id'])
            state=order.get('state')
            if state=='CANCELED':
                db.execute("UPDATE credit_reservations SET state='released' WHERE id=?",(row['id'],))
                return self._account(db,c['id'])
            if state!='COMPLETED':return self._account(db,c['id'])
            matches=[d for d in order.get('discounts',[]) if d.get('catalog_object_id')==discount_id]
            if len(matches)!=1:raise StationError('A unique tracked discount is required.')
            applied=matches[0].get('applied_money',{})
            amount=applied.get('amount')
            if applied.get('currency')!='USD' or isinstance(amount,bool) or not isinstance(amount,int) or not 0<amount<=row['cents']:
                raise StationError('Tracked discount amount does not match the reservation.')
            db.execute("UPDATE credit_reservations SET state='consumed' WHERE id=?",(row['id'],))
            self._entry(db,c['id'],-amount,'discount_redeem',order['id'],'Verified completed order','order:'+order['id'])
            return self._account(db,c['id'])

    def restore(self,order_id,amount,refund_id,note):
        amount=cents(amount)
        if not refund_id or not note:raise StationError('Verified refund reference and reason are required.')
        with self.store.db() as db:
            self._begin(db)
            original=db.execute("SELECT * FROM credit_entries WHERE source=?",('order:'+order_id,)).fetchone()
            if not original:raise StationError('Original redemption not found.')
            source='refund:'+str(refund_id)
            old=db.execute('SELECT * FROM credit_entries WHERE source=?',(source,)).fetchone()
            if old:
                if old['cents']!=amount or old['reference']!=order_id:raise StationError('Refund reference reused.')
                return self._account(db,original['customer_id'])
            restored=db.execute("SELECT COALESCE(SUM(cents),0) FROM credit_entries WHERE kind='refund' AND reference=?",(order_id,)).fetchone()[0]
            if restored+amount>-original['cents']:raise StationError('Refund exceeds the original redeemed credit.')
            self._entry(db,original['customer_id'],amount,'refund',order_id,note,source)
            return self._account(db,original['customer_id'])

    def export(self,ident):
        stream=io.StringIO();writer=csv.writer(stream)
        writer.writerow(['Date','Kind','Amount','Balance','Reference','Note'])
        with self.store.db() as db:
            self._customer(db,ident)
            for row in db.execute('SELECT * FROM credit_entries WHERE customer_id=? ORDER BY id',(ident,)):
                safe=lambda x: "'"+x if isinstance(x,str) and x.startswith(('=','+','-','@','\t','\r')) else x
                writer.writerow([row['at'],row['kind'],f"{row['cents']/100:.2f}",f"{row['balance_cents']/100:.2f}",safe(row['reference']),safe(row['note'])])
        return stream.getvalue()
