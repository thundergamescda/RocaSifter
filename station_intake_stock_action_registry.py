"""Local inventory-only action intents; inert until explicit epoch activation.

Preparation owns codec/JSON/hash work. Publication owns only bounded scalars,
captured tuples, B1 CAS and this table/audit transaction. Full startup inspection
is deliberately separate from the thin final guard. No profile/cloud imports.
"""
from dataclasses import dataclass
from datetime import datetime
import json
import math
import re
import sqlite3
import time
import unicodedata
import uuid

import station_intake_receiving_registry as projections
from station_intake_stock_action_codec import canonical, request_fingerprint, validate_intent
from station_store import StationError


ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
TABLE = 'intake_stock_action_intents'
TABLES = frozenset((TABLE,))
ACTIVE_INDEX = TABLE + '_active'
CHILD_INDEX = TABLE + '_child'
SENTINEL_KEY = 'intake_stock_operations_initialized_v1'
SENTINEL_VALUE = '{"version":1}'
FIELDS = ('service_origin', 'tenant_id', 'intent_id', 'receipt_id', 'receiving_id',
    'server_revision', 'plan_hash', 'action_kind', 'request_key', 'intent_body',
    'request_fingerprint', 'state', 'result_fingerprint', 'applied_receipt_revision',
    'applied_proof_hash', 'revision', 'created_at', 'updated_at')
UPDATE_FIELDS = ('state', 'result_fingerprint', 'applied_receipt_revision',
                 'applied_proof_hash', 'revision', 'updated_at')
MAX_INTEGER = 2**63 - 1
ACTIONS = frozenset(('commit', 'recover', 'authorize', 'cancel'))
STATES = frozenset(('pending', 'applied', 'acknowledged'))
HEX = re.compile('[a-f0-9]{64}')
LOCAL = re.compile('R-[A-Za-z0-9-]{1,100}')
STORE = re.compile('[a-z0-9][a-z0-9-]{2,49}')
AUDITS = frozenset(('original_intake_stock_action_reserved', 'original_intake_stock_action_applied',
    'original_intake_stock_action_reaffirmed', 'original_intake_stock_action_acknowledged'))
DDL = {
    TABLE: '''CREATE TABLE intake_stock_action_intents (
        service_origin TEXT NOT NULL, tenant_id TEXT NOT NULL, intent_id TEXT NOT NULL,
        receipt_id TEXT NOT NULL REFERENCES receipts(id), receiving_id TEXT NOT NULL,
        server_revision INTEGER NOT NULL CHECK(typeof(server_revision)='integer' AND server_revision BETWEEN 0 AND 9223372036854775807),
        plan_hash TEXT NOT NULL, action_kind TEXT NOT NULL CHECK(action_kind IN ('commit','recover','authorize','cancel')),
        request_key TEXT NOT NULL, intent_body TEXT NOT NULL CHECK(typeof(intent_body)='text' AND length(CAST(intent_body AS BLOB)) BETWEEN 1 AND 65536),
        request_fingerprint TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','applied','acknowledged')),
        result_fingerprint TEXT CHECK(result_fingerprint IS NULL OR typeof(result_fingerprint)='text'),
        applied_receipt_revision INTEGER CHECK(applied_receipt_revision IS NULL OR (typeof(applied_receipt_revision)='integer' AND applied_receipt_revision BETWEEN 1 AND 9223372036854775807)),
        applied_proof_hash TEXT CHECK(applied_proof_hash IS NULL OR typeof(applied_proof_hash)='text'),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision BETWEEN 1 AND 9223372036854775807),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY(service_origin,tenant_id,intent_id),
        UNIQUE(service_origin,tenant_id,request_key),
        FOREIGN KEY(service_origin,tenant_id,receiving_id,server_revision,receipt_id)
            REFERENCES intake_stock_projection_versions(service_origin,tenant_id,receiving_id,server_revision,receipt_id),
        FOREIGN KEY(service_origin,tenant_id,receiving_id,server_revision,applied_proof_hash)
            REFERENCES intake_stock_projection_observations(service_origin,tenant_id,receiving_id,server_revision,proof_hash),
        CHECK((state='pending' AND revision=1 AND result_fingerprint IS NULL AND applied_receipt_revision IS NULL AND applied_proof_hash IS NULL)
           OR (state='applied' AND revision>=2 AND result_fingerprint IS NOT NULL AND applied_receipt_revision IS NOT NULL AND applied_proof_hash IS NOT NULL)
           OR (state='acknowledged' AND revision>=3 AND result_fingerprint IS NOT NULL AND applied_receipt_revision IS NOT NULL AND applied_proof_hash IS NOT NULL)))''',
    ACTIVE_INDEX: "CREATE UNIQUE INDEX intake_stock_action_intents_active ON intake_stock_action_intents(service_origin,tenant_id,receiving_id) WHERE state IN ('pending','applied')",
    CHILD_INDEX: 'CREATE INDEX intake_stock_action_intents_child ON intake_stock_action_intents(service_origin,tenant_id,receipt_id,created_at,intent_id)',
}


def _error():
    return StationError('The original stock action needs owner review. Keep its saved intent and request key; no replacement or repair was performed.')


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value: raise ValueError()
    return value


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= MAX_INTEGER: raise ValueError()
    return value


def _hash(value):
    if type(value) is not str or not HEX.fullmatch(value): raise ValueError()
    return value


def _stamp(value):
    if (type(value) is not str or not 1 <= len(value) <= 60
        or any(unicodedata.category(character) in ('Cc', 'Cs', 'Cf') for character in value)
        or len(value.encode('utf-8')) > 240): raise ValueError()
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError()
    return value


def _session(value, tenant_id):
    if (type(value) is not tuple or len(value) != 5 or type(value[0]) is not str
        or not re.fullmatch('[a-f0-9]{32}', value[0]) or _uuid(value[1]) != tenant_id
        or type(value[2]) is not str or not STORE.fullmatch(value[2])
        or value[3] not in ('owner', 'manager') or type(value[3]) is not str
        or type(value[4]) not in (int, float) or not math.isfinite(value[4]) or value[4] <= time.time()):
        raise ValueError()
    return value


def _mapping(row):
    return dict(zip(FIELDS, row)) if row is not None else None


def _tuple(value):
    return tuple(value[field] for field in FIELDS) if value is not None else None


def initialization_state(db):
    """Fixed schema names and exact bounded sentinel only; no intent decoding."""
    try:
        fixed = {'settings': ('table', 'settings'), TABLE: ('table', TABLE),
                 ACTIVE_INDEX: ('index', TABLE), CHILD_INDEX: ('index', TABLE)}
        rows = list(db.execute('SELECT name,type,tbl_name FROM sqlite_master WHERE name COLLATE NOCASE IN (?,?,?,?)', tuple(fixed)))
        if (len(rows) != len({row[0] for row in rows})
            or any(row[0] not in fixed or tuple(row[1:]) != fixed[row[0]] for row in rows)):
            raise _error()
        names = {row[0] for row in rows}
        owned = names - {'settings'}
        if 'settings' not in names:
            if owned or db.execute("SELECT 1 FROM sqlite_master WHERE substr(name,1,7) COLLATE NOCASE<>'sqlite_' LIMIT 1").fetchone():
                raise _error()
            return 'absent'
        # Return only tiny integer comparison flags. Neither a malicious key
        # nor a large/falsey value is materialized as a Python string.
        sentinel = list(db.execute('''SELECT
            CASE WHEN typeof(key)='text' AND length(CAST(key AS BLOB))=? AND key COLLATE BINARY=? THEN 1 ELSE 0 END,
            CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))=? AND value COLLATE BINARY=? THEN 1 ELSE 0 END
            FROM settings WHERE substr(key,1,?) COLLATE NOCASE=? LIMIT 2''',
            (len(SENTINEL_KEY), SENTINEL_KEY, len(SENTINEL_VALUE), SENTINEL_VALUE, len(SENTINEL_KEY), SENTINEL_KEY)))
        if not owned and not sentinel: return 'absent'
        if owned != {TABLE, ACTIVE_INDEX, CHILD_INDEX} or len(sentinel) != 1 or tuple(sentinel[0]) != (1, 1):
            raise _error()
        return 'present'
    except StationError:
        raise
    except (ValueError, TypeError, AttributeError, sqlite3.Error):
        raise _error() from None


_TEXT_LIMITS = {'service_origin': 256, 'tenant_id': 36, 'intent_id': 36, 'receipt_id': 102,
    'receiving_id': 36, 'plan_hash': 64, 'action_kind': 9, 'request_key': 37,
    'intent_body': 65536, 'request_fingerprint': 64, 'state': 12,
    'result_fingerprint': 64, 'applied_proof_hash': 64, 'created_at': 240, 'updated_at': 240}
_NULLABLE = frozenset(('result_fingerprint', 'applied_receipt_revision', 'applied_proof_hash'))
_BOUNDS = {}
for _field in FIELDS:
    if _field in _TEXT_LIMITS:
        _condition = "typeof(" + _field + ")='text' AND length(CAST(" + _field + ' AS BLOB)) BETWEEN 1 AND ' + str(_TEXT_LIMITS[_field])
    else:
        _condition = "typeof(" + _field + ")='integer' AND " + _field + ' BETWEEN ' + ('0' if _field == 'server_revision' else '1') + ' AND 9223372036854775807'
    _BOUNDS[_field] = '(' + _condition + ')' if _field not in _NULLABLE else '(' + _field + ' IS NULL OR (' + _condition + '))'
_SELECT = ','.join('CASE WHEN ' + _BOUNDS[field] + ' THEN ' + field + ' ELSE NULL END' for field in FIELDS)
_PARENT_CHECK = '''EXISTS (SELECT 1 FROM receipts r WHERE r.id=intake_stock_action_intents.receipt_id)
    AND EXISTS (SELECT 1 FROM intake_stock_projection_versions v
        WHERE v.service_origin=intake_stock_action_intents.service_origin
          AND v.tenant_id=intake_stock_action_intents.tenant_id AND v.receiving_id=intake_stock_action_intents.receiving_id
          AND v.server_revision=intake_stock_action_intents.server_revision AND v.receipt_id=intake_stock_action_intents.receipt_id)
    AND (applied_proof_hash IS NULL OR EXISTS (SELECT 1 FROM intake_stock_projection_observations o
        WHERE o.service_origin=intake_stock_action_intents.service_origin
          AND o.tenant_id=intake_stock_action_intents.tenant_id AND o.receiving_id=intake_stock_action_intents.receiving_id
          AND o.server_revision=intake_stock_action_intents.server_revision AND o.proof_hash=intake_stock_action_intents.applied_proof_hash))'''
_SELECT += ',CASE WHEN ' + ' AND '.join(_BOUNDS.values()) + ' AND ' + _PARENT_CHECK + ' THEN 1 ELSE 0 END'


def _shape(row):
    if type(row) is not tuple or len(row) != len(FIELDS): raise ValueError()
    value = _mapping(row)
    if value['service_origin'] != ORIGIN: raise ValueError()
    for field in ('tenant_id', 'intent_id', 'receiving_id'): _uuid(value[field])
    if type(value['receipt_id']) is not str or not LOCAL.fullmatch(value['receipt_id']): raise ValueError()
    _integer(value['server_revision']); _integer(value['revision'], 1)
    for field in ('plan_hash', 'request_fingerprint'): _hash(value[field])
    if value['action_kind'] not in ACTIONS or value['state'] not in STATES: raise ValueError()
    if value['request_key'] != 'iss1_' + uuid.UUID(value['intent_id']).hex: raise ValueError()
    if type(value['intent_body']) is not str or not 1 <= len(value['intent_body']) <= 65536: raise ValueError()
    for field in ('created_at', 'updated_at'):
        if type(value[field]) is not str or not 1 <= len(value[field]) <= 60: raise ValueError()
    if value['state'] == 'pending':
        if value['revision'] != 1 or any(value[field] is not None for field in _NULLABLE): raise ValueError()
    else:
        if value['revision'] < (3 if value['state'] == 'acknowledged' else 2): raise ValueError()
        _integer(value['applied_receipt_revision'], 1)
        _hash(value['result_fingerprint']); _hash(value['applied_proof_hash'])
    return value


def _decoded(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError()
            result[key] = value
        return result
    def invalid(value): raise ValueError()
    def finite(value):
        result = float(value)
        if not math.isfinite(result): raise ValueError()
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)


def _full_row(row):
    value = _shape(row)
    if len(value['intent_body'].encode('utf-8')) > 65536: raise ValueError()
    intent = _decoded(value['intent_body']); validate_intent(intent)
    if canonical(intent) != value['intent_body'] or request_fingerprint(intent) != value['request_fingerprint']:
        raise ValueError()
    bindings = {'service_origin': 'serviceOrigin', 'tenant_id': 'tenantId', 'intent_id': 'intentId',
        'receipt_id': 'stockReceiptId', 'receiving_id': 'receivingId', 'server_revision': 'serverRevision',
        'plan_hash': 'planHash', 'action_kind': 'kind', 'request_key': 'requestKey'}
    if any(value[field] != intent[key] for field, key in bindings.items()): raise ValueError()
    _stamp(value['created_at']); _stamp(value['updated_at'])
    return value, intent


def _rows(db, where='', parameters=()):
    for raw in db.execute('SELECT ' + _SELECT + ' FROM ' + TABLE + where, parameters):
        if raw[-1] != 1: raise _error()
        row = tuple(raw[:-1]); _shape(row)
        yield row


def _pair(db, tenant_id, receiving_id, intent_id):
    if initialization_state(db) != 'present' or projections.initialization_state(db) != 'present': raise _error()
    active = list(_rows(db, " WHERE service_origin=? AND tenant_id=? AND receiving_id=? AND state IN ('pending','applied') LIMIT 2",
                   (ORIGIN, tenant_id, receiving_id)))
    if len(active) > 1: raise _error()
    selected = active if intent_id is None else list(_rows(db,
        ' WHERE service_origin=? AND tenant_id=? AND intent_id=? LIMIT 2', (ORIGIN, tenant_id, intent_id)))
    if len(selected) > 1 or selected and _mapping(selected[0])['receiving_id'] != receiving_id: raise _error()
    return {'active': active[0] if active else None, 'intent': selected[0] if selected else None}


def read_pair(store, tenant_id, receiving_id, *, intent_id=None):
    try:
        _uuid(tenant_id); _uuid(receiving_id)
        if intent_id is not None: _uuid(intent_id)
        with store.db() as db: captured = _pair(db, tenant_id, receiving_id, intent_id)
        for row in captured.values():
            if row is not None: _full_row(row)
        return {key: _mapping(row) for key, row in captured.items()}
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError, RecursionError):
        raise _error() from None


def _foreign_groups(db):
    groups = {}
    for row in db.execute('PRAGMA foreign_key_list(' + TABLE + ')'):
        groups.setdefault(row[0], []).append(tuple(row[1:]))
    return sorted(tuple(sorted(rows)) for rows in groups.values())


def inspect_database(db):
    """Full startup/explicit-activation check, never called by final action CAS."""
    try:
        if initialization_state(db) == 'absent': return False
        for name, expected in DDL.items():
            row = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (name,)).fetchone()
            # Preserve literal CHECK/index predicate contents; similar PRAGMA
            # shape is not permission to repair or accept a different epoch.
            if row is None or type(row[0]) is not str or row[0].strip() != expected.strip(): raise _error()
        columns = list(db.execute('PRAGMA table_xinfo(' + TABLE + ')'))
        if tuple(row[1] for row in columns) != FIELDS: raise _error()
        primary = ('service_origin', 'tenant_id', 'intent_id')
        for row in columns:
            field = row[1]
            expected_type = 'INTEGER' if field in ('server_revision', 'revision', 'applied_receipt_revision') else 'TEXT'
            if (row[2].upper() != expected_type or row[3] != (0 if field in _NULLABLE else 1)
                or row[4] is not None or row[6] != 0 or row[5] != (primary.index(field) + 1 if field in primary else 0)):
                raise _error()
        def group(target, left, right):
            return tuple((number, target, source, destination, 'NO ACTION', 'NO ACTION', 'NONE')
                for number, (source, destination) in enumerate(zip(left, right)))
        basis = ('service_origin', 'tenant_id', 'receiving_id', 'server_revision')
        expected_foreign = sorted((group('receipts', ('receipt_id',), ('id',)),
            group(projections.VERSIONS, basis + ('receipt_id',), basis + ('receipt_id',)),
            group(projections.OBSERVATIONS, basis + ('applied_proof_hash',), basis + ('proof_hash',))))
        if _foreign_groups(db) != expected_foreign or db.execute('PRAGMA foreign_key_check(' + TABLE + ')').fetchone(): raise _error()
        expected_indexes = {
            'sqlite_autoindex_' + TABLE + '_1': (1, 'pk', 0, primary),
            'sqlite_autoindex_' + TABLE + '_2': (1, 'u', 0, ('service_origin', 'tenant_id', 'request_key')),
            ACTIVE_INDEX: (1, 'c', 1, ('service_origin', 'tenant_id', 'receiving_id')),
            CHILD_INDEX: (0, 'c', 0, ('service_origin', 'tenant_id', 'receipt_id', 'created_at', 'intent_id'))}
        actual = {row[1]: (row[2], row[3], row[4], tuple(item[2] for item in db.execute('PRAGMA index_info("' + row[1] + '")')))
                  for row in db.execute('PRAGMA index_list(' + TABLE + ')')}
        if actual != expected_indexes or db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=? LIMIT 1", (TABLE,)).fetchone():
            raise _error()
        if projections.initialization_state(db) != 'present': raise _error()
        projections.inspect_database(db)
        for row in _rows(db): _full_row(row)
        return True
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError, RecursionError):
        raise _error() from None


def initialize(store):
    """Only an explicit dependency-checked feature epoch may call this."""
    try:
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
            if projections.initialization_state(db) != 'present': raise _error()
            projections.inspect_database(db)
            if initialization_state(db) == 'absent':
                for name in (TABLE, ACTIVE_INDEX, CHILD_INDEX): db.execute(DDL[name])
                db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (SENTINEL_KEY, SENTINEL_VALUE))
            inspect_database(db)
    except StationError:
        raise
    except sqlite3.Error:
        raise _error() from None


@dataclass(frozen=True)
class PreparedStockAction:
    tenant_id: str
    receiving_id: str
    server_revision: int
    receipt_id: str
    proof_hash: str
    session_facts: tuple
    expected_projection: projections.ExpectedProjection
    expected_action: tuple | None
    expected_active: tuple | None
    row: tuple
    update_values: tuple
    audit_kind: str
    audit_at: str
    audit_detail: str


def _common(receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp):
    if type(receipt) is not dict or type(proof) is not dict or type(expected_projection) is not projections.ExpectedProjection:
        raise ValueError()
    child = expected_projection.child
    if (type(child) is not tuple or len(child) != 5 or child[2] is not None or child[4] != 0
        or receipt.get('id') != child[0] or receipt.get('updated') != child[3]
        or receipt.get('deleted_at') is not None or canonical(receipt) != child[1]): raise ValueError()
    revision = _integer(receipt.get('revision'), 1)
    marker = receipt.get('original_receiving_recovery')
    if (type(marker) is not dict or set(marker) != projections.MARKER_FIELDS
        or type(marker.get('version')) is not int or marker['version'] != 2
        or marker.get('originKind') != 'physical_intake' or marker.get('serviceOrigin') != ORIGIN
        or proof.get('workflowKind') != 'inventory_only'
        or receipt.get('purchase') is not None or receipt.get('requires_purchase') is not False): raise ValueError()
    tenant = _uuid(marker['tenantId']); receiving = _uuid(marker['receivingId'])
    server_revision = _integer(marker['serverRevision']); _hash(marker['planHash']); _hash(marker['proofHash'])
    _uuid(marker['physicalIntakeId'])
    for field in ('physicalManifestHash', 'sourceSnapshotHash', 'restorationSourceHash'): _hash(marker[field])
    if any(proof.get(key) != marker[key] for key in ('receivingId', 'serverRevision', 'planHash', 'proofHash',
        'sourceSnapshotHash', 'restorationSourceHash')): raise ValueError()
    _session(session_facts, tenant)
    if session_facts[2] != marker['store']: raise ValueError()
    _stamp(stamp)
    def packed(value, fields):
        if type(value) is not tuple or len(value) != len(fields): raise ValueError()
        return dict(zip(fields, value))
    link = packed(expected_projection.link, projections.LINK_FIELDS)
    version = packed(expected_projection.version, projections.VERSION_FIELDS)
    observation = packed(expected_projection.observation, projections.OBSERVATION_FIELDS)
    if (expected_projection.version != expected_projection.active_version
        or expected_projection.observation != expected_projection.active_observation
        or link['active_receipt_id'] != child[0] or version['receipt_id'] != child[0]
        or any(row['service_origin'] != ORIGIN or row['tenant_id'] != tenant or row['receiving_id'] != receiving
               or row['server_revision'] != server_revision for row in (link, version, observation))
        or link['plan_hash'] != marker['planHash'] or version['plan_hash'] != marker['planHash']
        or any(row['physical_intake_id'] != marker['physicalIntakeId']
               or row['physical_manifest_hash'] != marker['physicalManifestHash'] for row in (link, version))
        or version['source_snapshot_hash'] != marker['sourceSnapshotHash']
        or observation['restoration_source_hash'] != marker['restorationSourceHash']
        or link['proof_hash'] != marker['proofHash'] or observation['proof_hash'] != marker['proofHash']
        or expected_projection.ordinary_references or expected_projection.financial_links or expected_projection.financial_snapshots):
        raise ValueError()
    for row in (expected_action, expected_active):
        if row is not None:
            value, unused = _full_row(row)
            if value['tenant_id'] != tenant or value['receiving_id'] != receiving: raise ValueError()
    return marker, revision


def _intent_binding(intent, receipt, proof, *, fresh, expected_projection):
    validate_intent(intent)
    marker = receipt['original_receiving_recovery']
    if (any(intent[key] != marker[key] for key in ('serviceOrigin', 'tenantId', 'store', 'receivingId',
        'serverRevision', 'planHash', 'physicalIntakeId', 'physicalManifestHash', 'sourceSnapshotHash'))
        or intent['stockReceiptId'] != receipt['id'] or intent['stockRevision'] > receipt['revision']
        or intent['provider'] != proof.get('provider') or intent['labelProfile'] != proof.get('labelProfile')):
        raise ValueError()
    if fresh:
        observation = dict(zip(projections.OBSERVATION_FIELDS, expected_projection.observation))
        if (intent['stockRevision'] != receipt['revision'] or intent['proofHash'] != proof['proofHash']
            or intent['restorationSourceHash'] != proof['restorationSourceHash']
            or intent['stateBasisHash'] != proof.get('stateBasisHash')
            or intent['operationHash'] != observation['operation_hash']): raise ValueError()


def _prepared(receipt, proof, expected_projection, expected_action, expected_active, session_facts, row, stamp, kind):
    changed = row != expected_action
    value = _shape(row)
    updates = tuple(value[field] for field in UPDATE_FIELDS) if changed and expected_action is not None else ()
    return PreparedStockAction(value['tenant_id'], value['receiving_id'], value['server_revision'],
        value['receipt_id'], proof['proofHash'], session_facts, expected_projection, expected_action,
        expected_active, row, updates, kind if changed else '', stamp if changed else '',
        'Saved the original inventory-only action basis; provider attribution remains unknown.' if changed else '')


def prepare_reservation(*, receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp, intent):
    try:
        _common(receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp)
        _intent_binding(intent, receipt, proof, fresh=expected_action is None, expected_projection=expected_projection)
        raw = canonical(intent)
        if len(raw.encode('utf-8')) > 65536: raise ValueError()
        if expected_action is not None:
            old, unused = _full_row(expected_action)
            if (old['state'] != 'pending' or expected_active != expected_action
                or old['intent_body'] != raw or old['request_fingerprint'] != request_fingerprint(intent)):
                raise ValueError()
            row = expected_action
        else:
            if expected_active is not None: raise ValueError()
            row = (ORIGIN, intent['tenantId'], intent['intentId'], intent['stockReceiptId'], intent['receivingId'],
                intent['serverRevision'], intent['planHash'], intent['kind'], intent['requestKey'], raw,
                request_fingerprint(intent), 'pending', None, None, None, 1, stamp, stamp)
        return _prepared(receipt, proof, expected_projection, expected_action, expected_active, session_facts,
                         row, stamp, 'original_intake_stock_action_reserved')
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise _error() from None


def _transition(*, receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp, result_fingerprint=None, acknowledge=False):
    _common(receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp)
    if expected_action is None: raise ValueError()
    old, intent = _full_row(expected_action)
    _intent_binding(intent, receipt, proof, fresh=False, expected_projection=expected_projection)
    if old['state'] != 'acknowledged' and expected_active != expected_action: raise ValueError()
    if old['state'] == 'acknowledged':
        if receipt['revision'] < old['applied_receipt_revision']: raise ValueError()
        if receipt['revision'] == old['applied_receipt_revision'] and proof['proofHash'] != old['applied_proof_hash']:
            raise ValueError()
        if not acknowledge and result_fingerprint != old['result_fingerprint']: raise ValueError()
        row = expected_action; kind = ''
    elif acknowledge:
        if (old['state'] != 'applied' or old['applied_receipt_revision'] != receipt['revision']
            or old['applied_proof_hash'] != proof['proofHash']): raise ValueError()
        row_value = dict(old, state='acknowledged', revision=_integer(old['revision'] + 1, 1), updated_at=stamp)
        row = _tuple(row_value); kind = 'original_intake_stock_action_acknowledged'
    else:
        _hash(result_fingerprint)
        if old['state'] == 'applied' and result_fingerprint != old['result_fingerprint']: raise ValueError()
        if old['state'] == 'applied' and receipt['revision'] < old['applied_receipt_revision']: raise ValueError()
        if old['state'] == 'applied' and receipt['revision'] == old['applied_receipt_revision']:
            if proof['proofHash'] != old['applied_proof_hash']: raise ValueError()
            row = expected_action; kind = ''
        else:
            row_value = dict(old, state='applied', result_fingerprint=result_fingerprint,
                applied_receipt_revision=receipt['revision'], applied_proof_hash=proof['proofHash'],
                revision=_integer(old['revision'] + 1, 1), updated_at=stamp)
            row = _tuple(row_value)
            kind = 'original_intake_stock_action_applied' if old['state'] == 'pending' else 'original_intake_stock_action_reaffirmed'
    return _prepared(receipt, proof, expected_projection, expected_action, expected_active, session_facts, row, stamp, kind)


def prepare_applied(*, receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp, result_fingerprint):
    try:
        return _transition(receipt=receipt, proof=proof, expected_projection=expected_projection,
            expected_action=expected_action, expected_active=expected_active, session_facts=session_facts,
            stamp=stamp, result_fingerprint=result_fingerprint)
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise _error() from None


def prepare_acknowledged(*, receipt, proof, expected_projection, expected_action, expected_active, session_facts, stamp):
    try:
        return _transition(receipt=receipt, proof=proof, expected_projection=expected_projection,
            expected_action=expected_action, expected_active=expected_active, session_facts=session_facts,
            stamp=stamp, acknowledge=True)
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise _error() from None


def assert_pair_current(db, *, tenant_id, receiving_id, server_revision, proof_hash, session_facts,
                        current_session_facts, expected_projection, expected_action, expected_active, intent_id=None):
    """Read-only exact CAS; a fresh selected B1 observation may be absent here."""
    try:
        _uuid(tenant_id); _uuid(receiving_id); _integer(server_revision); _hash(proof_hash)
        _session(session_facts, tenant_id); _session(current_session_facts, tenant_id)
        if (session_facts != current_session_facts
            or any(type(left) is not type(right) for left, right in zip(session_facts, current_session_facts))): raise ValueError()
        if intent_id is not None: _uuid(intent_id)
        for row in (expected_action, expected_active):
            if row is not None:
                value = _shape(row)
                if value['tenant_id'] != tenant_id or value['receiving_id'] != receiving_id: raise ValueError()
        projections.assert_current(db, expected_projection, tenant_id, receiving_id, server_revision, proof_hash)
        actual = _pair(db, tenant_id, receiving_id, intent_id)
        if actual['intent'] != expected_action or actual['active'] != expected_active: raise _error()
        return {key: _mapping(row) for key, row in actual.items()}
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError):
        raise _error() from None


def _prepared_shape(prepared):
    if type(prepared) is not PreparedStockAction: raise ValueError()
    value = _shape(prepared.row)
    if (value['tenant_id'] != prepared.tenant_id or value['receiving_id'] != prepared.receiving_id
        or value['server_revision'] != prepared.server_revision or value['receipt_id'] != prepared.receipt_id): raise ValueError()
    _hash(prepared.proof_hash)
    projection = prepared.expected_projection
    if (type(projection) is not projections.ExpectedProjection
        or type(projection.child) is not tuple or len(projection.child) != 5
        or projection.child[0] != prepared.receipt_id or projection.child[2] is not None
        or type(projection.observation) is not tuple or len(projection.observation) != len(projections.OBSERVATION_FIELDS)
        or projection.observation != projection.active_observation): raise ValueError()
    observation = dict(zip(projections.OBSERVATION_FIELDS, projection.observation))
    if (observation['service_origin'] != ORIGIN or observation['tenant_id'] != prepared.tenant_id
        or observation['receiving_id'] != prepared.receiving_id or observation['server_revision'] != prepared.server_revision
        or observation['proof_hash'] != prepared.proof_hash): raise ValueError()
    if type(prepared.update_values) is not tuple: raise ValueError()
    changed = prepared.row != prepared.expected_action
    expected_update = tuple(value[field] for field in UPDATE_FIELDS) if changed and prepared.expected_action is not None else ()
    if prepared.update_values != expected_update: raise ValueError()
    if prepared.expected_action is not None:
        old = _shape(prepared.expected_action)
        if any(value[field] != old[field] for field in FIELDS if field not in UPDATE_FIELDS): raise ValueError()
        if changed:
            if old['state'] == 'acknowledged' or value['revision'] != old['revision'] + 1: raise ValueError()
            if old['state'] == 'pending' and value['state'] != 'applied': raise ValueError()
            if old['state'] == 'applied':
                if value['result_fingerprint'] != old['result_fingerprint']: raise ValueError()
                if value['state'] == 'applied' and value['applied_receipt_revision'] <= old['applied_receipt_revision']:
                    raise ValueError()
                if value['state'] == 'acknowledged' and any(value[field] != old[field]
                    for field in ('applied_receipt_revision', 'applied_proof_hash')): raise ValueError()
    if changed:
        if (prepared.audit_kind not in AUDITS or prepared.audit_at != value['updated_at']
            or type(prepared.audit_detail) is not str or not 1 <= len(prepared.audit_detail) <= 500): raise ValueError()
    elif (prepared.audit_kind, prepared.audit_at, prepared.audit_detail) != ('', '', ''):
        raise ValueError()
    return value


def assert_current(db, prepared, current_session_facts):
    try:
        value = _prepared_shape(prepared)
        return assert_pair_current(db, tenant_id=prepared.tenant_id, receiving_id=prepared.receiving_id,
            server_revision=prepared.server_revision, proof_hash=prepared.proof_hash,
            session_facts=prepared.session_facts, current_session_facts=current_session_facts,
            expected_projection=prepared.expected_projection, expected_action=prepared.expected_action,
            expected_active=prepared.expected_active, intent_id=value['intent_id'])
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise _error() from None


def _commit(store, prepared, current_session_facts, reservation):
    try:
        value = _prepared_shape(prepared)
        if reservation:
            if value['state'] != 'pending' or prepared.update_values: raise ValueError()
        elif prepared.expected_action is None or value['state'] == 'pending':
            raise ValueError()
        with store.db() as db:
            db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
            assert_current(db, prepared, current_session_facts)
            if prepared.row == prepared.expected_action: return _mapping(prepared.row)
            if reservation:
                if prepared.expected_action is not None or prepared.expected_active is not None: raise _error()
                db.execute('INSERT INTO ' + TABLE + ' (' + ','.join(FIELDS) + ') VALUES (' + ','.join('?' for field in FIELDS) + ')', prepared.row)
            else:
                changed = db.execute('UPDATE ' + TABLE + ' SET ' + ','.join(field + '=?' for field in UPDATE_FIELDS)
                    + ' WHERE ' + ' AND '.join(field + ' IS ?' for field in FIELDS), prepared.update_values + prepared.expected_action)
                if changed.rowcount != 1: raise _error()
            db.execute('INSERT INTO events(receipt_id,at,kind,detail) VALUES (?,?,?,?)',
                       (prepared.receipt_id, prepared.audit_at, prepared.audit_kind, prepared.audit_detail))
            return _mapping(prepared.row)
    except StationError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, OverflowError):
        raise _error() from None


def commit_reservation(store, prepared, *, current_session_facts):
    return _commit(store, prepared, current_session_facts, True)


def commit_transition(store, prepared, *, current_session_facts):
    return _commit(store, prepared, current_session_facts, False)
