"""Desktop relay to the fixed Stock & Sleeve service; staff tokens stay in the OS vault."""
import json
import hashlib
import hmac
import re
import threading
import time
import uuid
from urllib.parse import quote
import requests
from station_store import StationError, Vault

ORIGIN='https://stock-and-sleeve-api.onrender.com'
ROLES=('owner','manager','buyer','cashier')


class CloudError(StationError):
    def __init__(self,message,*,status=400,offline=False,pending=False,session_changed=False):
        super().__init__(message);self.status=status;self.offline=offline;self.pending=pending;self.session_changed=session_changed


class CloudClient:
    def __init__(self,root,*,transport=None,vault=None,pending_vault=None,clock=None):
        self.vault=vault or Vault(root)
        if vault is None:self.vault.path=self.vault.root/'cloud.session'
        self.pending_vault=pending_vault or Vault(root)
        if pending_vault is None:self.pending_vault.path=self.pending_vault.root/'cloud.pending'
        self.transport=transport or requests.Session()
        self.transport.trust_env=False
        self.clock=clock or time.time
        self.lock=threading.RLock()

    def _saved(self):
        raw=self.vault.read()
        if not raw:return None
        try:
            saved=json.loads(raw)
            if (not isinstance(saved,dict) or saved.get('origin')!=ORIGIN
                or not isinstance(saved.get('token'),str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',saved['token'])
                or saved.get('role') not in ROLES
                or not re.fullmatch(r'[a-f0-9]{32}',saved.get('session_id',''))
                or not isinstance(saved.get('expires_at'),(int,float))):raise ValueError()
            return saved
        except (ValueError,TypeError):
            raise CloudError('The saved cloud session is invalid. Sign out here and sign in again.',status=401) from None

    def _clear(self):
        # Only this dedicated staff-session vault is removed; Square receiving credentials are separate.
        if self.vault.path.exists():self.vault.path.unlink()

    @staticmethod
    def _expected(saved,expected):
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{32}',expected) or not saved or not hmac.compare_digest(saved['session_id'],expected)):
            raise CloudError('The signed-in workspace changed. Refresh before continuing.',status=409,session_changed=True)

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def _pending(self):
        raw=self.pending_vault.read()
        if not raw:return []
        try:
            value=json.loads(raw)
            if value.get('version')!=1 or value.get('origin')!=ORIGIN or not isinstance(value.get('operations'),list) or len(value['operations'])>50:raise ValueError()
            rows=value['operations']
            for row in rows:
                if (row.get('kind') not in ('customer','adjust') or not isinstance(row.get('body'),dict)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',row.get('request_key',''))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',row.get('store',''))
                    or row['body'].get('request_key')!=row['request_key']
                    or row.get('fingerprint')!=self._fingerprint(row['body'])):raise ValueError()
                if row['kind']=='adjust':self.customer_id(row.get('customer_id'))
            return rows
        except (ValueError,TypeError,AttributeError):
            raise CloudError('The saved pending-action journal needs recovery. Contact support before changing shared balances.',status=409,pending=True) from None

    def _save_pending(self,rows):
        if rows:self.pending_vault.set(json.dumps({'version':1,'origin':ORIGIN,'operations':rows},separators=(',',':')))
        elif self.pending_vault.path.exists():self.pending_vault.path.unlink()

    def _finish_pending(self,store,key):
        self._save_pending([row for row in self._pending() if (row['store'],row['request_key'])!=(store,key)])

    def _mutate(self,kind,body,customer_id=None,*,expected_session_id=None):
        # Persist the exact retry key/body before the network call. A crash or
        # lost response must not turn one correction into two separate actions.
        with self.lock:
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():raise CloudError('Sign in to your store cloud account.',status=401)
            key=body.get('request_key')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('A valid request key is required.')
            rows=self._pending();existing=next((row for row in rows if row['store']==saved['store'] and row['request_key']==key),None)
            fingerprint=self._fingerprint(body)
            if existing and (existing['kind']!=kind or existing.get('customer_id')!=customer_id or existing['fingerprint']!=fingerprint):
                raise CloudError('The pending action has different details. Recover that saved action before creating another.',status=409,pending=True)
            if not existing:
                if any(row['store']==saved['store'] for row in rows):raise CloudError('Resolve the saved pending action before changing shared credit again.',status=409,pending=True)
                if len(rows)>=50:raise CloudError('Resolve the pending actions on this computer before continuing.',status=409,pending=True)
                rows.append({'kind':kind,'customer_id':customer_id,'store':saved['store'],'request_key':key,
                             'body':body,'fingerprint':fingerprint,'created_at':self.clock()})
                self._save_pending(rows)
            path='/api/credit/customers'+('/'+customer_id+'/adjust' if kind=='adjust' else '')
            try:result=self._authenticated('POST',path,data=body)
            except CloudError as error:
                # A new action explicitly rejected by the service did not commit.
                # An older uncertain action stays recoverable even after a role change.
                if not existing and error.status<500 and not error.offline:self._finish_pending(saved['store'],key)
                else:error.pending=True
                raise
            self._finish_pending(saved['store'],key)
            return result

    def pending(self,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();rows=self._pending()
            if saved and saved['expires_at']<=self.clock():self._clear();saved=None
            if saved:self._expected(saved,expected_session_id)
            if not saved:return {'pending':[],'hasOtherPending':bool(rows)}
            visible=[{'requestKey':row['request_key'],'action':row['kind'],'customerId':row.get('customer_id'),
                      'amount_cents':row['body'].get('amount_cents'),'entryKind':row['body'].get('kind'),
                      'reference':row['body'].get('reference'),'createdAt':row['created_at']} for row in rows if row['store']==saved['store']]
            return {'pending':visible,'hasOtherPending':any(row['store']!=saved['store'] for row in rows)}

    def recover(self,key,*,retry=False,expected_session_id=None):
        with self.lock:
            saved=self._saved()
            if saved and saved['expires_at']<=self.clock():self._clear();saved=None
            self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns the pending action.',status=401)
            row=next((row for row in self._pending() if row['store']==saved['store'] and row['request_key']==key),None)
            if row is None:raise CloudError('Pending action not found for this store.',status=404)
            status=self._authenticated('GET','/api/credit/requests/'+quote(key,safe=''))
            if status.get('found') is True:
                self._finish_pending(saved['store'],key)
                return {'resolved':True,'alreadyCompleted':True,'result':status.get('result')}
            if status.get('found') is not False:raise CloudError('The server could not confirm the saved action. Keep it for recovery.',status=502,pending=True)
            if not retry:return {'resolved':False,'alreadyCompleted':False,'message':'The server has no completed record yet. Retry the saved action using its original key.'}
            result=self._mutate(row['kind'],row['body'],row.get('customer_id'))
            return {'resolved':True,'alreadyCompleted':False,'result':result}

    def _call(self,method,path,*,token=None,data=None):
        headers={'Accept':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        try:
            response=self.transport.request(method,ORIGIN+path,headers=headers,json=data,
                                            timeout=(5,20),allow_redirects=False)
        except requests.RequestException:
            raise CloudError('Cloud service is unavailable. Shared credit cannot be changed offline. Retry the same saved action when connected.',status=503,offline=True) from None
        if 300<=response.status_code<400:
            raise CloudError('The cloud service returned an unexpected redirect. Contact support.',status=502)
        if len(response.content)>2_000_000:
            raise CloudError('The cloud response was too large. Contact support.',status=502)
        try:result=response.json()
        except (ValueError,TypeError):
            raise CloudError('The cloud service returned an unreadable response. Retry later.',status=502) from None
        if not isinstance(result,dict):raise CloudError('The cloud service response was invalid.',status=502)
        if not 200<=response.status_code<300:
            message=result.get('error')
            if not isinstance(message,str) or not 1<=len(message)<=500:message='The cloud action could not complete. Retry with the same request key.'
            raise CloudError(message,status=response.status_code,offline=response.status_code>=500)
        return result

    def _authenticated(self,method,path,*,data=None,expected_session_id=None):
        with self.lock:
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():
                self._clear()
                raise CloudError('Sign in to your store cloud account.',status=401)
            try:return self._call(method,path,token=saved['token'],data=data)
            except CloudError as error:
                if error.status==401:self._clear()
                raise

    def login(self,data):
        slug=str(data.get('storeSlug','')).strip().lower()
        email=data.get('email');password=data.get('password')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',slug):raise CloudError('Enter your store ID.')
        if not isinstance(email,str) or len(email)>254 or not isinstance(password,str) or not 1<=len(password)<=128:
            raise CloudError('Enter your staff email and password.')
        with self.lock:
            old=self._saved()
            if old and old['expires_at']<=self.clock():self._clear();old=None
            if old:raise CloudError('Sign out of the current cloud account before switching staff or stores.',status=409)
            result=self._call('POST','/api/auth/login',data={'store_slug':slug,'email':email.strip(),'password':password})
            token=result.get('access_token');expiry=result.get('expires_in')
            if (not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',token)
                or isinstance(expiry,bool) or not isinstance(expiry,int) or not 0<expiry<=28800
                or result.get('store')!=slug or result.get('role') not in ROLES):
                raise CloudError('The cloud sign-in response could not be verified.',status=502)
            # Passwords are sent once over HTTPS and never saved in local settings.
            session_id=uuid.uuid4().hex
            self.vault.set(json.dumps({'origin':ORIGIN,'token':token,'expires_at':self.clock()+expiry,
                                       'store':slug,'role':result['role'],'session_id':session_id},separators=(',',':')))
            return {'signedIn':True,'store':slug,'role':result['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':session_id}

    def status(self):
        with self.lock:
            try:
                saved=self._saved()
                if not saved:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN}
                me=self._authenticated('GET','/api/me')
                if me.get('store')!=saved['store'] or me.get('role') not in ROLES:
                    self._clear();raise CloudError('The cloud account changed. Sign in again.',status=401)
                return {'signedIn':True,'store':me['store'],'role':me['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id']}
            except CloudError as error:
                if error.status==401:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN,'message':str(error)}
                if error.offline and saved:return {'signedIn':False,'savedSession':True,'offline':True,'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id'],'message':str(error)}
                raise

    def logout(self,*,expected_session_id=None):
        with self.lock:
            try:saved=self._saved()
            except CloudError:saved=None
            if saved:self._expected(saved,expected_session_id)
            self._clear()
            if not saved:return {'signedIn':False,'serverRevoked':False,'message':'Signed out on this computer.'}
            try:
                self._call('POST','/api/auth/logout',token=saved['token'],data={})
                return {'signedIn':False,'serverRevoked':True,'message':'Signed out and the server session was revoked.'}
            except CloudError as error:
                if error.status==401:return {'signedIn':False,'serverRevoked':True,'message':'Signed out; the server session has already ended.'}
                return {'signedIn':False,'serverRevoked':False,'message':'Signed out on this computer. The server could not confirm revocation; that session expires within eight hours.'}

    @staticmethod
    def customer_id(value):
        try:return str(uuid.UUID(str(value)))
        except (ValueError,TypeError,AttributeError):raise CloudError('Select a shared-credit customer.') from None

    def customers(self,query='',*,expected_session_id=None):
        if not isinstance(query,str) or len(query)>200:raise CloudError('Keep customer searches under 200 characters.')
        return self._authenticated('GET','/api/credit/customers?q='+quote(query,safe=''),expected_session_id=expected_session_id)

    def account(self,ident,*,expected_session_id=None):
        return self._authenticated('GET','/api/credit/customers/'+self.customer_id(ident),expected_session_id=expected_session_id)

    def create_customer(self,data,*,expected_session_id=None):
        fields={key:data.get(key) for key in ('request_key','display_name','contact','square_customer_id','permission_confirmed')}
        return self._mutate('customer',fields,expected_session_id=expected_session_id)

    def adjust(self,ident,data,*,expected_session_id=None):
        if data.get('kind') not in ('opening','adjust_add','adjust_remove'):
            raise CloudError('Use an opening balance or a manager correction. Trade issuance needs the accepted-trade workflow.')
        fields={key:data.get(key) for key in ('request_key','amount_cents','kind','reference','reason','version')}
        return self._mutate('adjust',fields,self.customer_id(ident),expected_session_id=expected_session_id)
