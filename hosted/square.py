"""Server-only Square OAuth and minimal signed-event ingress."""
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
import hashlib
import base64
import hmac
import json
import re
import secrets
from urllib.parse import urlencode, quote, urlsplit
from cryptography.fernet import Fernet, MultiFernet, InvalidToken
from psycopg.types.json import Jsonb
import requests
from .security import ServiceError, digest, provider_id, webhook_valid
from .ledger import require_role
from .database import audit


@dataclass(frozen=True)
class VerifiedSquareAccess:
    """Ephemeral server-only authority for the exact introspected OAuth token.

    Never serialize this object to a browser, desktop, journal or audit record.
    The redacted repr prevents accidental token disclosure in diagnostics.
    A receiving adapter still rechecks staff authority and its frozen operation.
    """
    token: str = field(repr=False)
    proof: dict


class Square:
    SCOPES=('MERCHANT_PROFILE_READ','CUSTOMERS_READ','ITEMS_READ','INVENTORY_READ','ORDERS_READ','PAYMENTS_READ')
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

    def configured(self):
        return bool(self.config.get('SQUARE_APPLICATION_ID') and self.config.get('SQUARE_APPLICATION_SECRET') and self.cipher)

    def status(self,actor):
        with self.database.transaction() as db:
            row=db.execute('SELECT merchant_id,environment,expires_at,revoked_at FROM ss_square_connections WHERE tenant_id=%s',(actor['tenant_id'],)).fetchone()
        if not row:
            state='not_connected';message='This store has not connected Square.'
        elif row['environment']!=self.environment:
            state='environment_mismatch'
            message='The saved Square connection uses a different environment. Resolve the saved store connection before authorizing this environment.'
        elif row['revoked_at']:
            state='revoked';message='Square authorization was revoked. The store owner must reconnect.'
        else:
            state='connected';message='A Square authorization is saved for this environment. Checkout is not enabled.'
        connected=state=='connected'
        return {'oauth_configured':self.configured(),'connected':connected,'connection_status':state,'message':message,
                'merchant_id':row['merchant_id'] if connected else None,
                'environment':self.environment,'saved_environment':row['environment'] if row else None,
                # A saved authorization and our requested scopes are not proof
                # of the permissions Square actually granted this token.
                'scopes':[],'requested_scopes':list(self.SCOPES),'grants_verified':False,
                'write_checkout_enabled':False}

    def begin(self,actor):
        require_role(actor,'owner')
        if not self.configured():raise ServiceError('The hosted Square application is not configured yet.',503)
        state=secrets.token_urlsafe(32)
        with self.database.transaction() as db:
            db.execute('DELETE FROM ss_oauth_states WHERE expires_at<now()')
            db.execute("INSERT INTO ss_oauth_states(state_hash,tenant_id,session_hash,expires_at) VALUES (%s,%s,%s,now()+interval '5 minutes')",(digest(state),actor['tenant_id'],actor['session_hash']))
            audit(db,actor['tenant_id'],actor['staff_id'],'square-authorization-started',actor['tenant_id'])
        return self.base+'/oauth2/authorize?'+urlencode({'client_id':self.config['SQUARE_APPLICATION_ID'],'redirect_uri':self.redirect,
                   'scope':' '.join(self.SCOPES),'state':state,'session':'false'})

    def _token(self,payload):
        try:
            response=self.transport.post(self.base+'/oauth2/token',json=payload|{'client_id':self.config['SQUARE_APPLICATION_ID'],
                         'client_secret':self.config['SQUARE_APPLICATION_SECRET']},timeout=20,allow_redirects=False)
            if response.status_code!=200:raise ValueError()
            response.raise_for_status();result=response.json()
            if not all(isinstance(result.get(k),str) and result[k] for k in ('access_token','refresh_token','expires_at')):
                raise ValueError()
            if payload.get('grant_type')=='authorization_code' and 'merchant_id' not in result:
                raise ValueError()
            if 'merchant_id' in result:
                result['merchant_id']=provider_id(result['merchant_id'],'Square merchant ID')
            expiry=datetime.fromisoformat(result['expires_at'].replace('Z','+00:00'))
            if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError()
            return result,expiry
        except Exception:
            raise ServiceError('Square authorization could not be confirmed. Reconnect from your store account.',502) from None

    def complete(self,actor,state,code,error=None):
        require_role(actor,'owner')
        if not self.configured():raise ServiceError('Square OAuth is not configured.',503)
        # Consume state before network access. A failed/uncertain exchange needs a new flow.
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_oauth_states WHERE state_hash=%s FOR UPDATE',(digest(str(state)),)).fetchone()
            if not row or row['tenant_id']!=actor['tenant_id'] or not hmac.compare_digest(row['session_hash'],actor['session_hash']) or row['expires_at']<datetime.now(timezone.utc):
                raise ServiceError('Authorization expired or belongs to another store session.',403)
            db.execute('DELETE FROM ss_oauth_states WHERE state_hash=%s',(row['state_hash'],))
        if error or not code:raise ServiceError('Square authorization was declined. You can start again.')
        result,expiry=self._token({'grant_type':'authorization_code','code':code,'redirect_uri':self.redirect})
        merchant=provider_id(result.get('merchant_id'),'Square merchant ID')
        with self.database.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-tenant:'+str(actor['tenant_id']),))
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-merchant:'+merchant,))
            existing=db.execute('SELECT tenant_id,merchant_id,environment FROM ss_square_connections WHERE tenant_id=%s OR merchant_id=%s FOR UPDATE',(actor['tenant_id'],merchant)).fetchall()
            if any(r['tenant_id']!=actor['tenant_id'] or r['merchant_id']!=merchant or r['environment']!=self.environment for r in existing):
                raise ServiceError('A different store/merchant connection already exists. Resolve it before reconnecting.',409)
            sealed=self.cipher.encrypt(json.dumps(result).encode()).decode()
            db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                          VALUES (%s,%s,%s,%s,%s) ON CONFLICT (tenant_id) DO UPDATE SET encrypted_tokens=EXCLUDED.encrypted_tokens,
                          expires_at=EXCLUDED.expires_at,revoked_at=NULL,updated_at=now()''',
                       (actor['tenant_id'],merchant,self.environment,sealed,expiry))
            audit(db,actor['tenant_id'],actor['staff_id'],'square-connected',merchant,{'environment':self.environment})
        return self.status(actor)

    def access_token(self,tenant):
        if not self.configured():raise ServiceError('Square OAuth is not configured.',503)
        with self.database.transaction() as db:
            row=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s FOR UPDATE',(tenant,)).fetchone()
            if not row or row['revoked_at'] or row['environment']!=self.environment:raise ServiceError('Reconnect Square in the configured environment before continuing.',409)
            try:tokens=json.loads(self.cipher.decrypt(row['encrypted_tokens'].encode()))
            except (InvalidToken,ValueError,KeyError):raise ServiceError('Saved Square authorization is unavailable. Reconnect.',503) from None
            if row['expires_at']<datetime.now(timezone.utc)+timedelta(days=7):
                refreshed,expiry=self._token({'grant_type':'refresh_token','refresh_token':tokens['refresh_token']})
                if refreshed.get('merchant_id') and refreshed['merchant_id']!=row['merchant_id']:
                    raise ServiceError('Square merchant changed during refresh.',409)
                tokens|=refreshed
                sealed=self.cipher.encrypt(json.dumps(tokens).encode()).decode()
                db.execute('UPDATE ss_square_connections SET encrypted_tokens=%s,expires_at=%s,updated_at=now() WHERE tenant_id=%s',(sealed,expiry,tenant))
            return tokens['access_token']

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
            row=db.execute('SELECT merchant_id,environment,encrypted_tokens,expires_at,revoked_at FROM ss_square_connections WHERE tenant_id=%s',
                           (tenant,)).fetchone()
            try:
                if not row or row['revoked_at'] or row['environment']!=self.environment:raise ValueError()
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
        if any(current[key]!=snapshot[key] for key in ('merchant_id','environment','encrypted_tokens','expires_at')):
            raise ServiceError('Square authorization changed during permission verification. Review and retry the same operation.',409)
        if not required.issubset(scopes):
            raise ServiceError('Square has not granted all required permissions. The owner must reconnect with receiving access.',409)
        proof={'merchant_id':snapshot['merchant_id'],'application_id':self.config['SQUARE_APPLICATION_ID'],
               'environment':self.environment,'scopes':sorted(scopes),'required_scopes':sorted(required),
               'expires_at':expires.astimezone(timezone.utc).isoformat(),'checked_at':now.isoformat(),
               'square_version':self.API_VERSION,'method':'oauth-token-status','grants_verified':True}
        return VerifiedSquareAccess(token,proof)

    def verified_grants(self,tenant,required_scopes):
        return self.verified_access(tenant,required_scopes).proof

    def ingest(self,raw,signature):
        key=self.config.get('SQUARE_WEBHOOK_SIGNATURE_KEY','')
        if not key:raise ServiceError('Square event ingress is not configured.',503)
        if not webhook_valid(signature,self.webhook_url,raw,key):raise ServiceError('Event signature is invalid.',403)
        try:
            payload=json.loads(raw);event_id=provider_id(payload['event_id'],'Event ID')
            merchant=provider_id(payload['merchant_id'],'Merchant ID');kind=payload['type']
            resource=merchant if kind=='oauth.authorization.revoked' else provider_id(payload['data']['id'],'Resource ID')
        except (ValueError,KeyError,TypeError):raise ServiceError('Event envelope is invalid.') from None
        if kind not in ('order.created','order.updated','payment.created','payment.updated','refund.created','refund.updated','oauth.authorization.revoked'):
            return {'accepted':True,'ignored':True}
        with self.database.transaction() as db:
            conn=db.execute('SELECT tenant_id,revoked_at FROM ss_square_connections WHERE merchant_id=%s',(merchant,)).fetchone()
            if not conn or conn['revoked_at']:return {'accepted':True,'ignored':True}
            if kind=='oauth.authorization.revoked':
                db.execute("UPDATE ss_square_connections SET revoked_at=now(),encrypted_tokens='' WHERE tenant_id=%s",(conn['tenant_id'],))
                db.execute("UPDATE ss_square_jobs SET state='canceled',last_error_code='authorization_revoked' WHERE tenant_id=%s AND state IN ('pending','processing')",(conn['tenant_id'],))
                audit(db,conn['tenant_id'],None,'square-authorization-revoked',merchant)
                return {'accepted':True,'revoked':True}
            fingerprint=hashlib.sha256(raw).hexdigest()
            old=db.execute('SELECT body_hash FROM ss_square_jobs WHERE event_id=%s',(event_id,)).fetchone()
            if old and old['body_hash']!=fingerprint:raise ServiceError('Event ID conflicts with a previous notification.',409)
            db.execute('''INSERT INTO ss_square_jobs(event_id,tenant_id,event_type,resource_id,body_hash)
                          VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',(event_id,conn['tenant_id'],kind,resource,fingerprint))
            return {'accepted':True,'duplicate':bool(old)}
