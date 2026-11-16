"""Local persistence for verified, nonfinancial original-intake stock records.

Only the explicit profile feature upgrade calls initialize. This module never
imports profile/cloud/financial coordinators and never creates a provider action.
prepare performs all JSON/hash/summary work before caller guards. commit and
assert_current use only exact immutable scalars, captured bytes and SQLite CAS.
These internal dataclasses are not a hostile in-process capability boundary.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import sqlite3
import uuid

from station_store import StationError


ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
VERSIONS = 'intake_stock_projection_versions'
OBSERVATIONS = 'intake_stock_projection_observations'
LINKS = 'intake_stock_projection_links'
TABLES = frozenset((VERSIONS, OBSERVATIONS, LINKS))
VERSION_FIELDS = ('service_origin', 'tenant_id', 'receiving_id', 'server_revision',
    'receipt_id', 'physical_intake_id', 'physical_manifest_hash', 'source_snapshot_hash',
    'plan_hash', 'normalized_plan_body', 'created_at')
OBSERVATION_FIELDS = ('service_origin', 'tenant_id', 'receiving_id', 'server_revision',
    'proof_hash', 'restoration_source_hash', 'restoration_source_body', 'operation_hash',
    'operation_body', 'claim_hash', 'claim_body', 'observed_at')
LINK_FIELDS = ('service_origin', 'tenant_id', 'receiving_id', 'active_receipt_id',
    'server_revision', 'plan_hash', 'physical_intake_id', 'physical_manifest_hash',
    'proof_hash', 'revision')
FIELDS = {VERSIONS: VERSION_FIELDS, OBSERVATIONS: OBSERVATION_FIELDS, LINKS: LINK_FIELDS}
KEY_FIELDS = ('service_origin', 'tenant_id', 'receiving_id', 'server_revision')
PRIMARY = {VERSIONS: KEY_FIELDS, OBSERVATIONS: KEY_FIELDS + ('proof_hash',),
           LINKS: KEY_FIELDS[:3]}
FINANCIAL_LINK_FIELDS = ('service_origin', 'tenant_id', 'receiving_id',
    'active_receipt_id', 'server_revision', 'plan_hash', 'financial_receipt_id', 'revision')
FINANCIAL_SNAPSHOT_FIELDS = ('service_origin', 'tenant_id', 'receiving_id',
    'server_revision', 'receipt_id', 'plan_hash', 'restoration_source_hash',
    'plan_snapshot', 'operation_snapshot', 'operation_snapshot_hash')
HASH = re.compile(r'[a-f0-9]{64}')
LOCAL_ID = re.compile(r'R-[A-Za-z0-9-]{1,100}')
STORE = re.compile(r'[a-z0-9][a-z0-9-]{2,49}')
MAX_INTEGER = 2**63 - 1
MARKER = 'original_receiving_recovery'
# Collection contract v1 belongs to this feature epoch. Provenance is the
# explicit public desktop claim DTO, hosted/intake.py::_claim_result. Every
# accepted field has an identified local evidence purpose; an upstream extra
# field stops publication rather than silently expanding stored collection.
# This is a collection guard, not a retention policy or legal certification.
STORAGE_INPUT_CONTRACT_VERSION = 1
CLAIM_STORAGE_FIELD_PURPOSES = {
    'physical_intake_id': 'Correlate the original physical claim with version and operation.',
    'version': 'Preserve the exact claim observation used for coherence verification.',
    'state': 'Verify open/consumed claim semantics against the original operation.',
    'physical_reference': 'Retain the owner-confirmed physical-delivery reference.',
    'manifest_hash': 'Bind the immutable source manifest and original copy budget.',
    'semantic_hash': 'Verify normalized source identity without substituting current CSV rows.',
    'source_manifest': 'Reverify original identities, physical quantities and source lineage.',
    'bound_trade_id': 'Check claim lifecycle/isolation without using a trade as financial origin.',
    'additional_delivery_of': 'Preserve explicit additional-delivery lineage when present.',
    'created_at': 'Retain the original claim lifecycle timestamp when the DTO provides it.',
    'consumed_at': 'Retain the exact consumption lifecycle timestamp when provided.',
    'policy': 'Require the original one-operation and physical-identity policy facts.',
}
CLAIM_STORAGE_FIELDS = frozenset(CLAIM_STORAGE_FIELD_PURPOSES)
MARKER_FIELDS = frozenset(('version', 'originKind', 'serviceOrigin', 'tenantId',
    'store', 'physicalIntakeId', 'physicalManifestHash', 'sourceSnapshotHash',
    'receivingId', 'serverRevision', 'planHash', 'restorationSourceHash', 'proofHash',
    'snapshotReference', 'recoveredAt'))
_V1_MARKER_FIELDS = ('version', 'serviceOrigin', 'tenantId', 'financialReceiptId',
    'tradeId', 'receiptHash', 'physicalIntakeId', 'physicalManifestHash', 'receivingId',
    'serverRevision', 'planHash', 'restorationSourceHash', 'proofHash', 'snapshotReference', 'recoveredAt')
IDENTITY_FIELDS = {'name': 'card_name', 'game': 'game', 'game_label': 'game_label',
    'set': 'card_set', 'number': 'card_num', 'condition': 'condition', 'printing': 'printing',
    'language': 'language', 'grading_company': 'grader', 'grade': 'grade',
    'grade_qualifier': 'grade_qualifier', 'certification': 'cert_number', 'year': 'year',
    'sport': 'sport', 'league': 'league', 'team': 'team', 'player': 'player',
    'serial_number': 'serial_number', 'manufacturer': 'manufacturer'}
DDL = {
    VERSIONS: '''CREATE TABLE intake_stock_projection_versions (
        service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, receiving_id TEXT NOT NULL,
        server_revision INTEGER NOT NULL CHECK(server_revision>=0),
        receipt_id TEXT NOT NULL UNIQUE REFERENCES receipts(id),
        physical_intake_id TEXT NOT NULL, physical_manifest_hash TEXT NOT NULL,
        source_snapshot_hash TEXT NOT NULL, plan_hash TEXT NOT NULL,
        normalized_plan_body TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(service_origin,tenant_id,receiving_id,server_revision),
        UNIQUE(service_origin,tenant_id,receiving_id,server_revision,receipt_id))''',
    OBSERVATIONS: '''CREATE TABLE intake_stock_projection_observations (
        service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, receiving_id TEXT NOT NULL,
        server_revision INTEGER NOT NULL CHECK(server_revision>=0), proof_hash TEXT NOT NULL,
        restoration_source_hash TEXT NOT NULL, restoration_source_body TEXT NOT NULL,
        operation_hash TEXT NOT NULL, operation_body TEXT NOT NULL, claim_hash TEXT NOT NULL,
        claim_body TEXT NOT NULL, observed_at TEXT NOT NULL,
        PRIMARY KEY(service_origin,tenant_id,receiving_id,server_revision,proof_hash),
        FOREIGN KEY(service_origin,tenant_id,receiving_id,server_revision)
            REFERENCES intake_stock_projection_versions(service_origin,tenant_id,receiving_id,server_revision))''',
    LINKS: '''CREATE TABLE intake_stock_projection_links (
        service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, receiving_id TEXT NOT NULL,
        active_receipt_id TEXT NOT NULL REFERENCES receipts(id),
        server_revision INTEGER NOT NULL CHECK(server_revision>=0), plan_hash TEXT NOT NULL,
        physical_intake_id TEXT NOT NULL, physical_manifest_hash TEXT NOT NULL,
        proof_hash TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>=1),
        PRIMARY KEY(service_origin,tenant_id,receiving_id),
        FOREIGN KEY(service_origin,tenant_id,receiving_id,server_revision,active_receipt_id)
            REFERENCES intake_stock_projection_versions(service_origin,tenant_id,receiving_id,server_revision,receipt_id),
        FOREIGN KEY(service_origin,tenant_id,receiving_id,server_revision,proof_hash)
            REFERENCES intake_stock_projection_observations(service_origin,tenant_id,receiving_id,server_revision,proof_hash))''',
}


def _error(message='The original intake stock record changed or needs owner review. Preview its exact original plan again; saved sources and operations were kept.'):
    return StationError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(raw):
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def validate_claim_storage_fields(claim):
    """Reject collection drift; preserve known optional values without filling.

    Required core facts and their types are still checked by the unchanged pure
    verifier and prepare's correlations. This helper neither strips proof-bound
    fields nor invents missing lifecycle dates in older supported observations.
    """
    if type(claim) is not dict or not set(claim) <= CLAIM_STORAGE_FIELDS:
        raise _error('The original claim includes unsupported stored data. Keep the original source for owner review; no additional claim fields were collected.')
    try:
        if 'created_at' in claim:
            _stamp(claim['created_at'])
        if 'consumed_at' in claim and claim['consumed_at'] is not None:
            _stamp(claim['consumed_at'])
        if 'additional_delivery_of' in claim and claim['additional_delivery_of'] is not None:
            _uuid(claim['additional_delivery_of'])
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise _error('The original claim includes unsupported stored data. Keep its exact original lifecycle fields for owner review; no replacement values were invented.') from None


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError()
    return value


def _id(value):
    if type(value) is not str or not LOCAL_ID.fullmatch(value):
        raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not HASH.fullmatch(value):
        raise ValueError()
    return value


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER:
        raise ValueError()
    return value


def _stamp(value):
    if (type(value) is not str or len(value) > 60
            or datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None):
        raise ValueError()
    return value


def _row(row, fields):
    return dict(zip(fields, tuple(row))) if row is not None else None


def _tuple(row, fields):
    return tuple(row[name] for name in fields) if row is not None else None


def _family(db):
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = names & TABLES
    if present != TABLES:
        raise _error('Enable the supported original-intake stock-record feature before using this registry. Partial or missing metadata was not repaired.')


def _v1_metadata_condition():
    """Fixed SQLite route-kind predicates, not financial/provider proof.

    Field names and SQL tokens here are module constants, never caller input.
    A valid existing financial-origin marker may coexist with a first new table
    family. A version number alone cannot classify unknown stock bytes as v1.
    """
    prefix = '$.original_receiving_recovery.'
    def value(name):
        return "json_extract(body,'" + prefix + name + "')"
    def kind(name):
        return "json_type(body,'" + prefix + name + "')"
    predicates = [
        "(SELECT count(*) FROM json_each(body) WHERE key='original_receiving_recovery')=1",
        '(SELECT count(*) FROM json_each(body))=(SELECT count(DISTINCT key) FROM json_each(body))',
        "json_type(body,'$.original_receiving_recovery')='object'",
        "(SELECT count(*) FROM json_each(body,'$.original_receiving_recovery'))=15",
        "(SELECT count(DISTINCT key) FROM json_each(body,'$.original_receiving_recovery'))=15",
        "NOT EXISTS (SELECT 1 FROM json_each(body,'$.original_receiving_recovery') WHERE key NOT IN (" +
            ','.join("'" + name + "'" for name in _V1_MARKER_FIELDS) + '))',
        kind('version') + "='integer'", value('version') + '=1',
        kind('serviceOrigin') + "='text'", value('serviceOrigin') + "='" + ORIGIN + "'",
        kind('serverRevision') + "='integer'", value('serverRevision') + ' BETWEEN 0 AND 9223372036854775807',
        kind('snapshotReference') + "='text'",
        value('snapshotReference') + '=' + value('receivingId') + "||':'||" + value('serverRevision'),
        kind('recoveredAt') + "='text'", 'length(' + value('recoveredAt') + ') BETWEEN 1 AND 60',
        'length(CAST(' + value('recoveredAt') + ' AS BLOB))<=240',
    ]
    for name in ('tenantId', 'tradeId', 'physicalIntakeId', 'receivingId'):
        item = value(name)
        predicates.extend((kind(name) + "='text'", 'length(' + item + ')=36',
            'length(CAST(' + item + ' AS BLOB))=length(' + item + ')',
            item + " GLOB '????????-????-????-????-????????????'",
            item + " NOT GLOB '*[^0-9a-f-]*'", "length(replace(" + item + ",'-',''))=32"))
    for name in ('receiptHash', 'physicalManifestHash', 'planHash', 'restorationSourceHash', 'proofHash'):
        item = value(name)
        predicates.extend((kind(name) + "='text'", 'length(' + item + ')=64',
            'length(CAST(' + item + ' AS BLOB))=length(' + item + ')', item + " NOT GLOB '*[^0-9a-f]*'"))
    financial = value('financialReceiptId')
    predicates.extend((kind('financialReceiptId') + "='text'", 'length(' + financial + ') BETWEEN 3 AND 102',
        'length(CAST(' + financial + ' AS BLOB))=length(' + financial + ')',
        'substr(' + financial + ",1,2)='R-'", 'substr(' + financial + ",3) NOT GLOB '*[^A-Za-z0-9-]*'"))
    return ' AND '.join('(' + condition + ')' for condition in predicates)


def initialization_state(db):
    """Recognize a present family or genuinely safe first/resumable absence.

    The common present path reads only fixed sqlite_master entries. The missing
    path never recreates metadata around a detached new/unknown stock child.
    SQL CASE guards malformed bodies before JSON path predicates. One coherent
    cursor classifies each body and projects only <=240-byte v1 recoveredAt
    scalars for the existing date validator. No Python JSON, body hashing,
    callback, DDL or full snapshot inspection occurs.
    """
    try:
        entries = list(db.execute('''SELECT name,type FROM sqlite_master
            WHERE name COLLATE NOCASE IN ('intake_stock_projection_versions','intake_stock_projection_observations',
                'intake_stock_projection_links','receipts')'''))
        fixed = TABLES | {'receipts'}
        if (any(row[0] not in fixed or row[1] != 'table' for row in entries)
            or len(entries) != len({row[0] for row in entries})):
            raise _error('The original stock-record schema names are ambiguous. Keep all saved metadata for owner recovery; setup did not recreate it.')
        kinds = {row[0]: row[1] for row in entries}
        present = set(kinds) & TABLES
        if present:
            if present != TABLES or any(kinds[name] != 'table' for name in TABLES) or kinds.get('receipts') != 'table':
                raise _error('The original stock-record metadata is partial or detached. Keep it for owner recovery; setup did not repair it.')
            return 'present'
        if 'receipts' not in kinds:
            if db.execute("SELECT 1 FROM sqlite_master WHERE substr(name,1,7) COLLATE NOCASE<>'sqlite_' LIMIT 1").fetchone():
                raise _error('Saved database metadata remains without its receipts table. Keep it for owner recovery; setup did not infer an empty profile.')
            return 'absent'  # Explicitly observed empty user schema.
        if kinds['receipts'] != 'table':
            raise _error()
        condition = _v1_metadata_condition()
        rows = db.execute('''WITH classified AS (SELECT body,CASE
            WHEN typeof(body)<>'text' OR NOT json_valid(body) THEN -1
            WHEN json_type(body)<>'object' THEN -1
            WHEN NOT EXISTS (SELECT 1 FROM json_each(body)
                WHERE key='original_receiving_recovery') THEN 0
            ELSE CASE WHEN ''' + condition + ''' THEN 1 ELSE -1 END END AS marker_kind FROM receipts)
            SELECT marker_kind,CASE WHEN marker_kind=1
                THEN json_extract(body,'$.original_receiving_recovery.recoveredAt') ELSE NULL END
            FROM classified''')
        for marker_kind, recovered_at in rows:
            if marker_kind == -1:
                raise _error('Saved stock-record metadata exists without its complete registry. Keep all saved bytes for owner recovery; setup did not recreate the missing family.')
            if marker_kind == 1:
                # SQL established exact shape and both character/byte caps in
                # this same cursor snapshot. No full body enters Python.
                _stamp(recovered_at)
        return 'absent'
    except (ValueError, TypeError, AttributeError, sqlite3.Error, OverflowError):
        raise _error('The original stock-record setup state could not be verified. Keep the saved metadata for owner recovery; no absence or repair was inferred.') from None


def _foreign_groups(db, table):
    groups = {}
    for row in db.execute('PRAGMA foreign_key_list(' + table + ')'):
        groups.setdefault(row[0], []).append(tuple(row[1:]))
    return sorted(tuple(sorted(rows)) for rows in groups.values())


def _expected_foreign(table):
    def group(target, source, destination):
        return tuple((number, target, left, right, 'NO ACTION', 'NO ACTION', 'NONE')
                     for number, (left, right) in enumerate(zip(source, destination)))
    if table == VERSIONS:
        return [group('receipts', ('receipt_id',), ('id',))]
    if table == OBSERVATIONS:
        return [group(VERSIONS, KEY_FIELDS, KEY_FIELDS)]
    return sorted((group('receipts', ('active_receipt_id',), ('id',)),
        group(VERSIONS, KEY_FIELDS + ('active_receipt_id',), KEY_FIELDS + ('receipt_id',)),
        group(OBSERVATIONS, KEY_FIELDS + ('proof_hash',), KEY_FIELDS + ('proof_hash',))))


def _unique_indexes(db, table):
    return {tuple(row[2] for row in db.execute('PRAGMA index_info("' + index[1].replace('"', '""') + '")'))
            for index in db.execute('PRAGMA index_list(' + table + ')')
            if index[2] == 1 and index[4] == 0}


def inspect_database(db):
    """Read-only schema/reference recognition; never fresh provider authority.

    Full snapshot JSON/hashes are verified by the coordinator after DB reads.
    Recognition uses SQLite JSON validity/byte bounds and small scalar fields,
    not materialization of every historical plan/operation/claim into Python.
    """
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not names & TABLES:
            return False
        _family(db)
        for table, fields in FIELDS.items():
            declaration = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            # PRAGMA does not expose CHECK clauses or FK deferrability. This
            # epoch owns one fixed declaration; accept whitespace/case changes
            # only, and never infer that a similar existing schema is repairable.
            if (declaration is None or type(declaration[0]) is not str
                or ''.join(declaration[0].split()).casefold() != ''.join(DDL[table].split()).casefold()):
                raise _error()
            columns = list(db.execute('PRAGMA table_xinfo(' + table + ')'))
            if (tuple(row[1] for row in columns) != fields
                or any(row[2].upper() != ('INTEGER' if row[1] in ('server_revision', 'revision') else 'TEXT')
                       or row[3] != 1 or row[4] is not None or row[6] != 0 for row in columns)
                or tuple(row[1] for row in sorted(columns, key=lambda value: value[5]) if row[5]) != PRIMARY[table]
                or _foreign_groups(db, table) != _expected_foreign(table)):
                raise _error()
            indexes = _unique_indexes(db, table)
            required = {PRIMARY[table]}
            if table == VERSIONS:
                required |= {('receipt_id',), KEY_FIELDS + ('receipt_id',)}
            if not required <= indexes or db.execute('PRAGMA foreign_key_check(' + table + ')').fetchone():
                raise _error()
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=? LIMIT 1", (table,)).fetchone():
                raise _error()
            tests = [('typeof(' + name + ")<>'" + ('integer' if name in ('server_revision', 'revision') else 'text') + "'") for name in fields]
            tests += ['server_revision<0', 'server_revision>9223372036854775807']
            if table == LINKS:
                tests += ['revision<1', 'revision>9223372036854775807']
            if db.execute('SELECT 1 FROM ' + table + ' WHERE ' + ' OR '.join(tests) + ' LIMIT 1').fetchone():
                raise _error()
        bodies = ((VERSIONS, 'normalized_plan_body', 2_000_000),
                  (OBSERVATIONS, 'restoration_source_body', 2_000_000),
                  (OBSERVATIONS, 'operation_body', 8_388_608),
                  (OBSERVATIONS, 'claim_body', 1_048_576))
        for table, field, maximum in bodies:
            if db.execute('SELECT 1 FROM ' + table + ' WHERE length(CAST(' + field + ' AS BLOB))>? '
                "OR CASE WHEN json_valid(" + field + ") THEN json_type(" + field + ") ELSE 'invalid' END<>'object' LIMIT 1",
                (maximum,)).fetchone():
                raise _error()
        any_rows = False
        for table, fields in FIELDS.items():
            scalar_fields = tuple(name for name in fields if not name.endswith('_body'))
            for values in db.execute('SELECT ' + ','.join(scalar_fields) + ' FROM ' + table):
                row = dict(zip(scalar_fields, tuple(values)))
                if row['service_origin'] != ORIGIN:
                    raise _error()
                _uuid(row['tenant_id']); _uuid(row['receiving_id']); _integer(row['server_revision'])
                for name, value in row.items():
                    if name.endswith('_hash'):
                        _hash(value)
                    elif name in ('receipt_id', 'active_receipt_id'):
                        _id(value)
                    elif name == 'physical_intake_id':
                        _uuid(value)
                    elif name in ('created_at', 'observed_at'):
                        _stamp(value)
                    elif name == 'revision':
                        _integer(value, 1)
                any_rows = True
        if db.execute('''SELECT 1 FROM intake_stock_projection_links l
            JOIN intake_stock_projection_versions v ON v.service_origin=l.service_origin
                AND v.tenant_id=l.tenant_id AND v.receiving_id=l.receiving_id AND v.server_revision=l.server_revision
            WHERE v.receipt_id<>l.active_receipt_id OR v.plan_hash<>l.plan_hash
                OR v.physical_intake_id<>l.physical_intake_id
                OR v.physical_manifest_hash<>l.physical_manifest_hash LIMIT 1''').fetchone():
            raise _error()
        return any_rows
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError):
        raise _error() from None


def initialize(store):
    """Explicit epoch caller only; create complete family in one transaction."""
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            state = initialization_state(db)
            if state == 'absent':
                for table in (VERSIONS, OBSERVATIONS, LINKS):
                    db.execute(DDL[table])
            inspect_database(db)
    except sqlite3.Error:
        raise _error() from None


def _ordinary_references(db, tenant_id, receiving_id):
    # Only a fully correlated, registered exact-v2 child is excluded. All
    # tombstones, unknown/malformed markers and original ordinary routes occupy.
    return [dict(zip(('id', 'body', 'deleted_at'), tuple(row))) for row in db.execute('''
        SELECT r.id,r.body,r.deleted_at FROM receipts r
        WHERE (json_extract(r.body,'$.hosted_receive.receiving_id')=? OR EXISTS (
            SELECT 1 FROM intake_stock_projection_versions registered
            WHERE registered.receipt_id=r.id AND registered.service_origin=?
                AND registered.tenant_id=? AND registered.receiving_id=?)) AND NOT EXISTS (
            SELECT 1 FROM intake_stock_projection_versions v
            JOIN intake_stock_projection_observations o ON o.service_origin=v.service_origin
                AND o.tenant_id=v.tenant_id AND o.receiving_id=v.receiving_id AND o.server_revision=v.server_revision
            WHERE v.receipt_id=r.id AND v.service_origin=? AND v.tenant_id=? AND v.receiving_id=?
                AND json_type(r.body,'$.original_receiving_recovery.version')='integer'
                AND json_extract(r.body,'$.original_receiving_recovery.version')=2
                AND json_type(r.body)='object'
                AND (SELECT count(*) FROM json_each(r.body))=
                    (SELECT count(DISTINCT key) FROM json_each(r.body))
                AND json_type(r.body,'$.original_receiving_recovery')='object'
                AND (SELECT count(*) FROM json_each(r.body,'$.original_receiving_recovery'))=15
                AND (SELECT count(DISTINCT key) FROM json_each(r.body,'$.original_receiving_recovery'))=15
                AND NOT EXISTS (SELECT 1 FROM json_each(r.body,'$.original_receiving_recovery')
                    WHERE key NOT IN ('version','originKind','serviceOrigin','tenantId','store',
                        'physicalIntakeId','physicalManifestHash','sourceSnapshotHash','receivingId',
                        'serverRevision','planHash','restorationSourceHash','proofHash','snapshotReference','recoveredAt'))
                AND json_type(r.body,'$.original_receiving_recovery.originKind')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.originKind')='physical_intake'
                AND json_type(r.body,'$.original_receiving_recovery.serviceOrigin')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.serviceOrigin')=v.service_origin
                AND json_type(r.body,'$.original_receiving_recovery.tenantId')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.tenantId')=v.tenant_id
                AND json_type(r.body,'$.original_receiving_recovery.receivingId')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.receivingId')=v.receiving_id
                AND json_type(r.body,'$.original_receiving_recovery.serverRevision')='integer'
                AND json_extract(r.body,'$.original_receiving_recovery.serverRevision')=v.server_revision
                AND json_type(r.body,'$.original_receiving_recovery.planHash')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.planHash')=v.plan_hash
                AND json_type(r.body,'$.original_receiving_recovery.physicalIntakeId')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.physicalIntakeId')=v.physical_intake_id
                AND json_type(r.body,'$.original_receiving_recovery.physicalManifestHash')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.physicalManifestHash')=v.physical_manifest_hash
                AND json_type(r.body,'$.original_receiving_recovery.sourceSnapshotHash')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.sourceSnapshotHash')=v.source_snapshot_hash
                AND json_type(r.body,'$.original_receiving_recovery.proofHash')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.proofHash')=o.proof_hash
                AND json_type(r.body,'$.original_receiving_recovery.restorationSourceHash')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.restorationSourceHash')=o.restoration_source_hash
                AND json_type(r.body,'$.original_receiving_recovery.store')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.store')=json_extract(r.body,'$.hosted_receive.store')
                AND length(json_extract(r.body,'$.original_receiving_recovery.store')) BETWEEN 3 AND 50
                AND length(CAST(json_extract(r.body,'$.original_receiving_recovery.store') AS BLOB))=
                    length(json_extract(r.body,'$.original_receiving_recovery.store'))
                AND substr(json_extract(r.body,'$.original_receiving_recovery.store'),1,1) GLOB '[a-z0-9]'
                AND json_extract(r.body,'$.original_receiving_recovery.store') NOT GLOB '*[^a-z0-9-]*'
                AND json_type(r.body,'$.original_receiving_recovery.recoveredAt')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.recoveredAt')=v.created_at
                AND json_type(r.body,'$.id')='text' AND json_extract(r.body,'$.id')=r.id
                AND json_type(r.body,'$.created')='text' AND json_extract(r.body,'$.created')=v.created_at
                AND json_type(r.body,'$.updated')='text' AND json_extract(r.body,'$.updated')=r.updated
                AND typeof(r.pickup_pending)='integer' AND r.pickup_pending=0
                AND typeof(r.deleted_at) IN ('null','text')
                AND (json_type(r.body,'$.deleted_at') IS NULL OR json_type(r.body,'$.deleted_at') IN ('null','text'))
                AND json_extract(r.body,'$.deleted_at') IS r.deleted_at
                AND json_type(r.body,'$.purchase')='null'
                AND json_type(r.body,'$.requires_purchase')='false'
                AND json_type(r.body,'$.pickup_pending')='false'
                AND json_type(r.body,'$.prepared')='false'
                AND json_type(r.body,'$.inventory_intent')='null'
                AND json_type(r.body,'$.files')='object'
                AND NOT EXISTS (SELECT 1 FROM json_each(r.body,'$.files'))
                AND json_extract(r.body,'$.mode')='labels'
                AND json_type(r.body,'$.shared_trade') IS NULL
                AND json_type(r.body,'$.tracked_credit') IS NULL
                AND json_type(r.body,'$.original_trade_recovery') IS NULL
                AND json_type(r.body,'$.financialReceiptId') IS NULL
                AND json_type(r.body,'$.tradeId') IS NULL
                AND json_type(r.body,'$.receiptHash') IS NULL
                AND json_type(r.body,'$.acceptedLineId') IS NULL
                AND json_type(r.body,'$.customer') IS NULL
                AND json_type(r.body,'$.customer_id') IS NULL
                AND json_type(r.body,'$.customerId') IS NULL
                AND json_type(r.body,'$.customer_reference')='text'
                AND json_extract(r.body,'$.customer_reference')=''
                AND json_type(r.body,'$.hosted_receive')='object'
                AND (SELECT count(*) FROM json_each(r.body,'$.hosted_receive'))=11
                AND (SELECT count(DISTINCT key) FROM json_each(r.body,'$.hosted_receive'))=11
                AND NOT EXISTS (SELECT 1 FROM json_each(r.body,'$.hosted_receive')
                    WHERE key NOT IN ('version','restoration_projection','receiving_id','server_revision',
                        'plan_hash','store','state','server_state','label_profile','snapshot_reference','message'))
                AND json_type(r.body,'$.hosted_receive.version')='integer'
                AND json_extract(r.body,'$.hosted_receive.version')=2
                AND json_type(r.body,'$.hosted_receive.restoration_projection')='true'
                AND json_type(r.body,'$.hosted_receive.receiving_id')='text'
                AND json_extract(r.body,'$.hosted_receive.receiving_id')=v.receiving_id
                AND json_type(r.body,'$.hosted_receive.server_revision')='integer'
                AND json_extract(r.body,'$.hosted_receive.server_revision')=v.server_revision
                AND json_type(r.body,'$.hosted_receive.plan_hash')='text'
                AND json_extract(r.body,'$.hosted_receive.plan_hash')=v.plan_hash
                AND json_type(r.body,'$.hosted_receive.store')='text'
                AND json_type(r.body,'$.hosted_receive.state')='text'
                AND json_type(r.body,'$.hosted_receive.server_state')='text'
                AND json_type(r.body,'$.hosted_receive.label_profile')='object'
                AND json_type(r.body,'$.hosted_receive.message')='text'
                AND json_type(r.body,'$.hosted_receive.snapshot_reference')='text'
                AND json_extract(r.body,'$.hosted_receive.snapshot_reference')=
                    (v.receiving_id||':'||v.server_revision||':'||o.proof_hash)
                AND json_type(r.body,'$.original_receiving_recovery.snapshotReference')='text'
                AND json_extract(r.body,'$.original_receiving_recovery.snapshotReference')=
                    (v.receiving_id||':'||v.server_revision||':'||o.proof_hash)) ORDER BY r.id''',
        (receiving_id, ORIGIN, tenant_id, receiving_id, ORIGIN, tenant_id, receiving_id))]


def _financial_references(db, tenant_id, receiving_id):
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'receiving_recovery_links', 'receiving_recovery_snapshots'}
    present = names & required
    if present and present != required:
        raise _error()
    if not present:
        return [], []
    key = (ORIGIN, tenant_id, receiving_id)
    if db.execute('''SELECT 1 FROM receiving_recovery_snapshots
        WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND (
            typeof(plan_snapshot)<>'text' OR length(CAST(plan_snapshot AS BLOB))>2000000
            OR typeof(operation_snapshot)<>'text' OR length(CAST(operation_snapshot AS BLOB))>8388608)
        LIMIT 1''', key).fetchone():
        raise _error()
    links = [_row(row, FINANCIAL_LINK_FIELDS) for row in db.execute(
        'SELECT ' + ','.join(FINANCIAL_LINK_FIELDS) + ' FROM receiving_recovery_links '
        'WHERE service_origin=? AND tenant_id=? AND receiving_id=? ORDER BY server_revision', key)]
    snapshots = [_row(row, FINANCIAL_SNAPSHOT_FIELDS) for row in db.execute(
        'SELECT ' + ','.join(FINANCIAL_SNAPSHOT_FIELDS) + ' FROM receiving_recovery_snapshots '
        'WHERE service_origin=? AND tenant_id=? AND receiving_id=? ORDER BY server_revision', key)]
    return links, snapshots


def _capture(db, tenant_id, receiving_id, server_revision=None, proof_hash=None):
    key = (ORIGIN, tenant_id, receiving_id)
    link = _row(db.execute('SELECT ' + ','.join(LINK_FIELDS) + ' FROM ' + LINKS +
        ' WHERE service_origin=? AND tenant_id=? AND receiving_id=?', key).fetchone(), LINK_FIELDS)
    chosen_revision = server_revision if server_revision is not None else (link['server_revision'] if link else None)
    chosen_proof = proof_hash
    if chosen_proof is None and link and chosen_revision == link['server_revision']:
        chosen_proof = link['proof_hash']
    if chosen_revision is not None and chosen_proof is None:
        raise _error('Choose the exact saved proof when reading an original plan revision.')
    def version(revision):
        if revision is None:
            return None
        bounded = db.execute('SELECT typeof(normalized_plan_body),length(CAST(normalized_plan_body AS BLOB)) FROM ' + VERSIONS +
            ' WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?',
            key + (revision,)).fetchone()
        if bounded is None:
            return None
        if bounded[0] != 'text' or bounded[1] > 2_000_000:
            raise _error()
        return _row(db.execute('SELECT ' + ','.join(VERSION_FIELDS) + ' FROM ' + VERSIONS +
            ' WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=?',
            key + (revision,)).fetchone(), VERSION_FIELDS)
    def observation(revision, hashed):
        if revision is None or hashed is None:
            return None
        bounded = db.execute('''SELECT typeof(restoration_source_body),length(CAST(restoration_source_body AS BLOB)),
            typeof(operation_body),length(CAST(operation_body AS BLOB)),typeof(claim_body),length(CAST(claim_body AS BLOB))
            FROM intake_stock_projection_observations
            WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=? AND proof_hash=?''',
            key + (revision, hashed)).fetchone()
        if bounded is None:
            return None
        if (bounded[0] != 'text' or bounded[1] > 2_000_000 or bounded[2] != 'text'
            or bounded[3] > 8_388_608 or bounded[4] != 'text' or bounded[5] > 1_048_576):
            raise _error()
        return _row(db.execute('SELECT ' + ','.join(OBSERVATION_FIELDS) + ' FROM ' + OBSERVATIONS +
            ' WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND server_revision=? AND proof_hash=?',
            key + (revision, hashed)).fetchone(), OBSERVATION_FIELDS)
    financial_links, financial_snapshots = _financial_references(db, tenant_id, receiving_id)
    return {'link': link, 'version': version(chosen_revision),
        'observation': observation(chosen_revision, chosen_proof),
        'active_version': version(link['server_revision']) if link else None,
        'active_observation': observation(link['server_revision'], link['proof_hash']) if link else None,
        'ordinary_references': _ordinary_references(db, tenant_id, receiving_id),
        'financial_links': financial_links, 'financial_snapshots': financial_snapshots}


def read(store, tenant_id, receiving_id, *, server_revision=None, proof_hash=None):
    """Capture exact selected/active raw rows; decode/prove after DB closes."""
    try:
        _uuid(tenant_id); _uuid(receiving_id)
        if server_revision is not None:
            _integer(server_revision)
        if proof_hash is not None:
            _hash(proof_hash)
        with store.db() as db:
            db.execute('BEGIN')
            _family(db)
            return _capture(db, tenant_id, receiving_id, server_revision, proof_hash)
    except (ValueError, TypeError, sqlite3.Error):
        raise _error() from None


def read_by_receipt(store, tenant_id, receipt_id):
    try:
        _uuid(tenant_id); _id(receipt_id)
        with store.db() as db:
            db.execute('BEGIN')
            _family(db)
            bounded = db.execute('SELECT typeof(normalized_plan_body),length(CAST(normalized_plan_body AS BLOB)) FROM ' + VERSIONS +
                ' WHERE service_origin=? AND tenant_id=? AND receipt_id=?', (ORIGIN, tenant_id, receipt_id)).fetchone()
            if bounded is None or bounded[0] != 'text' or bounded[1] > 2_000_000:
                raise _error()
            version = _row(db.execute('SELECT ' + ','.join(VERSION_FIELDS) + ' FROM ' + VERSIONS +
                ' WHERE service_origin=? AND tenant_id=? AND receipt_id=?',
                (ORIGIN, tenant_id, receipt_id)).fetchone(), VERSION_FIELDS)
            if version is None:
                raise _error()
            link = _row(db.execute('SELECT ' + ','.join(LINK_FIELDS) + ' FROM ' + LINKS +
                ' WHERE service_origin=? AND tenant_id=? AND receiving_id=?',
                (ORIGIN, tenant_id, version['receiving_id'])).fetchone(), LINK_FIELDS)
            bounded = db.execute('SELECT typeof(body),length(CAST(body AS BLOB)) FROM receipts WHERE id=?',
                                 (receipt_id,)).fetchone()
            if bounded is None or bounded[0] != 'text' or bounded[1] > 8_388_608:
                raise _error()
            child = db.execute('SELECT body,deleted_at,updated,pickup_pending FROM receipts WHERE id=?',
                               (receipt_id,)).fetchone()
            if child is None:
                raise _error()
            return {'version': version, 'link': link, 'childRaw': child[0],
                    'childDeletedAt': child[1], 'childUpdated': child[2], 'pickupPending': child[3]}
    except (ValueError, TypeError, sqlite3.Error):
        raise _error() from None


@dataclass(frozen=True)
class ExpectedProjection:
    sources: tuple
    child: object
    link: object
    version: object
    observation: object
    active_version: object
    active_observation: object
    ordinary_references: tuple
    financial_links: tuple
    financial_snapshots: tuple


@dataclass(frozen=True)
class PreparedProjection:
    service_origin: str
    tenant_id: str
    store: str
    receiving_id: str
    server_revision: int
    physical_intake_id: str
    physical_manifest_hash: str
    source_snapshot_hash: str
    plan_hash: str
    proof_hash: str
    receipt_id: str
    receipt_updated: str
    receipt_revision: int
    receipt_body: str
    summary_body: str
    summary_digest: str
    normalized_plan_body: str
    version_created_at: str
    restoration_source_hash: str
    restoration_source_body: str
    operation_hash: str
    operation_body: str
    claim_hash: str
    claim_body: str
    observed_at: str
    mode: str
    event_kind: str
    event_detail: str


def _marker(receipt):
    marker = receipt.get(MARKER)
    if (type(marker) is not dict or set(marker) != MARKER_FIELDS
        or type(marker.get('version')) is not int or marker['version'] != 2
        or marker.get('originKind') != 'physical_intake' or marker.get('serviceOrigin') != ORIGIN
        or type(marker.get('store')) is not str or not STORE.fullmatch(marker['store'])):
        raise ValueError()
    for name in ('tenantId', 'physicalIntakeId', 'receivingId'):
        _uuid(marker[name])
    for name in ('physicalManifestHash', 'sourceSnapshotHash', 'planHash',
                 'restorationSourceHash', 'proofHash'):
        _hash(marker[name])
    _integer(marker['serverRevision']); _stamp(marker['recoveredAt'])
    if marker['snapshotReference'] != marker['receivingId'] + ':' + str(marker['serverRevision']) + ':' + marker['proofHash']:
        raise ValueError()
    return marker


def _same(left, right):
    return canonical(left) == canonical(right)


def _cards(receipt, proof):
    rows = proof.get('stockRows')
    cards = receipt.get('cards')
    if (type(rows) is not list or not 1 <= len(rows) <= 1000 or type(cards) is not list
        or len(cards) != len(rows)):
        raise ValueError()
    seen = set()
    for card, row in zip(cards, rows):
        if type(card) is not dict or type(row) is not dict or type(row.get('identity')) is not dict:
            raise ValueError()
        review = _uuid(row.get('reviewLineId'))
        if review in seen:
            raise ValueError()
        seen.add(review)
        _hash(row.get('sourceLineKey')); _hash(row.get('identityHash'))
        _integer(row.get('quantity'), 1); _integer(row.get('sellingCents'))
        if (row.get('acceptedLineId') is not None or row.get('unitIntakeCents') is not None
            or row.get('reviewed') is not True or type(row.get('zeroPriceConfirmed')) is not bool
            or row['sellingCents'] == 0 and row['zeroPriceConfirmed'] is not True
            or card.get('row_id') != review or card.get('source_line_key') != row['sourceLineKey']
            or type(card.get('qty')) is not int or card['qty'] != row['quantity']
            or any(card.get(field, '') != row['identity'].get(key, '') for key, field in IDENTITY_FIELDS.items())
            or type(card.get('final_price')) not in (int, float)
            or Decimal(str(card['final_price'])) * 100 != row['sellingCents']
            or type(card.get('intake_value')) not in (int, float) or card['intake_value'] != 0
            or card.get('include') is not True or card.get('reviewed') is not True
            or card.get('price_confirmed') is not True or card.get('price_missing') is not False
            or card.get('operator_price') is not True or card.get('storage_location') != ''
            or card.get('sku_override') != '' or card.get('sku') != row.get('targetSku')
            or type(card.get('sku')) is not str or not 1 <= len(card['sku']) <= 100
            or card.get('entry_source') != 'hosted_intake_receiving_projection'
            or any(name in card for name in ('hosted_sku_resolution', 'acceptedLineId',
                'accepted_line_id', 'financialReceiptId', 'tradeId', 'receiptHash'))):
            raise ValueError()


def prepare(store, receipt, proof, source, operation, claim, *, existing=None, mode='create'):
    """Prepare immutable bytes outside caller session/service/SQLite guards.

    The caller already verified coherent fresh source using the unchanged pure
    verifier. This persistence check binds that proof and its exact full rows to
    the new nonfinancial child; it neither authenticates a remote server nor
    duplicates financial/receiving normalization or imports those coordinators.
    """
    try:
        if type(receipt) is not dict or type(proof) is not dict or type(source) is not dict or type(operation) is not dict or type(claim) is not dict:
            raise ValueError()
        validate_claim_storage_fields(claim)
        if type(mode) is not str or mode not in ('create', 'refresh', 'restore', 'reuse'):
            raise ValueError()
        marker = _marker(receipt)
        receipt_id = _id(receipt.get('id'))
        updated = _stamp(receipt.get('updated')); created = _stamp(receipt.get('created'))
        receipt_revision = _integer(receipt.get('revision'), 1)
        if mode == 'create':
            if existing is not None or receipt_revision != 1 or created != marker['recoveredAt']:
                raise ValueError()
        else:
            if type(existing) is not dict or existing.get('id') != receipt_id:
                raise ValueError()
            old_marker = _marker(existing)
            old_revision = _integer(existing.get('revision'), 1)
            expected_revision = old_revision if mode == 'reuse' else old_revision + 1
            if (old_revision == MAX_INTEGER and mode != 'reuse' or receipt_revision != expected_revision
                or created != existing.get('created') or marker['recoveredAt'] != old_marker['recoveredAt']
                or any(marker[key] != old_marker[key] for key in ('tenantId', 'store', 'physicalIntakeId',
                    'physicalManifestHash', 'sourceSnapshotHash', 'receivingId', 'serverRevision', 'planHash'))):
                raise ValueError()
            if mode == 'reuse' and not _same(receipt, existing):
                raise ValueError()
        if (receipt.get('source_hash') != 'hosted-original-intake:' + marker['receivingId'] + ':' + str(marker['serverRevision'])
            or receipt.get('intake_source') != 'hosted_intake_receiving_projection'
            or 'purchase' not in receipt or receipt['purchase'] is not None
            or receipt.get('requires_purchase') is not False or receipt.get('mode') != 'labels'
            or 'inventory_intent' not in receipt or receipt['inventory_intent'] is not None
            or receipt.get('pickup_pending') is not False or receipt.get('prepared') is not False
            or type(receipt.get('files')) is not dict or receipt['files']
            or receipt.get('customer_reference') != '' or receipt.get('deleted_at') is not None
            or any(name in receipt for name in ('shared_trade', 'tracked_credit', 'original_trade_recovery',
                'financialReceiptId', 'tradeId', 'receiptHash', 'acceptedLineId', 'customer', 'customer_id', 'customerId'))):
            raise ValueError()
        scalars = {'receivingId': 'receiving_id', 'serverRevision': 'revision',
            'planHash': 'plan_hash', 'sourceSnapshotHash': 'source_snapshot_hash',
            'physicalIntakeId': 'physical_intake_id', 'physicalManifestHash': 'physical_manifest_hash'}
        plan = source.get('normalizedPlan')
        if (type(plan) is not dict or source.get('workflowKind') != 'inventory_only'
            or operation.get('workflow_kind') != 'inventory_only' or plan.get('workflow_kind') != 'inventory_only'
            or any(source.get(name) is not None for name in ('acceptedTradeId', 'acceptedReceiptHash'))
            or any(operation.get(name) is not None or plan.get(name) is not None
                   for name in ('accepted_trade_id', 'accepted_receipt_hash'))
            or any(source.get(name) != marker[name] or operation.get(remote) != marker[name]
                   for name, remote in scalars.items())
            or any(proof.get(name) != marker[name] for name in ('receivingId', 'serverRevision',
                'planHash', 'sourceSnapshotHash', 'restorationSourceHash', 'proofHash'))
            or type(operation.get('revision')) is not int or type(source.get('serverRevision')) is not int
            or proof.get('workflowKind') != 'inventory_only' or type(proof.get('received')) is not bool
            or proof.get('state') != operation.get('state')
            or not _same(proof.get('provider'), operation.get('provider'))
            or not _same(proof.get('labelProfile'), plan.get('client_label_profile'))
            or not _same(proof.get('admission'), source.get('admission'))
            or proof.get('stateBasisHash') != source.get('stateBasisHash')
            or claim.get('physical_intake_id') != marker['physicalIntakeId']
            or claim.get('manifest_hash') != marker['physicalManifestHash']
            or not _same(receipt.get('source_manifest'), claim.get('source_manifest'))
            or not _same(receipt.get('hosted_label_profile'), proof.get('labelProfile'))
            or receipt.get('inventory_status') != ('received' if proof['received'] else 'not_received')):
            raise ValueError()
        for name, remote in scalars.items():
            if name not in ('receivingId', 'serverRevision', 'planHash') and plan.get(remote) != marker[name]:
                raise ValueError()
        normalized = canonical(plan)
        source_body = canonical(source); operation_body = canonical(operation); claim_body = canonical(claim)
        if (digest(normalized) != marker['planHash']
            or digest(canonical({'domain': 'stock-and-sleeve:receiving-restoration-source:v1',
                'source': {key: value for key, value in source.items() if key != 'restorationSourceHash'}})) != marker['restorationSourceHash']
            or digest(canonical({'domain': 'stock-and-sleeve:receiving-restoration-state:v1',
                'operation': operation, 'admission': source.get('admission')})) != source.get('stateBasisHash')
            or digest(canonical({'domain': 'stock-and-sleeve:receiving-restoration-proof:v1',
                'proof': {key: value for key, value in proof.items() if key != 'proofHash'}})) != marker['proofHash']):
            raise ValueError()
        descriptive = {'state': 'confirmed', 'store': marker['store'],
            'source_manifest_hash': marker['physicalManifestHash'], 'physical_intake_id': marker['physicalIntakeId'],
            'manifest_hash': marker['physicalManifestHash'], 'semantic_hash': claim.get('semantic_hash'),
            'server_state': claim.get('state'), 'server_version': claim.get('version'),
            'physical_reference': claim.get('physical_reference'), 'bound_trade_id': claim.get('bound_trade_id')}
        if not _same(receipt.get('intake_claim'), descriptive):
            raise ValueError()
        hosted = receipt.get('hosted_receive')
        hosted_fields = {'version', 'restoration_projection', 'receiving_id', 'server_revision', 'plan_hash',
            'store', 'state', 'server_state', 'label_profile', 'snapshot_reference', 'message'}
        state = ('confirmed' if proof['received'] else 'cancelled_before_write' if proof['state'] == 'cancelled_before_write'
                 else 'planned' if proof['state'] == 'ready' else 'needs_attention')
        if (type(hosted) is not dict or set(hosted) != hosted_fields or type(hosted.get('version')) is not int
            or hosted['version'] != 2 or hosted.get('restoration_projection') is not True
            or hosted.get('receiving_id') != marker['receivingId']
            or type(hosted.get('server_revision')) is not int or hosted['server_revision'] != marker['serverRevision']
            or hosted.get('plan_hash') != marker['planHash'] or hosted.get('store') != marker['store']
            or hosted.get('state') != state or hosted.get('server_state') != proof['state']
            or not _same(hosted.get('label_profile'), proof['labelProfile'])
            or hosted.get('snapshot_reference') != marker['snapshotReference']
            or type(hosted.get('message')) is not str or len(hosted['message']) > 1000):
            raise ValueError()
        _cards(receipt, proof)
        body = canonical(receipt); summary = canonical(store._make_summary(receipt))
        for raw, maximum in ((body, 8_388_608), (normalized, 2_000_000),
                             (source_body, 2_000_000), (operation_body, 8_388_608), (claim_body, 1_048_576)):
            if len(raw.encode('utf-8')) > maximum:
                raise ValueError()
        kind = {'create': 'original_intake_stock_projected', 'refresh': 'original_intake_stock_refreshed',
                'restore': 'original_intake_stock_restored', 'reuse': 'original_intake_stock_reused'}[mode]
        detail = 'Verified the original physical-intake stock plan. No agreement, credit, payment or provider action.'
        return PreparedProjection(ORIGIN, marker['tenantId'], marker['store'], marker['receivingId'],
            marker['serverRevision'], marker['physicalIntakeId'], marker['physicalManifestHash'],
            marker['sourceSnapshotHash'], marker['planHash'], marker['proofHash'], receipt_id,
            updated, receipt_revision, body, summary, digest(summary), normalized, created,
            marker['restorationSourceHash'], source_body, digest(operation_body), operation_body,
            digest(claim_body), claim_body, updated, mode, kind, detail)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, InvalidOperation):
        raise _error() from None


def _expected_shape(expected):
    if type(expected) is not ExpectedProjection or type(expected.sources) is not tuple:
        raise _error()
    seen = set()
    for row in expected.sources:
        if (type(row) is not tuple or len(row) != 3 or type(row[1]) is not str
            or row[2] is not None and type(row[2]) is not str):
            raise _error()
        _id(row[0])
        if row[0] in seen:
            raise _error()
        seen.add(row[0])
    for row, size in ((expected.child, 5), (expected.link, len(LINK_FIELDS)),
        (expected.version, len(VERSION_FIELDS)), (expected.observation, len(OBSERVATION_FIELDS)),
        (expected.active_version, len(VERSION_FIELDS)), (expected.active_observation, len(OBSERVATION_FIELDS))):
        if row is not None and (type(row) is not tuple or len(row) != size):
            raise _error()
    if expected.child is not None:
        ident, raw, deleted, updated, pending = expected.child
        _id(ident)
        if (type(raw) is not str or deleted is not None and type(deleted) is not str
            or type(updated) is not str or type(pending) is not int or pending not in (0, 1)):
            raise _error()
    for rows, fields in ((expected.ordinary_references, ('id', 'body', 'deleted_at')),
        (expected.financial_links, FINANCIAL_LINK_FIELDS), (expected.financial_snapshots, FINANCIAL_SNAPSHOT_FIELDS)):
        if type(rows) is not tuple or any(type(row) is not tuple or len(row) != len(fields) for row in rows):
            raise _error()


def assert_current(db, expected, tenant_id, receiving_id, server_revision, proof_hash):
    """Read-only raw/scalar recheck shared by final views and atomic commits."""
    try:
        _expected_shape(expected); _uuid(tenant_id); _uuid(receiving_id)
        _integer(server_revision); _hash(proof_hash)
        _family(db)
        for ident, raw, deleted in expected.sources:
            current = db.execute('SELECT body,deleted_at FROM receipts WHERE id=?', (ident,)).fetchone()
            if current is None or tuple(current) != (raw, deleted):
                raise _error()
        if expected.child is not None:
            current = db.execute('SELECT id,body,deleted_at,updated,pickup_pending FROM receipts WHERE id=?',
                                 (expected.child[0],)).fetchone()
            if current is None or tuple(current) != expected.child:
                raise _error()
        current = _capture(db, tenant_id, receiving_id, server_revision, proof_hash)
        for name, fields in (('link', LINK_FIELDS), ('version', VERSION_FIELDS),
            ('observation', OBSERVATION_FIELDS), ('active_version', VERSION_FIELDS),
            ('active_observation', OBSERVATION_FIELDS)):
            if _tuple(current[name], fields) != getattr(expected, name):
                raise _error()
        for name, fields in (('ordinary_references', ('id', 'body', 'deleted_at')),
            ('financial_links', FINANCIAL_LINK_FIELDS), ('financial_snapshots', FINANCIAL_SNAPSHOT_FIELDS)):
            if tuple(_tuple(row, fields) for row in current[name]) != getattr(expected, name):
                raise _error()
        return current
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        raise _error() from None


def _prepared_shape(value):
    if type(value) is not PreparedProjection or value.service_origin != ORIGIN:
        raise _error()
    _uuid(value.tenant_id); _uuid(value.receiving_id); _uuid(value.physical_intake_id)
    _id(value.receipt_id); _integer(value.server_revision); _integer(value.receipt_revision, 1)
    if type(value.store) is not str or not STORE.fullmatch(value.store) or value.mode not in ('create', 'refresh', 'restore', 'reuse'):
        raise _error()
    for name in ('physical_manifest_hash', 'source_snapshot_hash', 'plan_hash', 'proof_hash',
                 'summary_digest', 'restoration_source_hash', 'operation_hash', 'claim_hash'):
        _hash(getattr(value, name))
    for name in ('receipt_updated', 'version_created_at', 'observed_at', 'receipt_body', 'summary_body',
                 'normalized_plan_body', 'restoration_source_body', 'operation_body', 'claim_body', 'event_kind', 'event_detail'):
        if type(getattr(value, name)) is not str:
            raise _error()


def _version_tuple(prepared):
    return (ORIGIN, prepared.tenant_id, prepared.receiving_id, prepared.server_revision,
        prepared.receipt_id, prepared.physical_intake_id, prepared.physical_manifest_hash,
        prepared.source_snapshot_hash, prepared.plan_hash, prepared.normalized_plan_body, prepared.version_created_at)


def _observation_tuple(prepared):
    return (ORIGIN, prepared.tenant_id, prepared.receiving_id, prepared.server_revision,
        prepared.proof_hash, prepared.restoration_source_hash, prepared.restoration_source_body,
        prepared.operation_hash, prepared.operation_body, prepared.claim_hash, prepared.claim_body, prepared.observed_at)


def commit(store, prepared, expected, *, restore_existing=False):
    """Exact one-transaction publication under caller's short local guards.

    No Python JSON, full hash, encryption, network, caller callback or save_receipt
    call occurs here. Fixed SQLite JSON scalar predicates identify references and
    receipt revisions. A mismatch aborts; a competing winner must be reread/reproved by
    the coordinator outside guards instead of silently accepting changed facts.
    """
    try:
        _prepared_shape(prepared); _expected_shape(expected)
        if type(restore_existing) is not bool or restore_existing != (prepared.mode == 'restore'):
            raise _error()
        key = (ORIGIN, prepared.tenant_id, prepared.receiving_id)
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
            current = assert_current(db, expected, prepared.tenant_id, prepared.receiving_id,
                                     prepared.server_revision, prepared.proof_hash)
            if current['ordinary_references'] or current['financial_links'] or current['financial_snapshots']:
                raise _error('A local record already references this original receiving operation. Open or review that record; no parallel stock record was created.')
            version = _version_tuple(prepared); observation = _observation_tuple(prepared)
            existing_version = _tuple(current['version'], VERSION_FIELDS)
            existing_observation = _tuple(current['observation'], OBSERVATION_FIELDS)
            link = current['link']
            if existing_version is not None and existing_version != version:
                raise _error()
            # A proof identifies immutable source/operation/claim bytes. Only the
            # first observed_at is retained when the same proof is seen again.
            if existing_observation is not None and existing_observation[:-1] != observation[:-1]:
                raise _error()
            if prepared.mode == 'create':
                if (expected.child is not None or existing_version is not None or existing_observation is not None
                    or not expected.sources or prepared.receipt_id in {row[0] for row in expected.sources}
                    or link is not None and (link['server_revision'] >= prepared.server_revision
                        or link['physical_intake_id'] != prepared.physical_intake_id
                        or link['physical_manifest_hash'] != prepared.physical_manifest_hash)):
                    raise _error()
                if db.execute('SELECT 1 FROM receipts WHERE id=?', (prepared.receipt_id,)).fetchone():
                    raise _error()
                db.execute('INSERT INTO receipts(id,source_hash,updated,body,deleted_at,pickup_pending) VALUES (?,?,?,?,NULL,0)',
                    (prepared.receipt_id, 'hosted-original-intake:' + prepared.receiving_id + ':' + str(prepared.server_revision),
                     prepared.receipt_updated, prepared.receipt_body))
                db.execute('INSERT INTO ' + VERSIONS + ' (' + ','.join(VERSION_FIELDS) + ') VALUES (' + ','.join('?' for _ in VERSION_FIELDS) + ')', version)
            else:
                if (expected.child is None or expected.child[0] != prepared.receipt_id
                    or existing_version is None or link is None
                    or link['active_receipt_id'] != prepared.receipt_id or link['server_revision'] != prepared.server_revision
                    or link['plan_hash'] != prepared.plan_hash
                    or link['physical_intake_id'] != prepared.physical_intake_id
                    or link['physical_manifest_hash'] != prepared.physical_manifest_hash):
                    raise _error()
                restoring = expected.child[2] is not None
                if restoring != restore_existing:
                    raise _error()
                old_revision = db.execute("SELECT json_type(body,'$.revision'),json_extract(body,'$.revision') FROM receipts WHERE id=?",
                                          (prepared.receipt_id,)).fetchone()
                if (old_revision is None or old_revision[0] != 'integer' or type(old_revision[1]) is not int
                    or prepared.receipt_revision != old_revision[1] + (0 if prepared.mode == 'reuse' else 1)):
                    raise _error()
                if prepared.mode == 'reuse':
                    if (restoring or existing_observation is None or link['proof_hash'] != prepared.proof_hash
                        or expected.child[1] != prepared.receipt_body or expected.child[3] != prepared.receipt_updated
                        or expected.child[4] != 0):
                        raise _error()
                    return {'receipt_id': prepared.receipt_id, 'link_revision': link['revision'],
                            'reused': True, 'restored': False, 'refreshed': False, 'receipt_body': expected.child[1]}
                if prepared.mode == 'refresh' and (restoring or link['proof_hash'] == prepared.proof_hash):
                    raise _error()
                changed = db.execute('''UPDATE receipts SET body=?,updated=?,deleted_at=NULL,pickup_pending=0
                    WHERE id=? AND body=? AND deleted_at IS ? AND updated=? AND pickup_pending=?''',
                    (prepared.receipt_body, prepared.receipt_updated, prepared.receipt_id,
                     expected.child[1], expected.child[2], expected.child[3], expected.child[4]))
                if changed.rowcount != 1:
                    raise _error()
            if existing_observation is None:
                db.execute('INSERT INTO ' + OBSERVATIONS + ' (' + ','.join(OBSERVATION_FIELDS) + ') VALUES (' + ','.join('?' for _ in OBSERVATION_FIELDS) + ')', observation)
            local_revision = 1 if link is None else _integer(link['revision'], 1) + 1
            if local_revision > MAX_INTEGER:
                raise _error()
            link_values = key + (prepared.receipt_id, prepared.server_revision, prepared.plan_hash,
                prepared.physical_intake_id, prepared.physical_manifest_hash, prepared.proof_hash, local_revision)
            if link is None:
                db.execute('INSERT INTO ' + LINKS + ' (' + ','.join(LINK_FIELDS) + ') VALUES (' + ','.join('?' for _ in LINK_FIELDS) + ')', link_values)
            else:
                changed = db.execute('''UPDATE intake_stock_projection_links SET active_receipt_id=?,server_revision=?,
                    plan_hash=?,physical_intake_id=?,physical_manifest_hash=?,proof_hash=?,revision=?
                    WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND revision=?''',
                    link_values[3:] + key + (link['revision'],))
                if changed.rowcount != 1:
                    raise _error()
            db.execute('''INSERT INTO receipt_summaries(receipt_id,receipt_updated,format_version,body,digest)
                VALUES (?,?,?,?,?) ON CONFLICT(receipt_id) DO UPDATE SET receipt_updated=excluded.receipt_updated,
                format_version=excluded.format_version,body=excluded.body,digest=excluded.digest''',
                (prepared.receipt_id, prepared.receipt_updated, store.SUMMARY_VERSION, prepared.summary_body, prepared.summary_digest))
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                (prepared.receipt_id, prepared.observed_at, prepared.event_kind, prepared.event_detail))
            return {'receipt_id': prepared.receipt_id, 'link_revision': local_revision,
                'reused': prepared.mode != 'create', 'restored': prepared.mode == 'restore',
                'refreshed': prepared.mode == 'refresh', 'receipt_body': prepared.receipt_body}
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError):
        raise _error() from None
