"""Durable exact original-stock action intents, never provider authority.

Prepare outside OS/session/service/SQLite guards. The controller supplies fresh
session scalars while holding the profile OS guard, then the service lock, and
calls commit. No journal/file/network operation belongs in this module. An
uncertain journal write or response leaves the pending row and its original key.
Only a verified durable SAME-child refresh permits applied; only exact journal
key acknowledgement permits acknowledged. Those two external facts are the
controller's responsibility, not claims inferred from a local database row.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import math
import re
import sqlite3
import uuid

from hosted_labels import profile as label_profile
from receiving_evidence import provider_id
from station_http_json import preflight_request_body
from station_store import StationError, now
from station_receiving_reference_fences import assert_unfenced

ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
TABLE = 'receiving_action_intents'
FIELDS = ('service_origin','tenant_id','intent_id','receipt_id','financial_receipt_id',
    'receiving_id','server_revision','plan_hash','action_kind','request_key','intent_body',
    'request_fingerprint','state','result_fingerprint','applied_receipt_revision',
    'applied_proof_hash','revision','created_at','updated_at')
COLUMNS = {TABLE:set(FIELDS)}
LINK_FIELDS = ('revision','active_receipt_id','server_revision','plan_hash','financial_receipt_id')
SNAPSHOT_FIELDS = ('service_origin','tenant_id','receiving_id','server_revision','receipt_id',
    'plan_hash','restoration_source_hash','plan_snapshot','operation_snapshot','operation_snapshot_hash')
INTENT_KEYS = {'version','serviceOrigin','tenantId','store','intentId','stockReceiptId',
    'financialReceiptId','tradeId','receiptHash','receivingId','serverRevision','planHash',
    'restorationSourceHash','proofHash','stateBasisHash','operationHash','provider','labelProfile',
    'kind','requestKey','body','createdAt'}
KINDS = frozenset(('commit','recover','authorize','cancel'))
_HASH = re.compile(r'[a-f0-9]{64}')
_LOCAL = re.compile(r'R-[A-Za-z0-9-]{1,100}')
_KEY = re.compile(r'[A-Za-z0-9_-]{1,150}')
_STORE = re.compile(r'[a-z0-9][a-z0-9-]{2,49}')
DDL = '''CREATE TABLE receiving_action_intents (
 service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, intent_id TEXT NOT NULL,
 receipt_id TEXT NOT NULL REFERENCES receipts(id),
 financial_receipt_id TEXT NOT NULL REFERENCES receipts(id), receiving_id TEXT NOT NULL,
 server_revision INTEGER NOT NULL CHECK(server_revision>=0), plan_hash TEXT NOT NULL,
 action_kind TEXT NOT NULL CHECK(action_kind IN ('commit','recover','authorize','cancel')),
 request_key TEXT NOT NULL, intent_body TEXT NOT NULL, request_fingerprint TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','applied','acknowledged')),
 result_fingerprint TEXT, applied_receipt_revision INTEGER, applied_proof_hash TEXT,
 revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(service_origin,tenant_id,intent_id),
 UNIQUE(service_origin,tenant_id,request_key),
 CHECK((state='pending' AND revision=1 AND result_fingerprint IS NULL AND applied_receipt_revision IS NULL AND applied_proof_hash IS NULL)
 OR (state='applied' AND revision>=2 AND result_fingerprint IS NOT NULL AND applied_receipt_revision IS NOT NULL AND applied_receipt_revision>=1 AND applied_proof_hash IS NOT NULL)
 OR (state='acknowledged' AND revision>=3 AND result_fingerprint IS NOT NULL AND applied_receipt_revision IS NOT NULL AND applied_receipt_revision>=1 AND applied_proof_hash IS NOT NULL)))'''
INDEXES = {
    'receiving_action_intents_active': "CREATE UNIQUE INDEX receiving_action_intents_active ON receiving_action_intents(service_origin,tenant_id,receiving_id) WHERE state IN ('pending','applied')",
    'receiving_action_intents_child': 'CREATE INDEX receiving_action_intents_child ON receiving_action_intents(service_origin,tenant_id,receipt_id,created_at,intent_id)',
}

# These are local public session facts, never a bearer token or an MFA boolean.
# The caller freshly reads them under the OS guard and enforces current expiry.
SessionFacts = tuple[str, str, str, str, int | float]  # nonce, tenant, store, role, expiry
SourceRows = tuple[tuple[str, str, None], ...]  # id, exact body, deleted_at


class ActionChanged(StationError):
    """Reread current context and the ORIGINAL durable intent; never rekey."""


def _error():
    return ActionChanged('The original stock action changed or needs owner recovery. '
        'Keep its saved request key and review the current original stock record.')


def _walk(value, active):
    kind=type(value)
    if value is None or kind in (bool,int):return
    if kind is float:
        if not math.isfinite(value):raise ValueError()
        return
    if kind is str:
        value.encode('utf-8');return
    if kind not in (dict,list):raise ValueError()
    ident=id(value)
    if ident in active:raise ValueError()
    active.add(ident)
    try:
        if kind is dict and any(type(key) is not str for key in value):raise ValueError()
        for key in value if kind is dict else ():_walk(key,active)
        for child in value.values() if kind is dict else value:_walk(child,active)
    finally:active.remove(ident)


def canonical(value):
    _walk(value,set())
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):raise ValueError()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
    return value


def _local(value):
    if type(value) is not str or not _LOCAL.fullmatch(value):raise ValueError()
    return value


def _int(value,low=0,high=2**63-1):
    if type(value) is not int or not low<=value<=high:raise ValueError()
    return value


def _stamp(value):
    if type(value) is not str or len(value)>60 or datetime.fromisoformat(value).tzinfo is None:raise ValueError()
    return value


def _session(value):
    if type(value) is not tuple or len(value)!=5:raise ValueError()
    nonce,tenant,store,role,expiry=value
    if type(nonce) is not str or not 1<=len(nonce)<=200 or type(store) is not str or not _STORE.fullmatch(store):raise ValueError()
    _uuid(tenant)
    if role not in ('owner','manager') or type(role) is not str:raise ValueError()
    if type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=0:raise ValueError()
    nonce.encode('utf-8')
    return value


def _validate_intent(value):
    if type(value) is not dict or set(value)!=INTENT_KEYS or type(value['version']) is not int or value['version']!=1 or value['serviceOrigin']!=ORIGIN:raise ValueError()
    for field in ('tenantId','intentId','tradeId','receivingId'):_uuid(value[field])
    for field in ('stockReceiptId','financialReceiptId'):_local(value[field])
    if value['stockReceiptId']==value['financialReceiptId'] or type(value['store']) is not str or not _STORE.fullmatch(value['store']):raise ValueError()
    _int(value['serverRevision']);_stamp(value['createdAt'])
    for field in ('receiptHash','planHash','restorationSourceHash','proofHash','stateBasisHash','operationHash'):_hash(value[field])
    if type(value['requestKey']) is not str or not _KEY.fullmatch(value['requestKey']) or type(value['kind']) is not str or value['kind'] not in KINDS:raise ValueError()
    provider=value['provider']
    if type(provider) is not dict or set(provider)!={'merchant_id','environment','application_id','location_id'} or provider['environment'] not in ('sandbox','production'):raise ValueError()
    for field in ('merchant_id','application_id','location_id'):provider_id(provider[field])
    if type(value['labelProfile']) is not dict or label_profile(value['labelProfile'])!=value['labelProfile']:raise ValueError()
    body=value['body'];terms={'request_key','revision','plan_hash'}
    extras={'commit':{'confirmed','excluded_not_received_ack'},'authorize':{'resume_same_operation'},'cancel':{'reason'},'recover':set()}[value['kind']]
    if type(body) is not dict or set(body)!=terms|extras:raise ValueError()
    if (type(body['revision']) is not int or body['revision']!=value['serverRevision']
        or type(body['plan_hash']) is not str or body['plan_hash']!=value['planHash']
        or type(body['request_key']) is not str or body['request_key']!=value['requestKey']):raise ValueError()
    for field in extras-{'reason'}:
        if body[field] is not True:raise ValueError()
    if 'reason' in extras and (type(body['reason']) is not str or not 10<=len(body['reason'].strip())<=200
        or len(body['reason'])>200 or any(ord(char)<32 or ord(char)==127 for char in body['reason'])):raise ValueError()
    preflight_request_body(body)
    return value


def request_fingerprint(intent):
    try:
        _validate_intent(intent)
        return hashlib.sha256(canonical(intent).encode('utf-8')).hexdigest()
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError):raise _error() from None


def _row_tuple(row):
    if type(row) is not dict or set(row)!=set(FIELDS):raise ValueError()
    return tuple(row[key] for key in FIELDS)


def _validate_row(row):
    _row_tuple(row);value=json.loads(row['intent_body']);_validate_intent(value)
    if canonical(value)!=row['intent_body'] or request_fingerprint(value)!=row['request_fingerprint']:raise ValueError()
    for column,key in (('service_origin','serviceOrigin'),('tenant_id','tenantId'),('intent_id','intentId'),('receipt_id','stockReceiptId'),
        ('financial_receipt_id','financialReceiptId'),('receiving_id','receivingId'),('server_revision','serverRevision'),
        ('plan_hash','planHash'),('action_kind','kind'),('request_key','requestKey'),('created_at','createdAt')):
        if type(row[column]) is not type(value[key]) or row[column]!=value[key]:raise ValueError()
    _stamp(row['updated_at']);_int(row['revision'],1)
    if row['state']=='pending':
        if row['revision']!=1 or any(row[key] is not None for key in ('result_fingerprint','applied_receipt_revision','applied_proof_hash')):raise ValueError()
    elif row['state'] in ('applied','acknowledged'):
        _int(row['revision'],{'applied':2,'acknowledged':3}[row['state']])
        _hash(row['result_fingerprint']);_int(row['applied_receipt_revision'],1);_hash(row['applied_proof_hash'])
    else:raise ValueError()
    return row


def _sql(value):return ' '.join(value.strip().rstrip(';').split())


def initialize(store):
    """Atomic recognized schema only; never repair a foreign existing table."""
    with store.db() as db:
        db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
            db.execute(DDL)
            for statement in INDEXES.values():db.execute(statement)
        inspect_database(db)


def inspect_database(db):
    """Hosted metadata recognition, without parsing or hashing request bodies."""
    row=db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone()
    if not row:return False
    if _sql(row[0])!=_sql(DDL):raise _error()
    columns=list(db.execute('PRAGMA table_info('+TABLE+')'));nullable={'result_fingerprint','applied_receipt_revision','applied_proof_hash'}
    integers={'server_revision','applied_receipt_revision','revision'}
    if ({row[1] for row in columns}!=set(FIELDS) or any(row[2]!=('INTEGER' if row[1] in integers else 'TEXT') or row[3]!=(0 if row[1] in nullable else 1) for row in columns)
        or db.execute('PRAGMA foreign_key_check('+TABLE+')').fetchone()):raise _error()
    for name,statement in INDEXES.items():
        actual=db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
        if not actual or _sql(actual[0])!=_sql(statement):raise _error()
    metadata_fields=tuple(field for field in FIELDS if field!='intent_body')
    for row in db.execute('SELECT '+','.join(metadata_fields)+',length(CAST(intent_body AS BLOB)) AS body_size FROM '+TABLE):
        # Profile recognition uses a read-only default tuple connection; Store
        # uses sqlite3.Row. Project exact ordered fields for both row factories.
        value=dict(zip((*metadata_fields,'body_size'),tuple(row)))
        try:
            if value['service_origin']!=ORIGIN:raise ValueError()
            for field in ('tenant_id','intent_id','receiving_id'):_uuid(value[field])
            _local(value['receipt_id']);_local(value['financial_receipt_id']);_int(value['server_revision']);_hash(value['plan_hash']);_hash(value['request_fingerprint'])
            if value['receipt_id']==value['financial_receipt_id'] or value['action_kind'] not in KINDS or type(value['request_key']) is not str or not _KEY.fullmatch(value['request_key']):raise ValueError()
            _stamp(value['created_at']);_stamp(value['updated_at']);_int(value['body_size'],1,65536)
            if value['state']=='pending':
                if value['revision']!=1 or any(value[key] is not None for key in nullable):raise ValueError()
            elif value['state'] in ('applied','acknowledged'):
                _int(value['revision'],{'applied':2,'acknowledged':3}[value['state']])
                _hash(value['result_fingerprint']);_int(value['applied_receipt_revision'],1);_hash(value['applied_proof_hash'])
            else:raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):raise _error() from None
    return True


@dataclass(frozen=True)
class PreparedAction:
    tenant_id: str
    receiving_id: str
    server_revision: int
    receipt_id: str
    session_facts: SessionFacts
    expected_sources: SourceRows
    expected_link: tuple
    expected_snapshot: tuple
    expected_active: tuple | None
    expected_intent: tuple | None
    row: tuple
    audit_at: str | None
    audit_detail: str | None


def _context(child,expected_sources,link,snapshot,intent,session_facts):
    _session(session_facts);_validate_intent(intent)
    if session_facts[1:3]!=(intent['tenantId'],intent['store']):raise ValueError()
    if type(child) is not dict or child.get('id')!=intent['stockReceiptId'] or type(child.get('revision')) is not int or not 1<=child['revision']<=2**63-1:raise ValueError()
    marker=child.get('original_receiving_recovery')
    if (type(marker) is not dict or type(marker.get('version')) is not int or marker['version']!=1
        or child.get('purchase') is not None or child.get('mode')!='labels' or child.get('requires_purchase') is not False
        or child.get('inventory_intent') is not None or any(key in child for key in ('shared_trade','tracked_credit','original_trade_recovery'))):raise ValueError()
    for field in ('serviceOrigin','tenantId','financialReceiptId','tradeId','receiptHash','receivingId','serverRevision','planHash'):
        if type(marker.get(field)) is not type(intent[field]) or marker.get(field)!=intent[field]:raise ValueError()
    for field in ('restorationSourceHash','proofHash'):_hash(marker.get(field))
    if type(expected_sources) is not tuple:raise ValueError()
    seen=set()
    for row in expected_sources:
        if type(row) is not tuple or len(row)!=3 or row[2] is not None or type(row[1]) is not str:raise ValueError()
        ident=_local(row[0])
        if ident in seen:raise ValueError()
        seen.add(ident);body=json.loads(row[1]);canonical(body)
        if type(body) is not dict or body.get('id')!=ident:raise ValueError()
        if ident==child['id'] and canonical(body)!=canonical(child):raise ValueError()
    if not {child['id'],intent['financialReceiptId']}<=seen:raise ValueError()
    if type(link) is not dict or set(link)!={'service_origin','tenant_id','receiving_id',*LINK_FIELDS}:raise ValueError()
    expected_link=tuple(link[key] for key in LINK_FIELDS)
    if (link['service_origin'],link['tenant_id'],link['receiving_id'])!=(ORIGIN,intent['tenantId'],intent['receivingId']):raise ValueError()
    _int(link['revision'],1)
    if expected_link!=(link['revision'],child['id'],intent['serverRevision'],intent['planHash'],intent['financialReceiptId']) or type(link['server_revision']) is not int:raise ValueError()
    if type(snapshot) is not dict or set(snapshot)!=set(SNAPSHOT_FIELDS):raise ValueError()
    expected_snapshot=tuple(snapshot[key] for key in SNAPSHOT_FIELDS)
    if expected_snapshot[:6]!=(ORIGIN,intent['tenantId'],intent['receivingId'],intent['serverRevision'],child['id'],intent['planHash']) or type(snapshot['server_revision']) is not int:raise ValueError()
    if snapshot['restoration_source_hash']!=marker['restorationSourceHash']:raise ValueError()
    _hash(snapshot['operation_snapshot_hash'])
    for field in ('plan_snapshot','operation_snapshot'):
        if type(snapshot[field]) is not str:raise ValueError()
    # The controller verifies the complete remote source/proof. Here we verify
    # local raw/scalar consistency and keep those exact bytes for the last CAS.
    if hashlib.sha256(snapshot['operation_snapshot'].encode('utf-8')).hexdigest()!=snapshot['operation_snapshot_hash']:raise ValueError()
    return expected_link,expected_snapshot


def prepare_reservation(store,*,child,expected_sources,link,snapshot,intent,session_facts,expected_active=None,expected_intent=None):
    """Prepare an insert or exact same-key no-op, without writing anything."""
    try:
        expected_link,expected_snapshot=_context(child,expected_sources,link,snapshot,intent,session_facts)
        # Creation must still be the exact preview basis. Completion may have
        # a newer proof, but a new pending request may not silently rebase.
        marker=child['original_receiving_recovery']
        if expected_intent is None and any(marker[key]!=intent[key] for key in ('restorationSourceHash','proofHash')):raise ValueError()
        body=canonical(intent)
        if len(body.encode('utf-8'))>65536:raise ValueError()
        fingerprint=request_fingerprint(intent)
        old_active=_row_tuple(_validate_row(expected_active)) if expected_active is not None else None
        old_intent=_row_tuple(_validate_row(expected_intent)) if expected_intent is not None else None
        if expected_active is not None and (expected_active['service_origin'],expected_active['tenant_id'],expected_active['receiving_id'])!=(ORIGIN,intent['tenantId'],intent['receivingId']):raise ValueError()
        if expected_intent is not None:
            if expected_intent['intent_body']!=body or expected_intent['request_fingerprint']!=fingerprint:raise ValueError()
            if expected_intent['state'] in ('pending','applied') and old_active!=old_intent:raise ValueError()
            row=old_intent;stamp=None;detail=None
        else:
            if expected_active is not None:raise ValueError()
            stamp=now()
            row=(ORIGIN,intent['tenantId'],intent['intentId'],child['id'],intent['financialReceiptId'],intent['receivingId'],intent['serverRevision'],intent['planHash'],
                intent['kind'],intent['requestKey'],body,fingerprint,'pending',None,None,None,1,intent['createdAt'],stamp)
            detail=canonical({'version':1,'intentId':intent['intentId'],'requestKey':intent['requestKey'],'receivingId':intent['receivingId'],
                'serverRevision':intent['serverRevision'],'planHash':intent['planHash'],'kind':intent['kind'],'providerActionPerformed':False})
        return PreparedAction(intent['tenantId'],intent['receivingId'],intent['serverRevision'],child['id'],session_facts,expected_sources,
            expected_link,expected_snapshot,old_active,old_intent,row,stamp,detail)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError):raise _error() from None


def _prepare_transition(store,*,child,expected_sources,link,snapshot,expected_intent,session_facts,state,result_fingerprint=None):
    try:
        _validate_row(expected_intent);intent=json.loads(expected_intent['intent_body'])
        expected_link,expected_snapshot=_context(child,expected_sources,link,snapshot,intent,session_facts)
        old=_row_tuple(expected_intent);current=expected_intent['state'];row=dict(expected_intent)
        marker=child['original_receiving_recovery'];proof=_hash(marker['proofHash'])
        if state=='applied':
            _hash(result_fingerprint)
            if current=='pending':
                row.update(state='applied',result_fingerprint=result_fingerprint,applied_receipt_revision=child['revision'],applied_proof_hash=proof,revision=2,updated_at=now())
            else:
                if current not in ('applied','acknowledged') or result_fingerprint!=row['result_fingerprint'] or child['revision']<row['applied_receipt_revision']:raise ValueError()
                if proof!=row['applied_proof_hash']:
                    # Another authorized client may have durably refreshed this
                    # SAME projection before the original journal key is ACKed.
                    # The controller freshly verifies that coherent proof. Keep
                    # the immutable result/key, and reaffirm only forward local
                    # progress; never reopen an acknowledged intent.
                    if current!='applied' or child['revision']<=row['applied_receipt_revision']:raise ValueError()
                    counter=_int(row['revision'],2,2**63-2)+1
                    row.update(applied_receipt_revision=child['revision'],applied_proof_hash=proof,revision=counter,updated_at=now())
        elif state=='acknowledged':
            if current not in ('applied','acknowledged') or proof!=row['applied_proof_hash'] or child['revision']<row['applied_receipt_revision']:raise ValueError()
            if current=='applied':row.update(state='acknowledged',revision=_int(row['revision'],2,2**63-2)+1,updated_at=now())
        else:raise ValueError()
        active=old if current in ('pending','applied') else None
        return PreparedAction(intent['tenantId'],intent['receivingId'],intent['serverRevision'],child['id'],session_facts,expected_sources,
            expected_link,expected_snapshot,active,old,_row_tuple(row),None,None)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError):raise _error() from None


def prepare_applied(store,*,child,expected_sources,link,snapshot,expected_intent,result_fingerprint,session_facts):
    """Controller has verified the result and durably refreshed this SAME child."""
    return _prepare_transition(store,child=child,expected_sources=expected_sources,link=link,snapshot=snapshot,
        expected_intent=expected_intent,result_fingerprint=result_fingerprint,session_facts=session_facts,state='applied')


def prepare_acknowledged(store,*,child,expected_sources,link,snapshot,expected_intent,session_facts):
    """Controller has durably removed only this already-applied journal key."""
    return _prepare_transition(store,child=child,expected_sources=expected_sources,link=link,snapshot=snapshot,
        expected_intent=expected_intent,session_facts=session_facts,state='acknowledged')


def _cas(db,prepared,current_session_facts):
    if type(current_session_facts) is not tuple or len(current_session_facts)!=5 or any(type(a) is not type(b) or a!=b for a,b in zip(current_session_facts,prepared.session_facts)):raise _error()
    # New component is inactive until the profile epoch/controller integration.
    # A present fence nevertheless blocks every later reservation/transition in
    # this SAME transaction. The helper imports no action registry code.
    try:assert_unfenced(db,ORIGIN,prepared.tenant_id,prepared.row[2],prepared.row[9])
    except StationError:raise _error() from None
    for ident,raw,deleted in prepared.expected_sources:
        row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(ident,)).fetchone()
        if not row or tuple(row)!=(raw,deleted):raise _error()
    key=(ORIGIN,prepared.tenant_id,prepared.receiving_id)
    link=db.execute('SELECT '+','.join(LINK_FIELDS)+' FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?',key).fetchone()
    if not link or tuple(link)!=prepared.expected_link:raise _error()
    snapshot=db.execute('SELECT '+','.join(SNAPSHOT_FIELDS)+' FROM receiving_recovery_snapshots WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?',key+(prepared.server_revision,)).fetchone()
    if not snapshot or tuple(snapshot)!=prepared.expected_snapshot:raise _error()
    active=db.execute('SELECT '+','.join(FIELDS)+' FROM '+TABLE+" WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND state IN ('pending','applied')",key).fetchone()
    if (tuple(active) if active else None)!=prepared.expected_active:raise _error()
    intent=db.execute('SELECT '+','.join(FIELDS)+' FROM '+TABLE+' WHERE service_origin=? AND tenant_id=? AND request_key=?',(ORIGIN,prepared.tenant_id,prepared.row[9])).fetchone()
    if (tuple(intent) if intent else None)!=prepared.expected_intent:raise _error()


def _commit(store,prepared,current_session_facts,*,reservation):
    if type(prepared) is not PreparedAction:raise _error()
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE');_cas(db,prepared,current_session_facts)
            reused=prepared.expected_intent==prepared.row
            if not reused:
                if reservation:
                    if prepared.expected_intent is not None or prepared.audit_at is None or prepared.audit_detail is None:raise _error()
                    db.execute('INSERT INTO '+TABLE+' ('+','.join(FIELDS)+') VALUES ('+','.join('?' for _ in FIELDS)+')',prepared.row)
                    db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                        (prepared.receipt_id,prepared.audit_at,'original_stock_action_reserved',prepared.audit_detail))
                else:
                    if prepared.expected_intent is None or prepared.audit_at is not None or prepared.audit_detail is not None:raise _error()
                    db.execute('UPDATE '+TABLE+' SET state=?,result_fingerprint=?,applied_receipt_revision=?,applied_proof_hash=?,revision=?,updated_at=? WHERE service_origin=? AND tenant_id=? AND intent_id=?',
                        (prepared.row[12],prepared.row[13],prepared.row[14],prepared.row[15],prepared.row[16],prepared.row[18],ORIGIN,prepared.tenant_id,prepared.row[2]))
    except sqlite3.Error:raise _error() from None
    return {'intent':dict(zip(FIELDS,prepared.row)),'reused':reused}


def commit_reservation(store,prepared,*,current_session_facts):
    """Caller holds OS session guard then service lock; no journal callback."""
    return _commit(store,prepared,current_session_facts,reservation=True)


def commit_transition(store,prepared,*,current_session_facts):
    return _commit(store,prepared,current_session_facts,reservation=False)


def read_intent(store,tenant_id,*,intent_id=None,request_key=None):
    try:
        _uuid(tenant_id)
        if (intent_id is None)==(request_key is None):raise ValueError()
        if intent_id is not None:_uuid(intent_id)
        elif type(request_key) is not str or not _KEY.fullmatch(request_key):raise ValueError()
        field='intent_id' if intent_id is not None else 'request_key';value=intent_id if intent_id is not None else request_key
        with store.db() as db:row=db.execute('SELECT '+','.join(FIELDS)+' FROM '+TABLE+' WHERE service_origin=? AND tenant_id=? AND '+field+'=?',(ORIGIN,tenant_id,value)).fetchone()
        return _validate_row(dict(row)) if row else None
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError,sqlite3.Error):raise _error() from None


def read_active(store,tenant_id,receiving_id):
    try:
        _uuid(tenant_id);_uuid(receiving_id)
        with store.db() as db:rows=db.execute('SELECT '+','.join(FIELDS)+' FROM '+TABLE+" WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND state IN ('pending','applied')",(ORIGIN,tenant_id,receiving_id)).fetchall()
        if len(rows)>1:raise ValueError()
        return _validate_row(dict(rows[0])) if rows else None
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError,sqlite3.Error):raise _error() from None


def list_intents(store,tenant_id,*,receipt_id=None,limit=100,offset=0):
    try:
        _uuid(tenant_id);_int(limit,1,1000);_int(offset)
        where='service_origin=? AND tenant_id=?';values=[ORIGIN,tenant_id]
        if receipt_id is not None:_local(receipt_id);where+=' AND receipt_id=?';values.append(receipt_id)
        with store.db() as db:rows=db.execute('SELECT '+','.join(FIELDS)+' FROM '+TABLE+' WHERE '+where+' ORDER BY created_at,intent_id LIMIT ? OFFSET ?',(*values,limit,offset)).fetchall()
        return [_validate_row(dict(row)) for row in rows]
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError,UnicodeError,sqlite3.Error):raise _error() from None
