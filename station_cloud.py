"""Desktop relay to the fixed Stock & Sleeve service; staff tokens stay in the OS vault."""
import json
import copy
import hashlib
import hmac
import re
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote
from pathlib import Path
import requests
from station_store import StationError, Vault
from station_profile import initialize_profile, ProfilePolicyError
from station_http_json import RequestBodyError, preflight_request_body

ORIGIN='https://stock-and-sleeve-api.onrender.com'
ROLES=('owner','manager','buyer','cashier')


class CompactJSONSession(requests.Session):
    """Use the normal requests/TLS/adapter path with exact compact UTF-8 JSON."""
    def request(self,method,url,**options):
        value=options.get('json')
        if value is not None and not options.get('data') and not options.get('files'):
            encoded=preflight_request_body(value)
            options.pop('json')
            options['data']=encoded
            headers=requests.structures.CaseInsensitiveDict(options.get('headers') or {})
            if 'Content-Type' not in self.headers:
                headers.setdefault('Content-Type','application/json')
            options['headers']=headers
        return super().request(method,url,**options)


def response_budget(method,path):
    """Only typed receiving projections may carry their repeated final receipt.

    Auth, customers, locations, readiness and retirement retain the old cap.
    This changes no request, provider-body or generic API input limit.
    """
    operation=r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}'
    if (method=='POST' and path=='/api/receiving/plans'
        or method=='GET' and re.fullmatch(r'/api/receiving/(?:'+operation+r'|requests/[A-Za-z0-9_-]{1,150}|intakes/'+operation+r'/history)',path)
        or method=='POST' and re.fullmatch(r'/api/receiving/'+operation+r'/(?:commit|recover|authorize|cancel)',path)
        or method=='PUT' and re.fullmatch(r'/api/receiving/'+operation+r'/plan',path)):
        return 8*1024*1024
    return 2_000_000


class CloudError(StationError):
    def __init__(self,message,*,status=400,offline=False,pending=False,session_changed=False,mfa_restart=False,code=None,physical_intake_id=None,receiving_id=None,original_trade_id=None):
        super().__init__(message);self.status=status;self.offline=offline;self.pending=pending;self.session_changed=session_changed;self.mfa_restart=mfa_restart
        self.code=code;self.physical_intake_id=physical_intake_id
        self.original_trade_id=original_trade_id
        self.receiving_id=receiving_id


class CloudClient:
    def __init__(self,root,*,transport=None,vault=None,pending_vault=None,clock=None,intake_vault=None,receiving_vault=None,profile=None):
        root=Path(root)
        self.profile=profile if profile is not None else initialize_profile(root,store_root=root)
        # Startup creates the lock, so later read-only setup checks need not
        # create profile/session state. This lock is separate from policy I/O.
        with self._session_guard():pass
        self.vault=vault or Vault(root)
        if vault is None:self.vault.path=self.vault.root/'cloud.session'
        self.pending_vault=pending_vault or Vault(root)
        if pending_vault is None:self.pending_vault.path=self.pending_vault.root/'cloud.pending'
        self.intake_vault=intake_vault or Vault(root)
        if intake_vault is None:self.intake_vault.path=self.intake_vault.root/'cloud.intake'
        self.transport=transport or CompactJSONSession()
        self.transport.trust_env=False
        self.clock=clock or time.time
        self.lock=threading.RLock()
        # Verification is deliberately memory-only. Restarting cannot revive a
        # password/MFA challenge or reuse a one-time proof from a disk journal.
        self._mfa=None
        self._mfa_completed=None
        from station_cloud_journal_cas import CloudJournalCAS
        self.pending_storage=CloudJournalCAS(self,'pending')
        self.intake_storage=CloudJournalCAS(self,'intake')
        from station_hosted_receive import ReceivingJournal
        self.receiving=ReceivingJournal(self,root,receiving_vault)

    def _profile_snapshot(self):
        try:return self.profile.safe_snapshot()
        except ProfilePolicyError as error:
            raise CloudError(str(error),status=409,code=error.code) from None

    @contextmanager
    def _session_guard(self):
        try:
            with self.profile.cloud_session_guard():yield
        except ProfilePolicyError as error:
            raise CloudError(str(error),status=409,code=error.code) from None

    @staticmethod
    def _tenant_identity(value):
        if not isinstance(value,str):
            raise CloudError('This store service needs an account update before this computer can sign in.',status=426,code='STORE_IDENTITY_REQUIRED')
        try:
            if str(uuid.UUID(value))!=value:raise ValueError()
        except (ValueError,AttributeError):
            raise CloudError('The store identity could not be verified. No account was changed on this computer.',status=502,code='STORE_IDENTITY_INVALID') from None
        return value

    def _require_saved_binding(self,saved):
        if self._profile_snapshot()['profileMode']!='hosted':return
        tenant=self._tenant_identity(saved.get('tenant_id'))
        try:binding=self.profile.binding()
        except ProfilePolicyError as error:
            raise CloudError(str(error),status=409,code=error.code) from None
        if binding!={'tenant_id':tenant,'service_origin':ORIGIN}:
            raise CloudError('This account does not match this computer’s store profile. Use a separate profile for another store; saved work was kept.',status=409,code='STORE_PROFILE_MISMATCH')

    def _verified_me(self,result,saved):
        if self._profile_snapshot()['profileMode']!='hosted':return
        if result.get('store')!=saved['store'] or result.get('role') not in ROLES:
            raise CloudError('The cloud account changed. Sign out and sign in to this profile’s store again.',status=409,session_changed=True,code='STORE_PROFILE_MISMATCH')
        if self._profile_snapshot()['profileMode']=='hosted':
            tenant=self._tenant_identity(result.get('tenant_id'))
            if tenant!=saved.get('tenant_id'):
                raise CloudError('The cloud store identity changed. Saved work was kept; use this profile only for its original store.',status=409,session_changed=True,code='STORE_PROFILE_MISMATCH')
            self._require_saved_binding(saved)

    def _saved(self):
        with self._session_guard():return self._saved_unlocked()

    def _saved_unlocked(self):
        """Read only while holding the short cross-process session guard."""
        self._profile_snapshot()
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
        except (ValueError,TypeError):
            raise CloudError('The saved cloud session is invalid. Sign out here and sign in again.',status=401) from None
        self._require_saved_binding(saved)
        return saved

    def _clear(self,expected_session_id=None):
        with self._session_guard():return self._clear_unlocked(expected_session_id)

    def _clear_unlocked(self,expected_session_id=None):
        # Only this dedicated staff-session vault is removed; Square receiving credentials are separate.
        if not self.vault.path.exists():return
        if expected_session_id is not None:
            try:saved=json.loads(self.vault.read())
            except (ValueError,TypeError,StationError):
                raise CloudError('The saved sign-in cannot be matched safely. Explicitly sign out on this computer to recover; store binding and journals were kept.',status=409,code='SESSION_SAVE_RECOVERY_REQUIRED') from None
            if not isinstance(saved,dict) or saved.get('session_id')!=expected_session_id:
                raise CloudError('The signed-in workspace changed. Its current sign-in was kept.',status=409,session_changed=True)
        self.vault.path.unlink()

    @staticmethod
    def _expected(saved,expected):
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch(r'[a-f0-9]{32}',expected) or not saved or not hmac.compare_digest(saved['session_id'],expected)):
            raise CloudError('The signed-in workspace changed. Refresh before continuing.',status=409,session_changed=True)

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    @staticmethod
    def _preflight_body(body,*,pending=False):
        try:return preflight_request_body(body)
        except RequestBodyError as error:
            raise CloudError(str(error),status=413 if error.too_large else 400,pending=pending,
                code='REQUEST_BODY_TOO_LARGE' if error.too_large else 'REQUEST_BODY_INVALID') from None

    def _pending(self):
        return self.pending_storage.read()

    def _parse_pending_text(self,raw):
        if not raw:return []
        try:
            value=json.loads(raw)
            if value.get('version')!=1 or value.get('origin')!=ORIGIN or not isinstance(value.get('operations'),list) or len(value['operations'])>50:raise ValueError()
            rows=value['operations'];seen=set()
            for row in rows:
                if (row.get('kind') not in ('customer','adjust','trade_create','trade_accept','trade_void') or not isinstance(row.get('body'),dict)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',row.get('request_key',''))
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',row.get('store',''))
                    or row['body'].get('request_key')!=row['request_key']
                    or row.get('fingerprint')!=self._fingerprint(row['body'])):raise ValueError()
                identity=(row['store'],row['request_key'])
                if identity in seen:raise ValueError()
                seen.add(identity)
                if row['kind']=='adjust':self.customer_id(row.get('customer_id'))
                if row['kind'].startswith('trade_'):
                    if not isinstance(row.get('receipt_id'),str) or not re.fullmatch(r'R-[A-Za-z0-9-]{1,100}',row['receipt_id']):raise ValueError()
                    if row['kind']!='trade_create':self.customer_id(row.get('trade_id'))
                    if 'result' in row and (not isinstance(row['result'],dict) or row.get('result_fingerprint')!=self._fingerprint(row['result'])):raise ValueError()
            return rows
        except (ValueError,TypeError,AttributeError):
            raise CloudError('The saved pending-action journal needs recovery. Contact support before changing shared balances.',status=409,pending=True) from None

    def _save_pending(self,rows):
        return self.pending_storage.save(rows)

    def _finish_pending(self,store,key,*,expected_session_id=None,row=None):
        saved=self.pending_storage.session(expected_session_id)
        if saved['store']!=store:raise CloudError('The saved action belongs to another store.',status=409,pending=True)
        return self.pending_storage.finish_key(key,saved,original_row=row)

    def _mutate(self,kind,body,customer_id=None,*,expected_session_id=None,trade_id=None,receipt_id=None,_journal_holder=None):
        # Persist the exact retry key/body before the network call. A crash or
        # lost response must not turn one correction into two separate actions.
        with self.lock:
            saved=self.pending_storage.session(expected_session_id)
            key=body.get('request_key')
            if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('A valid request key is required.')
            existing=next((row for row in self._pending() if row['store']==saved['store'] and row['request_key']==key),None)
            if existing is None:self._preflight_body(body)
            fingerprint=self._fingerprint(body)
            candidate={'kind':kind,'customer_id':customer_id,'store':saved['store'],'request_key':key,
                       'body':body,'fingerprint':fingerprint,'created_at':self.clock(),
                       **({'trade_id':trade_id,'receipt_id':receipt_id} if kind.startswith('trade_') else {})}
            row,created=self.pending_storage.reserve(candidate,saved)
            if _journal_holder is not None:_journal_holder['row']=copy.deepcopy(row)
            if kind.startswith('trade_') and 'result' in row:return row['result']
            path=('/api/trades'+('' if kind=='trade_create' else '/'+trade_id+'/'+kind.removeprefix('trade_')) if kind.startswith('trade_')
                  else '/api/credit/customers'+('/'+customer_id+'/adjust' if kind=='adjust' else ''))
            try:result=self._authenticated('POST',path,data=row['body'],expected_session_id=saved['session_id'])
            except CloudError as error:
                # A new action explicitly rejected by the service did not commit.
                # An older uncertain action stays recoverable even after a role change.
                if not kind.startswith('trade_') and created and error.status<500 and not error.offline and not error.session_changed:
                    try:self._finish_pending(saved['store'],key,expected_session_id=saved['session_id'],row=row)
                    except (CloudError,StationError,OSError):error.pending=True
                else:error.pending=True
                raise
            if kind.startswith('trade_'):
                self.pending_storage.remember(row,result,saved)
            else:self.pending_storage.finish(row,saved)
            return result

    def pending(self,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();rows=self._pending();intakes=self._intake_pending();receiving=self.receiving.read()
            if saved and saved['expires_at']<=self.clock():self._clear(saved['session_id']);saved=None
            if saved:self._expected(saved,expected_session_id)
            if not saved:return {'pending':[],'hasOtherPending':bool(rows or intakes or receiving)}
            visible=[{'requestKey':row['request_key'],'action':row['kind'],'customerId':row.get('customer_id'),
                      'amount_cents':row['body'].get('amount_cents'),'entryKind':row['body'].get('kind'),
                      'reference':row['body'].get('reference'),'createdAt':row['created_at'],
                      'receiptId':row.get('receipt_id'),'tradeId':row.get('trade_id')} for row in rows if row['store']==saved['store']]
            visible.extend({'requestKey':row['request_key'],'action':'intake_claim','receiptId':row['receipt_id'],
                            'createdAt':row['created_at']} for row in intakes if row['store']==saved['store'])
            visible.extend({'requestKey':row['request_key'],'action':'hosted_receiving','receiptId':row['receipt_id'],
                            'receivingId':row['receiving_id'],'createdAt':row['created_at']} for row in receiving if row['store']==saved['store'])
            return {'pending':visible,'hasOtherPending':any(row['store']!=saved['store'] for row in rows+intakes+receiving)}

    def recover(self,key,*,retry=False,expected_session_id=None):
        with self.lock:
            saved=self._saved()
            if saved and saved['expires_at']<=self.clock():self._clear(saved['session_id']);saved=None
            self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns the pending action.',status=401)
            row=next((row for row in self._pending() if row['store']==saved['store'] and row['request_key']==key),None)
            if row is None:raise CloudError('Pending action not found for this store.',status=404)
            if row['kind'].startswith('trade_'):
                raise CloudError('Open the saved local trade to reconcile this action with its hosted receipt.',status=409,pending=True)
            status=self._authenticated('GET','/api/credit/requests/'+quote(key,safe=''),expected_session_id=saved['session_id'])
            if status.get('found') is True:
                self._finish_pending(saved['store'],key,expected_session_id=saved['session_id'],row=row)
                return {'resolved':True,'alreadyCompleted':True,'result':status.get('result')}
            if status.get('found') is not False:raise CloudError('The server could not confirm the saved action. Keep it for recovery.',status=502,pending=True)
            if not retry:return {'resolved':False,'alreadyCompleted':False,'message':'The server has no completed record yet. Retry the saved action using its original key.'}
            result=self._mutate(row['kind'],row['body'],row.get('customer_id'),expected_session_id=saved['session_id'])
            return {'resolved':True,'alreadyCompleted':False,'result':result}

    def _call(self,method,path,*,token=None,data=None):
        headers={'Accept':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        if data is not None:self._preflight_body(data)
        try:
            response=self.transport.request(method,ORIGIN+path,headers=headers,json=data,
                                            timeout=(5,20),allow_redirects=False)
        except RequestBodyError as error:
            raise CloudError(str(error),status=413 if error.too_large else 400,
                code='REQUEST_BODY_TOO_LARGE' if error.too_large else 'REQUEST_BODY_INVALID') from None
        except requests.RequestException:
            raise CloudError('Cloud service is unavailable. Shared credit cannot be changed offline. Retry the same saved action when connected.',status=503,offline=True) from None
        if 300<=response.status_code<400:
            raise CloudError('The cloud service returned an unexpected redirect. Contact support.',status=502)
        if len(response.content)>response_budget(method,path):
            raise CloudError('The cloud response was too large. Contact support.',status=502)
        try:result=response.json()
        except (ValueError,TypeError):
            raise CloudError('The cloud service returned an unreadable response. Retry later.',status=502) from None
        if not isinstance(result,dict):raise CloudError('The cloud service response was invalid.',status=502)
        if not 200<=response.status_code<300:
            message=result.get('error')
            if not isinstance(message,str) or not 1<=len(message)<=500:message='The cloud action could not complete. Retry with the same request key.'
            code=result.get('code');code=code if isinstance(code,str) and re.fullmatch(r'[A-Z_]{1,80}',code) else None
            try:intake=str(uuid.UUID(str(result['physical_intake_id']))) if result.get('physical_intake_id') else None
            except (ValueError,TypeError,AttributeError):intake=None
            try:receiving=str(uuid.UUID(str(result['receiving_id']))) if result.get('receiving_id') else None
            except (ValueError,TypeError,AttributeError):receiving=None
            raise CloudError(message,status=response.status_code,offline=response.status_code>=500,code=code,physical_intake_id=intake,receiving_id=receiving)
        return result

    def _authenticated(self,method,path,*,data=None,expected_session_id=None):
        with self.lock:
            self._cancel_guard()
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():
                if saved:self._clear(saved['session_id'])
                raise CloudError('Sign in to your store cloud account.',status=401)
            try:
                result=self._call(method,path,token=saved['token'],data=data)
                if method=='GET' and path=='/api/me':self._verified_me(result,saved)
                return result
            except CloudError as error:
                if error.status==401:self._clear(saved['session_id'])
                raise

    def _commit_login(self,result,slug,*,require_mfa=False):
        token=result.get('access_token');expiry=result.get('expires_in')
        if (result.get('mfa_required') or 'challenge_token' in result or not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,256}',token)
            or isinstance(expiry,bool) or not isinstance(expiry,int) or not 0<expiry<=28800
            or result.get('store')!=slug or result.get('role') not in ROLES
            or (require_mfa and result.get('mfa_authenticated') is not True)
            or (not require_mfa and result.get('mfa_authenticated') is True)):
            raise CloudError('The cloud sign-in response could not be verified.',status=502,mfa_restart=True)
        snapshot=self._profile_snapshot()
        tenant=None
        if snapshot['profileMode']=='hosted' or 'tenant_id' in result:
            tenant=self._tenant_identity(result.get('tenant_id'))
        if snapshot['profileMode']=='hosted':
            try:self.profile.bind_verified_store(tenant,ORIGIN)
            except ProfilePolicyError as error:
                raise CloudError(str(error),status=409,code=error.code) from None
        session_id=uuid.uuid4().hex
        if require_mfa and self._mfa:self._mfa['issued_session_id']=session_id
        saved={'origin':ORIGIN,'token':token,'expires_at':self.clock()+expiry,
               'store':slug,'role':result['role'],'session_id':session_id,
               'mfa_authenticated':require_mfa}
        if tenant is not None:saved['tenant_id']=tenant
        with self._session_guard():
            if self._saved_unlocked() is not None:
                raise CloudError('Another sign-in completed on this computer. Its current session was kept; refresh before continuing.',status=409,session_changed=True)
            try:self.vault.set(json.dumps(saved,separators=(',',':')))
            except (OSError,StationError):
                try:self._clear_unlocked(session_id)
                except (OSError,CloudError):
                    raise CloudError('The sign-in save could not be recovered safely. Explicitly sign out on this computer before retrying; store binding and journals were kept.',status=409,code='SESSION_SAVE_RECOVERY_REQUIRED') from None
                raise CloudError('The sign-in could not be saved on this computer. Its store binding and saved work were kept. Retry the same store when storage is available.',status=503,code='SESSION_STORAGE_UNAVAILABLE') from None
        return {'signedIn':True,'store':slug,'role':result['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':session_id}

    def read_setup_metadata(self,expected_session_id):
        """Authenticated read-only metadata; errors never clear local authority.

        This deliberately bypasses _authenticated's existing 401 session cleanup.
        No location lookup, token introspection, OAuth action or provider write is
        performed by these fixed service GETs. The Setup helper validates and
        projects the bounded enum/boolean metadata before exposing it to the UI.
        """
        with self.lock:
            self._cancel_guard()
            saved=self._saved()
            self._expected(saved,expected_session_id)
            if not saved or saved['expires_at']<=self.clock():
                raise CloudError('Sign in to this profile’s store to check setup. Saved files were kept.',status=401)
            def setup_call(path):
                try:return self._call('GET',path,token=saved['token'])
                except CloudError as error:
                    status=error.status if error.status in (401,403,409,426,429) else 503 if error.offline or error.status>=500 else 502
                    message=('Sign in again to check this store’s setup. Saved files were kept.' if status==401 else
                             'This account cannot check that setup step.' if status==403 else
                             'Wait before refreshing store setup again.' if status==429 else
                             'Store setup could not be verified. Saved files were kept; refresh when the service is available.')
                    raise CloudError(message,status=status,offline=error.offline) from None
            me=setup_call('/api/me')
            self._verified_me(me,saved)
            public_me={key:me.get(key) for key in ('tenant_id','store','role','mfa_enabled','mfa_authenticated')}
            def metadata(path,keys):
                try:value=setup_call(path)
                except CloudError as error:
                    if error.status==403:return {'status':403,'body':None}
                    raise
                return {'status':200,'body':{key:value[key] for key in keys if key in value}}
            square=metadata('/api/square/status',('oauth_configured','connected','connection_status','environment','saved_environment',
                            'requested_profile','grants_verified','write_checkout_enabled','scopes','requested_scopes'))
            receiving=metadata('/api/receiving/readiness',('protocol_version','write_enabled','server_write_enabled','tenant_write_enabled',
                               'same_price_existing_supported','new_price_supported','new_price_write_enabled','new_parent_enabled',
                               'location_selection_required','fresh_mfa_required','max_targets','grants_verified','required_scopes'))
            current=self._saved()
            if (not current or current.get('session_id')!=saved['session_id']
                or current.get('store')!=saved['store'] or current.get('tenant_id')!=saved.get('tenant_id')):
                raise CloudError('The signed-in workspace changed while setup was checked. Refresh for the current account; saved work was kept.',status=409,session_changed=True)
            return {'sessionId':saved['session_id'],'me':public_me,'square':square,'receiving':receiving}

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
            if old and old['expires_at']<=self.clock():self._clear(old['session_id']);old=None
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
                # A full server response could not be installed locally. Until
                # its revocation is confirmed, this is cancellation recovery,
                # rather than another usable password/MFA challenge.
                challenge['cancel_only']=True
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
            if saved and saved['session_id']==pending['issued_session_id']:self._clear(pending['issued_session_id'])

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
                    self._clear(saved['session_id']);raise CloudError('The cloud account changed. Sign in again.',status=401)
                capabilities=me.get('capabilities')
                public={key:capabilities[key] for key in ('physical_intake_claims','trade_claim_required','new_trade_acceptance_requires_fresh_mfa','hosted_receiving') if isinstance(capabilities,dict) and key in capabilities and type(capabilities[key]) in (bool,int)}
                return {'signedIn':True,'store':me['store'],'role':me['role'],'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id'],'capabilities':public}
            except CloudError as error:
                if error.status==401:return {'signedIn':False,'mode':'pilot','portalUrl':ORIGIN,'message':str(error)}
                if error.offline and saved:return {'signedIn':False,'savedSession':True,'offline':True,'mode':'pilot','portalUrl':ORIGIN,'sessionId':saved['session_id'],'message':str(error)}
                raise

    def logout(self,*,expected_session_id=None,expected_challenge_id=None):
        with self.lock:
            if expected_challenge_id:return self.cancel_mfa({'challengeId':expected_challenge_id})
            if self._mfa:return self.cancel_mfa({'challengeId':self._mfa['nonce']})
            # Empty/corrupt-session recovery must be atomic too: a second
            # supported client cannot install a new session between this read
            # and the explicit local clear. The remote revoke is outside it.
            with self._session_guard():
                try:saved=self._saved_unlocked()
                except CloudError:saved=None
                if saved:self._expected(saved,expected_session_id)
                challenge=self._mfa;self._mfa=None;self._mfa_completed=None
                self._clear_unlocked(saved['session_id'] if saved else None)
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

    def read_original_trade(self,ident,*,expected_session_id=None):
        """Read recovery evidence without changing sessions or operation journals.

        The domain recovery adapter validates every nested financial/source fact.
        This boundary supplies current store/staff authority and checks that the
        same local session still exists after the fixed, read-only service calls.
        """
        import math
        with self.lock:
            self._cancel_guard()
            if self._profile_snapshot()['profileMode']!='hosted':
                raise CloudError('Open this store’s separate hosted profile to recover an original agreement.',status=409,code='HOSTED_WORKFLOW_REQUIRED')
            if not isinstance(expected_session_id,str) or not re.fullmatch(r'[a-f0-9]{32}',expected_session_id):
                raise CloudError('Refresh the signed-in workspace before recovering an original trade.',status=409,session_changed=True)
            ident=self.customer_id(ident)
            saved=self._saved();self._expected(saved,expected_session_id)
            if (not saved or type(saved['expires_at']) not in (int,float)
                or not math.isfinite(saved['expires_at']) or saved['expires_at']<=self.clock()):
                raise CloudError('Sign in again to read the original trade. Saved work was kept.',status=401)
            me=self._call('GET','/api/me',token=saved['token']);self._verified_me(me,saved)
            if me.get('role') not in ('owner','manager'):
                raise CloudError('An owner or manager must recover original trade source records.',status=403)
            trade=self._call('GET','/api/trades/'+ident,token=saved['token'])
            if trade.get('id')!=ident or trade.get('state') not in ('accepted_cash','accepted_credit'):
                raise CloudError('Choose an original accepted trade to recover; no new agreement was made.',status=409,code='ORIGINAL_TRADE_NOT_ACCEPTED')
            try:claim_id=self.customer_id(trade.get('physical_intake_id'))
            except CloudError:
                raise CloudError('The original source evidence is unavailable. Keep the hosted trade reference for owner review.',status=409,code='SOURCE_RECOVERY_EVIDENCE_UNAVAILABLE') from None
            claim=self._call('GET','/api/intake-claims/'+claim_id,token=saved['token'])
            if (claim.get('physical_intake_id')!=claim_id or claim.get('state')!='consumed'
                or claim.get('bound_trade_id')!=ident):
                raise CloudError('The original trade and physical intake could not be verified together. Saved work was kept.',status=409,code='SOURCE_RECOVERY_EVIDENCE_UNAVAILABLE')
            with self._session_guard():
                current=self._saved_unlocked();self._expected(current,expected_session_id)
                if (not current or current.get('tenant_id')!=saved['tenant_id']
                    or current.get('store')!=saved['store']
                    or type(current.get('expires_at')) not in (int,float)
                    or not math.isfinite(current['expires_at']) or current['expires_at']<=self.clock()):
                    raise CloudError('The signed-in workspace changed. Refresh before recovering this trade.',status=409,session_changed=True)
            return {'sessionId':saved['session_id'],'tenantId':saved['tenant_id'],
                    'store':saved['store'],'role':me['role'],'trade':trade,'claim':claim}

    def trade_get(self,ident,*,expected_session_id=None):
        return self._authenticated('GET','/api/trades/'+self.customer_id(ident),expected_session_id=expected_session_id)

    def assert_record_session_unlocked(self,expected,tenant,store):
        """Short local publication check; caller holds the OS session guard."""
        import math
        saved=self._saved_unlocked();self._expected(saved,expected)
        if (not saved or saved.get('store')!=store or saved.get('tenant_id')!=tenant
            or saved.get('role') not in ('owner','manager','buyer')
            or type(saved.get('expires_at')) not in (int,float)
            or not math.isfinite(saved['expires_at']) or saved['expires_at']<=self.clock()):
            raise CloudError('The signed-in record workspace changed. Saved work was kept.',status=409,session_changed=True)
        return saved

    def record_authority(self,*,expected_session_id=None):
        """Current reader authority, without clearing sessions or any journals."""
        import math
        with self.lock:
            self._cancel_guard()
            if not isinstance(expected_session_id,str) or not re.fullmatch(r'[a-f0-9]{32}',expected_session_id):
                raise CloudError('Refresh the signed-in workspace before opening an original record.',status=409,session_changed=True)
            saved=self._saved();self._expected(saved,expected_session_id)
            if (not saved or type(saved.get('expires_at')) not in (int,float)
                or not math.isfinite(saved['expires_at']) or saved['expires_at']<=self.clock()):
                raise CloudError('Sign in again to read this original agreement. Saved work was kept.',status=401)
            me=self._call('GET','/api/me',token=saved['token']);self._verified_me(me,saved)
            if me.get('store')!=saved['store'] or me.get('role') not in ROLES:
                raise CloudError('The signed-in store changed. Saved work was kept.',status=409,session_changed=True)
            if saved.get('tenant_id') is not None and self._tenant_identity(me.get('tenant_id'))!=saved['tenant_id']:
                raise CloudError('The signed-in store identity changed. Saved work was kept.',status=409,session_changed=True)
            if me['role'] not in ('owner','manager','buyer'):
                raise CloudError('A buyer, manager or owner must read original trade records.',status=403)
            authority={'sessionId':saved['session_id'],'tenantId':saved.get('tenant_id'),'store':saved['store'],'role':me['role']}
            with self._session_guard():
                self.assert_record_session_unlocked(expected_session_id,authority['tenantId'],authority['store'])
            return authority

    def read_accepted_trade(self,ident,*,expected_session_id=None):
        """Fixed read-only original agreement path, preserving current journals."""
        with self.lock:
            ident=self.customer_id(ident)
            authority=self.record_authority(expected_session_id=expected_session_id)
            saved=self._saved();self._expected(saved,expected_session_id)
            trade=self._call('GET','/api/trades/'+ident,token=saved['token'])
            if trade.get('id')!=ident or trade.get('state') not in ('accepted_cash','accepted_credit'):
                raise CloudError('Choose the original accepted trade record. Printing will not accept another agreement.',status=409)
            with self._session_guard():
                self.assert_record_session_unlocked(expected_session_id,authority['tenantId'],authority['store'])
            return dict(authority,trade=trade)

    def read_receiving_trade(self,ident,*,expected_session_id=None):
        """Read original accepted goods for receiving, never touch journals."""
        with self.lock:
            ident=self.customer_id(ident)
            authority=self.record_authority(expected_session_id=expected_session_id)
            if authority['role'] not in ('owner','manager'):
                raise CloudError('An owner or manager must verify original receiving quantities.',status=403)
            saved=self._saved();self._expected(saved,expected_session_id)
            trade=self._call('GET','/api/trades/'+ident,token=saved['token'])
            if trade.get('id')!=ident or trade.get('state') not in ('accepted_cash','accepted_credit'):
                raise CloudError('The original accepted receiving source could not be verified. Keep its saved operation and request.',status=409,pending=True)
            with self._session_guard():
                self.assert_record_session_unlocked(expected_session_id,authority['tenantId'],authority['store'])
            return dict(authority,trade=trade)

    def original_trade_inventory(self,ident,*,expected_session_id=None):
        """Read typed history only after verifying the original accepted source."""
        with self.lock:
            evidence=self.read_original_trade(ident,expected_session_id=expected_session_id)
            saved=self._saved();self._expected(saved,expected_session_id)
            history=self._call('GET','/api/receiving/intakes/'+self.customer_id(evidence['claim']['physical_intake_id'])+'/history',token=saved['token'])
            with self._session_guard():
                self.assert_recovery_session_unlocked(expected_session_id,evidence['tenantId'],evidence['store'])
            return dict(evidence,history=history)

    def read_intake_receiving_history(self,claim_id,*,expected_session_id):
        """Original intake history, without mirroring claims or touching journals."""
        return self._read_intake_receiving(claim_id,None,expected_session_id)

    def read_intake_receiving_source(self,claim_id,receiving_id,*,expected_session_id):
        """Coherent nonfinancial read; the pure verifier grants no action."""
        return self._read_intake_receiving(claim_id,receiving_id,expected_session_id)

    def _read_intake_receiving(self,claim_id,receiving_id,expected):
        from hosted_labels import fingerprint
        def invalid():
            return CloudError('The original intake history or plan changed. Refresh its review; saved work was kept.',
                              status=409,code='INTAKE_RECEIVING_CHANGED')
        try:
            if type(claim_id) is not str or str(uuid.UUID(claim_id))!=claim_id:raise ValueError()
            if receiving_id is not None and (type(receiving_id) is not str or str(uuid.UUID(receiving_id))!=receiving_id):raise ValueError()
        except (ValueError,TypeError,AttributeError):raise invalid() from None
        with self.lock:
            if self._profile_snapshot()['profileMode']!='hosted':
                raise CloudError('Use the original store’s hosted profile to review intake stock.',status=409,code='HOSTED_WORKFLOW_REQUIRED')
            authority=self.record_authority(expected_session_id=expected)
            if authority['role'] not in ('owner','manager'):
                raise CloudError('An owner or manager must review original intake stock.',status=403)
            saved=self._saved();self._expected(saved,expected)
            claim=self._call('GET','/api/intake-claims/'+claim_id,token=saved['token'])
            history=self._call('GET','/api/receiving/intakes/'+claim_id+'/history',token=saved['token'])
            try:
                if (type(claim) is not dict or claim.get('physical_intake_id')!=claim_id
                    or type(claim.get('manifest_hash')) is not str or not re.fullmatch('[a-f0-9]{64}',claim['manifest_hash'])
                    or type(history) is not dict or set(history)!={'version','physical_intake_id','operations','lookup_complete'}
                    or type(history['version']) is not int or history['version']!=1
                    or history['physical_intake_id']!=claim_id or history['lookup_complete'] is not True
                    or type(history['operations']) is not list or len(history['operations'])>1):raise ValueError()
                for operation in history['operations']:
                    if (type(operation) is not dict or type(operation.get('receiving_id')) is not str
                        or str(uuid.UUID(operation['receiving_id']))!=operation['receiving_id']
                        or type(operation.get('revision')) is not int or not 0<=operation['revision']<=2**63-1
                        or type(operation.get('plan_hash')) is not str or not re.fullmatch('[a-f0-9]{64}',operation['plan_hash'])
                        or operation.get('physical_intake_id')!=claim_id or operation.get('physical_manifest_hash')!=claim['manifest_hash']
                        or operation.get('workflow_kind') not in ('inventory_only','accepted_trade')
                        or operation.get('state') not in ('ready','cancelled_before_write','catalog_pending','catalog_confirmed',
                            'inventory_pending','outcome_unknown','needs_review','received')):raise ValueError()
                if receiving_id is not None and (len(history['operations'])!=1 or history['operations'][0]['receiving_id']!=receiving_id):raise ValueError()
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError):raise invalid() from None
            result=dict(authority,claim=copy.deepcopy(claim),history=copy.deepcopy(history))
            if receiving_id is not None:
                selected=history['operations'][0]
                if selected['workflow_kind']!='inventory_only':
                    raise CloudError('This stock plan belongs to an accepted agreement. Use its original agreement review.',
                                     status=409,code='INTAKE_RECEIVING_FINANCIAL_ORIGIN')
                operation=self._call('GET','/api/receiving/'+receiving_id,token=saved['token'])
                source=self._call('GET','/api/receiving/'+receiving_id+'/restoration-source',token=saved['token'])
                final=self._call('GET','/api/receiving/'+receiving_id,token=saved['token'])
                try:
                    if (fingerprint(selected)!=fingerprint(operation) or fingerprint(operation)!=fingerprint(final)
                        or type(source) is not dict or source.get('receivingId')!=receiving_id
                        or type(source.get('serverRevision')) is not int or source['serverRevision']!=selected['revision']
                        or source.get('planHash')!=selected['plan_hash'] or source.get('physicalIntakeId')!=claim_id
                        or source.get('physicalManifestHash')!=claim['manifest_hash'] or source.get('workflowKind')!='inventory_only'
                        or source.get('acceptedTradeId') is not None or source.get('acceptedReceiptHash') is not None):raise ValueError()
                except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):raise invalid() from None
                result.update(operation=copy.deepcopy(final),restorationSource=copy.deepcopy(source))
            final_claim=self._call('GET','/api/intake-claims/'+claim_id,token=saved['token'])
            try:
                if fingerprint(claim)!=fingerprint(final_claim):raise ValueError()
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):raise invalid() from None
            final_authority=self.record_authority(expected_session_id=expected)
            if any(final_authority.get(key)!=authority[key] for key in ('sessionId','tenantId','store','role')):raise invalid()
            with self._session_guard():
                current=self.assert_recovery_session_unlocked(expected,authority['tenantId'],authority['store'])
                if current.get('role') not in ('owner','manager'):raise invalid()
            return result

    def read_receiving_restoration(self,trade_id,receiving_id,*,expected_session_id):
        """Read one coherent original operation; never touch action journals.

        Full source/stock proof remains the pure verifier's responsibility. This
        boundary pins the selected history entry and all operation bytes across
        the fixed GET sequence, then rechecks remote and local read authority.
        """
        from hosted_labels import fingerprint
        with self.lock:
            trade_id=self.customer_id(trade_id);receiving_id=self.customer_id(receiving_id)
            evidence=self.read_original_trade(trade_id,expected_session_id=expected_session_id)
            saved=self._saved();self._expected(saved,expected_session_id)
            claim_id=evidence['claim']['physical_intake_id']
            history=self._call('GET','/api/receiving/intakes/'+claim_id+'/history',token=saved['token'])
            if (set(history)!={'version','physical_intake_id','operations','lookup_complete'}
                or type(history.get('version')) is not int or history['version']!=1
                or history.get('physical_intake_id')!=claim_id or history.get('lookup_complete') is not True
                or type(history.get('operations')) is not list or len(history['operations'])!=1
                or not isinstance(history['operations'][0],dict)
                or history['operations'][0].get('receiving_id')!=receiving_id):
                raise CloudError('The selected original operation is not in a complete unambiguous intake history. Refresh its original reference.',
                                 status=409,code='ORIGINAL_RECEIVING_CHANGED')
            operation=self._call('GET','/api/receiving/'+receiving_id,token=saved['token'])
            source=self._call('GET','/api/receiving/'+receiving_id+'/restoration-source',token=saved['token'])
            final=self._call('GET','/api/receiving/'+receiving_id,token=saved['token'])
            try:
                selected=history['operations'][0]
                if (not isinstance(selected,dict) or selected.get('receiving_id')!=receiving_id
                    or fingerprint(selected)!=fingerprint(operation) or fingerprint(operation)!=fingerprint(final)):raise ValueError()
                if (operation.get('receiving_id')!=receiving_id or type(operation.get('revision')) is not int
                    or not 0<=operation['revision']<=2**63-1
                    or not isinstance(operation.get('plan_hash'),str) or not re.fullmatch('[a-f0-9]{64}',operation['plan_hash'])
                    or operation.get('physical_intake_id')!=claim_id
                    or operation.get('physical_manifest_hash')!=evidence['claim']['manifest_hash']):raise ValueError()
                if operation.get('workflow_kind')=='accepted_trade':
                    if (operation.get('accepted_trade_id')!=trade_id
                        or operation.get('accepted_receipt_hash')!=evidence['trade']['receipt_hash']):raise ValueError()
                elif (operation.get('workflow_kind')!='inventory_only' or operation.get('state')!='cancelled_before_write'
                      or operation.get('accepted_trade_id') is not None or operation.get('accepted_receipt_hash') is not None):raise ValueError()
                fields={'version','receivingId','serverRevision','planHash','sourceSnapshotHash','physicalIntakeId',
                    'physicalManifestHash','workflowKind','acceptedTradeId','acceptedReceiptHash','normalizedPlan',
                    'admission','stateBasisHash','restorationSourceHash'}
                if (set(source)!=fields or type(source.get('version')) is not int or source['version']!=1
                    or type(source.get('serverRevision')) is not int or not isinstance(source.get('normalizedPlan'),dict)
                    or type(source.get('admission')) is not dict
                    or set(source['admission'])!={'everAdmitted','catalogStepPresent','inventoryStepPresent','leaseActive'}
                    or any(type(value) is not bool for value in source['admission'].values())):raise ValueError()
                links={'receivingId':'receiving_id','serverRevision':'revision','planHash':'plan_hash',
                    'sourceSnapshotHash':'source_snapshot_hash','physicalIntakeId':'physical_intake_id',
                    'physicalManifestHash':'physical_manifest_hash','workflowKind':'workflow_kind',
                    'acceptedTradeId':'accepted_trade_id','acceptedReceiptHash':'accepted_receipt_hash'}
                if any(source[left]!=operation.get(right) for left,right in links.items()):raise ValueError()
                if (source['stateBasisHash']!=fingerprint({'domain':'stock-and-sleeve:receiving-restoration-state:v1',
                        'operation':operation,'admission':source['admission']})
                    or source['restorationSourceHash']!=fingerprint({'domain':'stock-and-sleeve:receiving-restoration-source:v1',
                        'source':{key:value for key,value in source.items() if key!='restorationSourceHash'}})):raise ValueError()
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):
                raise CloudError('The original receiving history, plan or state changed while reading. Refresh its original reference; saved work was kept.',
                                 status=409,code='ORIGINAL_RECEIVING_CHANGED') from None
            # This is deliberately the last remote read, after all large hashes.
            me=self._call('GET','/api/me',token=saved['token']);self._verified_me(me,saved)
            if (me.get('store')!=evidence['store'] or me.get('tenant_id')!=evidence['tenantId']
                or me.get('role') not in ('owner','manager')):
                raise CloudError('Current owner or manager access to this store is required. Saved work was kept.',status=403)
            with self._session_guard():
                current=self.assert_recovery_session_unlocked(expected_session_id,evidence['tenantId'],evidence['store'])
                if current.get('role') not in ('owner','manager'):
                    raise CloudError('The signed-in receiving workspace changed. Saved work was kept.',status=409,session_changed=True)
            return {'sessionId':evidence['sessionId'],'tenantId':evidence['tenantId'],'store':evidence['store'],
                    'role':me['role'],'trade':evidence['trade'],'claim':evidence['claim'],
                    'history':history,'operation':final,'restorationSource':source}

    def assert_recovery_session_unlocked(self,expected,tenant,store):
        """Caller holds the short session guard; never performs network I/O."""
        import math
        saved=self._saved_unlocked();self._expected(saved,expected)
        if (not saved or saved.get('tenant_id')!=tenant or saved.get('store')!=store
            or type(saved.get('expires_at')) not in (int,float)
            or not math.isfinite(saved['expires_at']) or saved['expires_at']<=self.clock()):
            raise CloudError('The signed-in workspace changed. Saved work was kept.',status=409,session_changed=True)
        return saved

    def trade_request(self,key,*,expected_session_id=None):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('Invalid saved trade request.')
        return self._authenticated('GET','/api/credit/requests/'+quote(key,safe=''),expected_session_id=expected_session_id)

    def trade_create(self,body,receipt_id,*,expected_session_id=None,_journal_holder=None):
        return self._mutate('trade_create',body,expected_session_id=expected_session_id,receipt_id=receipt_id,_journal_holder=_journal_holder)

    def trade_accept(self,ident,body,receipt_id,*,expected_session_id=None,_journal_holder=None):
        return self._mutate('trade_accept',body,expected_session_id=expected_session_id,trade_id=self.customer_id(ident),receipt_id=receipt_id,_journal_holder=_journal_holder)

    def trade_void(self,ident,body,receipt_id,*,expected_session_id=None,_journal_holder=None):
        return self._mutate('trade_void',body,expected_session_id=expected_session_id,trade_id=self.customer_id(ident),receipt_id=receipt_id,_journal_holder=_journal_holder)

    def trade_journal(self,key,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to the store that owns this trade.',status=401)
            return next((dict(row) for row in self._pending() if row['store']==saved['store'] and row['request_key']==key and row['kind'].startswith('trade_')),None)

    def trade_finish(self,key,*,expected_session_id=None,original_row=None):
        with self.lock:
            saved=self.pending_storage.session(expected_session_id)
            self.pending_storage.finish_key(key,saved,trade_only=True,original_row=original_row)

    def _intake_pending(self):
        """Separate typed journal; never rewrite the original credit/trade journal."""
        return self.intake_storage.read()

    def _parse_intake_text(self,raw):
        if not raw:return []
        try:
            value=json.loads(raw)
            if (not isinstance(value,dict) or value.get('version')!=1 or value.get('origin')!=ORIGIN
                or not isinstance(value.get('operations'),list) or len(value['operations'])>50):raise ValueError()
            rows=value['operations'];seen=set()
            for row in rows:
                if (not isinstance(row,dict) or row.get('kind')!='intake_claim'
                    or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,49}',row.get('store',''))
                    or not re.fullmatch(r'R-[A-Za-z0-9-]{1,100}',row.get('receipt_id',''))
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',row.get('request_key',''))
                    or not isinstance(row.get('body'),dict) or row['body'].get('request_key')!=row['request_key']
                    or row.get('fingerprint')!=self._fingerprint(row['body'])):raise ValueError()
                identity=(row['store'],row['request_key'])
                if identity in seen:raise ValueError()
                seen.add(identity)
                if 'result' in row and (not isinstance(row['result'],dict) or row.get('result_fingerprint')!=self._fingerprint(row['result'])):raise ValueError()
            return rows
        except (ValueError,TypeError,AttributeError):
            raise CloudError('The saved physical-intake journal needs owner recovery. Keep the original batch; do not create a replacement physical intake.',status=409,pending=True) from None

    def _save_intake_pending(self,rows):
        return self.intake_storage.save(rows)

    def intake_get(self,ident,*,expected_session_id=None):
        return self._authenticated('GET','/api/intake-claims/'+self.customer_id(ident),expected_session_id=expected_session_id)

    def intake_request(self,key,*,expected_session_id=None):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('Invalid saved physical-intake request.')
        return self._authenticated('GET','/api/intake-claims/requests/'+quote(key,safe=''),expected_session_id=expected_session_id)

    def intake_create(self,body,receipt_id,*,expected_session_id=None,_journal_holder=None):
        with self.lock:
            saved=self.intake_storage.session(expected_session_id)
            key=body.get('request_key') if isinstance(body,dict) else None
            if (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key)
                or not isinstance(receipt_id,str) or not re.fullmatch(r'R-[A-Za-z0-9-]{1,100}',receipt_id)
                or set(body)-{'request_key','physical_reference','source_manifest','physical_delivery_verified',
                              'additional_delivery_of','additional_delivery_confirmed','additional_delivery_reason'}):
                raise CloudError('Use the supported physical-intake request fields.')
            existing=next((row for row in self._intake_pending() if row['store']==saved['store'] and row['request_key']==key),None)
            if existing is None:self._preflight_body(body)
            hashed=self._fingerprint(body)
            candidate={'kind':'intake_claim','store':saved['store'],'receipt_id':receipt_id,'request_key':key,
                       'body':body,'fingerprint':hashed,'created_at':self.clock()}
            row,_=self.intake_storage.reserve(candidate,saved)
            if _journal_holder is not None:_journal_holder['row']=copy.deepcopy(row)
            if 'result' in row:return row['result']
            try:result=self._authenticated('POST','/api/intake-claims',data=row['body'],expected_session_id=saved['session_id'])
            except CloudError as error:error.pending=True;raise
            self.intake_storage.remember(row,result,saved)
            return result

    def intake_journal(self,key,*,expected_session_id=None):
        with self.lock:
            saved=self._saved();self._expected(saved,expected_session_id)
            if not saved:raise CloudError('Sign in to recover this physical intake.',status=401)
            return next((dict(row) for row in self._intake_pending() if row['store']==saved['store'] and row['request_key']==key),None)

    def intake_remember(self,key,result,*,expected_session_id=None,original_row=None):
        with self.lock:
            saved=self.intake_storage.session(expected_session_id)
            if original_row is None or not isinstance(result,dict):raise CloudError('The original physical-intake journal is missing. Keep the batch for owner recovery.',status=409,pending=True)
            row=self.intake_storage.original(original_row,saved,key=key)
            self.intake_storage.remember(row,result,saved)

    def intake_finish(self,key,*,expected_session_id=None,original_row=None):
        with self.lock:
            saved=self.intake_storage.session(expected_session_id)
            self.intake_storage.finish_key(key,saved,original_row=original_row)

    def receiving_readiness(self,*,expected_session_id=None):
        return self._authenticated('GET','/api/receiving/readiness',expected_session_id=expected_session_id)

    def receiving_locations(self,*,expected_session_id=None):
        return self._authenticated('GET','/api/receiving/locations',expected_session_id=expected_session_id)

    def receiving_get(self,ident,*,expected_session_id=None):
        from station_hosted_receive import uuid_value
        return self._authenticated('GET','/api/receiving/'+uuid_value(ident),expected_session_id=expected_session_id)

    def receiving_request(self,key,*,expected_session_id=None):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}',key):raise CloudError('Invalid saved receiving request.')
        return self._authenticated('GET','/api/receiving/requests/'+quote(key,safe=''),expected_session_id=expected_session_id)

    def receiving_mutate(self,kind,body,receipt_id,label_profile,*,expected_session_id=None,receiving_id=None):
        return self.receiving.mutate(kind,body,receipt_id,label_profile,expected_session_id,receiving_id)
