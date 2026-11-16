"""Server-only Square OAuth and minimal signed-event ingress."""
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
import hashlib
import base64
import hmac
import json
import logging
import re
import secrets
import time
import uuid
from urllib.parse import urlencode, quote, urlsplit
from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from psycopg.types.json import Jsonb
import requests
from .security import ServiceError, digest, provider_id, webhook_valid
from .ledger import require_role
from .database import audit
from .intake import current_authority
from .cleanup_events import signed_envelope, inventory_event, record_inventory
from .square_renewal import renewal_health


_LOG=logging.getLogger(__name__)


@dataclass(frozen=True,repr=False)
class _TokenContext:
    application_id: str
    secret: str
    environment: str
    base: str
    api_version: str
    encryption_keys: str
    public_origin: str
    cipher: object
    transport: object


@dataclass(frozen=True)
class VerifiedSquareAccess:
    """Ephemeral server-only authority for the exact introspected OAuth token.

    Never serialize this object to a browser, desktop, journal or audit record.
    The redacted repr prevents accidental token disclosure in diagnostics.
    A receiving adapter still rechecks staff authority and its frozen operation.
    """
    token: str = field(repr=False)
    proof: dict
    generation: str | None = field(default=None, repr=False)


class Square:
    SCOPES=('MERCHANT_PROFILE_READ','CUSTOMERS_READ','ITEMS_READ','INVENTORY_READ','ORDERS_READ','PAYMENTS_READ')
    RECEIVING_SCOPES=SCOPES+('ITEMS_WRITE','INVENTORY_WRITE')
    AUTHORITY_SCOPES=frozenset(SCOPES+('ITEMS_WRITE','INVENTORY_WRITE'))
    API_VERSION='2026-09-16'

    def __init__(self,database,config,transport=None):
        self.database,self.config=database,config
        self.transport=transport or requests.Session()
        self.transport.trust_env=False
        env=config.get('SQUARE_ENVIRONMENT','sandbox')
        if env not in ('sandbox','production'):raise ServiceError('Invalid Square environment.',503)
        self.environment=env
        self.base='https://connect.squareupsandbox.com' if env=='sandbox' else 'https://connect.squareup.com'
        self.redirect=config['PUBLIC_BASE_URL']+'/oauth/square/callback'
        self.webhook_url=config['PUBLIC_BASE_URL']+'/webhooks/square'
        keys=[k.strip() for k in config.get('APP_ENCRYPTION_KEYS','').split(',') if k.strip()]
        def encryption_key(value):
            # Hosting-generated random secrets can be used without exposing key material
            # in source. This is domain-separated derivation, not password stretching.
            if len(value)<32:raise ValueError()
            if len(value)==44:
                try:return Fernet(value.encode())
                except (ValueError,TypeError):pass
            derived=base64.urlsafe_b64encode(hashlib.sha256(b'stock-and-sleeve:tokens:v1:'+value.encode()).digest())
            return Fernet(derived)
        try:self.cipher=MultiFernet([encryption_key(k) for k in keys]) if keys else None
        except (ValueError,TypeError):raise ServiceError('The server encryption key is invalid.',503) from None
        self._cipher_configuration=config.get('APP_ENCRYPTION_KEYS','')
        self._configured_cipher=self.cipher

    def configured(self):
        return bool(self.config.get('SQUARE_APPLICATION_ID') and self.config.get('SQUARE_APPLICATION_SECRET') and self.cipher)

    def _token_context(self):
        """Freeze private request inputs; never log or serialize this object."""
        env=self.config.get('SQUARE_ENVIRONMENT','sandbox')
        base={'production':'https://connect.squareup.com','sandbox':'https://connect.squareupsandbox.com'}.get(env)
        try:
            app=provider_id(self.config.get('SQUARE_APPLICATION_ID'),'Square application ID')
            secret=self.config.get('SQUARE_APPLICATION_SECRET')
            keys=self.config.get('APP_ENCRYPTION_KEYS','')
            origin=self.config.get('PUBLIC_BASE_URL')
            if (not isinstance(secret,str) or not secret or not isinstance(keys,str) or not keys or
                    keys!=self._cipher_configuration or self.cipher is not self._configured_cipher or
                    not self.cipher or not base or self.environment!=env or self.base!=base or
                    self.transport.trust_env is not False or not isinstance(origin,str) or
                    self.redirect!=origin+'/oauth/square/callback' or self.API_VERSION!=Square.API_VERSION):raise ValueError()
            if isinstance(self.transport,requests.Session) and (self.transport.verify is not True or
                    self.transport.get_adapter(base+'/oauth2/token').max_retries.total!=0):
                raise ValueError()
        except (ValueError,TypeError,AttributeError):
            raise ServiceError('Square OAuth configuration changed or is unavailable. Reconnect before continuing.',503) from None
        return _TokenContext(app,secret,env,base,self.API_VERSION,keys,origin,self.cipher,self.transport)

    def _require_token_context(self,context):
        current=self._token_context()
        if (current.cipher is not context.cipher or current.transport is not context.transport or
                any(getattr(current,key)!=getattr(context,key) for key in
                    ('application_id','secret','environment','base','api_version','encryption_keys','public_origin'))):
            raise ServiceError('Square OAuth configuration changed during authorization. Retry from the store account.',409)

    def _notice_barrier(self,db,*,configuration=None):
        # This must be the transaction's first SQL, including before authority
        # reads. Read a fresh committed receipt cursor only AFTER the barrier.
        # No provider call may occur while this short app/environment lock is held.
        application=configuration.application_id if configuration else self.config['SQUARE_APPLICATION_ID']
        environment=configuration.environment if configuration else self.environment
        db.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                   ('square-notices:'+application+':'+environment,))

    def _notice_cursor(self,db,merchant=None,*,configuration=None):
        clause=' AND merchant_id=%s' if merchant is not None else ''
        args=[configuration.application_id,configuration.environment] if configuration else [self.config['SQUARE_APPLICATION_ID'],self.environment]
        if merchant is not None:args.append(merchant)
        row=db.execute('''SELECT COALESCE(MAX(notice_cursor),0) AS notice_cursor
                          FROM ss_square_revocation_receipts WHERE application_id=%s AND environment=%s'''+clause,args).fetchone()
        return row['notice_cursor']

    @staticmethod
    def _generation(row):
        value=row.get('authorization_generation') if row else None
        return str(value) if value is not None else None

    def status(self,actor):
        with self.database.transaction() as db:
            row=db.execute('SELECT merchant_id,environment,expires_at,revoked_at,requested_profile,pending_notice_cursor,oauth_application_id,token_obtained_at FROM ss_square_connections WHERE tenant_id=%s',(actor['tenant_id'],)).fetchone()
        if not row:
            state='not_connected';message='This store has not connected Square.'
        elif row['environment']!=self.environment:
            state='environment_mismatch'
            message='The saved Square connection uses a different environment. Resolve the saved store connection before authorizing this environment.'
        elif row.get('pending_notice_cursor') is not None:
            state='revocation_pending'
            message='Square sent an authorization notice. Access is paused and saved work is preserved. The owner must start a new connection to continue.'
        elif row['revoked_at']:
            state='revoked';message='Square authorization was revoked. The store owner must reconnect.'
        elif row.get('oauth_application_id') is not None and row['oauth_application_id']!=self.config.get('SQUARE_APPLICATION_ID'):
            state='application_mismatch';message='The saved Square authorization belongs to a different application. The owner must reconnect.'
        else:
            state='connected';message='A Square authorization is saved for this environment. Checkout is not enabled.'
        connected=state=='connected'
        requested_profile=row.get('requested_profile','standard') if row else 'standard'
        return {'oauth_configured':self.configured(),'connected':connected,'connection_status':state,'message':message,
                'merchant_id':row['merchant_id'] if connected else None,
                'environment':self.environment,'saved_environment':row['environment'] if row else None,
                # A saved authorization and our requested scopes are not proof
                # of the permissions Square actually granted this token.
                'scopes':[],'requested_scopes':list(self.RECEIVING_SCOPES if requested_profile=='receiving' else self.SCOPES),
                'requested_profile':requested_profile,'grants_verified':False,
                'write_checkout_enabled':False,'renewal':renewal_health(row,self.config.get('SQUARE_APPLICATION_ID'))}

    def begin(self,actor,*,profile='standard'):
        require_role(actor,'owner')
        if profile not in ('standard','receiving'):raise ServiceError('Select a supported Square authorization purpose.')
        scopes=self.RECEIVING_SCOPES if profile=='receiving' else self.SCOPES
        if not self.configured():raise ServiceError('The hosted Square application is not configured yet.',503)
        state=secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            self._notice_barrier(db)
            current_authority(db,actor,fresh_mfa=profile=='receiving',allowed_roles=('owner',))
            cursor=self._notice_cursor(db)
            connection=db.execute('SELECT authorization_generation FROM ss_square_connections WHERE tenant_id=%s',
                                  (actor['tenant_id'],)).fetchone()
            db.execute('DELETE FROM ss_oauth_states WHERE expires_at<now()')
            db.execute('''INSERT INTO ss_oauth_states(state_hash,tenant_id,session_hash,expires_at,requested_profile,application_id,environment,requested_scopes,notice_cursor,expected_connection_generation)
                          VALUES (%s,%s,%s,now()+interval '5 minutes',%s,%s,%s,%s,%s,%s)''',
                       (digest(state),actor['tenant_id'],actor['session_hash'],profile,self.config['SQUARE_APPLICATION_ID'],self.environment,Jsonb(list(scopes)),cursor,self._generation(connection)))
            audit(db,actor['tenant_id'],actor['staff_id'],'square-authorization-started',actor['tenant_id'],{'requested_profile':profile})
        return self.base+'/oauth2/authorize?'+urlencode({'client_id':self.config['SQUARE_APPLICATION_ID'],'redirect_uri':self.redirect,
                   'scope':' '.join(scopes),'state':state,'session':'false'})

    def _token(self,payload,*,configuration=None):
        # Timeout/deadline checks are cooperative at operation/chunk boundaries,
        # not a hard cancellation guarantee while blocking I/O is pending.
        context=configuration or self._token_context()
        response=None
        try:
            deadline=time.monotonic()+25
            response=context.transport.post(context.base+'/oauth2/token',json=payload|{'client_id':context.application_id,
                         'client_secret':context.secret},headers={'Square-Version':context.api_version},
                         timeout=20,allow_redirects=False,stream=True,verify=True)
            if response.status_code!=200:raise ValueError()
            length=response.headers.get('Content-Length')
            if length is not None and (not isinstance(length,str) or not re.fullmatch(r'[0-9]{1,8}',length) or int(length)>65536):raise ValueError()
            chunks=[];total=0
            for chunk in response.iter_content(chunk_size=8192):
                if not isinstance(chunk,bytes) or time.monotonic()>=deadline:raise ValueError()
                total+=len(chunk)
                if total>65536:raise ValueError()
                chunks.append(chunk)
            result=signed_envelope(b''.join(chunks))
            errors=result.get('errors')
            if errors is not None and (type(errors) is not list or errors):raise ValueError()
            if not all(isinstance(result.get(k),str) and 1<=len(result[k])<=1024 and
                    all(33<=ord(c)<=126 for c in result[k]) for k in ('access_token','refresh_token')):
                raise ValueError()
            if (not isinstance(result.get('expires_at'),str) or not 20<=len(result['expires_at'])<=48 or
                    result.get('token_type','bearer')!='bearer' or result.get('client_id',context.application_id)!=context.application_id):raise ValueError()
            if payload.get('grant_type')=='authorization_code' and 'merchant_id' not in result:
                raise ValueError()
            if 'merchant_id' in result:
                result['merchant_id']=provider_id(result['merchant_id'],'Square merchant ID')
            value=result['expires_at']
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})',value):raise ValueError()
            if int(value[11:13])>23 or int(value[14:16])>59 or int(value[17:19])>59:raise ValueError()
            if not value.endswith('Z') and (int(value[-5:-3])>23 or int(value[-2:])>59):raise ValueError()
            expiry=datetime.fromisoformat(value.replace('Z','+00:00'))
            if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError()
            return result,expiry
        except Exception:
            raise ServiceError('Square authorization could not be confirmed. Reconnect from your store account.',502) from None
        finally:
            if response is not None:
                try:response.close()
                except Exception:raise ServiceError('Square authorization could not be confirmed. Reconnect from your store account.',502) from None

    def complete(self,actor,state,code,error=None):
        require_role(actor,'owner')
        if not self.configured():raise ServiceError('Square OAuth is not configured.',503)
        configuration=self._token_context()
        # Consume state before network access. A failed/uncertain exchange needs a new flow.
        with self.database.transaction() as db:
            self._notice_barrier(db,configuration=configuration)
            self._require_token_context(configuration)
            peek=db.execute('SELECT requested_profile FROM ss_oauth_states WHERE state_hash=%s',(digest(str(state)),)).fetchone()
            current_authority(db,actor,fresh_mfa=bool(peek and peek['requested_profile']=='receiving'),allowed_roles=('owner',))
            row=db.execute('SELECT * FROM ss_oauth_states WHERE state_hash=%s FOR UPDATE',(digest(str(state)),)).fetchone()
            if not row or row['tenant_id']!=actor['tenant_id'] or not hmac.compare_digest(row['session_hash'],actor['session_hash']) or row['expires_at']<datetime.now(timezone.utc):
                raise ServiceError('Authorization expired or belongs to another store session.',403)
            profile=row.get('requested_profile')
            expected=list(self.RECEIVING_SCOPES if profile=='receiving' else self.SCOPES)
            if (profile not in ('standard','receiving') or row.get('application_id')!=configuration.application_id or
                    row.get('environment')!=configuration.environment or row.get('requested_scopes')!=expected):
                raise ServiceError('The authorization purpose, application or environment changed. Start a new Square connection.',409)
            notice_cursor=row.get('notice_cursor')
            if type(notice_cursor) is not int or not 0<=notice_cursor<=self._notice_cursor(db,configuration=configuration):
                raise ServiceError('Start a new Square connection after the current authorization notice.',409)
            expected_generation=str(row['expected_connection_generation']) if row.get('expected_connection_generation') is not None else None
            if profile=='receiving':current_authority(db,actor,fresh_mfa=True,allowed_roles=('owner',))
            db.execute('DELETE FROM ss_oauth_states WHERE state_hash=%s',(row['state_hash'],))
        if error or not code:raise ServiceError('Square authorization was declined. You can start again.')
        self._require_token_context(configuration)
        result,expiry=self._token({'grant_type':'authorization_code','code':code,'redirect_uri':configuration.public_origin+'/oauth/square/callback'},configuration=configuration)
        merchant=provider_id(result.get('merchant_id'),'Square merchant ID')
        with self.database.transaction() as db:
            self._notice_barrier(db,configuration=configuration)
            self._require_token_context(configuration)
            # The provider response can outlive the owner's session or role.
            # Both profiles need live authority; only receiving requires step-up.
            current_authority(db,actor,fresh_mfa=profile=='receiving',allowed_roles=('owner',))
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-tenant:'+str(actor['tenant_id']),))
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-merchant:'+merchant,))
            existing=db.execute('SELECT tenant_id,merchant_id,environment,authorization_generation FROM ss_square_connections WHERE tenant_id=%s OR merchant_id=%s FOR UPDATE',(actor['tenant_id'],merchant)).fetchall()
            if any(r['tenant_id']!=actor['tenant_id'] or r['merchant_id']!=merchant or r['environment']!=configuration.environment for r in existing):
                raise ServiceError('A different store/merchant connection already exists. Resolve it before reconnecting.',409)
            current=next((item for item in existing if item['tenant_id']==actor['tenant_id']),None)
            if self._generation(current)!=expected_generation or self._notice_cursor(db,merchant,configuration=configuration)>notice_cursor:
                raise ServiceError('Square authorization changed after this flow began. Start a new owner connection; saved work is preserved.',409)
            if expiry<=datetime.now(timezone.utc)+timedelta(minutes=1):raise ServiceError('Square authorization expired before it could be saved. Start a new connection.',409)
            self._require_token_context(configuration)
            sealed=configuration.cipher.encrypt(json.dumps(result).encode()).decode()
            db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at,requested_profile,authorization_generation,oauth_application_id,token_obtained_at)
                          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp()) ON CONFLICT (tenant_id) DO UPDATE SET encrypted_tokens=EXCLUDED.encrypted_tokens,
                          expires_at=EXCLUDED.expires_at,requested_profile=EXCLUDED.requested_profile,authorization_generation=EXCLUDED.authorization_generation,
                          oauth_application_id=EXCLUDED.oauth_application_id,token_obtained_at=EXCLUDED.token_obtained_at,
                          renewal_lease_token=NULL,renewal_lease_until=NULL,renewal_retry_after=NULL,renewal_error_code=NULL,
                          revoked_at=NULL,pending_notice_cursor=NULL,updated_at=now()''',
                       (actor['tenant_id'],merchant,configuration.environment,sealed,expiry,profile,uuid.uuid4(),configuration.application_id))
            audit(db,actor['tenant_id'],actor['staff_id'],'square-connected',merchant,{'environment':configuration.environment,'requested_profile':profile})
        return self.status(actor)

    def access_token(self,tenant):
        token,_=self._access_or_renew(tenant)
        return token

    def renew(self,tenant):
        """Private background entry point. Return metadata only, never a token."""
        _,renewed=self._access_or_renew(tenant)
        return {'status':'renewed' if renewed else 'not_due'}

    def _locked_token_row(self,db,tenant,context,merchant=None,*,active=True):
        self._notice_barrier(db,configuration=context)  # FIRST SQL, including before any authority read.
        self._require_token_context(context)
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-tenant:'+str(tenant),))
        if merchant is None:
            peek=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
            self._usable_token_row(peek,context)
            merchant=peek['merchant_id']
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-merchant:'+merchant,))
        row=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s FOR UPDATE',(tenant,)).fetchone()
        if active:self._usable_token_row(row,context)
        if row and row['merchant_id']!=merchant:raise ServiceError('Square merchant changed during authorization.',409)
        return row

    @staticmethod
    def _usable_token_row(row,context):
        if (not row or row['revoked_at'] or row.get('pending_notice_cursor') is not None or row['environment']!=context.environment or
                (row.get('oauth_application_id') is not None and row['oauth_application_id']!=context.application_id)):
            raise ServiceError('Reconnect Square in the configured application and environment before continuing.',409)

    @staticmethod
    def _same_token_authority(current,snapshot):
        return bool(current and all(current.get(key)==snapshot.get(key) for key in
            ('tenant_id','merchant_id','environment','authorization_generation','encrypted_tokens','expires_at',
             'oauth_application_id','token_obtained_at','requested_profile')))

    def _renewal_failed(self,tenant,context,snapshot,lease,code):
        # A changed config/connection leaves at most the old expiring lease.
        # Failure cleanup never changes saved credentials, lineage or authority.
        try:
            self._require_token_context(context)
            with self.database.transaction() as db:
                row=self._locked_token_row(db,tenant,context,snapshot['merchant_id'],active=False)
                if self._same_token_authority(row,snapshot) and row['renewal_lease_token']==lease:
                    db.execute('''UPDATE ss_square_connections SET renewal_lease_token=NULL,renewal_lease_until=NULL,
                        renewal_retry_after=clock_timestamp()+interval '15 minutes',renewal_error_code=%s
                        WHERE tenant_id=%s AND renewal_lease_token=%s''',(code,tenant,lease))
        except ServiceError:
            pass  # No authority is granted; the lease/cooldown expires naturally.

    def _access_or_renew(self,tenant):
        # A foreground failure preserves credentials and fails the operation;
        # it never falls back to a stale/unpublished response. Lease expiry is
        # a publication fence, not cancellation of the old network request.
        context=self._token_context()
        with self.database.transaction() as db:
            row=self._locked_token_row(db,tenant,context)
            now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            health=renewal_health(row,context.application_id,now=now)
            due=health['due']
            if due:
                if row['renewal_lease_until'] is not None and row['renewal_lease_until']>now:
                    raise ServiceError('Square authorization is being renewed. Retry shortly.',409)
                if row['renewal_retry_after'] is not None and row['renewal_retry_after']>now:
                    raise ServiceError('Square authorization renewal is waiting before another attempt. Retry later or reconnect.',409)
                lease=uuid.uuid4()
                db.execute('''UPDATE ss_square_connections SET renewal_lease_token=%s,
                    renewal_lease_until=clock_timestamp()+interval '60 seconds',renewal_attempted_at=clock_timestamp(),
                    renewal_retry_after=clock_timestamp()+interval '15 minutes',renewal_error_code=NULL WHERE tenant_id=%s''',(lease,tenant))
            self._require_token_context(context)
            snapshot=dict(row)
            notice=self._notice_cursor(db,row['merchant_id'],configuration=context)
        # Capture has committed. Decryption/HTTP hold NO transaction or SQL lock.
        if due and (health['stale'] or health['age_status']=='unknown'):
            _LOG.warning('Square OAuth renewal needs attention for tenant %s; token age is %s.',tenant,health['age_status'])
        try:
            tokens=json.loads(context.cipher.decrypt(snapshot['encrypted_tokens'].encode()))
            if (not isinstance(tokens,dict) or any(not isinstance(tokens.get(k),str) or not 1<=len(tokens[k])<=1024 or
                    not all(33<=ord(c)<=126 for c in tokens[k]) for k in ('access_token','refresh_token')) or
                    tokens.get('merchant_id',snapshot['merchant_id'])!=snapshot['merchant_id']):raise ValueError()
            if not due:
                self._require_token_context(context)
                return tokens['access_token'],False
            self._require_token_context(context)
            refreshed,expiry=self._token({'grant_type':'refresh_token','refresh_token':tokens['refresh_token']},configuration=context)
            if refreshed.get('merchant_id')!=snapshot['merchant_id']:
                raise ServiceError('Square merchant changed during refresh.',409)
            if (not hmac.compare_digest(refreshed['refresh_token'],tokens['refresh_token']) or
                    hmac.compare_digest(refreshed['access_token'],tokens['access_token']) or expiry<=snapshot['expires_at']):
                raise ServiceError('Square did not return a new private-code-flow authorization. Reconnect or retry later.',502)
            merged=tokens|refreshed
            sealed=context.cipher.encrypt(json.dumps(merged).encode()).decode()
            with self.database.transaction() as db:
                current=self._locked_token_row(db,tenant,context,snapshot['merchant_id'])
                checked=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
                if (not self._same_token_authority(current,snapshot) or current['renewal_lease_token']!=lease or
                        current['renewal_lease_until']<=checked or self._notice_cursor(db,snapshot['merchant_id'],configuration=context)!=notice or
                        expiry<=checked+timedelta(minutes=1)):
                    raise ServiceError('Square authorization changed during renewal. Retry the operation with current authorization.',409)
                self._require_token_context(context)
                saved=db.execute('''UPDATE ss_square_connections SET encrypted_tokens=%s,expires_at=%s,
                    oauth_application_id=%s,token_obtained_at=%s,renewal_lease_token=NULL,renewal_lease_until=NULL,
                    renewal_retry_after=NULL,renewal_error_code=NULL,updated_at=clock_timestamp()
                    WHERE tenant_id=%s AND authorization_generation=%s AND merchant_id=%s AND environment=%s
                      AND encrypted_tokens=%s AND expires_at=%s AND oauth_application_id IS NOT DISTINCT FROM %s
                      AND token_obtained_at IS NOT DISTINCT FROM %s AND requested_profile IS NOT DISTINCT FROM %s AND renewal_lease_token=%s
                      AND renewal_lease_until>clock_timestamp() AND revoked_at IS NULL AND pending_notice_cursor IS NULL
                      AND %s>clock_timestamp()+interval '1 minute'
                    RETURNING tenant_id''',(sealed,expiry,context.application_id,checked,tenant,snapshot['authorization_generation'],
                        snapshot['merchant_id'],snapshot['environment'],snapshot['encrypted_tokens'],snapshot['expires_at'],
                        snapshot['oauth_application_id'],snapshot['token_obtained_at'],snapshot['requested_profile'],lease,expiry)).fetchone()
                if not saved:raise ServiceError('Square authorization changed during renewal. Retry with current authorization.',409)
            self._require_token_context(context)
            return merged['access_token'],True
        except (InvalidToken,ValueError,KeyError,TypeError,AttributeError) as error:
            if due:self._renewal_failed(tenant,context,snapshot,lease,'authority_changed' if isinstance(error,ServiceError) and error.status==409 else 'provider_failure')
            if isinstance(error,ServiceError):raise
            raise ServiceError('Saved Square authorization could not be renewed. Reconnect or retry later.',503) from None

    def retrieve(self,tenant,kind,resource_id,*,catalog_version=None):
        if kind not in ('orders','payments','refunds','locations','catalog'):raise ServiceError('Unsupported Square read.')
        if kind=='catalog':
            if type(catalog_version) is not int or not 1<=catalog_version<2**63:
                raise ServiceError('An exact supported catalog version is required.')
        elif catalog_version is not None:raise ServiceError('Catalog version is only supported for catalog reads.')
        if kind!='locations':resource_id=provider_id(resource_id,'Square resource ID')
        token=self.access_token(tenant)
        path=('/v2/catalog/object/'+quote(resource_id,safe='')+'?'+urlencode({'catalog_version':catalog_version}) if kind=='catalog'
              else '/v2/'+kind+('/'+quote(resource_id,safe='') if kind!='locations' else ''))
        try:
            response=self.transport.get(self.base+path,headers={'Authorization':'Bearer '+token,'Square-Version':self.API_VERSION},timeout=20,allow_redirects=False)
            if response.status_code!=200:raise ValueError()
            response.raise_for_status();return response.json()
        except Exception:raise ServiceError('Square could not confirm the resource. Retry reconciliation later.',502) from None

    def _authority_snapshot(self,tenant,token):
        with self.database.transaction() as db:
            row=db.execute('SELECT merchant_id,environment,encrypted_tokens,expires_at,revoked_at,authorization_generation,pending_notice_cursor,oauth_application_id,token_obtained_at FROM ss_square_connections WHERE tenant_id=%s',
                           (tenant,)).fetchone()
            try:
                if not row or row['revoked_at'] or row.get('pending_notice_cursor') is not None or row['environment']!=self.environment:raise ValueError()
                if row.get('oauth_application_id') is not None and row['oauth_application_id']!=self.config.get('SQUARE_APPLICATION_ID'):raise ValueError()
                saved=json.loads(self.cipher.decrypt(row['encrypted_tokens'].encode()))
                if not isinstance(token,str) or not token or not hmac.compare_digest(saved['access_token'],token):raise ValueError()
                provider_id(row['merchant_id'],'Square merchant ID')
                if row['expires_at']<=datetime.now(timezone.utc)+timedelta(minutes=1):raise ValueError()
            except (ValueError,TypeError,KeyError,InvalidToken,AttributeError):
                raise ServiceError('The saved Square authorization changed or is unavailable. Reconnect and review this operation.',409) from None
        return dict(row)

    def verified_access(self,tenant,required_scopes):
        """Verify actual token grants and identity; never infer from request scopes.

        Square documents token status for production credentials. Sandbox proof
        remains an explicit provider acceptance gate, not a permissive fallback.
        This performs a read-only OAuth introspection, never a catalog/stock write.
        """
        if (not isinstance(required_scopes,(tuple,list,frozenset,set)) or not required_scopes
                or any(not isinstance(scope,str) or scope not in self.AUTHORITY_SCOPES for scope in required_scopes)):
            raise ServiceError('Select supported server-owned Square permissions.')
        required=frozenset(required_scopes)
        if self.environment!='production':
            raise ServiceError('Square Sandbox token permission proof is not accepted yet. Hosted receiving remains unavailable until provider verification is completed.',409)
        token=self.access_token(tenant)
        snapshot=self._authority_snapshot(tenant,token)
        response=None
        try:
            response=self.transport.post(self.base+'/oauth2/token/status',
                headers={'Authorization':'Bearer '+token,'Square-Version':self.API_VERSION,'Content-Type':'application/json'},
                timeout=20,allow_redirects=False,stream=True)
            if response.status_code!=200:raise ValueError()
            length=response.headers.get('Content-Length')
            if length is not None and (not re.fullmatch(r'[0-9]{1,8}',length) or int(length)>65536):raise ValueError()
            chunks=[];total=0
            for chunk in response.iter_content(chunk_size=8192):
                if not isinstance(chunk,bytes):raise ValueError()
                total+=len(chunk)
                if total>65536:raise ValueError()
                chunks.append(chunk)
            result=json.loads(b''.join(chunks).decode('utf-8'))
            if not isinstance(result,dict) or result.get('errors'):raise ValueError()
            scopes=result.get('scopes')
            if (not isinstance(scopes,list) or not 1<=len(scopes)<=256
                    or any(not isinstance(scope,str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,99}',scope) for scope in scopes)
                    or len(set(scopes))!=len(scopes)):raise ValueError()
            if (result.get('client_id')!=self.config['SQUARE_APPLICATION_ID']
                    or result.get('merchant_id')!=snapshot['merchant_id']):raise ValueError()
            expiry=result.get('expires_at')
            if (not isinstance(expiry,str) or not re.fullmatch(
                    r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})',expiry)):raise ValueError()
            expires=datetime.fromisoformat(expiry.replace('Z','+00:00'))
            now=datetime.now(timezone.utc)
            if expires<=now+timedelta(minutes=1):raise ValueError()
        except Exception:
            raise ServiceError('Square token identity and permissions could not be verified. Reconnect or retry before receiving.',502) from None
        finally:
            if response is not None:response.close()
        # Revocation/reconnection while the provider call was in flight cannot
        # leave usable authority attached to the previous saved connection.
        current=self._authority_snapshot(tenant,token)
        if any(current.get(key)!=snapshot.get(key) for key in ('merchant_id','environment','encrypted_tokens','expires_at','oauth_application_id','token_obtained_at')) or self._generation(current)!=self._generation(snapshot):
            raise ServiceError('Square authorization changed during permission verification. Review and retry the same operation.',409)
        if not required.issubset(scopes):
            raise ServiceError('Square has not granted all required permissions. The owner must reconnect with receiving access.',409)
        proof={'merchant_id':snapshot['merchant_id'],'application_id':self.config['SQUARE_APPLICATION_ID'],
               'environment':self.environment,'scopes':sorted(scopes),'required_scopes':sorted(required),
               'expires_at':expires.astimezone(timezone.utc).isoformat(),'checked_at':now.isoformat(),
               'square_version':self.API_VERSION,'method':'oauth-token-status','grants_verified':True}
        return VerifiedSquareAccess(token,proof,self._generation(snapshot))

    def verified_grants(self,tenant,required_scopes):
        return self.verified_access(tenant,required_scopes).proof

    def ingest(self,raw,signature):
        key=self.config.get('SQUARE_WEBHOOK_SIGNATURE_KEY','')
        if not key:raise ServiceError('Square event ingress is not configured.',503)
        application=self.config.get('SQUARE_APPLICATION_ID')
        if not isinstance(application,str) or not application.strip():
            raise ServiceError('Square event ingress application identity is not configured.',503)
        if not webhook_valid(signature,self.webhook_url,raw,key):raise ServiceError('Event signature is invalid.',403)
        payload=signed_envelope(raw)
        if payload.get('type')=='inventory.count.updated':
            return record_inventory(self,raw,inventory_event(payload))
        try:
            event_id=provider_id(payload['event_id'],'Event ID')
            merchant=provider_id(payload['merchant_id'],'Merchant ID');kind=payload['type']
            resource=merchant if kind=='oauth.authorization.revoked' else provider_id(payload['data']['id'],'Resource ID')
        except (ValueError,KeyError,TypeError):raise ServiceError('Event envelope is invalid.') from None
        if kind not in ('order.created','order.updated','payment.created','payment.updated','refund.created','refund.updated','oauth.authorization.revoked'):
            return {'accepted':True,'ignored':True}
        fingerprint=hashlib.sha256(raw).hexdigest()
        with self.database.transaction() as db:
            if kind=='oauth.authorization.revoked':
                self._notice_barrier(db)
                scope=(self.config['SQUARE_APPLICATION_ID'],self.environment,event_id)
                old=db.execute('SELECT body_hash,outcome FROM ss_square_revocation_receipts WHERE application_id=%s AND environment=%s AND event_id=%s',scope).fetchone()
                if old:
                    if old['body_hash']!=fingerprint:raise ServiceError('Event ID conflicts with a previous notification.',409)
                    return dict(old['outcome'])|{'duplicate':True}
                peek=db.execute('SELECT tenant_id FROM ss_square_connections WHERE merchant_id=%s',(merchant,)).fetchone()
                if peek:db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-tenant:'+str(peek['tenant_id']),))
                db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-merchant:'+merchant,))
                conn=db.execute('SELECT tenant_id,environment FROM ss_square_connections WHERE merchant_id=%s FOR UPDATE',(merchant,)).fetchone()
                active=bool(conn and conn['environment']==self.environment)
                outcome={'accepted':True,'revocation_pending':True} if active else {'accepted':True,'ignored':True}
                inserted=db.execute('''INSERT INTO ss_square_revocation_receipts(application_id,environment,event_id,merchant_id,body_hash,outcome)
                                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (application_id,environment,event_id) DO NOTHING RETURNING notice_cursor''',
                                    (*scope,merchant,fingerprint,Jsonb(outcome))).fetchone()
                if not inserted:
                    old=db.execute('SELECT body_hash,outcome FROM ss_square_revocation_receipts WHERE application_id=%s AND environment=%s AND event_id=%s',scope).fetchone()
                    if not old or old['body_hash']!=fingerprint:raise ServiceError('Event ID conflicts with a previous notification.',409)
                    return dict(old['outcome'])|{'duplicate':True}
                if active:
                    # revoked_at is the existing LOCAL access-disable marker.
                    # It is not attributed to provider event time; ciphertext,
                    # jobs, claims, reservations and credit remain untouched.
                    db.execute('''UPDATE ss_square_connections SET revoked_at=COALESCE(revoked_at,clock_timestamp()),
                                  pending_notice_cursor=%s WHERE tenant_id=%s''',(inserted['notice_cursor'],conn['tenant_id']))
                    audit(db,conn['tenant_id'],None,'square-authorization-notice-pending',merchant,{'notice_cursor':inserted['notice_cursor']})
                return outcome|{'duplicate':False}
            conn=db.execute('SELECT tenant_id,revoked_at,pending_notice_cursor FROM ss_square_connections WHERE merchant_id=%s',(merchant,)).fetchone()
            # Pending means pause work, not discard arriving resource events.
            if not conn or (conn['revoked_at'] and conn.get('pending_notice_cursor') is None):return {'accepted':True,'ignored':True}
            old=db.execute('SELECT body_hash FROM ss_square_jobs WHERE event_id=%s',(event_id,)).fetchone()
            if old and old['body_hash']!=fingerprint:raise ServiceError('Event ID conflicts with a previous notification.',409)
            db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash)
                          VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(event_id,conn['tenant_id'],kind,resource,fingerprint))
            return {'accepted':True,'duplicate':bool(old)}
