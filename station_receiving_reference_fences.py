"""An unreserved browser reminder fence, never a provider/financial result.

INACTIVE COMPONENT: the controller, UI and profile feature epoch are not wired.
An already-running old writer must be excluded by a tested policy epoch before
this can be activated. Unknown-table startup recognition alone is insufficient.

Prepare outside all guards. Commit requires the caller's common profile OS
guard, service lock, fresh owner/manager remote proof, exact found:false lookup,
and a freshly validated journal with NO matching row (pending included). The
caller compares its exact encrypted predecessor and passes it again here. This
module cannot infer those facts from a boolean, nor reconstruct a missing body.
No journal, receipt, financial, provider or transport mutation occurs here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid

from station_store import StationError

ORIGIN='https://stock-and-sleeve-api.onrender.com'
TABLE='receiving_action_reference_fences'
INTENTS='receiving_action_intents'
FIELDS=('service_origin','tenant_id','intent_id','request_key','receipt_id','financial_receipt_id',
        'receiving_id','server_revision','plan_hash','action_kind','reference_body','reference_fingerprint',
        'evidence_body','evidence_fingerprint','version','resolution_kind','created_at')
COLUMNS={TABLE:set(FIELDS)}
REF_KEYS={'version','serviceOrigin','tenantId','store','stockReceiptId','receivingId','serverRevision',
          'planHash','intentId','requestKey','kind'}
EVIDENCE_KEYS={'version','resolutionKind','bodyAvailable','requestResultObserved','requestAttribution',
               'lookupHash','lookupObservedAt','currentProofHash','restorationSourceHash','stateBasisHash',
               'operationHash','journalMatchingRowAbsent','journalPredecessorHash'}
LINK_FIELDS=('revision','active_receipt_id','server_revision','plan_hash','financial_receipt_id')
SNAPSHOT_FIELDS=('service_origin','tenant_id','receiving_id','server_revision','receipt_id',
                 'plan_hash','restoration_source_hash','plan_snapshot','operation_snapshot','operation_snapshot_hash')
KINDS=frozenset(('commit','recover','authorize','cancel'))
MAX_JOURNAL_BYTES=16*1024*1024
MAX_SOURCE_ROWS=1024
MAX_SOURCE_BYTES=32*1024*1024
DDL='''CREATE TABLE receiving_action_reference_fences (
 service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, intent_id TEXT NOT NULL,
 request_key TEXT NOT NULL, receipt_id TEXT NOT NULL REFERENCES receipts(id),
 financial_receipt_id TEXT NOT NULL REFERENCES receipts(id), receiving_id TEXT NOT NULL,
 server_revision INTEGER NOT NULL CHECK(server_revision>=0), plan_hash TEXT NOT NULL,
 action_kind TEXT NOT NULL CHECK(action_kind IN ('commit','recover','authorize','cancel')),
 reference_body TEXT NOT NULL, reference_fingerprint TEXT NOT NULL,
 evidence_body TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version=1),
 resolution_kind TEXT NOT NULL CHECK(resolution_kind='local_reference_retired'), created_at TEXT NOT NULL,
 PRIMARY KEY(service_origin,tenant_id,intent_id), UNIQUE(service_origin,tenant_id,request_key))'''
_HASH=re.compile('[a-f0-9]{64}')
_KEY=re.compile('[A-Za-z0-9_-]{1,150}')
_LOCAL=re.compile('R-[A-Za-z0-9-]{1,100}')
_STORE=re.compile('[a-z0-9][a-z0-9-]{2,49}')

class ReferenceFenceChanged(StationError):pass

def _error():
    return ReferenceFenceChanged('The saved original-stock reference changed or needs owner review. '
        'Keep its original reference. No request body or provider result has been reconstructed.')

def _int(value,low=0):
    if type(value) is not int or not low<=value<=2**63-1:raise ValueError()
    return value

def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError()
    return value

def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):raise ValueError()
    return value

def _local(value):
    if type(value) is not str or not _LOCAL.fullmatch(value):raise ValueError()
    return value

def _stamp(value):
    if type(value) is not str or len(value)>60:raise ValueError()
    parsed=datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds()!=0:raise ValueError()

def _walk(value,active):
    kind=type(value)
    if value is None or kind in (bool,int):return
    if kind is float:
        if not math.isfinite(value):raise ValueError()
        return
    if kind is str:value.encode('utf-8');return
    if kind not in (dict,list) or id(value) in active:raise ValueError()
    active.add(id(value))
    try:
        if kind is dict:
            if any(type(key) is not str for key in value):raise ValueError()
            for key in value:key.encode('utf-8')
        for item in value.values() if kind is dict else value:_walk(item,active)
    finally:active.remove(id(value))

def canonical(value):
    _walk(value,set())
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)

def _fingerprint(domain,value):
    return hashlib.sha256(canonical({'domain':domain,'value':value}).encode('utf-8')).hexdigest()

def _reference(value):
    if type(value) is not dict or set(value)!=REF_KEYS or type(value['version']) is not int or value['version']!=1 or value['serviceOrigin']!=ORIGIN:raise ValueError()
    for key in ('tenantId','receivingId','intentId'):_uuid(value[key])
    _local(value['stockReceiptId']);_int(value['serverRevision']);_hash(value['planHash'])
    if type(value['store']) is not str or not _STORE.fullmatch(value['store']):raise ValueError()
    if type(value['requestKey']) is not str or not _KEY.fullmatch(value['requestKey']):raise ValueError()
    if type(value['kind']) is not str or value['kind'] not in KINDS:raise ValueError()
    if len(canonical(value).encode('utf-8'))>4096:raise ValueError()

def _evidence(value):
    if type(value) is not dict or set(value)!=EVIDENCE_KEYS:raise ValueError()
    if (type(value['version']) is not int or value['version']!=1 or value['resolutionKind']!='local_reference_retired'
        or value['bodyAvailable'] is not False or value['requestResultObserved'] is not False
        or value['requestAttribution']!='unknown' or value['journalMatchingRowAbsent'] is not True):raise ValueError()
    for field in ('lookupHash','currentProofHash','restorationSourceHash','stateBasisHash','operationHash'):_hash(value[field])
    if value['journalPredecessorHash'] is not None:_hash(value['journalPredecessorHash'])
    _stamp(value['lookupObservedAt'])
    if value['lookupHash']!=hashlib.sha256(canonical({'found':False,'result':None}).encode('utf-8')).hexdigest():raise ValueError()
    if len(canonical(value).encode('utf-8'))>4096:raise ValueError()

def _predecessor(value):
    if value is not None and (type(value) is not bytes or len(value)>MAX_JOURNAL_BYTES):raise ValueError()

def _session(value):
    if type(value) is not tuple or len(value)!=5:raise ValueError()
    nonce,tenant,store,role,expiry=value
    if type(nonce) is not str or not 1<=len(nonce)<=200 or type(store) is not str or not _STORE.fullmatch(store):raise ValueError()
    nonce.encode('utf-8');_uuid(tenant)
    if type(role) is not str or role not in ('owner','manager'):raise ValueError()
    if type(expiry) not in (int,float) or not math.isfinite(expiry) or expiry<=0:raise ValueError()

def _sql(value):return ' '.join(value.strip().rstrip(';').split())

def inspect_database(db):
    """Bounded schema metadata ONLY; do not scan/parse/hash historical rows."""
    rows=db.execute('SELECT type,sql FROM sqlite_master WHERE name=? LIMIT 2',(TABLE,)).fetchall()
    if not rows:return False
    if len(rows)!=1 or rows[0][0]!='table' or _sql(rows[0][1])!=_sql(DDL):raise _error()
    columns=db.execute('PRAGMA table_info('+TABLE+')').fetchmany(len(FIELDS)+1)
    integers={'server_revision','version'}
    if len(columns)!=len(FIELDS) or any(row[1]!=name or row[2]!=('INTEGER' if name in integers else 'TEXT') or row[3]!=1 for row,name in zip(columns,FIELDS)):raise _error()
    indexes=db.execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') LIMIT 3",(TABLE,)).fetchall()
    if len(indexes)!=2 or {row[0] for row in indexes}!={f'sqlite_autoindex_{TABLE}_1',f'sqlite_autoindex_{TABLE}_2'} or any(row[1] is not None for row in indexes):raise _error()
    foreign=db.execute('PRAGMA foreign_key_list('+TABLE+')').fetchmany(3)
    if len(foreign)!=2 or {(row[2],row[3],row[4]) for row in foreign}!={('receipts','receipt_id','id'),('receipts','financial_receipt_id','id')}:raise _error()
    return True

def initialize(store):
    """Standalone hosted-component setup; NOT profile activation or an epoch gate."""
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(TABLE,)).fetchone():db.execute(DDL)
            inspect_database(db)
    except sqlite3.Error:raise _error() from None

def assert_unfenced(db,origin,tenant,intent_id,request_key):
    """Called in action registry's SAME BEGIN; legacy absent table is allowed."""
    if not inspect_database(db):return
    for column,value in (('intent_id',intent_id),('request_key',request_key)):
        if db.execute('SELECT 1 FROM '+TABLE+' WHERE service_origin=? AND tenant_id=? AND '+column+'=? LIMIT 1',(origin,tenant,value)).fetchone():raise _error()

def _validate_row(row):
    if type(row) is not dict or set(row)!=set(FIELDS):raise ValueError()
    if any(type(row[key]) is not str or not 1<=len(row[key].encode('utf-8'))<=4096 for key in ('reference_body','evidence_body')):raise ValueError()
    ref=json.loads(row['reference_body']);evidence=json.loads(row['evidence_body']);_reference(ref);_evidence(evidence)
    if canonical(ref)!=row['reference_body'] or canonical(evidence)!=row['evidence_body']:raise ValueError()
    if _fingerprint('stock-and-sleeve:local-reference:v1',ref)!=row['reference_fingerprint']:raise ValueError()
    if _fingerprint('stock-and-sleeve:local-reference-retirement:v1',evidence)!=row['evidence_fingerprint']:raise ValueError()
    for column,key in (('service_origin','serviceOrigin'),('tenant_id','tenantId'),('intent_id','intentId'),('request_key','requestKey'),
                       ('receipt_id','stockReceiptId'),('receiving_id','receivingId'),('server_revision','serverRevision'),('plan_hash','planHash'),('action_kind','kind')):
        if type(row[column]) is not type(ref[key]) or row[column]!=ref[key]:raise ValueError()
    _local(row['financial_receipt_id']);_stamp(row['created_at'])
    if row['financial_receipt_id']==row['receipt_id'] or type(row['version']) is not int or row['version']!=1 or row['resolution_kind']!='local_reference_retired':raise ValueError()
    return row

def _select_by(db,tenant,column,value):
    where=' WHERE service_origin=? AND tenant_id=? AND '+column+'=?'
    values=(ORIGIN,tenant,value)
    raw=db.execute('SELECT '+','.join(FIELDS)+' FROM '+TABLE+where+
        ' AND length(CAST(reference_body AS BLOB)) BETWEEN 1 AND 4096 AND length(CAST(evidence_body AS BLOB)) BETWEEN 1 AND 4096 LIMIT 1',values).fetchone()
    if raw is None and db.execute('SELECT 1 FROM '+TABLE+where+' LIMIT 1',values).fetchone():raise _error()
    return tuple(raw) if raw is not None else None

def _select(db,tenant,intent_id,key):
    found=[]
    for column,value in (('intent_id',intent_id),('request_key',key)):
        row=_select_by(db,tenant,column,value)
        if row is not None:found.append(tuple(row))
    if len(found)==2 and found[0]!=found[1]:raise _error()
    return found[0] if found else None

def read_fence(store,tenant_id,*,intent_id=None,request_key=None):
    try:
        _uuid(tenant_id)
        if (intent_id is None)==(request_key is None):raise ValueError()
        if intent_id is not None:_uuid(intent_id)
        elif type(request_key) is not str or not _KEY.fullmatch(request_key):raise ValueError()
        field='intent_id' if intent_id is not None else 'request_key';value=intent_id if intent_id is not None else request_key
        with store.db() as db:
            if not inspect_database(db):return None
            raw=_select_by(db,tenant_id,field,value)
            row=dict(zip(FIELDS,tuple(raw))) if raw else None
        return _validate_row(row) if row is not None else None
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,UnicodeError,RecursionError,sqlite3.Error):raise _error() from None

@dataclass(frozen=True)
class PreparedReferenceFence:
    session_facts:tuple
    expected_sources:tuple
    expected_link:tuple
    expected_snapshot:tuple
    expected_journal_predecessor:bytes|None
    expected_fence:tuple|None
    row:tuple
    audit_detail:str

def prepare_retirement(store,*,reference,child,expected_sources,link,snapshot,evidence,session_facts,expected_journal_predecessor):
    """Read/validate an existing fence and prepare immutable bytes outside guards."""
    try:
        _reference(reference);_evidence(evidence);_session(session_facts);_predecessor(expected_journal_predecessor)
        if session_facts[1:3]!=(reference['tenantId'],reference['store']):raise ValueError()
        if type(child) is not dict or child.get('id')!=reference['stockReceiptId']:raise ValueError()
        _int(child.get('revision'),1);marker=child.get('original_receiving_recovery')
        if (type(marker) is not dict or type(marker.get('version')) is not int or marker['version']!=1 or child.get('purchase') is not None
            or child.get('mode')!='labels' or child.get('requires_purchase') is not False or child.get('inventory_intent') is not None
            or any(key in child for key in ('shared_trade','tracked_credit','original_trade_recovery'))):raise ValueError()
        for field in ('serviceOrigin','tenantId','receivingId','serverRevision','planHash'):
            if type(marker.get(field)) is not type(reference[field]) or marker[field]!=reference[field]:raise ValueError()
        financial=_local(marker['financialReceiptId']);_uuid(marker['tradeId']);_hash(marker['receiptHash'])
        if financial==child['id']:raise ValueError()
        if marker['proofHash']!=evidence['currentProofHash'] or marker['restorationSourceHash']!=evidence['restorationSourceHash']:raise ValueError()
        expected_journal_hash=hashlib.sha256(expected_journal_predecessor).hexdigest() if expected_journal_predecessor is not None else None
        if evidence['journalPredecessorHash']!=expected_journal_hash:raise ValueError()
        if type(expected_sources) is not tuple or not 2<=len(expected_sources)<=MAX_SOURCE_ROWS:raise ValueError()
        seen=set();total=0
        for row in expected_sources:
            if type(row) is not tuple or len(row)!=3 or row[2] is not None or type(row[1]) is not str:raise ValueError()
            ident=_local(row[0]);total+=len(row[1].encode('utf-8'))
            if ident in seen or total>MAX_SOURCE_BYTES:raise ValueError()
            seen.add(ident);parsed=json.loads(row[1])
            if type(parsed) is not dict or parsed.get('id')!=ident:raise ValueError()
            if ident==child['id'] and canonical(parsed)!=canonical(child):raise ValueError()
        if not {child['id'],financial}<=seen:raise ValueError()
        if type(link) is not dict or set(link)!={'service_origin','tenant_id','receiving_id',*LINK_FIELDS}:raise ValueError()
        _int(link['revision'],1);_int(link['server_revision'])
        if (link['service_origin'],link['tenant_id'],link['receiving_id'])!=(ORIGIN,reference['tenantId'],reference['receivingId']):raise ValueError()
        link_tuple=tuple(link[k] for k in LINK_FIELDS)
        if link_tuple!=(link['revision'],child['id'],reference['serverRevision'],reference['planHash'],financial):raise ValueError()
        if type(snapshot) is not dict or set(snapshot)!=set(SNAPSHOT_FIELDS):raise ValueError()
        _int(snapshot['server_revision']);snapshot_tuple=tuple(snapshot[k] for k in SNAPSHOT_FIELDS)
        if snapshot_tuple[:6]!=(ORIGIN,reference['tenantId'],reference['receivingId'],reference['serverRevision'],child['id'],reference['planHash']):raise ValueError()
        if snapshot['restoration_source_hash']!=evidence['restorationSourceHash'] or snapshot['operation_snapshot_hash']!=evidence['operationHash']:raise ValueError()
        if type(snapshot['plan_snapshot']) is not str or type(snapshot['operation_snapshot']) is not str:raise ValueError()
        if hashlib.sha256(snapshot['operation_snapshot'].encode('utf-8')).hexdigest()!=snapshot['operation_snapshot_hash']:raise ValueError()
        previous=[]
        for kwargs in ({'intent_id':reference['intentId']},{'request_key':reference['requestKey']}):
            value=read_fence(store,reference['tenantId'],**kwargs)
            if value is not None:previous.append(tuple(value[k] for k in FIELDS))
        if previous and any(row!=previous[0] for row in previous):raise ValueError()
        old=previous[0] if previous else None
        ref_body=canonical(reference);ref_hash=_fingerprint('stock-and-sleeve:local-reference:v1',reference)
        if old is not None and (old[10]!=ref_body or old[11]!=ref_hash or old[5]!=financial):raise ValueError()
        stamp=datetime.now(timezone.utc).isoformat();evidence_body=canonical(evidence)
        row=old or (ORIGIN,reference['tenantId'],reference['intentId'],reference['requestKey'],child['id'],financial,reference['receivingId'],
            reference['serverRevision'],reference['planHash'],reference['kind'],ref_body,ref_hash,evidence_body,
            _fingerprint('stock-and-sleeve:local-reference-retirement:v1',evidence),1,'local_reference_retired',stamp)
        detail=canonical({'version':1,'intentId':reference['intentId'],'requestKey':reference['requestKey'],
            'receivingId':reference['receivingId'],'resolutionKind':'local_reference_retired','referenceFingerprint':ref_hash,
            'requestResultObserved':False,'requestAttribution':'unknown','bodyAvailable':False,'providerActionPerformed':False})
        return PreparedReferenceFence(session_facts,expected_sources,link_tuple,snapshot_tuple,expected_journal_predecessor,old,row,detail)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,UnicodeError,RecursionError,sqlite3.Error):raise _error() from None

def commit_retirement(store,prepared,*,current_session_facts,current_journal_predecessor,journal_absent_attested):
    """Caller supplies fresh journal/authority facts under OS guard then service lock."""
    if type(prepared) is not PreparedReferenceFence or journal_absent_attested is not True:raise _error()
    if type(current_journal_predecessor) is not type(prepared.expected_journal_predecessor) or current_journal_predecessor!=prepared.expected_journal_predecessor:raise _error()
    if (type(current_session_facts) is not tuple or len(current_session_facts)!=5
        or any(type(a) is not type(b) or a!=b for a,b in zip(current_session_facts,prepared.session_facts))
        or current_session_facts[4]<=time.time()):raise _error()
    row=prepared.row;key=(ORIGIN,row[1],row[6])
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
            if not inspect_database(db):raise _error()
            if current_session_facts[4]<=time.time():raise _error()
            # Exact bytes/scalars only inside the short transaction.
            if type(current_journal_predecessor) is not type(prepared.expected_journal_predecessor) or current_journal_predecessor!=prepared.expected_journal_predecessor:raise _error()
            for ident,raw,deleted in prepared.expected_sources:
                actual=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(ident,)).fetchone()
                if actual is None or tuple(actual)!=(raw,deleted):raise _error()
            actual=db.execute('SELECT '+','.join(LINK_FIELDS)+' FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?',key).fetchone()
            if actual is None or tuple(actual)!=prepared.expected_link:raise _error()
            actual=db.execute('SELECT '+','.join(SNAPSHOT_FIELDS)+' FROM receiving_recovery_snapshots WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?',key+(row[7],)).fetchone()
            if actual is None or tuple(actual)!=prepared.expected_snapshot:raise _error()
            for field,value in (('intent_id',row[2]),('request_key',row[3])):
                if db.execute('SELECT 1 FROM '+INTENTS+' WHERE service_origin=? AND tenant_id=? AND '+field+'=? LIMIT 1',(ORIGIN,row[1],value)).fetchone():raise _error()
            if db.execute('SELECT 1 FROM '+INTENTS+" WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND state IN ('pending','applied') LIMIT 1",key).fetchone():raise _error()
            existing=_select(db,row[1],row[2],row[3])
            if existing!=prepared.expected_fence:raise _error()
            if existing is None:
                db.execute('INSERT INTO '+TABLE+' ('+','.join(FIELDS)+') VALUES ('+','.join('?' for _ in FIELDS)+')',row)
                db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',(row[4],row[16],'original_stock_reference_retired',prepared.audit_detail))
    except sqlite3.Error:raise _error() from None
    return {'fence':dict(zip(FIELDS,row)),'reused':prepared.expected_fence is not None}
