"""Bounded immutable processing of original signed inventory receipt evidence.

Caller owns the observer's barrier/tenant/merchant/connection/config locks before
locking this progress head, then work. This module performs SQL only: processing
is not a provider read, read acknowledgement, continuity or cleanup eligibility.
"""
import hashlib
import json
import uuid

from psycopg.types.json import Jsonb

from .security import ServiceError


K=('tenant_id','application_id','environment','merchant_id','authorization_generation')
WHERE=' AND '.join(name+'=%s' for name in K)
BOUND=100
COUNT_BOUND=1000
COUNTERS=('receipts','facts','positive','negative','estimated','item','other_state','unknown_payload','interrupted_authority')
BASIS_FIELDS=('receipt_basis_version','work_kind','receipt_basis_hash','receipt_floor_batch_id','receipt_floor_hash','receipt_head_batch_id','receipt_head_hash')


def args(scope):return tuple(scope[name] for name in K)
def canonical(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def zeros():return {name:0 for name in COUNTERS}


def latest_hwm(db,scope):
    row=db.execute('''SELECT receipt_cursor FROM ss_cleanup_inventory_receipts WHERE '''+WHERE+'''
        AND binding_status IN ('active','revoked','revocation_pending','token_expired') ORDER BY receipt_cursor DESC LIMIT 1''',args(scope)).fetchone()
    return row['receipt_cursor'] if row else 0


def pending_count(db,scope,floor):
    rows=db.execute('''SELECT receipt_cursor FROM ss_cleanup_inventory_receipts WHERE '''+WHERE+'''
        AND binding_status IN ('active','revoked','revocation_pending','token_expired') AND receipt_cursor>%s
        ORDER BY receipt_cursor LIMIT 1001''',(*args(scope),floor)).fetchall()
    return len(rows),len(rows)<=COUNT_BOUND


def chain_digest(row):
    return digest({'version':2,'id':str(row['id']),'scope':[str(value) for value in args(row)],
        'config_version':row['config_version'],'is_genesis':row['is_genesis'],
        'previous_id':str(row['previous_id']) if row['previous_id'] else None,'previous_hash':row['previous_hash'],
        'low_exclusive':row['low_exclusive'],'high_inclusive':row['high_inclusive'],
        'manifest_hash':row['manifest_hash'],'delta_counts':row['delta_counts'],'prefix_counts':row['prefix_counts']})


def checked_node(db,scope,node_id,node_hash=None):
    row=db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE id=%s',(node_id,)).fetchone()
    if (not row or args(row)!=args(scope) or (node_hash is not None and row['chain_hash']!=node_hash)
            or digest(row['receipt_manifest'])!=row['manifest_hash'] or chain_digest(row)!=row['chain_hash']):
        raise ServiceError('Cleanup receipt proof is unavailable or changed.',409)
    return row


def progress(db,scope,*,lock=False):
    return db.execute('SELECT * FROM ss_cleanup_receipt_progress WHERE '+WHERE+(' FOR UPDATE' if lock else ''),args(scope)).fetchone()


def _insert(db,row):
    fields=('id',*K,'config_version','is_genesis','previous_id','previous_hash','low_exclusive','high_inclusive',
        'receipt_manifest','manifest_hash','delta_counts','prefix_counts','chain_hash')
    values=tuple(Jsonb(row[name]) if name in ('receipt_manifest','delta_counts','prefix_counts') else row[name] for name in fields)
    return db.execute('INSERT INTO ss_cleanup_receipt_batches('+','.join(fields)+') VALUES ('+','.join(['%s']*len(fields))+') RETURNING *',values).fetchone()


def ensure_head(db,scope,config_version):
    head=progress(db,scope,lock=True)
    if head:return checked_node(db,scope,head['head_id'],head['head_hash'])
    genesis=db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE '+WHERE+' AND is_genesis',args(scope)).fetchone()
    if not genesis:
        row={name:scope[name] for name in K}
        row.update(id=uuid.uuid4(),config_version=config_version,is_genesis=True,previous_id=None,previous_hash=None,
            low_exclusive=0,high_inclusive=0,receipt_manifest=[],manifest_hash=digest([]),delta_counts=zeros(),prefix_counts=zeros())
        row['chain_hash']=chain_digest(row); genesis=_insert(db,row)
    db.execute('''INSERT INTO ss_cleanup_receipt_progress
        (tenant_id,application_id,environment,merchant_id,authorization_generation,head_id,head_hash,processed_hwm,revision)
        VALUES (%s,%s,%s,%s,%s,%s,%s,0,0)''',(*args(scope),genesis['id'],genesis['chain_hash']))
    return genesis


def fold_once(db,scope,config_version,target_hwm):
    old=ensure_head(db,scope,config_version)
    if old['high_inclusive']>=target_hwm:return old,False
    rows=db.execute('''SELECT receipt_cursor,body_hash,fact_count,payload_status,binding_status FROM ss_cleanup_inventory_receipts
        WHERE '''+WHERE+''' AND binding_status IN ('active','revoked','revocation_pending','token_expired')
          AND receipt_cursor>%s AND receipt_cursor<=%s ORDER BY receipt_cursor LIMIT 100''',
        (*args(scope),old['high_inclusive'],target_hwm)).fetchall()
    if not rows:raise ServiceError('Cleanup receipt prefix cannot advance through an unknown range.',409)
    ids=[row['receipt_cursor'] for row in rows]
    facts=db.execute('''SELECT count(*) AS facts,
        count(*) FILTER(WHERE quantity::NUMERIC>0) AS positive,count(*) FILTER(WHERE quantity::NUMERIC<0) AS negative,
        count(*) FILTER(WHERE is_estimated IS TRUE) AS estimated,count(*) FILTER(WHERE catalog_object_type='ITEM') AS item,
        count(*) FILTER(WHERE state<>'IN_STOCK') AS other_state FROM ss_cleanup_inventory_facts WHERE receipt_cursor=ANY(%s)''',(ids,)).fetchone()
    if facts['facts']!=sum(row['fact_count'] for row in rows) or facts['facts']>100000:
        raise ServiceError('Cleanup receipt facts are incomplete.',409)
    delta={name:int(facts[name]) for name in ('facts','positive','negative','estimated','item','other_state')}
    delta.update(receipts=len(rows),unknown_payload=sum(row['payload_status']!='counts_present' for row in rows),
        interrupted_authority=sum(row['binding_status']!='active' for row in rows))
    manifest=[{'receipt_cursor':row['receipt_cursor'],'body_hash':row['body_hash']} for row in rows]
    row={name:scope[name] for name in K}
    row.update(id=uuid.uuid4(),config_version=config_version,is_genesis=False,previous_id=old['id'],previous_hash=old['chain_hash'],
        low_exclusive=old['high_inclusive'],high_inclusive=rows[-1]['receipt_cursor'],receipt_manifest=manifest,
        manifest_hash=digest(manifest),delta_counts=delta,prefix_counts={name:old['prefix_counts'][name]+delta[name] for name in COUNTERS})
    row['chain_hash']=chain_digest(row); new=_insert(db,row)
    saved=db.execute('''UPDATE ss_cleanup_receipt_progress SET head_id=%s,head_hash=%s,processed_hwm=%s,revision=revision+1
        WHERE '''+WHERE+' AND head_id=%s AND head_hash=%s RETURNING head_id',
        (new['id'],new['chain_hash'],new['high_inclusive'],*args(scope),old['id'],old['chain_hash'])).fetchone()
    if not saved:raise ServiceError('Cleanup receipt processing changed concurrently.',409)
    return new,True


def read_floor(db,scope,projection,config_version):
    if projection and projection.get('acknowledged_batch_id'):
        return checked_node(db,scope,projection['acknowledged_batch_id'])
    # A legacy numerical ack is not a verified prefix anchor. Reconsider its
    # original immutable facts once from genesis; do not invent a matching node.
    return db.execute('SELECT * FROM ss_cleanup_receipt_batches WHERE '+WHERE+' AND is_genesis',args(scope)).fetchone() or ensure_head(db,scope,config_version)


def basis(scope,config_version,kind,target_hwm,floor,head):
    value={'receipt_basis_version':2,'work_kind':kind,'receipt_floor_batch_id':floor['id'],'receipt_floor_hash':floor['chain_hash'],
        'receipt_head_batch_id':head['id'],'receipt_head_hash':head['chain_hash']}
    value['receipt_basis_hash']=digest({'version':2,'scope':[str(value) for value in args(scope)],'config_version':config_version,
        'work_kind':kind,'target_hwm':target_hwm,'floor_id':str(floor['id']),'floor_hash':floor['chain_hash'],
        'head_id':str(head['id']),'head_hash':head['chain_hash']})
    return value


def validate_basis(db,work,*,ready=False):
    if work['receipt_basis_version']!=2:raise ServiceError('Unsupported cleanup receipt basis.',409)
    floor=checked_node(db,work,work['receipt_floor_batch_id'],work['receipt_floor_hash'])
    head=checked_node(db,work,work['receipt_head_batch_id'],work['receipt_head_hash'])
    expected=basis(work,work['config_version'],work['work_kind'],work['receipt_hwm'],floor,head)
    if (any(work[name]!=expected[name] for name in BASIS_FIELDS) or floor['high_inclusive']>head['high_inclusive'] or
            head['high_inclusive']>work['receipt_hwm'] or work['receipt_manifest']!=head['receipt_manifest'] or work['manifest_hash']!=head['manifest_hash'] or
            any(head['prefix_counts'][name]<floor['prefix_counts'][name] for name in COUNTERS)):
        raise ServiceError('Cleanup receipt basis changed.',409)
    if ready:
        current=progress(db,work,lock=True)
        if (work['work_kind']!='collection' or head['high_inclusive']!=work['receipt_hwm'] or not current or
                current['head_id']!=head['id'] or current['head_hash']!=head['chain_hash']):
            raise ServiceError('Cleanup receipt preparation is not current.',409)
    return floor,head


def interval_flags(floor,head):
    delta={name:head['prefix_counts'][name]-floor['prefix_counts'][name] for name in COUNTERS}
    if any(value<0 for value in delta.values()):raise ServiceError('Cleanup receipt interval changed.',409)
    names={'positive':'notification_positive','negative':'notification_negative','estimated':'notification_estimated',
        'item':'notification_item_count','other_state':'notification_other_state','unknown_payload':'notification_coverage_unknown',
        'interrupted_authority':'notification_authority_interrupted'}
    return sorted(code for name,code in names.items() if delta[name])
