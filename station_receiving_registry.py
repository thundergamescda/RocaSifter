"""Atomic local stock projections of an already verified original operation.

This internal persistence layer does not authorize a server action. The caller
verifies original source evidence and current hosted authority before preparing
bytes, then holds the short session guard while committing their exact CAS.
Financial receipts, imports and receiving journals are never rewritten here.
"""
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
import uuid

from station_store import StationError


ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
HASH = re.compile(r'[a-f0-9]{64}')
LOCAL_ID = re.compile(r'R-[A-Za-z0-9-]{1,100}')
LINKS = 'receiving_recovery_links'
SNAPSHOTS = 'receiving_recovery_snapshots'
COLUMNS = {
    LINKS: {'service_origin', 'tenant_id', 'receiving_id', 'active_receipt_id',
            'server_revision', 'plan_hash', 'financial_receipt_id', 'revision'},
    SNAPSHOTS: {'service_origin', 'tenant_id', 'receiving_id', 'server_revision',
                'receipt_id', 'plan_hash', 'restoration_source_hash', 'plan_snapshot',
                'operation_snapshot', 'operation_snapshot_hash'},
}


def _error():
    return StationError('The original receiving record changed or needs owner review. '
                        'Preview it again; its financial record and original operation were kept.')


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError()
    return value


def _id(value):
    if not isinstance(value, str) or not LOCAL_ID.fullmatch(value):
        raise ValueError()
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise ValueError()
    return value


def _revision(value):
    if type(value) is not int or not 0 <= value <= 2**63-1:
        raise ValueError()
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def initialize(store):
    """Create the pair atomically; never classify or convert a profile."""
    with store.db() as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS receiving_recovery_links (
            service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, receiving_id TEXT NOT NULL,
            active_receipt_id TEXT NOT NULL REFERENCES receipts(id),
            server_revision INTEGER NOT NULL CHECK(server_revision>=0), plan_hash TEXT NOT NULL,
            financial_receipt_id TEXT NOT NULL REFERENCES receipts(id),
            revision INTEGER NOT NULL CHECK(revision>=1),
            PRIMARY KEY(service_origin,tenant_id,receiving_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS receiving_recovery_snapshots (
            service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, receiving_id TEXT NOT NULL,
            server_revision INTEGER NOT NULL CHECK(server_revision>=0),
            receipt_id TEXT NOT NULL UNIQUE REFERENCES receipts(id), plan_hash TEXT NOT NULL,
            restoration_source_hash TEXT NOT NULL, plan_snapshot TEXT NOT NULL,
            operation_snapshot TEXT NOT NULL, operation_snapshot_hash TEXT NOT NULL,
            PRIMARY KEY(service_origin,tenant_id,receiving_id,server_revision))''')
        db.execute('''CREATE INDEX IF NOT EXISTS receipts_original_receiving_operation
            ON receipts(json_extract(body,'$.hosted_receive.receiving_id'))''')
        inspect_database(db)


def inspect_database(db):
    """Bounded structural profile inspection; full proof runs before use.

    Do not load/hash every large stored snapshot while recognizing a profile.
    This only recognizes the schema and local references as hosted metadata;
    neither these tables nor their checksums grant remote authorization.
    """
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = tables & COLUMNS.keys()
    if not present:
        return False
    if present != COLUMNS.keys():
        raise _error()
    for table, expected in COLUMNS.items():
        columns = list(db.execute('PRAGMA table_info('+table+')'))
        primary = ('service_origin','tenant_id','receiving_id') + (('server_revision',) if table==SNAPSHOTS else ())
        if ({row[1] for row in columns} != expected
            or tuple(row[1] for row in sorted(columns,key=lambda row:row[5]) if row[5]) != primary
            or any(row[3]!=1 or row[2].upper()!=('INTEGER' if row[1] in ('server_revision','revision') else 'TEXT') for row in columns)):
            raise _error()
        foreign = {(row[3],row[2],row[4]) for row in db.execute('PRAGMA foreign_key_list('+table+')')}
        required_foreign = {('receipt_id','receipts','id')} if table==SNAPSHOTS else {
            ('active_receipt_id','receipts','id'),('financial_receipt_id','receipts','id')}
        if foreign != required_foreign:
            raise _error()
        if db.execute('PRAGMA foreign_key_check('+table+')').fetchone():
            raise _error()
    unique_columns = [tuple(row[0] for row in db.execute('SELECT name FROM pragma_index_info(?) ORDER BY seqno',(index[1],)))
                      for index in db.execute('PRAGMA index_list('+SNAPSHOTS+')') if index[2]==1 and index[4]==0]
    if ('receipt_id',) not in unique_columns:
        raise _error()
    any_rows = False
    for row in db.execute('SELECT service_origin,tenant_id,receiving_id,active_receipt_id,server_revision,plan_hash,financial_receipt_id,revision FROM receiving_recovery_links'):
        origin, tenant, operation, receipt, revision, plan, financial, local_revision = tuple(row)
        if origin != ORIGIN:
            raise _error()
        _uuid(tenant); _uuid(operation); _id(receipt); _revision(revision); _hash(plan); _id(financial)
        if type(local_revision) is not int or not 1 <= local_revision <= 2**63-1:
            raise _error()
        snapshot = db.execute('''SELECT receipt_id,plan_hash FROM receiving_recovery_snapshots
            WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?''',
            (origin, tenant, operation, revision)).fetchone()
        if not snapshot or tuple(snapshot) != (receipt, plan):
            raise _error()
        any_rows = True
    # JSON validity/transport size are checked in SQLite, not by materializing
    # all historical proofs into Python during profile initialization.
    bad = db.execute('''SELECT 1 FROM receiving_recovery_snapshots WHERE
        service_origin<>? OR NOT json_valid(plan_snapshot) OR NOT json_valid(operation_snapshot)
        OR length(CAST(plan_snapshot AS BLOB))>2000000
        OR length(CAST(operation_snapshot AS BLOB))>8388608 LIMIT 1''', (ORIGIN,)).fetchone()
    if bad:
        raise _error()
    for row in db.execute('SELECT service_origin,tenant_id,receiving_id,server_revision,receipt_id,plan_hash,restoration_source_hash,operation_snapshot_hash FROM receiving_recovery_snapshots'):
        origin, tenant, operation, revision, receipt, plan, source_hash, operation_hash = tuple(row)
        if origin != ORIGIN:
            raise _error()
        _uuid(tenant); _uuid(operation); _revision(revision); _id(receipt)
        _hash(plan); _hash(source_hash); _hash(operation_hash)
        any_rows = True
    return any_rows


@dataclass(frozen=True)
class PreparedProjection:
    tenant_id: str
    receiving_id: str
    server_revision: int
    plan_hash: str
    financial_receipt_id: str
    receipt_id: str
    receipt_updated: str
    receipt_body: str
    summary_body: str
    summary_digest: str
    source_hash: str
    source_body: str
    operation_body: str
    operation_hash: str


def prepare(store, receipt, facts, source, operation, financial_receipt_id, *, existing_revision=None):
    """Serialize new child/summary/snapshots outside OS and DB write guards."""
    try:
        marker = receipt['original_receiving_recovery']
        tenant = _uuid(marker['tenantId']); ident = _uuid(facts['receivingId'])
        revision = _revision(facts['serverRevision']); plan = _hash(facts['planHash'])
        source_hash = _hash(facts['restorationSourceHash']); receipt_id = _id(receipt['id'])
        _id(financial_receipt_id)
        next_revision = 1
        if existing_revision is not None:
            if _revision(existing_revision)<1 or existing_revision==2**63-1:
                raise ValueError()
            next_revision=existing_revision+1
        if (marker.get('version') != 1 or type(marker['version']) is not int
            or marker.get('serviceOrigin') != ORIGIN or marker.get('receivingId') != ident
            or marker.get('serverRevision') != revision or type(marker.get('serverRevision')) is not int
            or marker.get('planHash') != plan or marker.get('restorationSourceHash') != source_hash
            or marker.get('financialReceiptId') != financial_receipt_id
            or receipt.get('purchase') is not None or any(key in receipt for key in ('shared_trade', 'tracked_credit', 'original_trade_recovery'))
            or receipt.get('requires_purchase') is not False or receipt.get('mode') != 'labels'
            or receipt_id==financial_receipt_id
            or receipt.get('inventory_intent') is not None or receipt.get('revision') != next_revision
            or type(receipt.get('revision')) is not int or not isinstance(receipt.get('updated'), str)
            or not isinstance(receipt.get('cards'), list) or not 1 <= len(receipt['cards']) <= 1000
            or source.get('receivingId') != ident or source.get('serverRevision') != revision
            or source.get('planHash') != plan or source.get('restorationSourceHash') != source_hash
            or operation.get('receiving_id') != ident or operation.get('revision') != revision
            or operation.get('plan_hash') != plan):
            raise ValueError()
        body = canonical(receipt); summary = canonical(store._make_summary(receipt))
        source_body = canonical(source); operation_body = canonical(operation)
        if len(source_body.encode('utf-8')) > 2_000_000 or len(operation_body.encode('utf-8')) > 8_388_608:
            raise ValueError()
        return PreparedProjection(tenant, ident, revision, plan, financial_receipt_id, receipt_id,
            receipt['updated'], body, summary, digest(summary), source_hash, source_body,
            operation_body, digest(operation_body))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise _error() from None


def read(store, tenant_id, receiving_id):
    """Read registry references only; caller still verifies complete snapshots."""
    _uuid(tenant_id); _uuid(receiving_id)
    with store.db() as db:
        link = db.execute('SELECT * FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?',
                          (ORIGIN, tenant_id, receiving_id)).fetchone()
        snapshots = db.execute('''SELECT * FROM receiving_recovery_snapshots
            WHERE service_origin=? AND tenant_id=? AND receiving_id=? ORDER BY server_revision''',
            (ORIGIN, tenant_id, receiving_id)).fetchall()
        ordinary=_ordinary_references(db,tenant_id,receiving_id)
        return {'link': dict(link) if link else None, 'snapshots': [dict(row) for row in snapshots],
                'ordinary_references':[dict(row) for row in ordinary]}


def _ordinary_references(db, tenant_id, receiving_id):
    return db.execute('''SELECT id,body,deleted_at FROM receipts WHERE
        json_extract(body,'$.hosted_receive.receiving_id')=?
        AND NOT EXISTS (SELECT 1 FROM receiving_recovery_snapshots s
            WHERE s.receipt_id=receipts.id AND s.service_origin=? AND s.tenant_id=? AND s.receiving_id=?
            AND json_type(receipts.body,'$.original_receiving_recovery.version')='integer'
            AND json_extract(receipts.body,'$.original_receiving_recovery.version')=1
            AND json_extract(receipts.body,'$.original_receiving_recovery.serviceOrigin')=s.service_origin
            AND json_extract(receipts.body,'$.original_receiving_recovery.tenantId')=s.tenant_id
            AND json_extract(receipts.body,'$.original_receiving_recovery.receivingId')=s.receiving_id
            AND json_type(receipts.body,'$.original_receiving_recovery.serverRevision')='integer'
            AND json_extract(receipts.body,'$.original_receiving_recovery.serverRevision')=s.server_revision
            AND json_extract(receipts.body,'$.original_receiving_recovery.planHash')=s.plan_hash)
        ORDER BY id''',(receiving_id,ORIGIN,tenant_id,receiving_id)).fetchall()


def _source_cas(db, prepared, expected_sources):
    if (not isinstance(expected_sources,tuple) or not expected_sources
        or any(not isinstance(row,tuple) or len(row)!=3 or not isinstance(row[0],str)
               or not isinstance(row[1],str) or row[2] is not None for row in expected_sources)
        or len({row[0] for row in expected_sources})!=len(expected_sources)
        or prepared.financial_receipt_id not in {row[0] for row in expected_sources}
        or prepared.receipt_id in {row[0] for row in expected_sources}):
        raise _error()
    for receipt_id,raw_body,deleted_at in expected_sources:
        current=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(receipt_id,)).fetchone()
        if not current or tuple(current)!=(raw_body,deleted_at):raise _error()


def commit_new(store, prepared, expected_sources, *, expected_link_revision=None):
    """Insert a single new revision projection with exact source/registry CAS.

    Reuse/deletion restore and remote-state refresh are separate explicit paths;
    a duplicate never silently replaces a child or chooses a financial mirror.
    expected_sources contains (local_id, exact_raw_body, deleted_at) captured by
    the coordinator outside guards, including the financial record and any CSV.
    """
    if not isinstance(prepared, PreparedProjection):
        raise _error()
    key = (ORIGIN, prepared.tenant_id, prepared.receiving_id)
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            _source_cas(db,prepared,expected_sources)
            link = db.execute('SELECT * FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?', key).fetchone()
            if link and (type(link['server_revision']) is not int or not 0<=link['server_revision']<=2**63-1
                         or type(link['revision']) is not int or not 1<=link['revision']<2**63-1):
                raise _error()
            if expected_link_revision is None:
                if link:
                    raise _error()
            elif (type(expected_link_revision) is not int or not link or link['revision'] != expected_link_revision
                  or link['financial_receipt_id'] != prepared.financial_receipt_id
                  or link['server_revision'] >= prepared.server_revision):
                raise _error()
            occupied = _ordinary_references(db,prepared.tenant_id,prepared.receiving_id)
            if occupied:
                raise StationError('A local record already references this original receiving operation. Open or review that record; no second stock record was created.')
            existing = db.execute('''SELECT 1 FROM receiving_recovery_snapshots
                WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?''',
                key+(prepared.server_revision,)).fetchone()
            if existing:
                raise _error()
            if link and (type(link['revision']) is not int or not 1<=link['revision']<2**63-1):
                raise _error()
            db.execute('INSERT INTO receipts(id,source_hash,updated,body,deleted_at,pickup_pending) VALUES (?,?,?,?,NULL,0)',
                (prepared.receipt_id, 'hosted-original-receiving:'+prepared.receiving_id+':'+str(prepared.server_revision),
                 prepared.receipt_updated, prepared.receipt_body))
            db.execute('INSERT INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                (prepared.receipt_id, prepared.receipt_updated, store.SUMMARY_VERSION,
                 prepared.summary_body, prepared.summary_digest))
            db.execute('''INSERT INTO receiving_recovery_snapshots
                (service_origin,tenant_id,receiving_id,server_revision,receipt_id,plan_hash,
                 restoration_source_hash,plan_snapshot,operation_snapshot,operation_snapshot_hash)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',
                key+(prepared.server_revision, prepared.receipt_id, prepared.plan_hash, prepared.source_hash,
                     prepared.source_body, prepared.operation_body, prepared.operation_hash))
            local_revision = link['revision']+1 if link else 1
            db.execute('''INSERT INTO receiving_recovery_links
                (service_origin,tenant_id,receiving_id,active_receipt_id,server_revision,plan_hash,financial_receipt_id,revision)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(service_origin,tenant_id,receiving_id) DO UPDATE SET
                active_receipt_id=excluded.active_receipt_id,server_revision=excluded.server_revision,
                plan_hash=excluded.plan_hash,financial_receipt_id=excluded.financial_receipt_id,revision=excluded.revision''',
                key+(prepared.receipt_id, prepared.server_revision, prepared.plan_hash, prepared.financial_receipt_id, local_revision))
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                (prepared.receipt_id, prepared.receipt_updated, 'original_receiving_projected',
                 'Opened the verified original receiving plan as a separate local stock record. No agreement, credit, payment or provider action.'))
    except sqlite3.Error:
        raise _error() from None
    return {'receipt_id': prepared.receipt_id, 'link_revision': local_revision}


def commit_existing(store, prepared, expected_sources, *, expected_link_revision,
                    expected_child_raw, expected_child_deleted_at,
                    expected_snapshot_source_body, expected_snapshot_operation_body,
                    expected_snapshot_source_hash, expected_snapshot_operation_hash,
                    restore_existing=False):
    """Reuse, explicitly restore or refresh the same active stock child.

    Caller proves the existing child and new remote evidence outside guards.
    The exact local sources, child, source/operation snapshot and link revision
    are compared again in one short transaction. No financial row is updated.
    """
    if (not isinstance(prepared,PreparedProjection) or type(expected_link_revision) is not int
        or not 1<=expected_link_revision<2**63-1 or type(restore_existing) is not bool
        or not isinstance(expected_child_raw,str)
        or expected_child_deleted_at is not None and not isinstance(expected_child_deleted_at,str)
        or not isinstance(expected_snapshot_source_body,str) or not isinstance(expected_snapshot_operation_body,str)
        or not isinstance(expected_snapshot_source_hash,str) or not HASH.fullmatch(expected_snapshot_source_hash)
        or not isinstance(expected_snapshot_operation_hash,str) or not HASH.fullmatch(expected_snapshot_operation_hash)):
        raise _error()
    key=(ORIGIN,prepared.tenant_id,prepared.receiving_id)
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON');db.execute('BEGIN IMMEDIATE')
            _source_cas(db,prepared,expected_sources)
            link=db.execute('SELECT * FROM receiving_recovery_links WHERE service_origin=? AND tenant_id=? AND receiving_id=?',key).fetchone()
            if (not link or type(link['revision']) is not int or link['revision']!=expected_link_revision
                or type(link['server_revision']) is not int or link['server_revision']!=prepared.server_revision
                or link['plan_hash']!=prepared.plan_hash or link['active_receipt_id']!=prepared.receipt_id
                or link['financial_receipt_id']!=prepared.financial_receipt_id):raise _error()
            if _ordinary_references(db,prepared.tenant_id,prepared.receiving_id):raise _error()
            child=db.execute('SELECT body,deleted_at FROM receipts WHERE id=?',(prepared.receipt_id,)).fetchone()
            snapshot=db.execute('''SELECT receipt_id,plan_hash,plan_snapshot,operation_snapshot,restoration_source_hash,operation_snapshot_hash
                FROM receiving_recovery_snapshots WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?''',
                key+(prepared.server_revision,)).fetchone()
            if (not child or tuple(child)!=(expected_child_raw,expected_child_deleted_at)
                or not snapshot or tuple(snapshot)!=(prepared.receipt_id,prepared.plan_hash,
                    expected_snapshot_source_body,expected_snapshot_operation_body,
                    expected_snapshot_source_hash,expected_snapshot_operation_hash)):raise _error()
            restoring=expected_child_deleted_at is not None
            if restoring and not restore_existing:raise _error()
            # Exact immutable snapshots already match: do not create another
            # revision, change local storage edits, or emit a duplicate audit.
            if not restoring and prepared.source_body==expected_snapshot_source_body and prepared.operation_body==expected_snapshot_operation_body:
                return {'receipt_id':prepared.receipt_id,'link_revision':expected_link_revision,
                        'reused':True,'restored':False,'refreshed':False,'receipt_body':expected_child_raw}
            changed=db.execute('''UPDATE receipts SET body=?,updated=?,deleted_at=NULL WHERE id=? AND body=? AND deleted_at IS ?''',
                (prepared.receipt_body,prepared.receipt_updated,prepared.receipt_id,expected_child_raw,expected_child_deleted_at))
            if changed.rowcount!=1:raise _error()
            db.execute('INSERT INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest) VALUES (?,?,?,?,?)',
                (prepared.receipt_id,prepared.receipt_updated,store.SUMMARY_VERSION,prepared.summary_body,prepared.summary_digest))
            changed=db.execute('''UPDATE receiving_recovery_snapshots SET restoration_source_hash=?,plan_snapshot=?,
                operation_snapshot=?,operation_snapshot_hash=? WHERE service_origin=? AND tenant_id=? AND receiving_id=?
                AND server_revision=? AND receipt_id=? AND plan_snapshot=? AND operation_snapshot=?''',
                (prepared.source_hash,prepared.source_body,prepared.operation_body,prepared.operation_hash)+key+
                (prepared.server_revision,prepared.receipt_id,expected_snapshot_source_body,expected_snapshot_operation_body))
            if changed.rowcount!=1:raise _error()
            local_revision=expected_link_revision+1
            changed=db.execute('''UPDATE receiving_recovery_links SET revision=? WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND revision=?''',
                (local_revision,)+key+(expected_link_revision,))
            if changed.rowcount!=1:raise _error()
            kind='original_receiving_restored' if restoring else 'original_receiving_refreshed'
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                (prepared.receipt_id,prepared.receipt_updated,kind,
                 'Verified the same original receiving stock record. No agreement, credit, payment or provider action.'))
    except sqlite3.Error:
        raise _error() from None
    return {'receipt_id':prepared.receipt_id,'link_revision':local_revision,'reused':True,
            'restored':restoring,'refreshed':not restoring,'receipt_body':prepared.receipt_body}
