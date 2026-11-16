"""Durable original-stock label pairs; authorization and file integrity are external.

Prepare outside session/service/write guards. Commit only after fresh authority,
under the caller's session guard then service lock. The callback only renames two
already complete files without overwriting. SQLite rollback cannot remove files;
the caller must read back the publication before cleaning its own orphan attempt.
"""
import copy
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import re
import sqlite3
import uuid

from station_store import StationError, now

ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
JOBS = 'receiving_label_jobs'
PUBLICATIONS = 'receiving_label_publications'
JOB_FIELDS = ('service_origin','tenant_id','job_id','receipt_id','receiving_id','server_revision',
    'plan_hash','proof_hash','publication_basis_hash','job_body','part_count','published_parts',
    'state','revision','created_at','updated_at')
PUBLICATION_FIELDS = ('service_origin','tenant_id','publication_id','publication_key','job_id','part_index',
    'receipt_id','receiving_id','server_revision','plan_hash','proof_hash','publication_basis_hash',
    'request_fingerprint','manifest_body','published_at')
COLUMNS = {JOBS:set(JOB_FIELDS), PUBLICATIONS:set(PUBLICATION_FIELDS)}
LINK_FIELDS = ('revision','active_receipt_id','server_revision','plan_hash','financial_receipt_id')
SNAPSHOT_FIELDS = ('service_origin','tenant_id','receiving_id','server_revision','receipt_id','plan_hash',
    'restoration_source_hash','plan_snapshot','operation_snapshot','operation_snapshot_hash')
JOB_KEYS = {'version','serviceOrigin','tenantId','jobId','stockReceiptId','receivingId','serverRevision',
    'planHash','proofHash','publicationBasisHash','plan','createdAt'}
MANIFEST_KEYS = {'version','serviceOrigin','tenantId','publicationId','publicationKey','jobId','partIndex',
    'stockReceiptId','receivingId','serverRevision','planHash','proofHash','publicationBasisHash','partHash',
    'copyOffset','copyCount','totalCopies','rendererVersion','fontHashes','renderBasisHash','files'}
_HASH = re.compile(r'[a-f0-9]{64}')
_LOCAL = re.compile(r'R-[A-Za-z0-9-]{1,100}')
DDL = {
JOBS:'''CREATE TABLE receiving_label_jobs (
 service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, job_id TEXT NOT NULL,
 receipt_id TEXT NOT NULL REFERENCES receipts(id), receiving_id TEXT NOT NULL,
 server_revision INTEGER NOT NULL CHECK(server_revision>=0), plan_hash TEXT NOT NULL,
 proof_hash TEXT NOT NULL, publication_basis_hash TEXT NOT NULL, job_body TEXT NOT NULL,
 part_count INTEGER NOT NULL CHECK(part_count>=1), published_parts INTEGER NOT NULL CHECK(published_parts>=0 AND published_parts<=part_count),
 state TEXT NOT NULL CHECK(state IN ('planned','published')), revision INTEGER NOT NULL CHECK(revision>=1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(service_origin,tenant_id,job_id))''',
PUBLICATIONS:'''CREATE TABLE receiving_label_publications (
 service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, publication_id TEXT NOT NULL,
 publication_key TEXT NOT NULL, job_id TEXT NOT NULL, part_index INTEGER NOT NULL CHECK(part_index>=0),
 receipt_id TEXT NOT NULL REFERENCES receipts(id), receiving_id TEXT NOT NULL,
 server_revision INTEGER NOT NULL CHECK(server_revision>=0), plan_hash TEXT NOT NULL,
 proof_hash TEXT NOT NULL, publication_basis_hash TEXT NOT NULL,
 request_fingerprint TEXT NOT NULL, manifest_body TEXT NOT NULL, published_at TEXT NOT NULL,
 PRIMARY KEY(service_origin,tenant_id,publication_id),
 UNIQUE(service_origin,tenant_id,publication_key),
 UNIQUE(service_origin,tenant_id,job_id,part_index),
 FOREIGN KEY(service_origin,tenant_id,job_id) REFERENCES receiving_label_jobs(service_origin,tenant_id,job_id))'''}
INDEXES = {
 'receiving_label_jobs_child': 'CREATE INDEX receiving_label_jobs_child ON receiving_label_jobs(service_origin,tenant_id,receipt_id,created_at,job_id)',
 'receiving_label_publications_child': 'CREATE INDEX receiving_label_publications_child ON receiving_label_publications(service_origin,tenant_id,receipt_id,published_at,publication_id)',
}


class PublicationChanged(StationError):
    """Rebuild current authority/context and read the same durable key; never rekey."""


def _error():
    return PublicationChanged('The original stock label publication changed or needs owner review. '
        'Reload its same job and publication key. No stock, payment or credit action was performed.')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    if type(value) is not str or not _HASH.fullmatch(value):raise ValueError()
    return value


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:raise ValueError()
    return value


def _local(value):
    if type(value) is not str or not _LOCAL.fullmatch(value):raise ValueError()
    return value


def _int(value, low=0, high=2**63-1):
    if type(value) is not int or not low <= value <= high:raise ValueError()
    return value


def _stamp(value):
    if type(value) is not str or len(value)>60 or datetime.fromisoformat(value).tzinfo is None:raise ValueError()
    return value


def _sql(value):
    return ' '.join(value.strip().rstrip(';').split())


def initialize(store):
    """Create both tables atomically; partial/foreign schemas are never repaired."""
    with store.db() as db:
        db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
        present={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & COLUMNS.keys()
        if present and present != COLUMNS.keys():raise _error()
        if not present:
            for statement in DDL.values():db.execute(statement)
            for statement in INDEXES.values():db.execute(statement)
        inspect_database(db)


def inspect_database(db):
    """Recognize the exact complete schema as hosted metadata, never authority."""
    rows={row[0]:row[1] for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'") if row[0] in COLUMNS}
    if not rows:return False
    if rows.keys()!=COLUMNS.keys():raise _error()
    for table, statement in DDL.items():
        if _sql(rows[table])!=_sql(statement):raise _error()
        columns=list(db.execute('PRAGMA table_info('+table+')'))
        integers={'server_revision','part_count','published_parts','revision'} if table==JOBS else {'server_revision','part_index'}
        if ({row[1] for row in columns}!=COLUMNS[table]
            or any(row[3]!=1 or row[2]!=('INTEGER' if row[1] in integers else 'TEXT') for row in columns)
            or db.execute('PRAGMA foreign_key_check('+table+')').fetchone()):raise _error()
    for name, statement in INDEXES.items():
        row=db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(name,)).fetchone()
        if not row or _sql(row[0])!=_sql(statement):raise _error()
    # No full bodies/hashes during profile recognition. Complete validation is
    # performed by read/prepare outside the short session and write guards.
    for row in db.execute('SELECT service_origin,tenant_id,job_id,receipt_id,receiving_id,server_revision,plan_hash,proof_hash,publication_basis_hash,part_count,published_parts,state,revision,length(CAST(job_body AS BLOB)) FROM '+JOBS):
        origin,tenant,job,child,operation,revision,plan,proof,basis,parts,published,state,local_revision,size=tuple(row)
        try:
            if origin!=ORIGIN:raise ValueError()
            _uuid(tenant);_uuid(job);_local(child);_uuid(operation);_int(revision);_hash(plan);_hash(proof);_hash(basis)
            _int(parts,1,1000000);_int(published,0,parts);_int(local_revision,1);_int(size,1,2000000)
            if state!=('published' if published==parts else 'planned'):raise ValueError()
        except (ValueError,TypeError,AttributeError):raise _error() from None
    for row in db.execute('SELECT service_origin,tenant_id,publication_id,publication_key,job_id,part_index,receipt_id,receiving_id,server_revision,plan_hash,proof_hash,publication_basis_hash,request_fingerprint,length(CAST(manifest_body AS BLOB)) FROM '+PUBLICATIONS):
        origin,tenant,publication,key,job,index,child,operation,revision,plan,proof,basis,fingerprint,size=tuple(row)
        try:
            if origin!=ORIGIN:raise ValueError()
            _uuid(tenant);_uuid(publication);_uuid(key);_uuid(job);_int(index,0,999999);_local(child);_uuid(operation)
            _int(revision);_hash(plan);_hash(proof);_hash(basis);_hash(fingerprint);_int(size,1,262144)
        except (ValueError,TypeError,AttributeError):raise _error() from None
    return True


def _validate_job(job):
    if type(job) is not dict or set(job)!=JOB_KEYS or type(job['version']) is not int or job['version']!=1 or job['serviceOrigin']!=ORIGIN:raise ValueError()
    _uuid(job['tenantId']);_uuid(job['jobId']);_local(job['stockReceiptId']);_uuid(job['receivingId']);_int(job['serverRevision'])
    for field in ('planHash','proofHash','publicationBasisHash'):_hash(job[field])
    _stamp(job['createdAt']);plan=job['plan']
    if type(plan) is not dict or type(plan.get('version')) is not int or plan['version']!=1 or plan.get('receivingId')!=job['receivingId']:raise ValueError()
    for field in ('terminalReceiptHash','cardsHash','renderPlanHash'):_hash(plan[field])
    total=_int(plan['totalCopies'],1,1000000);offset=_int(plan['copyOffset'],0,total-1);count=_int(plan['copyCount'],1,total-offset)
    size=_int(plan['partSize'],1,1000);parts=_int(plan['partCount'],1,1000000)
    if parts!=(count+size-1)//size:raise ValueError()
    budget=plan['budget'];_int(budget['maxArtifactBytes'],1,33554432)
    if type(budget) is not dict or budget.get('maxCopiesPerPart')!=1000 or type(budget.get('maxCopiesPerPart')) is not int:raise ValueError()
    rows=plan['rows']
    if type(rows) is not list or not 1<=len(rows)<=1000:raise ValueError()
    ids=[];copies=0
    for row in rows:
        if type(row) is not dict or set(row)!={'reviewLineId','quantity','cardHash'}:raise ValueError()
        ids.append(_uuid(row['reviewLineId']));copies+=_int(row['quantity'],1,100000);_hash(row['cardHash'])
    if ids!=sorted(set(ids)) or copies!=total:raise ValueError()
    return job


def _validate_manifest(job, manifest):
    if type(manifest) is not dict or set(manifest)!=MANIFEST_KEYS or type(manifest['version']) is not int or manifest['version']!=1:raise ValueError()
    for field in ('serviceOrigin','tenantId','jobId','stockReceiptId','receivingId','serverRevision','planHash','proofHash','publicationBasisHash'):
        if type(manifest[field]) is not type(job[field]) or manifest[field]!=job[field]:raise ValueError()
    ident=_uuid(manifest['publicationId']);_uuid(manifest['publicationKey']);index=_int(manifest['partIndex'],0,job['plan']['partCount']-1)
    plan=job['plan'];offset=plan['copyOffset']+index*plan['partSize'];count=min(plan['partSize'],plan['copyOffset']+plan['copyCount']-offset)
    if (type(manifest['copyOffset']) is not int or manifest['copyOffset']!=offset or type(manifest['copyCount']) is not int or manifest['copyCount']!=count
        or type(manifest['totalCopies']) is not int or manifest['totalCopies']!=plan['totalCopies']):raise ValueError()
    for field in ('partHash','renderBasisHash'):_hash(manifest[field])
    if type(manifest['rendererVersion']) is not str or not 1<=len(manifest['rendererVersion'])<=100:raise ValueError()
    if type(manifest['fontHashes']) is not dict or set(manifest['fontHashes'])!={'regular','bold'}:raise ValueError()
    for value in manifest['fontHashes'].values():_hash(value)
    files=manifest['files']
    if type(files) is not dict or set(files)!={'pdf','zpl'}:raise ValueError()
    for kind,copy_field in (('pdf','pages'),('zpl','expectedCopies')):
        item=files[kind]
        if type(item) is not dict or set(item)!={'basename','sha256','bytes',copy_field}:raise ValueError()
        if item['basename']!='ss-stock-label-'+ident+'.'+kind:raise ValueError()
        _hash(item['sha256']);_int(item['bytes'],1,plan['budget']['maxArtifactBytes'])
        if type(item[copy_field]) is not int or item[copy_field]!=count:raise ValueError()
    return manifest


def request_fingerprint(job, manifest):
    """Immutable logical request; competing attempts may have distinct file IDs."""
    try:
        _validate_job(job);_validate_manifest(job,manifest)
        terms={key:value for key,value in manifest.items() if key not in ('publicationId','files')}
        return hashlib.sha256(canonical({'job':job,'publication':terms}).encode('utf-8')).hexdigest()
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError):raise _error() from None


def _row_tuple(row, fields):
    if type(row) is not dict or set(row)!=set(fields):raise ValueError()
    return tuple(row[key] for key in fields)


@dataclass(frozen=True)
class PreparedPublication:
    tenant_id: str
    receipt_id: str
    financial_id: str
    receiving_id: str
    server_revision: int
    job_id: str
    publication_id: str
    publication_key: str
    part_index: int
    request_fingerprint: str
    expected_sources: tuple
    expected_link: tuple
    expected_snapshot: tuple
    expected_job: tuple | None
    expected_publication: tuple | None
    job_row: tuple
    publication_row: tuple
    receipt_body: str
    receipt_revision: int
    receipt_updated: str
    source_hash: str
    pickup_pending: int
    summary_body: str
    summary_digest: str
    audit_detail: str


def prepare(store, *, child, expected_sources, link, snapshot, job, manifest,
            request_fingerprint=None, expected_job=None, expected_publication=None):
    """Prepare complete typed bytes from an already authorized restoration context."""
    try:
        _validate_job(job);_validate_manifest(job,manifest)
        fingerprint=globals()['request_fingerprint'](job,manifest)
        if request_fingerprint is not None and request_fingerprint!=fingerprint:raise ValueError()
        ident=_local(child['id']);revision=_int(child['revision'],1,2**63-2);marker=child['original_receiving_recovery']
        if (ident!=job['stockReceiptId'] or type(marker) is not dict or marker.get('version')!=1 or type(marker.get('version')) is not int
            or child.get('purchase') is not None or child.get('mode')!='labels' or child.get('requires_purchase') is not False
            or child.get('inventory_status')!='received'
            or any(key in child for key in ('shared_trade','tracked_credit','original_trade_recovery'))):raise ValueError()
        for field in ('serviceOrigin','tenantId','receivingId','serverRevision','planHash','proofHash'):
            if type(marker.get(field)) is not type(job[field]) or marker.get(field)!=job[field]:raise ValueError()
        financial=_local(marker['financialReceiptId']);_hash(marker['restorationSourceHash'])
        if financial==ident or type(expected_sources) is not tuple or not expected_sources:raise ValueError()
        seen=set()
        for row in expected_sources:
            if type(row) is not tuple or len(row)!=3 or row[2] is not None or type(row[1]) is not str or row[0] in seen:raise ValueError()
            seen.add(_local(row[0]))
            if json.loads(row[1]).get('id')!=row[0]:raise ValueError()
            if row[0]==ident and canonical(json.loads(row[1]))!=canonical(child):raise ValueError()
        if not {ident,financial}<=seen:raise ValueError()
        expected_link=tuple(link[key] for key in LINK_FIELDS)
        if (set(link)!={'service_origin','tenant_id','receiving_id',*LINK_FIELDS} or link['service_origin']!=ORIGIN
            or link['tenant_id']!=job['tenantId'] or link['receiving_id']!=job['receivingId']
            or expected_link!=(link['revision'],ident,job['serverRevision'],job['planHash'],financial)):raise ValueError()
        _int(link['revision'],1);_int(link['server_revision'])
        expected_snapshot=tuple(snapshot[key] for key in SNAPSHOT_FIELDS)
        if (set(snapshot)!=set(SNAPSHOT_FIELDS) or expected_snapshot[:6]!=(ORIGIN,job['tenantId'],job['receivingId'],job['serverRevision'],ident,job['planHash'])
            or snapshot['restoration_source_hash']!=marker['restorationSourceHash']):raise ValueError()
        _int(snapshot['server_revision']);_hash(snapshot['operation_snapshot_hash'])
        if any(type(snapshot[key]) is not str for key in ('plan_snapshot','operation_snapshot')):raise ValueError()
        old_job=_row_tuple(expected_job,JOB_FIELDS) if expected_job is not None else None
        old_publication=_row_tuple(expected_publication,PUBLICATION_FIELDS) if expected_publication is not None else None
        job_body=canonical(job);manifest_body=canonical(manifest)
        if len(job_body.encode('utf-8'))>2000000 or len(manifest_body.encode('utf-8'))>262144:raise ValueError()
        published=0;job_revision=0
        if expected_job is not None:
            for column,field in (('service_origin','serviceOrigin'),('tenant_id','tenantId'),('job_id','jobId'),('receipt_id','stockReceiptId'),
                ('receiving_id','receivingId'),('server_revision','serverRevision'),('plan_hash','planHash'),('proof_hash','proofHash'),('publication_basis_hash','publicationBasisHash'),('created_at','createdAt')):
                if type(expected_job[column]) is not type(job[field]) or expected_job[column]!=job[field]:raise ValueError()
            if expected_job['job_body']!=job_body:raise ValueError()
            published=_int(expected_job['published_parts'],0,job['plan']['partCount']);job_revision=_int(expected_job['revision'],1,2**63-2)
            _stamp(expected_job['updated_at'])
            if (type(expected_job['part_count']) is not int or expected_job['part_count']!=job['plan']['partCount']
                or expected_job['state']!=('published' if published==job['plan']['partCount'] else 'planned')):raise ValueError()
        if expected_publication is not None:
            if expected_job is None or (expected_publication['service_origin'],expected_publication['tenant_id'],expected_publication['publication_key'],expected_publication['request_fingerprint'])!=(ORIGIN,job['tenantId'],manifest['publicationKey'],fingerprint):raise ValueError()
            old_manifest=json.loads(expected_publication['manifest_body']);_validate_manifest(job,old_manifest)
            if canonical(old_manifest)!=expected_publication['manifest_body'] or globals()['request_fingerprint'](job,old_manifest)!=fingerprint:raise ValueError()
            for column,field in (('publication_id','publicationId'),('publication_key','publicationKey'),('job_id','jobId'),('part_index','partIndex'),
                ('receipt_id','stockReceiptId'),('receiving_id','receivingId'),('server_revision','serverRevision'),('plan_hash','planHash'),('proof_hash','proofHash'),('publication_basis_hash','publicationBasisHash')):
                if type(expected_publication[column]) is not type(old_manifest[field]) or expected_publication[column]!=old_manifest[field]:raise ValueError()
            _stamp(expected_publication['published_at'])
        elif published>=job['plan']['partCount']:raise ValueError()
        stamp=now();next_child=copy.deepcopy(child)
        next_child['stock_label_outputs']={'version':1,'latestJobId':job['jobId']}
        next_child.update(revision=revision+1,updated=stamp)
        body=canonical(next_child);summary=store._make_summary(next_child)
        summary['stock_label_outputs']=copy.deepcopy(next_child['stock_label_outputs']);summary_body=canonical(summary)
        next_published=published if expected_publication is not None else published+1
        job_row=(ORIGIN,job['tenantId'],job['jobId'],ident,job['receivingId'],job['serverRevision'],job['planHash'],job['proofHash'],job['publicationBasisHash'],job_body,
            job['plan']['partCount'],next_published,'published' if next_published==job['plan']['partCount'] else 'planned',job_revision+(expected_publication is None),job['createdAt'],stamp)
        publication_row=(ORIGIN,job['tenantId'],manifest['publicationId'],manifest['publicationKey'],job['jobId'],manifest['partIndex'],ident,job['receivingId'],job['serverRevision'],
            job['planHash'],job['proofHash'],job['publicationBasisHash'],fingerprint,manifest_body,stamp)
        detail=canonical({'version':1,'jobId':job['jobId'],'publicationId':manifest['publicationId'],'partIndex':manifest['partIndex'],
            'copyOffset':manifest['copyOffset'],'copyCount':manifest['copyCount'],'providerActionPerformed':False})
        return PreparedPublication(job['tenantId'],ident,financial,job['receivingId'],job['serverRevision'],job['jobId'],manifest['publicationId'],manifest['publicationKey'],manifest['partIndex'],fingerprint,
            expected_sources,expected_link,expected_snapshot,old_job,old_publication,job_row,publication_row,body,revision+1,stamp,next_child.get('source_hash',''),
            int(bool(next_child.get('pickup_pending'))),summary_body,hashlib.sha256(summary_body.encode('utf-8')).hexdigest(),detail)
    except (ValueError,TypeError,KeyError,AttributeError,OverflowError,RecursionError):raise _error() from None


def _cas(db, prepared):
    for ident,raw,deleted in prepared.expected_sources:
        row=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(ident,)).fetchone()
        if not row or tuple(row)!=(raw,deleted):raise _error()
    key=(ORIGIN,prepared.tenant_id,prepared.receiving_id)
    row=db.execute('SELECT '+','.join(LINK_FIELDS)+' FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?',key).fetchone()
    if not row or tuple(row)!=prepared.expected_link:raise _error()
    row=db.execute('SELECT '+','.join(SNAPSHOT_FIELDS)+' FROM receiving_recovery_snapshots WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?',key+(prepared.server_revision,)).fetchone()
    if not row or tuple(row)!=prepared.expected_snapshot:raise _error()


def commit(store, prepared, publish_pair):
    """Atomically link one complete pair. Filesystem crash orphans are possible."""
    if type(prepared) is not PreparedPublication or not callable(publish_pair):raise _error()
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE');_cas(db,prepared)
            job=db.execute('SELECT '+','.join(JOB_FIELDS)+' FROM '+JOBS+' WHERE service_origin=? AND tenant_id=? AND job_id=?',(ORIGIN,prepared.tenant_id,prepared.job_id)).fetchone()
            if (tuple(job) if job else None)!=prepared.expected_job:raise _error()
            publication=db.execute('SELECT '+','.join(PUBLICATION_FIELDS)+' FROM '+PUBLICATIONS+' WHERE service_origin=? AND tenant_id=? AND publication_key=?',(ORIGIN,prepared.tenant_id,prepared.publication_key)).fetchone()
            if (tuple(publication) if publication else None)!=prepared.expected_publication:raise _error()
            if publication:
                if publication['request_fingerprint']!=prepared.request_fingerprint:raise _error()
                return {'publication':dict(publication),'job':dict(job),'reused':True,'receipt_id':prepared.receipt_id,'receipt_revision':prepared.receipt_revision-1}
            occupied=db.execute('SELECT 1 FROM '+PUBLICATIONS+' WHERE service_origin=? AND tenant_id=? AND job_id=? AND part_index=?',(ORIGIN,prepared.tenant_id,prepared.job_id,prepared.part_index)).fetchone()
            if occupied:raise _error()
            if job:
                db.execute('UPDATE '+JOBS+' SET published_parts=?,state=?,revision=?,updated_at=? WHERE service_origin=? AND tenant_id=? AND job_id=?',
                    (prepared.job_row[11],prepared.job_row[12],prepared.job_row[13],prepared.job_row[15],ORIGIN,prepared.tenant_id,prepared.job_id))
            else:db.execute('INSERT INTO '+JOBS+' ('+','.join(JOB_FIELDS)+') VALUES ('+','.join('?' for _ in JOB_FIELDS)+')',prepared.job_row)
            db.execute('INSERT INTO '+PUBLICATIONS+' ('+','.join(PUBLICATION_FIELDS)+') VALUES ('+','.join('?' for _ in PUBLICATION_FIELDS)+')',prepared.publication_row)
            db.execute('UPDATE receipts SET body=?,updated=?,source_hash=?,pickup_pending=? WHERE id=?',
                (prepared.receipt_body,prepared.receipt_updated,prepared.source_hash,prepared.pickup_pending,prepared.receipt_id))
            db.execute('INSERT INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                (prepared.receipt_id,prepared.receipt_updated,store.SUMMARY_VERSION,prepared.summary_body,prepared.summary_digest))
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                (prepared.receipt_id,prepared.receipt_updated,'original_stock_labels_published',prepared.audit_detail))
            publish_pair()
    except sqlite3.Error:raise _error() from None
    return {'publication':dict(zip(PUBLICATION_FIELDS,prepared.publication_row)),'job':dict(zip(JOB_FIELDS,prepared.job_row)),
        'reused':False,'receipt_id':prepared.receipt_id,'receipt_revision':prepared.receipt_revision}


def read_job(store, tenant_id, job_id):
    try:_uuid(tenant_id);_uuid(job_id)
    except (ValueError,TypeError,AttributeError):raise _error() from None
    with store.db() as db:row=db.execute('SELECT '+','.join(JOB_FIELDS)+' FROM '+JOBS+' WHERE service_origin=? AND tenant_id=? AND job_id=?',(ORIGIN,tenant_id,job_id)).fetchone()
    return dict(row) if row else None


def read_publication(store, tenant_id, *, publication_key=None, publication_id=None):
    try:
        _uuid(tenant_id)
        if (publication_key is None)==(publication_id is None):raise ValueError()
        ident=_uuid(publication_key if publication_key is not None else publication_id)
    except (ValueError,TypeError,AttributeError):raise _error() from None
    field='publication_key' if publication_key is not None else 'publication_id'
    with store.db() as db:row=db.execute('SELECT '+','.join(PUBLICATION_FIELDS)+' FROM '+PUBLICATIONS+' WHERE service_origin=? AND tenant_id=? AND '+field+'=?',(ORIGIN,tenant_id,ident)).fetchone()
    return dict(row) if row else None


def _list(store, table, fields, tenant_id, *, receipt_id=None, job_id=None, limit=100, offset=0):
    try:
        _uuid(tenant_id);_int(limit,1,1000);_int(offset)
        if receipt_id is not None:_local(receipt_id)
        if job_id is not None:_uuid(job_id)
    except (ValueError,TypeError,AttributeError):raise _error() from None
    where='service_origin=? AND tenant_id=?';values=[ORIGIN,tenant_id]
    if receipt_id is not None:where+=' AND receipt_id=?';values.append(receipt_id)
    if job_id is not None:where+=' AND job_id=?';values.append(job_id)
    order='created_at,job_id' if table==JOBS else 'job_id,part_index'
    with store.db() as db:rows=db.execute('SELECT '+','.join(fields)+' FROM '+table+' WHERE '+where+' ORDER BY '+order+' LIMIT ? OFFSET ?',(*values,limit,offset)).fetchall()
    return [dict(row) for row in rows]


def list_jobs(store, tenant_id, *, receipt_id=None, limit=100, offset=0):
    return _list(store,JOBS,JOB_FIELDS,tenant_id,receipt_id=receipt_id,limit=limit,offset=offset)


def list_publications(store, tenant_id, *, receipt_id=None, job_id=None, limit=100, offset=0):
    return _list(store,PUBLICATIONS,PUBLICATION_FIELDS,tenant_id,receipt_id=receipt_id,job_id=job_id,limit=limit,offset=offset)
