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
    def __init__(self,message,*,status=400,offline=False,pending=False,session_changed=False,mfa_restart=False):
        super().__init__(message);self.status=status;self.offline=offline;self.pending=pending;self.session_changed=session_changed;self.mfa_restart=mfa_restart


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
        # Verification is deliberately memory-only. Restarting cannot revive a
        # password/MFA challenge or reuse a one-time proof from a disk journal.
        self._mfa=None
        self._mfa_completed=None

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
                if (row.get('kind') not in ('customer','adjust','trade_create','trade_accept','trade_void') or not isinstance(row.get('body'),dict)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',row.get('request_key',''))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',row.get('store',''))
                    or row['body'].get('request_key')!=row['request_key']
                    or row.get('fingerprint')!=self._fingerprint(row['body'])):raise ValueError()
                if row['kind']=='adjust':self.customer_id(row.get('customer_id'))
                if row['kind'].startswith('trade_'):
                    if not isinstance(row.get('receipt_id'),str) or not re.fullmatch(r'R-[A-Za-z0-9-]{1,100}',row['receipt_id']):raise ValueError()
                    if row['kind']!='trade_create':self.customer_id(row.get('trade_id'))
                    if 'result' in row and (not isinstance(row['result'],dict) or row.get('result_fingerprint')!=self._fingerprint(row['result'])):raise ValueError()
            return rows
        except (ValueError,TypeError,AttributeError):
            raise CloudError('The saved pending-action journal needs recovery. Contact support before changing shared balances.',status=409,pending=True) from None

    def _save_pending(self,rows):
        if rows:self.pending_vault.set(json.dumps({'version':1,'origin':ORIGIN,'operations':rows},separators=(',',':')))
        elif self.pending_vault.path.exists():self.pending_vault.path.unlink()

    def _finish_pending(self,store,key):
        self._save_pending([row for row in self._pending() if (row['store'],row['request_key'])!=(store,key)])

    def _mutate(self,kind,body,customer_id=None,*,expected_session_id=None,trade_id=None,receipt_id=None):
        # Persist the exact retry key/body before the network call. A crash or
        # lost response must not turn one correction into two separate actions.
        with self.lock:
            self._cancel_guard()
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():raise CloudError('Sign in to your store cloud account.',status=401)
            key=body.get('request_key')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('A valid request key is required.')
            rows=self._pending();existing=next((row for row in rows if row['store']==saved['store'] and row['request_key']==key),None)
            fingerprint=self._fingerprint(body)
            if existing and (existing['kind']!=kind or existing.get('customer_id')!=customer_id or existing['fingerprint']!=fingerprint
                             or existing.get('trade_id')!=trade_id or existing.get('receipt_id')!=receipt_id):
                raise CloudError('The pending action has different details. Recover that saved action before creating another.',status=409,pending=True)
            if not existing:
                if any(row['store']==saved['store'] for row in rows):raise CloudError('Resolve the saved pending action before changing shared credit again.',status=409,pending=True)
                if len(rows)>=50:raise CloudError('Resolve the pending actions on this computer before continuing.',status=409,pending=True)
                rows.append({'kind':kind,'customer_id':customer_id,'store':saved['store'],'request_key':key,
                             'body':body,'fingerprint':fingerprint,'created_at':self.clock(),
                             **({'trade_id':trade_id,'receipt_id':receipt_id} if kind.startswith('trade_') else {})})
                self._save_pending(rows)
            if existing and kind.startswith('trade_') and 'result' in existing:return existing['result']
            path=('/api/trades'+('' if kind=='trade_create' else '/'+trade_id+'/'+kind.removeprefix('trade_')) if kind.startswith('trade_')
                  else '/api/credit/customers'+('/'+customer_id+'/adjust' if kind=='adjust' else ''))
            try:result=self._authenticated('POST',path,data=body)
            except CloudError as error:
                # A new action explicitly rejected by the service did not commit.
                # An older uncertain action stays recoverable even after a role change.
                if not kind.startswith('trade_') and not existing and error.status<500 and not error.offline:self._finish_pending(saved['store'],key)
                else:error.pending=True
                raise
            if kind.startswith('trade_'):
                rows=self._pending();row=next(row for row in rows if row['store']==saved['store'] and row['request_key']==key)
                row['result']=result;row['result_fingerprint']=self._fingerprint(result);self._save_pending(rows)
            else:self._finish_pending(saved['store'],key)
            return result

    def pending(self,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();rows=self._pending()
            if saved and saved['expires_at']<=self.clock():self._clear();saved=None
            if saved:self._expected(saved,expected_session_id)
            if not saved:return {'pending':[],'hasOtherPending':bool(rows)}
            visible=[{'requestKey':row['request_key'],'action':row['kind'],'customerId':row.get('customer_id'),
                      'amount_cents':row['body'].get('amount_cents'),'entryKind':row['body'].get('kind'),
                      'reference':row['body'].get('reference'),'createdAt':row['created_at'],
                      'receiptId':row.get('receipt_id'),'tradeId':row.get('trade_id')} for row in rows if row['store']==saved['store']]
            return {'pending':visible,'hasOtherPending':any(row['store']!=saved['store'] for row in rows)}

    def recover(self,key,*,retry=False,expected_session_id=None):
        with self.lock:
            saved=self._saved()
            if saved and saved['expires_at']<=self.clock():self._clear();saved=None
            self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns the pending action.',status=401)
            row=next((row for row in self._pending() if row['store']==saved['store'] and row['request_key']==key),None)
            if row is None:raise CloudError('Pending action not found for this store.',status=404)
            if row['kind'].startswith('trade_'):
                raise CloudError('Open the saved local trade to reconcile this action with its hosted receipt.',status=409,pending=True)
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
            self._cancel_guard()
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():
                self._clear()
                raise CloudError('Sign in to your store cloud account.',status=401)
            try:return self._call(method,path,token=saved['token'],data=data)
            except CloudError as error:
                if error.status==401:self._clear()
                raise

    def _commit_login(self,result,slug,*,require_mfa=False):
        token=result.get('access_token');expiry=result.get('expires_in')
        if (result.get('mfa_required') or 'challenge_token' in result or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',token)
            or isinstance(expiry,bool) or not isinstance(expiry,int) or not 0<expiry<=28800
            or result.get('store')!=slug or result.get('role') not in ROLES
            or (require_mfa and result.get('mfa_authenticated') is not True)
            or (not require_mfa and result.get('mfa_authenticated') is True)):
            raise CloudError('The cloud sign-in response could not be verified.',status=502,mfa_restart=True)
        session_id=uuid.uuid4().hex
        if require_mfa and self._mfa:self._mfa['issued_session_id']=session_id
        self.vault.set(json.dumps({'origin':ORIGIN,'token':token,'expires_at':self.clock()+expiry,
                                   'store':slug,'role':result['role'],'session_id':session_id,
                                   'mfa_authenticated':require_mfa},separators=(',',':')))
        return {'signedIn':True,'store':slug,'role':result['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':session_id}

    def _mfa_public(self):
        challenge=self._mfa
        if not challenge:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN}
        if challenge['expires_at']<=self.clock():
            if not challenge.get('uncertain') and not challenge.get('cancel_only'):
                self._mfa=None
                return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN,'message':'Verification expired. Sign in again.'}
        return {'signedIn':False,'mfaRequired':True,'challengeId':challenge['nonce'],'store':challenge['store'],
                'expiresIn':max(0,int(challenge['expires_at']-self.clock())),'methods':challenge['methods'],
                'verificationExpired':challenge['expires_at']<=self.clock(),
                'cancelRequired':bool(challenge.get('cancel_only')),
                'mode':'pilot','portalUrl':ORIGIN}

    def _cancel_remote_challenge(self,token):
        try:
            result=self._call('POST','/api/auth/mfa/cancel',data={'challenge_token':token})
            return result.get('cancelled') is True
        except CloudError:return False

    def _cancel_guard(self):
        if self._mfa and self._mfa.get('cancel_only'):
            raise CloudError('Finish server cancellation, or explicitly clear this sign-in on this computer, before continuing.',status=409)

    def login(self,data):
        slug=str(data.get('storeSlug','')).strip().lower()
        email=data.get('email');password=data.get('password')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',slug):raise CloudError('Enter your store ID.')
        if not isinstance(email,str) or len(email)>254 or not isinstance(password,str) or not 1<=len(password)<=128:
            raise CloudError('Enter your staff email and password.')
        with self.lock:
            self._cancel_guard()
            old=self._saved()
            if old and old['expires_at']<=self.clock():self._clear();old=None
            if old:raise CloudError('Sign out of the current cloud account before switching staff or stores.',status=409)
            previous=self._mfa
            if previous and not self._cancel_remote_challenge(previous['token']):
                raise CloudError('The previous verification could not be cancelled on the server. Reconnect and cancel it before starting another sign-in.',status=503,offline=True)
            self._mfa=None;self._mfa_completed=None
            try:result=self._call('POST','/api/auth/login',data={'store_slug':slug,'email':email.strip(),'password':password,'mfa_supported':True})
            except CloudError as error:
                message=('The store, email or password could not be verified.' if error.status==401 else
                         'Update Stock & Sleeve to complete your store’s sign-in verification.' if error.status==426 else
                         'Too many sign-in attempts. Wait before trying again.' if error.status==429 else
                         'Sign-in could not be confirmed. Reconnect and try again.' if error.offline else
                         'Sign-in could not complete. Try again or contact your store owner.')
                raise CloudError(message,status=error.status,offline=error.offline) from None
            if result.get('mfa_required') is True:
                challenge=result.get('challenge_token');expiry=result.get('expires_in');methods=result.get('methods')
                if ('access_token' in result or 'csrf_token' in result or result.get('store')!=slug
                    or not isinstance(challenge,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',challenge)
                    or type(expiry) is not int or not 0<expiry<=300 or not isinstance(methods,list)
                    or not methods or any(not isinstance(method,str) or method not in ('totp','recovery_code') for method in methods)
                    or len(methods)!=len(set(methods))):
                    raise CloudError('The verification request could not be confirmed. Sign in again.',status=502,mfa_restart=True)
                self._mfa={'token':challenge,'store':slug,'nonce':uuid.uuid4().hex,'expires_at':self.clock()+expiry,'methods':methods}
                return self._mfa_public()
            return self._commit_login(result,slug)

    def verify_mfa(self,data):
        with self.lock:
            challenge=self._mfa;nonce=data.get('challengeId')
            if (not challenge or not isinstance(nonce,str) or not re.fullmatch(r'[a-f0-9]{32}',nonce)
                or not hmac.compare_digest(challenge['nonce'],nonce)):
                raise CloudError('This verification request changed. Refresh or sign in again.',status=409,mfa_restart=True)
            self._cancel_guard()
            if challenge['expires_at']<=self.clock():
                if challenge.get('uncertain'):
                    raise CloudError('Verification expired. Cancel sign-in to finish clearing the previous attempt, then sign in again.',status=401)
                self._mfa=None
                raise CloudError('Verification expired. Sign in again.',status=401,mfa_restart=True)
            supplied=data.get('code')
            if not isinstance(supplied,str) or len(supplied)>80:raise CloudError('Enter a current authenticator code or an unused recovery code.')
            code=supplied.strip();recovery=code.replace('-','')
            if not re.fullmatch(r'[0-9]{6}',code) and not re.fullmatch(r'[A-Fa-f0-9]{32}',recovery):
                raise CloudError('Enter the six digits from your authenticator, or a complete unused recovery code.')
            if len(recovery)==32:code=recovery.lower()
            try:result=self._call('POST','/api/auth/mfa/challenge',data={'challenge_token':challenge['token'],'code':code})
            except CloudError as error:
                if error.status==401:
                    raise CloudError('That code could not be verified. Try a current authenticator code or a different unused recovery code. You can cancel and sign in again.',status=401) from None
                if error.status==429:
                    self._mfa=None
                    raise CloudError('Too many verification attempts. Wait before signing in again.',status=429,mfa_restart=True) from None
                if error.offline or error.status>=500:
                    challenge['uncertain']=True
                    raise CloudError('Verification could not be confirmed. Cancel sign-in and try again when connected; a recovery code may already have been used.',status=error.status,offline=True) from None
                raise CloudError('Verification could not complete. Cancel sign-in before trying again.',status=error.status) from None
            challenge['uncertain']=True
            try:session=self._commit_login(result,challenge['store'],require_mfa=True)
            except CloudError:
                if self._cancel_remote_challenge(challenge['token']):self._mfa=None;raise
                raise CloudError('Verification could not be confirmed. Cancel sign-in when connected before trying again.',status=502,offline=True) from None
            self._mfa=None
            self._mfa_completed={'nonce':nonce,'session_id':session['sessionId'],'token':challenge['token'],
                                 'store':challenge['store'],'expires_at':challenge['expires_at'],'methods':challenge['methods']}
            return session

    def cancel_mfa(self,data):
        with self.lock:
            nonce=data.get('challengeId');challenge=self._mfa;completed=self._mfa_completed
            if not isinstance(nonce,str) or not re.fullmatch(r'[a-f0-9]{32}',nonce):raise CloudError('Refresh the sign-in page before cancelling verification.',status=409,mfa_restart=True)
            if challenge and hmac.compare_digest(challenge['nonce'],nonce):
                pending=challenge
            elif completed and hmac.compare_digest(completed['nonce'],nonce):
                saved=self._saved()
                if saved and saved['session_id']!=completed['session_id']:raise CloudError('The signed-in workspace changed. Refresh before continuing.',status=409,session_changed=True)
                pending=dict(completed,issued_session_id=completed['session_id'])
            else:raise CloudError('This verification request changed. Refresh before continuing.',status=409,mfa_restart=True)
            pending.update(cancel_only=True,uncertain=True);self._mfa=pending;self._mfa_completed=None
            self._clear_cancelled_session(pending)
            revoked=self._cancel_remote_challenge(pending['token'])
            if revoked:
                self._mfa=None
                return {'signedIn':False,'serverRevoked':True,'message':'Verification cancelled. Sign in again when ready.'}
            return self._mfa_public()|{'serverRevoked':False,'message':'Sign-in is cleared locally, but server cancellation was not confirmed. Retry when connected, or clear this sign-in on this computer. Any issued server session expires within eight hours.'}

    def _clear_cancelled_session(self,pending):
        if pending.get('issued_session_id'):
            saved=self._saved()
            if saved and saved['session_id']==pending['issued_session_id']:self._clear()

    def clear_mfa_local(self,data):
        with self.lock:
            pending=self._mfa;nonce=data.get('challengeId')
            if (not pending or not pending.get('cancel_only') or not isinstance(nonce,str)
                or not re.fullmatch(r'[a-f0-9]{32}',nonce)
                or not hmac.compare_digest(pending['nonce'],nonce)):
                raise CloudError('Refresh the pending cancellation before clearing this sign-in.',status=409)
            self._clear_cancelled_session(pending);self._mfa=None;self._mfa_completed=None
            return {'signedIn':False,'serverRevoked':False,'message':'This sign-in was cleared on this computer. Server revocation was not confirmed; any issued session expires within eight hours. You can sign in again.'}

    def status(self):
        with self.lock:
            if self._mfa and self._mfa.get('cancel_only'):return self._mfa_public()
            try:
                saved=self._saved()
                if not saved:return self._mfa_public()
                me=self._authenticated('GET','/api/me')
                if me.get('store')!=saved['store'] or me.get('role') not in ROLES:
                    self._clear();raise CloudError('The cloud account changed. Sign in again.',status=401)
                return {'signedIn':True,'store':me['store'],'role':me['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id']}
            except CloudError as error:
                if error.status==401:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN,'message':str(error)}
                if error.offline and saved:return {'signedIn':False,'savedSession':True,'offline':True,'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id'],'message':str(error)}
                raise

    def logout(self,*,expected_session_id=None,expected_challenge_id=None):
        with self.lock:
            if expected_challenge_id:return self.cancel_mfa({'challengeId':expected_challenge_id})
            if self._mfa:return self.cancel_mfa({'challengeId':self._mfa['nonce']})
            try:saved=self._saved()
            except CloudError:saved=None
            if saved:self._expected(saved,expected_session_id)
            challenge=self._mfa;self._mfa=None;self._mfa_completed=None;self._clear()
            if challenge:self._cancel_remote_challenge(challenge['token'])
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

    def trade_get(self,ident,*,expected_session_id=None):
        return self._authenticated('GET','/api/trades/'+self.customer_id(ident),expected_session_id=expected_session_id)

    def trade_request(self,key,*,expected_session_id=None):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('Invalid saved trade request.')
        return self._authenticated('GET','/api/credit/requests/'+quote(key,safe=''),expected_session_id=expected_session_id)

    def trade_create(self,body,receipt_id,*,expected_session_id=None):
        return self._mutate('trade_create',body,expected_session_id=expected_session_id,receipt_id=receipt_id)

    def trade_accept(self,ident,body,receipt_id,*,expected_session_id=None):
        return self._mutate('trade_accept',body,expected_session_id=expected_session_id,trade_id=self.customer_id(ident),receipt_id=receipt_id)

    def trade_void(self,ident,body,receipt_id,*,expected_session_id=None):
        return self._mutate('trade_void',body,expected_session_id=expected_session_id,trade_id=self.customer_id(ident),receipt_id=receipt_id)

    def trade_journal(self,key,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns this trade.',status=401)
            return next((dict(row) for row in self._pending() if row['store']==saved['store'] and row['request_key']==key and row['kind'].startswith('trade_')),None)

    def trade_finish(self,key,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns this trade.',status=401)
            self._finish_pending(saved['store'],key)
