"""Generation-fenced diagnostic inventory reads, never cleanup eligibility.

No scheduler/route is registered here. Platform permission defaults off and is
distinct from owner consent, verified provider grants and actual scheduler health.
Every work item retains its original scope/config/receipt manifest. Partial count
witnesses are ordinal history; a later zero never overwrites a positive witness.
All provider I/O is between transactions. Completed pagination/bookends and a
receipt highwater prove neither atomic observation nor continuous-zero history.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import re
import uuid

from psycopg.types.json import Jsonb

from .cleanup_events import _timestamp
from . import cleanup_receipt_batches as batches
from .cleanup_square import CleanupSquare
from .intake import current_authority
from .receiving_plan import READ_SCOPES
from .security import ServiceError, identifier, provider_id


_K = ('tenant_id','application_id','environment','merchant_id','authorization_generation')
_WHERE = 'tenant_id=%s AND application_id=%s AND environment=%s AND merchant_id=%s AND authorization_generation=%s'
_SAFE = re.compile(r'[a-z][a-z0-9_]{0,63}')
_SHA = re.compile(r'[0-9a-f]{64}')
_QUANTITY = re.compile(r'[+-]?[0-9]+(?:\.[0-9]{1,5})?')
_FALSE = {'atomic_snapshot':False,'continuous_zero_verified':False,'deletion_enabled':False}


class CleanupObserverError(ServiceError):
    def __init__(self, message, status=409, *, code='OBSERVER_AUTHORITY_BLOCKED'):
        super().__init__(message,status); self.code=code


@dataclass(frozen=True)
class _Scope:
    application_id: str
    environment: str


@dataclass(frozen=True,repr=False)
class ObserverClaim:
    work: dict
    attempt: dict
    access: object
    configuration: object


def _canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')


def _hash(value):return hashlib.sha256(_canonical(value)).hexdigest()
def _key(row):return tuple(row[name] for name in _K)
def _uuid(value):
    try:return uuid.UUID(str(value))
    except (ValueError,TypeError,AttributeError):
        raise CleanupObserverError('Use a valid cleanup generation or request identifier.',400,code='OBSERVER_INPUT_INVALID') from None


def _input(value, *, configure=False):
    fields={'request_key','expected_generation','expected_config_version'}
    if configure:fields|={'enabled','background_enabled','cadence_seconds','stale_after_seconds'}
    try:
        if type(value) is not dict or set(value)!=fields:raise ValueError()
        result=dict(value); result['request_key']=identifier(value['request_key'],'Request key')
        result['expected_generation']=str(_uuid(value['expected_generation']))
        version=value['expected_config_version']
        if type(version) is not int or not 0<=version<2**63:raise ValueError()
        if configure:
            if type(value['enabled']) is not bool or type(value['background_enabled']) is not bool:raise ValueError()
            if value['background_enabled'] and not value['enabled']:raise ValueError()
            cadence,stale=value['cadence_seconds'],value['stale_after_seconds']
            if type(cadence) is not int or not 300<=cadence<=86400 or type(stale) is not int or not cadence<=stale<=604800:raise ValueError()
        return result
    except (ValueError,TypeError,KeyError):
        raise CleanupObserverError('Use the exact supported cleanup settings and request fields.',400,code='OBSERVER_INPUT_INVALID') from None


def _reasons(values):
    if type(values) is not list or len(values)>32 or any(type(code) is not str or not _SAFE.fullmatch(code) for code in values):
        raise ValueError('Invalid bounded reason codes.')
    return sorted(set(values))


def _minimized_result(value):
    """Validate again at the persistence boundary; never keep provider notes."""
    try:
        if type(value) is not dict or any(value.get(name) is not False for name in _FALSE):raise ValueError()
        for name in ('coverage_complete','pagination_complete','context_unchanged','bracketed'):
            if type(value.get(name)) is not bool:raise ValueError()
        reasons=_reasons(value['reason_codes'])
        hashes=[]
        for name in ('before_context_hash','after_context_hash'):
            digest=value.get(name)
            if digest is not None and (type(digest) is not str or not _SHA.fullmatch(digest)):raise ValueError()
            hashes.append(digest)
        locations=value['locations']; objects=value['objects']; counts=value['counts']; missing=value['missing']
        if type(locations) is not list or type(objects) is not dict or type(counts) is not list or len(locations)+len(objects)+len(counts)>100000:raise ValueError()
        locs=[]; loc_ids=set()
        for row in locations:
            ident=provider_id(row['id'])
            if ident in loc_ids or row['status'] not in ('ACTIVE','INACTIVE') or not re.fullmatch(r'[A-Z]{3}',row['currency']):raise ValueError()
            loc_ids.add(ident); locs.append({'id':ident,'status':row['status'],'currency':row['currency']})
        catalog=[]; variations=set(); parents={}
        for ident,row in objects.items():
            if provider_id(ident)!=row['id'] or row['type'] not in ('ITEM','ITEM_VARIATION') or type(row['version']) is not int or not 1<=row['version']<2**63:raise ValueError()
            item={'id':ident,'type':row['type'],'version':row['version']}
            if row['type']=='ITEM_VARIATION':
                item['parent_id']=provider_id(row['item_variation_data']['item_id']); variations.add(ident)
            else:
                children=row['item_data']['variations']
                if type(children) is not list:raise ValueError()
                child_ids=[provider_id(child['id']) for child in children]
                if len(set(child_ids))!=len(child_ids):raise ValueError()
                item['variation_ids']=sorted(child_ids); parents[ident]=set(child_ids)
            catalog.append(item)
        for item in catalog:
            if item['type']=='ITEM_VARIATION' and item['id'] not in parents.get(item['parent_id'],set()):raise ValueError()
        if any(child not in variations for children in parents.values() for child in children):raise ValueError()
        witnesses=[]; seen=set(); duplicate=False
        for row in counts:
            ident=provider_id(row['catalog_object_id']); location=provider_id(row['location_id'])
            quantity=row['quantity']
            if (len(ident)>100 or len(location)>100 or ident not in variations or location not in loc_ids or
                    row['state']!='IN_STOCK' or type(quantity) is not str or not 1<=len(quantity)<=26 or not _QUANTITY.fullmatch(quantity)):raise ValueError()
            kind=row.get('catalog_object_type')
            if 'catalog_object_type' in row and kind!='ITEM_VARIATION':raise ValueError()
            calculated=row.get('calculated_at')
            if 'calculated_at' in row:_timestamp(calculated)
            if 'is_estimated' in row and type(row['is_estimated']) is not bool:raise ValueError()
            pair=(ident,location); duplicate=duplicate or pair in seen; seen.add(pair)
            witnesses.append({'catalog_object_id':ident,'location_id':location,'catalog_object_type':kind,
                'state':'IN_STOCK','quantity':quantity,'calculated_at':calculated,'is_estimated':row.get('is_estimated')})
        if duplicate:reasons.append('duplicate_counts')
        if any(row['calculated_at'] is None for row in witnesses):reasons.append('count_timestamps_unknown')
        if any(row['is_estimated'] is True for row in witnesses):reasons.append('estimated_counts')
        if missing is not None:
            if type(missing) is not list or len(missing)>100000 or len(variations)*len(loc_ids)>100000:raise ValueError()
            pairs=[]
            for pair in missing:
                if type(pair) not in (list,tuple) or len(pair)!=2 or pair[0] not in variations or pair[1] not in loc_ids:raise ValueError()
                pairs.append(tuple(pair))
            if len(set(pairs))!=len(pairs) or set(pairs)!={(v,l) for v in variations for l in loc_ids}-seen:raise ValueError()
            missing=[list(pair) for pair in sorted(pairs)]
        if missing is None:reasons.append('missing_coverage_unknown')
        elif missing:reasons.append('missing_counts')
        if hashes[0] is None or hashes[1] is None or hashes[0]!=hashes[1]:reasons.append('context_unconfirmed')
        if not value['pagination_complete']:reasons.append('pagination_incomplete')
        if not value['context_unchanged']:reasons.append('context_changed')
        if not value['bracketed']:reasons.append('bookends_incomplete')
        if not value['coverage_complete']:reasons.append('coverage_incomplete')
        reasons=sorted(set(reasons))[:32]
        complete=bool(value['coverage_complete'] and value['pagination_complete'] and value['context_unchanged'] and value['bracketed'] and not reasons and missing==[])
        result={'coverage_complete':complete,'pagination_complete':value['pagination_complete'],
            'context_unchanged':value['context_unchanged'],'bracketed':value['bracketed'],'reason_codes':reasons,
            'before_context_hash':hashes[0],'after_context_hash':hashes[1],
            'context':{'locations':sorted(locs,key=lambda row:row['id']),'objects':sorted(catalog,key=lambda row:row['id'])},
            'counts':witnesses,'missing':missing}
        if len(_canonical(result))>16*1024*1024:raise ValueError()
        return result
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
        raise CleanupObserverError('Cleanup read evidence is incomplete or invalid.',409,code='OBSERVER_RESULT_INVALID') from None


def _empty_result(reason):
    return {'coverage_complete':False,'pagination_complete':False,'context_unchanged':False,'bracketed':False,
        'reason_codes':[reason],'before_context_hash':None,'after_context_hash':None,'context':{},'counts':[],'missing':None}


class CleanupObserver:
    def __init__(self,database,square,reader=None,*,enabled=False,allowed_environments=('production',),lease_seconds=120):
        if (type(allowed_environments) not in (tuple,list) or len(allowed_environments)>2 or
                any(type(env) is not str or env not in ('production','sandbox') for env in allowed_environments) or
                len(set(allowed_environments))!=len(allowed_environments) or type(lease_seconds) is not int or not 90<=lease_seconds<=600):
            raise ValueError('Invalid observer platform limits.')
        self.database,self.square=database,square
        self.reader=reader or CleanupSquare(square)
        self.enabled=enabled is True
        self.allowed_environments=tuple(allowed_environments); self.lease_seconds=lease_seconds

    def _scope(self):
        return _Scope(str(self.square.config.get('SQUARE_APPLICATION_ID') or ''),self.square.environment)

    @staticmethod
    def _authorize(db,actor,roles,*,fresh=False):
        try:current_authority(db,actor,allowed_roles=roles,fresh_mfa=fresh)
        except ServiceError as error:
            code='MFA_STEP_UP_REQUIRED' if getattr(error,'code',None)=='MFA_STEP_UP_REQUIRED' else 'OBSERVER_AUTHORITY_BLOCKED'
            raise CleanupObserverError('Use a current authorized store session and the required fresh MFA proof.',error.status,code=code) from None

    def _begin(self,db,context,tenant,*,actor=None,roles=('owner','manager'),fresh=False,scope=None,allow_denied=False):
        self.square._notice_barrier(db,configuration=context)  # FIRST SQL.
        denied=None
        if actor is not None:
            try:self._authorize(db,actor,roles,fresh=fresh)
            except CleanupObserverError:
                if not allow_denied:raise
                denied='requester_authority_changed'
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-tenant:'+str(tenant),))
        peek=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s',(tenant,)).fetchone()
        merchant=scope['merchant_id'] if scope else peek['merchant_id'] if peek else None
        if merchant:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('square-merchant:'+merchant,))
        conn=db.execute('SELECT * FROM ss_square_connections WHERE tenant_id=%s FOR UPDATE',(tenant,)).fetchone()
        if conn and merchant!=conn['merchant_id'] and scope is None:
            raise CleanupObserverError('Square connection changed while reading cleanup state.')
        target=scope or ({'tenant_id':tenant,'application_id':conn.get('oauth_application_id') or context.application_id,
            'environment':conn['environment'],'merchant_id':conn['merchant_id'],'authorization_generation':conn['authorization_generation']} if conn else None)
        config=projection=None
        if target:
            config=db.execute('SELECT * FROM ss_cleanup_observer_config WHERE '+_WHERE+' ORDER BY config_version DESC LIMIT 1 FOR UPDATE',_key(target)).fetchone()
            projection=db.execute('SELECT * FROM ss_cleanup_current_projection WHERE '+_WHERE+' FOR UPDATE',_key(target)).fetchone()
            # Progress always precedes work locks, including claim/finish. An
            # absent head is created only by authorized admission/preparation.
            batches.progress(db,target,lock=True)
        return conn,target,config,projection,denied

    @staticmethod
    def _generation(conn,data):
        if not conn or str(conn['authorization_generation'])!=data['expected_generation']:
            raise CleanupObserverError('The Square generation changed. Check current cleanup status before trying again.',409,code='OBSERVER_GENERATION_CHANGED')

    def _live(self,conn,scope,context,*,require_expiry=False,now=None):
        if (not conn or conn['tenant_id']!=scope['tenant_id'] or conn['merchant_id']!=scope['merchant_id'] or
                conn['environment']!=scope['environment'] or conn['authorization_generation']!=scope['authorization_generation'] or
                scope['application_id']!=context.application_id or scope['environment']!=context.environment or
                conn.get('oauth_application_id')!=scope['application_id'] or conn['revoked_at'] is not None or conn.get('pending_notice_cursor') is not None):
            return 'authorization_changed'
        if scope['environment'] not in self.allowed_environments:return 'environment_not_allowed'
        if require_expiry and conn['expires_at']<=(now or datetime.now(timezone.utc))+timedelta(minutes=1):return 'authorization_expired'
        return None

    def _receipts(self,db,scope):
        rows=db.execute('''SELECT receipt_cursor,body_hash,binding_status,payload_status,fact_count FROM ss_cleanup_inventory_receipts
            WHERE '''+_WHERE+''' AND binding_status IN ('active','revoked','revocation_pending','token_expired')
            ORDER BY receipt_cursor LIMIT 10001''',_key(scope)).fetchall()
        if len(rows)>10000 or sum(row['fact_count'] for row in rows)>100000:
            raise CleanupObserverError('Legacy cleanup receipt basis requires bounded preparation.',409,code='OBSERVER_RECEIPT_UPGRADE_REQUIRED')
        manifest=[{'receipt_cursor':row['receipt_cursor'],'body_hash':row['body_hash']} for row in rows]
        return (rows[-1]['receipt_cursor'] if rows else 0),manifest,_hash(manifest),rows

    def configure(self,actor,values):
        data=_input(values,configure=True); digest=_hash(data); current=self._scope()
        # A metadata-only hint lets withdrawal lock the ORIGINAL saved app scope
        # even when provider credentials/configuration are unavailable or changed.
        with self.database.transaction() as db:
            conn,target,_,_,_=self._begin(db,current,actor['tenant_id'],actor=actor,roles=('owner',),fresh=True)
            self._generation(conn,data)
        context=_Scope(target['application_id'],target['environment'])
        with self.database.transaction() as db:
            conn,scope,config,projection,_=self._begin(db,context,actor['tenant_id'],actor=actor,roles=('owner',),fresh=True,scope=target)
            self._generation(conn,data)
            if not conn or any(conn[name]!=target[name] for name in ('merchant_id','environment','authorization_generation')):
                raise CleanupObserverError('Square connection changed before cleanup settings were saved.',409,code='OBSERVER_GENERATION_CHANGED')
            old=db.execute('SELECT request_hash FROM ss_cleanup_observer_config WHERE tenant_id=%s AND request_key=%s',(actor['tenant_id'],data['request_key'])).fetchone()
            if old:
                if old['request_hash']!=digest:raise CleanupObserverError('Cleanup request key conflicts with an earlier request.',409,code='OBSERVER_REQUEST_CONFLICT')
                return {'accepted':True,'duplicate':True}
            version=config['config_version'] if config else 0
            if data['expected_config_version']!=version:raise CleanupObserverError('Cleanup settings changed. Check current status.',409,code='OBSERVER_CONFIG_CHANGED')
            if version>=2**63-1:raise CleanupObserverError('Cleanup settings version requires service review.',409,code='OBSERVER_CONFIG_CHANGED')
            if data['enabled']:
                full=self.square._token_context()
                if self._live(conn,scope,full):raise CleanupObserverError('Reconnect Square before enabling cleanup reads.')
            audit=db.execute('''INSERT INTO ss_audit(tenant_id,staff_id,action,entity_id,details)
                VALUES (%s,%s,'cleanup-observer-configured',%s,%s) RETURNING id''',
                (actor['tenant_id'],actor['staff_id'],str(scope['authorization_generation']),Jsonb({'version':version+1,'enabled':data['enabled'],'background_enabled':data['background_enabled']}))).fetchone()
            saved=db.execute('''INSERT INTO ss_cleanup_observer_config
                (tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,enabled,background_enabled,
                 cadence_seconds,stale_after_seconds,owner_staff_id,audit_id,request_key,request_hash)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (*_key(scope),version+1,data['enabled'],data['background_enabled'],data['cadence_seconds'],data['stale_after_seconds'],
                 actor['staff_id'],audit['id'],data['request_key'],digest)).fetchone()
            db.execute('''INSERT INTO ss_cleanup_current_projection
                (tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,health,reason_codes,next_due_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,application_id,environment,merchant_id,authorization_generation)
                DO UPDATE SET config_version=EXCLUDED.config_version,diagnostic_epoch=gen_random_uuid(),health=EXCLUDED.health,
                    reason_codes=EXCLUDED.reason_codes,next_due_at=EXCLUDED.next_due_at''',
                (*_key(scope),version+1,'stale' if data['enabled'] else 'disabled',Jsonb(['configuration_changed']),saved['created_at'] if data['enabled'] else None))
            return {'accepted':True,'duplicate':False}

    @staticmethod
    def _principal(work):
        if work['principal_kind']=='platform':return None
        return {'tenant_id':work['tenant_id'],'staff_id':work['staff_id'],'session_hash':work['session_hash'],'role':work['requested_role']}

    @staticmethod
    def _event(db,work,reason,attempt=None):
        db.execute('INSERT INTO ss_cleanup_work_events(work_id,attempt_id,reason_code) VALUES (%s,%s,%s)',(work['id'],attempt,reason))

    def _break_projection(self,db,work,reason):
        db.execute('''UPDATE ss_cleanup_current_projection SET diagnostic_epoch=gen_random_uuid(),health='stale',reason_codes=%s
            WHERE '''+_WHERE+' AND config_version=%s',(Jsonb([reason]),*_key(work),work['config_version']))

    def _retire_expired(self,db,work):
        attempt=db.execute('SELECT * FROM ss_cleanup_collection_attempts WHERE id=%s',(work['lease_token'],)).fetchone()
        if attempt:
            existing=db.execute('SELECT outcome FROM ss_cleanup_collections WHERE attempt_id=%s',(attempt['id'],)).fetchone()
            if existing and existing['outcome']!='fenced':raise CleanupObserverError('Cleanup work history requires review.',503)
            if not existing:self._write_collection(db,work,attempt,_empty_result('lease_expired'),_hash({'retired_attempt':str(attempt['id'])}),'fenced',[])
            self._event(db,work,'lease_expired',attempt['id'])
        self._break_projection(db,work,'lease_expired')
        db.execute("UPDATE ss_cleanup_collection_work SET state='superseded',lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp() WHERE id=%s AND lease_token=%s",(work['id'],work['lease_token']))

    def _insert_work(self,db,scope,config,trigger,actor,floor,head,hwm,*,parent=None):
        kind='collection' if head['high_inclusive']==hwm else 'receipt_prepare'
        proof=batches.basis(scope,config['config_version'],kind,hwm,floor,head)
        fields=('id',*_K,'config_version','receipt_hwm','receipt_manifest','manifest_hash','trigger_kind',
            'principal_kind','staff_id','session_hash','requested_role',*batches.BASIS_FIELDS,'parent_work_id')
        values=(uuid.uuid4(),*_key(scope),config['config_version'],hwm,Jsonb(head['receipt_manifest']),head['manifest_hash'],trigger,
            'manual' if actor is not None else 'platform',actor['staff_id'] if actor else None,
            actor['session_hash'] if actor else None,actor['role'] if actor else None,
            *(proof[name] for name in batches.BASIS_FIELDS),parent)
        return db.execute('INSERT INTO ss_cleanup_collection_work('+','.join(fields)+') VALUES ('+
            ','.join(['%s']*len(fields))+') RETURNING *',values).fetchone()

    def _enqueue(self,db,scope,config,projection,trigger,actor=None,*,fold=True):
        hwm=batches.latest_hwm(db,scope)
        head=batches.ensure_head(db,scope,config['config_version'])
        now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
        existing=db.execute("SELECT * FROM ss_cleanup_collection_work WHERE "+_WHERE+" AND state IN ('queued','reading') FOR UPDATE",_key(scope)).fetchone()
        if existing:
            stale=(existing['config_version']!=config['config_version'] or existing['receipt_basis_version']==1 or
                (existing['work_kind']=='collection' and existing['receipt_hwm']!=hwm))
            if existing['state']=='reading' and existing['lease_until']<=now:
                self._retire_expired(db,existing); existing=None
            elif existing['state']=='queued' and stale:
                db.execute("UPDATE ss_cleanup_collection_work SET state='superseded',finished_at=clock_timestamp() WHERE id=%s",(existing['id'],))
                self._event(db,existing,'superseded_before_read'); existing=None
            elif stale:return None
            else:return existing
        if fold:head,_=batches.fold_once(db,scope,config['config_version'],hwm)
        floor=batches.read_floor(db,scope,projection,config['config_version'])
        return self._insert_work(db,scope,config,trigger,actor,floor,head,hwm)

    def schedule_notification_scope(self,scope):
        if not self.enabled:return {'status':'disabled','scheduled':0}
        context=self.square._token_context()
        with self.database.transaction() as db:
            conn,target,config,projection,_=self._begin(db,context,scope['tenant_id'],scope=scope)
            if (self._live(conn,target,context) or not config or not config['enabled'] or not config['background_enabled']):
                return {'status':'blocked','scheduled':0}
            ack=projection['acknowledged_hwm'] if projection else 0
            intent=db.execute('SELECT 1 FROM ss_cleanup_refresh_queue WHERE '+_WHERE+' AND receipt_cursor>%s LIMIT 1',(*_key(scope),ack)).fetchone()
            if not intent:return {'status':'idle','scheduled':0}
            work=self._enqueue(db,target,config,projection,'notification')
            return {'status':'queued' if work else 'blocked','scheduled':int(work is not None)}

    def _schedule(self,kind,limit,*,fold=True,skip_active=False):
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('Invalid observer schedule limit.')
        if not self.enabled:return {'status':'disabled','scheduled':0}
        context=self.square._token_context()
        # Selection is a nonlocking hint. Every selected original K is readmitted
        # under full lock order; reconnect never substitutes a different K.
        condition=("EXISTS(SELECT 1 FROM ss_cleanup_refresh_queue q WHERE q.tenant_id=h.tenant_id AND q.application_id=h.application_id AND q.environment=h.environment AND q.merchant_id=h.merchant_id AND q.authorization_generation=h.authorization_generation AND q.receipt_cursor>COALESCE(p.acknowledged_hwm,0))"
                   if kind=='notification' else '(p.next_due_at IS NULL OR p.next_due_at<=clock_timestamp())')
        with self.database.transaction() as db:
            self.square._notice_barrier(db,configuration=context)
            rows=db.execute('''WITH head AS (SELECT DISTINCT ON (tenant_id,application_id,environment,merchant_id,authorization_generation) *
                FROM ss_cleanup_observer_config WHERE application_id=%s AND environment=%s
                ORDER BY tenant_id,application_id,environment,merchant_id,authorization_generation,config_version DESC)
                SELECT h.* FROM head h JOIN ss_square_connections c ON c.tenant_id=h.tenant_id AND c.merchant_id=h.merchant_id
                  AND c.environment=h.environment AND c.authorization_generation=h.authorization_generation AND c.oauth_application_id=h.application_id
                LEFT JOIN ss_cleanup_current_projection p ON p.tenant_id=h.tenant_id AND p.application_id=h.application_id
                  AND p.environment=h.environment AND p.merchant_id=h.merchant_id AND p.authorization_generation=h.authorization_generation
                WHERE h.enabled AND h.background_enabled AND c.revoked_at IS NULL AND c.pending_notice_cursor IS NULL AND '''+condition+'''
                AND NOT EXISTS(SELECT 1 FROM ss_cleanup_collection_work w WHERE w.tenant_id=h.tenant_id AND w.application_id=h.application_id
                    AND w.environment=h.environment AND w.merchant_id=h.merchant_id AND w.authorization_generation=h.authorization_generation
                    AND ((w.state='reading' AND w.lease_until>clock_timestamp()) OR (w.state='queued' AND w.receipt_basis_version=2 AND w.config_version=h.config_version
                         AND (w.work_kind='receipt_prepare' OR w.receipt_hwm=COALESCE((SELECT max(r.receipt_cursor) FROM ss_cleanup_inventory_receipts r
                             WHERE r.tenant_id=h.tenant_id AND r.application_id=h.application_id AND r.environment=h.environment
                               AND r.merchant_id=h.merchant_id AND r.authorization_generation=h.authorization_generation
                               AND r.binding_status IN ('active','revoked','revocation_pending','token_expired')),0)))))'''+
                (''' AND NOT EXISTS(SELECT 1 FROM ss_cleanup_collection_work w WHERE w.tenant_id=h.tenant_id
                    AND w.application_id=h.application_id AND w.environment=h.environment AND w.merchant_id=h.merchant_id
                    AND w.authorization_generation=h.authorization_generation AND w.receipt_basis_version=2
                    AND w.state IN ('queued','reading'))''' if skip_active else '')+'''
                ORDER BY COALESCE(p.next_due_at,h.created_at),h.tenant_id LIMIT %s''',(context.application_id,context.environment,limit)).fetchall()
        scheduled=0
        for scope in rows:
            with self.database.transaction() as db:
                conn,target,config,projection,_=self._begin(db,context,scope['tenant_id'],scope=scope)
                now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
                if self._live(conn,target,context) or not config or not config['enabled'] or not config['background_enabled']:continue
                if skip_active and db.execute('''SELECT 1 FROM ss_cleanup_collection_work WHERE '''+_WHERE+'''
                    AND receipt_basis_version=2 AND state IN ('queued','reading') LIMIT 1''',_key(target)).fetchone():continue
                if kind=='notification':
                    ack=projection['acknowledged_hwm'] if projection else 0
                    if not db.execute('SELECT 1 FROM ss_cleanup_refresh_queue WHERE '+_WHERE+' AND receipt_cursor>%s LIMIT 1',(*_key(target),ack)).fetchone():continue
                elif projection and projection['next_due_at'] and projection['next_due_at']>now:continue
                scheduled+=int(self._enqueue(db,target,config,projection,kind,fold=fold) is not None)
        return {'status':'scheduled' if scheduled else 'idle','scheduled':scheduled}

    def schedule_notifications(self,*,limit=25):return self._schedule('notification',limit)
    def schedule_periodic(self,*,limit=25):return self._schedule('periodic',limit)

    def _work_check(self,db,work,conn,config,context,denied=None,*,preparing=False):
        if denied:return denied
        if not self.enabled:return 'worker_disabled'
        try:self.square._require_token_context(context)
        except ServiceError:return 'provider_configuration_changed'
        if self._live(conn,work,context):return 'authorization_changed'
        if not config or not config['enabled'] or config['config_version']!=work['config_version']:return 'configuration_changed'
        if work['principal_kind']=='platform' and not config['background_enabled']:return 'background_disabled'
        if work['receipt_basis_version']==2:
            try:batches.validate_basis(db,work,ready=not preparing)
            except ServiceError:return 'receipt_basis_changed'
            if not preparing and batches.latest_hwm(db,work)!=work['receipt_hwm']:return 'notification_advanced'
        else:
            try:hwm,manifest,digest,_=self._receipts(db,work)
            except CleanupObserverError:return 'receipt_basis_upgrade_required'
            if hwm!=work['receipt_hwm'] or digest!=work['manifest_hash'] or manifest!=work['receipt_manifest']:return 'notification_advanced'
        return None

    def prepare_once(self,work_id):
        """One SQL-only bounded step; preparation never acknowledges a read.

        Ready collection checks are idempotent and perform no permission/data
        HTTP. A terminal preparation has exactly one durable same-principal child.
        """
        if not self.enabled:return {'status':'disabled','processed':False,'ready':False,**_FALSE}
        context=self.square._token_context()
        with self.database.transaction() as db:
            self.square._notice_barrier(db,configuration=context)
            hint=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s',(_uuid(work_id),)).fetchone()
        if not hint:return {'status':'idle','processed':False,'ready':False,**_FALSE}
        actor=self._principal(hint)
        with self.database.transaction() as db:
            conn,scope,config,projection,denied=self._begin(db,context,hint['tenant_id'],actor=actor,scope=hint,allow_denied=True)
            work=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s FOR UPDATE',(hint['id'],)).fetchone()
            if work['receipt_basis_version']!=2 or work['work_kind']=='collection':
                ready=work['state']=='queued' and not self._work_check(db,work,conn,config,context,denied)
                return {'status':'ready' if ready else 'idle','processed':False,'ready':ready,**_FALSE}
            if work['state']!='queued':return {'status':'idle','processed':False,'ready':False,**_FALSE}
            reason=self._work_check(db,work,conn,config,context,denied,preparing=True)
            if reason:
                self._block_queued(db,work,reason)
                return {'status':'blocked','processed':False,'ready':False,**_FALSE}
            head,advanced=batches.fold_once(db,work,work['config_version'],work['receipt_hwm'])
            if head['high_inclusive']<work['receipt_hwm']:
                db.execute('UPDATE ss_cleanup_collection_work SET next_attempt_at=clock_timestamp() WHERE id=%s',(work['id'],))
                return {'status':'receipt_progress','processed':advanced,'ready':False,**_FALSE}
            # Finish parent and insert child atomically. Do not call _enqueue:
            # a new tail must not cause a second fold in this operation.
            db.execute("UPDATE ss_cleanup_collection_work SET state='done',finished_at=clock_timestamp() WHERE id=%s",(work['id'],))
            target=batches.latest_hwm(db,work)
            floor=batches.read_floor(db,work,projection,config['config_version'])
            child=self._insert_work(db,work,config,work['trigger_kind'],actor,floor,head,target,parent=work['id'])
            return {'status':'receipt_progress','processed':True,'ready':child['work_kind']=='collection',**_FALSE}

    def _stable_access(self,access,work,*,now=None,fresh=True):
        try:
            proof=access.proof; checked=datetime.fromisoformat(proof['checked_at'].replace('Z','+00:00'))
            expires=datetime.fromisoformat(proof['expires_at'].replace('Z','+00:00')); now=now or datetime.now(timezone.utc)
            if (proof['application_id']!=work['application_id'] or proof['environment']!=work['environment'] or proof['merchant_id']!=work['merchant_id'] or
                    access.generation!=str(work['authorization_generation']) or proof['grants_verified'] is not True or
                    sorted(proof['required_scopes'])!=sorted(READ_SCOPES) or not set(READ_SCOPES)<=set(proof['scopes']) or
                    expires<=now+timedelta(minutes=1) or checked.utcoffset() is None or (fresh and not 0<=(now-checked).total_seconds()<=60)):
                raise ValueError()
            return _hash({'application_id':proof['application_id'],'environment':proof['environment'],'merchant_id':proof['merchant_id'],
                'generation':access.generation,'scopes':sorted(proof['scopes']),'required_scopes':sorted(proof['required_scopes']),
                'method':proof['method'],'square_version':proof['square_version'],'token_digest':hashlib.sha256(access.token.encode()).hexdigest()})
        except (ValueError,KeyError,TypeError,AttributeError):raise CleanupObserverError('Cleanup read authority changed.') from None

    def _block_queued(self,db,work,reason):
        if work['state']=='queued':
            db.execute("UPDATE ss_cleanup_collection_work SET state='blocked',finished_at=clock_timestamp() WHERE id=%s AND state='queued'",(work['id'],))
            self._event(db,work,reason); self._break_projection(db,work,reason)
        elif work['state']=='reading':
            now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            if work['lease_until']<=now:
                self._retire_expired(db,work)
                self._event(db,work,reason,work['lease_token'])

    def claim(self,work_id=None):
        if not self.enabled:return None
        context=self.square._token_context()
        with self.database.transaction() as db:
            self.square._notice_barrier(db,configuration=context)
            hint=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE '+('id=%s' if work_id else "(state='queued' AND next_attempt_at<=clock_timestamp()) OR (state='reading' AND lease_until<=clock_timestamp()) ORDER BY next_attempt_at,created_at,id LIMIT 1"),
                            (_uuid(work_id),) if work_id else ()).fetchone()
        if not hint:return None
        if hint['work_kind']=='receipt_prepare':return None
        actor=self._principal(hint)
        with self.database.transaction() as db:
            conn,scope,config,projection,denied=self._begin(db,context,hint['tenant_id'],actor=actor,scope=hint,allow_denied=True)
            work=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s FOR UPDATE',(hint['id'],)).fetchone()
            if not work or work['state'] not in ('queued','reading'):return None
            now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            if work['state']=='reading' and work['lease_until']>now:return None
            reason=self._work_check(db,work,conn,config,context,denied)
            if reason:
                self._block_queued(db,work,reason)
                if reason=='notification_advanced' and config and config['config_version']==work['config_version'] and not denied:
                    self._enqueue(db,work,config,projection,work['trigger_kind'],actor)
                return None
        # This is only permission introspection/renewal, never a data read. The
        # returned latest generation is checked against ORIGINAL work immediately.
        try:
            access=self.reader.access(work['tenant_id'])
            access_digest=self._stable_access(access,work)
        except Exception:
            with self.database.transaction() as db:
                conn,_,config,_,denied=self._begin(db,context,work['tenant_id'],actor=actor,scope=work,allow_denied=True)
                current=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s FOR UPDATE',(work['id'],)).fetchone()
                if current:self._block_queued(db,current,'read_grants_unavailable')
            return None
        with self.database.transaction() as db:
            conn,scope,config,projection,denied=self._begin(db,context,work['tenant_id'],actor=actor,scope=work,allow_denied=True)
            current=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s FOR UPDATE',(work['id'],)).fetchone()
            now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            if not current or current['state'] not in ('queued','reading') or (current['state']=='reading' and current['lease_until']>now):return None
            reason=self._work_check(db,current,conn,config,context,denied)
            if reason:self._block_queued(db,current,reason); return None
            self._stable_access(access,current,now=now); self.reader.assert_current(db,current['tenant_id'],access)
            if current['attempts']>=100:self._block_queued(db,current,'attempt_budget_exhausted'); return None
            if current['state']=='reading':
                self._retire_expired(db,current)
                # Keep original K/config/H on takeover; the old lease is dead.
                db.execute("UPDATE ss_cleanup_collection_work SET state='queued',finished_at=NULL WHERE id=%s",(current['id'],))
            lease=uuid.uuid4(); until=now+timedelta(seconds=self.lease_seconds)
            fields=('id','work_id','attempt',*_K,'config_version','receipt_hwm','manifest_hash',
                'notice_cursor','access_digest','started_at','lease_until',*batches.BASIS_FIELDS)
            values=(lease,current['id'],current['attempts']+1,*_key(current),current['config_version'],current['receipt_hwm'],current['manifest_hash'],
                self.square._notice_cursor(db,current['merchant_id'],configuration=context),access_digest,now,until,
                *(current[name] for name in batches.BASIS_FIELDS))
            attempt=db.execute('INSERT INTO ss_cleanup_collection_attempts('+','.join(fields)+') VALUES ('+
                ','.join(['%s']*len(fields))+') RETURNING *',values).fetchone()
            current=db.execute("UPDATE ss_cleanup_collection_work SET state='reading',attempts=attempts+1,lease_token=%s,lease_until=%s,finished_at=NULL WHERE id=%s RETURNING *",(lease,until,current['id'])).fetchone()
            return ObserverClaim(dict(current),dict(attempt),access,context)

    def _receipt_flags(self,db,work):
        if work['receipt_basis_version']==2:
            floor,head=batches.validate_basis(db,work,ready=True)
            return batches.interval_flags(floor,head)
        _,manifest,digest,receipts=self._receipts(db,work)
        if manifest!=work['receipt_manifest'] or digest!=work['manifest_hash']:raise CleanupObserverError('Cleanup receipt scope changed.')
        flags=set()
        if any(row['payload_status']!='counts_present' for row in receipts):flags.add('notification_coverage_unknown')
        if any(row['binding_status']!='active' for row in receipts):flags.add('notification_authority_interrupted')
        if manifest:
            facts=db.execute('''SELECT * FROM ss_cleanup_inventory_facts WHERE receipt_cursor=ANY(%s)
                ORDER BY receipt_cursor,ordinal LIMIT 100001''',([row['receipt_cursor'] for row in manifest],)).fetchall()
            if len(facts)>100000:raise CleanupObserverError('Cleanup receipt fact budget requires review.')
            for fact in facts:
                quantity=Decimal(fact['quantity'])
                if quantity>0:flags.add('notification_positive')
                if quantity<0:flags.add('notification_negative')
                if fact['is_estimated'] is True:flags.add('notification_estimated')
                if fact['catalog_object_type']=='ITEM':flags.add('notification_item_count')
                if fact['state']!='IN_STOCK':flags.add('notification_other_state')
        return sorted(flags)

    def _write_collection(self,db,work,attempt,result,digest,outcome,receipt_flags):
        # Fenced rows carry only registered original identity + bounded outcome.
        # No count/context from a lost generation/lease/authority is retained.
        if outcome=='fenced':
            result=_empty_result(result['reason_codes'][0])
        rows=result['counts']; missing=result['missing']
        fields=('id','attempt_id','work_id',*_K,'config_version','receipt_hwm','manifest_hash','result_hash','outcome','started_at',
            'coverage_complete','pagination_complete','context_unchanged','bracketed','reason_codes','before_context_hash','after_context_hash',
            'context','missing','missing_count','count_rows','nonzero_count','receipt_flags',*batches.BASIS_FIELDS)
        values=(uuid.uuid4(),attempt['id'],work['id'],*_key(work),work['config_version'],work['receipt_hwm'],work['manifest_hash'],digest,outcome,attempt['started_at'],
            outcome=='complete',result['pagination_complete'],result['context_unchanged'],result['bracketed'],Jsonb(result['reason_codes']),
            result['before_context_hash'],result['after_context_hash'],Jsonb(result['context']),Jsonb(missing) if missing is not None else None,
            len(missing) if missing is not None else None,len(rows),sum(Decimal(row['quantity'])!=0 for row in rows),Jsonb(receipt_flags),
            *(attempt[name] for name in batches.BASIS_FIELDS))
        collection=db.execute('INSERT INTO ss_cleanup_collections('+','.join(fields)+') VALUES ('+','.join(['%s']*len(fields))+') RETURNING *',values).fetchone()
        if rows:
            with db.cursor() as cursor:
                cursor.executemany('''INSERT INTO ss_cleanup_collection_counts(collection_id,ordinal,catalog_object_id,location_id,catalog_object_type,
                    state,quantity,calculated_at,is_estimated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    [(collection['id'],ordinal,row['catalog_object_id'],row['location_id'],row['catalog_object_type'],row['state'],row['quantity'],row['calculated_at'],row['is_estimated'])
                     for ordinal,row in enumerate(rows)])
        return collection

    @staticmethod
    def _latest_history(db,scope):
        return db.execute('''SELECT f.*,a.attempt_cursor FROM ss_cleanup_collection_attempts a
            JOIN ss_cleanup_collections f ON f.attempt_id=a.id WHERE '''+
            ' AND '.join('a.'+name+'=%s' for name in _K)+' ORDER BY a.attempt_cursor DESC LIMIT 1',_key(scope)).fetchone()

    def finish(self,claim,value,final_access=None):
        """Publish evidence/projection/receipt acknowledgement/finish atomically.

        Acknowledgement means the immutable receipt set was considered, including
        on partial coverage. It never means coverage/history was complete.
        """
        if not isinstance(claim,ObserverClaim):raise CleanupObserverError('A registered cleanup claim is required.',400,code='OBSERVER_INPUT_INVALID')
        result=_minimized_result(value)
        digest=_hash(result); work=claim.work; actor=self._principal(work); context=claim.configuration
        with self.database.transaction() as db:
            conn,scope,config,projection,denied=self._begin(db,context,work['tenant_id'],actor=actor,scope=work,allow_denied=True)
            current=db.execute('SELECT * FROM ss_cleanup_collection_work WHERE id=%s FOR UPDATE',(work['id'],)).fetchone()
            attempt=db.execute('SELECT * FROM ss_cleanup_collection_attempts WHERE id=%s AND work_id=%s',(claim.attempt['id'],work['id'])).fetchone()
            if not attempt or any(attempt[name]!=work[name] for name in (*_K,'config_version','receipt_hwm','manifest_hash',*batches.BASIS_FIELDS)):
                raise CleanupObserverError('Cleanup attempt identity is unavailable.')
            existing=db.execute('SELECT * FROM ss_cleanup_collections WHERE attempt_id=%s',(attempt['id'],)).fetchone()
            now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            reason=self._work_check(db,work,conn,config,context,denied)
            if not reason:
                try:
                    self.square._require_token_context(context)
                    if self._live(conn,work,context,require_expiry=True,now=now):raise ValueError()
                    initial=self._stable_access(claim.access,work,now=now,fresh=False)
                    after=self._stable_access(final_access,work,now=now)
                    if initial!=attempt['access_digest'] or after!=initial:raise ValueError()
                    self.reader.assert_current(db,work['tenant_id'],claim.access)
                    self.reader.assert_current(db,work['tenant_id'],final_access)
                except (ServiceError,ValueError,TypeError,AttributeError):reason='read_authority_changed'
            if not reason and self.square._notice_cursor(db,work['merchant_id'],configuration=context)!=attempt['notice_cursor']:
                reason='authorization_notice_advanced'
            if existing:
                if existing['result_hash']!=digest:raise CleanupObserverError('Cleanup attempt replay conflicts with saved evidence.',409,code='OBSERVER_REQUEST_CONFLICT')
                if reason:raise CleanupObserverError('Current cleanup authority no longer accepts this replay.')
                return {'status':existing['outcome'],'processed':True,'duplicate':True,**_FALSE}
            if not reason and work['receipt_basis_version']==2:
                floor=batches.read_floor(db,work,projection,work['config_version'])
                if floor['id']!=work['receipt_floor_batch_id'] or floor['chain_hash']!=work['receipt_floor_hash']:
                    reason='receipt_read_frontier_changed'
            own_lease=bool(current and current['state']=='reading' and current['lease_token']==attempt['id'] and current['lease_until']>now)
            if not own_lease:reason=reason or 'lease_lost'
            if reason:
                collection=self._write_collection(db,work,attempt,_empty_result(reason),digest,'fenced',[])
                self._event(db,work,reason,attempt['id'])
                if own_lease:
                    db.execute("UPDATE ss_cleanup_collection_work SET state='blocked',lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp() WHERE id=%s AND lease_token=%s",(work['id'],attempt['id']))
                # Never touch projection/ack or a newer/expired lease on rejection.
                return {'status':'fenced','processed':True,'duplicate':False,**_FALSE}
            receipt_flags=self._receipt_flags(db,work)
            outcome='complete' if result['coverage_complete'] else 'partial'
            collection=self._write_collection(db,work,attempt,result,digest,outcome,receipt_flags)
            old_latest=None
            if projection and projection['latest_collection_id']:
                old_latest=db.execute('''SELECT f.*,a.attempt_cursor FROM ss_cleanup_collections f
                    JOIN ss_cleanup_collection_attempts a ON a.id=f.attempt_id WHERE f.id=%s''',(projection['latest_collection_id'],)).fetchone()
            preceding=db.execute('''SELECT f.outcome,a.attempt_cursor FROM ss_cleanup_collection_attempts a
                JOIN ss_cleanup_collections f ON f.attempt_id=a.id WHERE '''+
                ' AND '.join('a.'+name+'=%s' for name in _K)+' AND a.attempt_cursor<%s ORDER BY a.attempt_cursor DESC LIMIT 1',
                (*_key(work),attempt['attempt_cursor'])).fetchone()
            context_hash=_hash(result['context'])
            broken=bool(not projection or projection['config_version']!=work['config_version'] or projection['health']!='complete' or
                projection['acknowledged_hwm']!=work['receipt_hwm'] or projection['context_hash']!=context_hash or not old_latest or
                (now-old_latest['finished_at']).total_seconds()>config['stale_after_seconds'] or outcome!='complete' or
                collection['nonzero_count'] or (old_latest and old_latest['nonzero_count']) or receipt_flags or
                (preceding and preceding['outcome']=='fenced'))
            epoch=uuid.uuid4() if broken else projection['diagnostic_epoch']
            reasons=list(result['reason_codes'])
            if collection['nonzero_count']:reasons.append('nonzero_stock_observed')
            reasons=sorted(set(receipt_flags+reasons))[:32]
            last_complete=collection['id'] if outcome=='complete' else projection['last_complete_id'] if projection else None
            db.execute('''INSERT INTO ss_cleanup_current_projection
                (tenant_id,application_id,environment,merchant_id,authorization_generation,config_version,latest_collection_id,last_complete_id,
                 acknowledged_hwm,diagnostic_epoch,health,reason_codes,context_hash,next_due_at,acknowledged_batch_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,application_id,environment,merchant_id,authorization_generation) DO UPDATE
                SET config_version=EXCLUDED.config_version,latest_collection_id=EXCLUDED.latest_collection_id,last_complete_id=EXCLUDED.last_complete_id,
                    acknowledged_hwm=EXCLUDED.acknowledged_hwm,diagnostic_epoch=EXCLUDED.diagnostic_epoch,health=EXCLUDED.health,
                    reason_codes=EXCLUDED.reason_codes,context_hash=EXCLUDED.context_hash,next_due_at=EXCLUDED.next_due_at,
                    acknowledged_batch_id=EXCLUDED.acknowledged_batch_id''',
                (*_key(work),work['config_version'],collection['id'],last_complete,work['receipt_hwm'],epoch,outcome,Jsonb(reasons),context_hash,
                 collection['finished_at']+timedelta(seconds=config['cadence_seconds']),work['receipt_head_batch_id']))
            saved=db.execute("UPDATE ss_cleanup_collection_work SET state='done',lease_token=NULL,lease_until=NULL,finished_at=clock_timestamp() WHERE id=%s AND state='reading' AND lease_token=%s AND lease_until>clock_timestamp() RETURNING id",(work['id'],attempt['id'])).fetchone()
            if not saved:raise CleanupObserverError('Cleanup lease expired before atomic publication.')
            return {'status':outcome,'processed':True,'duplicate':False,**_FALSE}

    def run_once(self,*,work_id=None):
        if not self.enabled:return {'status':'disabled','processed':False,**_FALSE}
        if work_id is None:
            # Admit at most one new scope without folding, even while other
            # stores have preparation work. Then spend the sole receipt step on
            # the oldest due work. A stale claim requeue never recurses here.
            admitted=self._schedule('notification',1,fold=False,skip_active=True)
            if not admitted['scheduled']:self._schedule('periodic',1,fold=False,skip_active=True)
            context=self.square._token_context()
            with self.database.transaction() as db:
                self.square._notice_barrier(db,configuration=context)
                hint=db.execute("SELECT id FROM ss_cleanup_collection_work WHERE (state='queued' AND next_attempt_at<=clock_timestamp()) OR (state='reading' AND lease_until<=clock_timestamp()) ORDER BY next_attempt_at,created_at,id LIMIT 1").fetchone()
            work_id=hint['id'] if hint else None
        if work_id is not None:
            context=self.square._token_context()
            with self.database.transaction() as db:
                self.square._notice_barrier(db,configuration=context)
                hint=db.execute('SELECT work_kind FROM ss_cleanup_collection_work WHERE id=%s',(_uuid(work_id),)).fetchone()
            if hint and hint['work_kind']=='receipt_prepare':return self.prepare_once(work_id)
        claim=self.claim(work_id)
        if claim is None:return {'status':'idle','processed':False,**_FALSE}
        try:
            # Check original K immediately before the first catalog/location read.
            self.square._require_token_context(claim.configuration)
            self._stable_access(claim.access,claim.work)
            value=self.reader.collect_attempt(claim.access)
            _minimized_result(value)
        except Exception:
            value={**_FALSE,'coverage_complete':False,'pagination_complete':False,'context_unchanged':False,'bracketed':False,
                'reason_codes':['collection_unavailable'],'before_context_hash':None,'after_context_hash':None,
                'locations':[],'objects':{},'counts':[],'missing':None}
        try:
            after=self.reader.access(claim.work['tenant_id'])
            self._stable_access(after,claim.work)
        except Exception:after=None
        return self.finish(claim,value,after)

    def status(self,actor):
        context=self._scope()
        with self.database.transaction() as db:
            conn,scope,config,projection,_=self._begin(db,context,actor['tenant_id'],actor=actor,roles=('owner','manager','buyer'))
            now=db.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            work=latest=complete=None; pending=processing=0; pending_exact=processing_exact=True; reasons=[]
            if scope:
                work=db.execute("SELECT state FROM ss_cleanup_collection_work WHERE "+_WHERE+" AND state IN ('queued','reading')",_key(scope)).fetchone()
                ack=projection['acknowledged_hwm'] if projection else 0
                pending,pending_exact=batches.pending_count(db,scope,ack)
                processed=batches.progress(db,scope)
                processing,processing_exact=batches.pending_count(db,scope,processed['processed_hwm'] if processed else 0)
                latest=self._latest_history(db,scope)
                if projection and projection['last_complete_id']:
                    complete=db.execute('SELECT * FROM ss_cleanup_collections WHERE id=%s',(projection['last_complete_id'],)).fetchone()
            enabled=bool(config and config['enabled']); background=bool(config and config['background_enabled'])
            age=max(0,int((now-latest['finished_at']).total_seconds())) if latest else None
            stale=bool(latest and config and age>config['stale_after_seconds'])
            blocked=self._live(conn,scope,context,require_expiry=True,now=now) if scope else None
            if not conn:state='not_connected'; reasons=['not_connected']
            elif blocked:state='authority_blocked'; reasons=[blocked]
            elif not enabled or not self.enabled:
                state='disabled'; reasons=['worker_disabled' if not self.enabled else 'owner_opt_in_required']
            elif pending or processing:state='stale'; reasons=['notification_backlog']
            elif work:state=work['state']; reasons=['read_in_progress' if state=='reading' else 'read_queued']
            elif latest and latest['outcome']=='fenced':state='stale'; reasons=list(latest['reason_codes'])
            elif stale:state='stale'; reasons=['read_stale']
            elif projection:state=projection['health']; reasons=list(projection['reason_codes'])
            else:state='idle'; reasons=['read_required']
            if projection and state not in ('complete','partial'):reasons+=list(projection['reason_codes'])
            current=bool(state=='complete' and latest and latest['coverage_complete'] and latest['receipt_basis_version']==2 and
                projection['config_version']==config['config_version'] and projection['acknowledged_batch_id']==latest['receipt_head_batch_id'] and
                pending_exact and processing_exact and not pending and not processing and not stale)
            missing=latest['missing'] if latest else None
            interval=lambda row: {'started_at':row['started_at'].isoformat(),'finished_at':row['finished_at'].isoformat()} if row else None
            last_attempt=interval(latest)
            if last_attempt:last_attempt.update(outcome=latest['outcome'],count_rows=latest['count_rows'],missing_count=latest['missing_count'])
            return {'generation':str(conn['authorization_generation']) if conn else None,'config_version':config['config_version'] if config else 0,
                'enabled':enabled,'background_enabled':background,'worker_enabled':self.enabled,
                'cadence_seconds':config['cadence_seconds'] if config else 3600,'stale_after_seconds':config['stale_after_seconds'] if config else 7200,
                'state':state,'reason_codes':sorted(set(reasons))[:32],'coverage_current':current,'last_attempt':last_attempt,
                'last_complete':interval(complete),'observed_age_seconds':age,'pending_notification_count':pending,
                'pending_notification_count_exact':pending_exact,'receipt_processing_pending_count':processing,
                'receipt_processing_pending_count_exact':processing_exact,
                'missing_count':latest['missing_count'] if latest else None,
                'missing':[{'object_id':pair[0],'location_id':pair[1]} for pair in missing[:50]] if missing is not None else [],
                'missing_truncated':bool(missing is not None and len(missing)>50),
                'next_due_at':projection['next_due_at'].isoformat() if projection and projection['next_due_at'] else None,**_FALSE}

    def request_manual(self,actor,values):
        data=_input(values); digest=_hash(data); context=self.square._token_context()
        with self.database.transaction() as db:
            conn,scope,config,projection,_=self._begin(db,context,actor['tenant_id'],actor=actor)
            self._generation(conn,data)
            old=db.execute('SELECT request_hash FROM ss_cleanup_manual_requests WHERE tenant_id=%s AND request_key=%s',(actor['tenant_id'],data['request_key'])).fetchone()
            if old:
                if old['request_hash']!=digest:raise CleanupObserverError('Cleanup request key conflicts with an earlier request.',409,code='OBSERVER_REQUEST_CONFLICT')
                return {'accepted':True,'duplicate':True}
            if not config or config['config_version']!=data['expected_config_version']:
                raise CleanupObserverError('Cleanup settings changed. Check current status.',409,code='OBSERVER_CONFIG_CHANGED')
            if not self.enabled or not config['enabled']:raise CleanupObserverError('Cleanup collection is disabled.',409,code='OBSERVER_DISABLED')
            if self._live(conn,scope,context):raise CleanupObserverError('Current Square authority does not permit this read.')
            work=self._enqueue(db,scope,config,projection,'manual',actor)
            if work is None:raise CleanupObserverError('An earlier cleanup read must finish before this configuration can be used.',409,code='OBSERVER_CONFIG_CHANGED')
            db.execute('''INSERT INTO ss_cleanup_manual_requests(tenant_id,request_key,request_hash,work_id,application_id,environment,merchant_id,
                authorization_generation,config_version,receipt_hwm,manifest_hash,staff_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (actor['tenant_id'],data['request_key'],digest,work['id'],work['application_id'],work['environment'],work['merchant_id'],
                 work['authorization_generation'],work['config_version'],work['receipt_hwm'],work['manifest_hash'],actor['staff_id']))
            return {'accepted':True,'duplicate':False}
