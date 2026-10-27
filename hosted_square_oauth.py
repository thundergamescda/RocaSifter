"""Hosted Square OAuth domain. Never configure a client secret on a store PC.

Requires deployment behind HTTPS and authenticated tenant sessions. This module
does not expose a public callback or mint staff sessions on its own.
"""
import hashlib
import hmac
import json
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
from station_store import StationError, Vault


class SquareOAuth:
    SCOPES=('MERCHANT_PROFILE_READ','ITEMS_READ','ITEMS_WRITE','INVENTORY_READ',
            'INVENTORY_WRITE','CUSTOMERS_READ','ORDERS_READ','PAYMENTS_READ')

    def __init__(self,store,*,client_id,client_secret,redirect_uri,environment,request,clock=time.time):
        if environment not in ('sandbox','production'):raise StationError('Invalid OAuth environment.')
        uri=urlsplit(redirect_uri)
        if uri.scheme!='https' or not uri.netloc or uri.username or uri.fragment or uri.query:
            raise StationError('Configure a registered HTTPS callback without query parameters.')
        if not client_id or not client_secret:raise StationError('Configure OAuth on the hosted service.')
        self.store,self.client_id,self.secret,self.redirect=store,client_id,client_secret,redirect_uri
        self.environment,self.request,self.clock=environment,request,clock
        self.base='https://connect.squareupsandbox.com' if environment=='sandbox' else 'https://connect.squareup.com'
        self.lock=threading.RLock()
        with store.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS oauth_states (digest TEXT PRIMARY KEY, tenant TEXT, session TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS oauth_connections (tenant TEXT PRIMARY KEY, merchant TEXT, environment TEXT);''')

    def _vault(self,tenant):
        root=self.store.root/'oauth'/hashlib.sha256(tenant.encode()).hexdigest()
        root.mkdir(parents=True,exist_ok=True)
        return Vault(root)

    def begin(self,tenant,session):
        if not tenant or not session:raise StationError('Authenticate the store owner before connecting Square.')
        state=secrets.token_urlsafe(32)
        with self.store.db() as db:
            db.execute('DELETE FROM oauth_states WHERE expires<?',(self.clock(),))
            db.execute('INSERT INTO oauth_states VALUES (?,?,?,?)',(hashlib.sha256(state.encode()).hexdigest(),tenant,hashlib.sha256(session.encode()).hexdigest(),self.clock()+300))
        return self.base+'/oauth2/authorize?'+urlencode({'client_id':self.client_id,'scope':' '.join(self.SCOPES),
                   'state':state,'redirect_uri':self.redirect,'session':'false'})

    def complete(self,*,tenant,session,state,code,error=None):
        with self.lock:
            with self.store.db() as db:
                db.execute('BEGIN IMMEDIATE')
                digest=hashlib.sha256(str(state).encode()).hexdigest()
                row=db.execute('SELECT * FROM oauth_states WHERE digest=?',(digest,)).fetchone()
                if not row or row['tenant']!=tenant or not hmac.compare_digest(row['session'],hashlib.sha256(session.encode()).hexdigest()) or row['expires']<self.clock():
                    raise StationError('Authorization state expired or does not match this store session.')
                db.execute('DELETE FROM oauth_states WHERE digest=?',(digest,))
            if error or not code:raise StationError('Square authorization was declined. Start a new connection.')
            result=self._token({'grant_type':'authorization_code','code':code,'redirect_uri':self.redirect})
            if not result.get('merchant_id'):raise StationError('Square did not return a merchant identity.')
            with self.store.db() as db:
                previous=db.execute('SELECT merchant FROM oauth_connections WHERE tenant=?',(tenant,)).fetchone()
                if previous and previous['merchant']!=result['merchant_id']:
                    raise StationError('This store is already connected to a different Square merchant.')
            self._vault(tenant).set(json.dumps(result))
            with self.store.db() as db:
                db.execute('INSERT OR REPLACE INTO oauth_connections VALUES (?,?,?)',(tenant,result['merchant_id'],self.environment))
            return self.status(tenant)

    def _token(self,payload):
        try:
            response=self.request(self.base+'/oauth2/token',json=payload|{'client_id':self.client_id,'client_secret':self.secret},timeout=20)
            response.raise_for_status();result=response.json()
            if result.get('errors') or not all(result.get(k) for k in ('access_token','refresh_token','expires_at')):
                raise ValueError()
            return result
        except Exception:
            raise StationError('Square authorization could not be confirmed. Reconnect; credentials were not exposed.') from None

    def refresh(self,tenant):
        with self.lock:
            if not self.status(tenant)['connected']:raise StationError('Connect Square first.')
            vault=self._vault(tenant);old=json.loads(vault.read())
            new=self._token({'grant_type':'refresh_token','refresh_token':old['refresh_token']})
            if new.get('merchant_id') and new['merchant_id']!=old['merchant_id']:raise StationError('Merchant changed during refresh.')
            vault.set(json.dumps(old|new))
            return self.status(tenant)

    def status(self,tenant):
        with self.store.db() as db:
            row=db.execute('SELECT merchant,environment FROM oauth_connections WHERE tenant=?',(tenant,)).fetchone()
        return {'connected':bool(row),'merchant_id':row['merchant'] if row else None,
                'environment':self.environment,'authentication':'oauth','hostingRequired':True}
