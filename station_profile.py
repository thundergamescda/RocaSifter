"""One immutable authority for a desktop profile's receiving/financial route.

This module must run before Store, credential migration or Tracker construction.
It never decrypts a vault or writes the existing receipt/ledger database. Local
OS access is still the trust boundary; the checksum detects accidental damage,
not a hostile administrator replacing this program or its files.
"""
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid

from station_store import StationError
from station_receiving_reference_fences import initialize as _initialize_reference_fences
from station_receiving_reference_fences import inspect_database as _inspect_reference_fences
from station_intake_receiving_registry import initialize as _initialize_intake_projections
from station_intake_receiving_registry import inspect_database as _inspect_intake_projections
from station_intake_receiving_registry import initialization_state as _intake_projection_initialization_state
from station_intake_stock_action_registry import initialize as _initialize_intake_stock_operations
from station_intake_stock_action_registry import inspect_database as _inspect_intake_stock_operations
from station_intake_stock_action_registry import initialization_state as _intake_stock_operation_initialization_state
from station_intake_stock_action_registry import TABLES as _INTAKE_STOCK_OPERATION_TABLES


SERVICE_ORIGIN = 'https://stock-and-sleeve-api.onrender.com'
POLICY_FILE = 'profile-policy.json'
CLOUD_SESSION_LOCK = 'cloud-session.lock'
FORMAT_VERSION = 1
REFERENCE_FENCE_FEATURE = 'original_stock_reference_fences_v1'
INTAKE_PROJECTION_FEATURE = 'original_intake_stock_projections_v1'
INTAKE_STOCK_OPERATIONS_FEATURE = 'original_intake_stock_operations_v1'
_REFERENCE_FEATURE_EPOCHS = {'original_stock_reference_fences': 1}
_INTAKE_PROJECTION_FEATURE_EPOCHS = {'original_intake_stock_projections': 1}
_INTAKE_STOCK_OPERATION_FEATURE_EPOCHS = {'original_intake_stock_operations': 1}
_HOSTED_FEATURE_EPOCHS = (_REFERENCE_FEATURE_EPOCHS, _INTAKE_PROJECTION_FEATURE_EPOCHS,
                        {**_REFERENCE_FEATURE_EPOCHS, **_INTAKE_PROJECTION_FEATURE_EPOCHS},
                        {**_INTAKE_PROJECTION_FEATURE_EPOCHS, **_INTAKE_STOCK_OPERATION_FEATURE_EPOCHS},
                        {**_REFERENCE_FEATURE_EPOCHS, **_INTAKE_PROJECTION_FEATURE_EPOCHS,
                         **_INTAKE_STOCK_OPERATION_FEATURE_EPOCHS})
_INTAKE_PROJECTION_TABLES = frozenset(('intake_stock_projection_links',
    'intake_stock_projection_versions', 'intake_stock_projection_observations'))
_MAX_JSON = 262_144
_CLOUD_FILES = ('cloud.session', 'cloud.pending', 'cloud.intake', 'cloud.receiving')
_LEGACY_TABLES = ('credit_customers', 'credit_entries', 'credit_requests', 'credit_reservations')
_TABLE_COLUMNS = {
    'settings': {'key', 'value'}, 'receipts': {'id', 'body'},
    'operations': {'receipt_id', 'key', 'request', 'response'},
    'credit_customers': {'id', 'name', 'balance_cents'},
    'credit_entries': {'id', 'customer_id', 'cents', 'source'},
    'credit_requests': {'key', 'body', 'response'},
    'credit_reservations': {'id', 'customer_id', 'cents', 'state'},
    'receiving_recovery_links': {'service_origin', 'tenant_id', 'receiving_id', 'active_receipt_id',
        'server_revision', 'plan_hash', 'financial_receipt_id', 'revision'},
    'receiving_recovery_snapshots': {'service_origin', 'tenant_id', 'receiving_id', 'server_revision',
        'receipt_id', 'plan_hash', 'restoration_source_hash', 'plan_snapshot', 'operation_snapshot',
        'operation_snapshot_hash'},
    'receiving_label_jobs': {'service_origin', 'tenant_id', 'job_id', 'receipt_id', 'receiving_id',
        'server_revision', 'plan_hash', 'proof_hash', 'publication_basis_hash', 'job_body',
        'part_count', 'published_parts', 'state', 'revision', 'created_at', 'updated_at'},
    'receiving_label_publications': {'service_origin', 'tenant_id', 'publication_id', 'publication_key',
        'job_id', 'part_index', 'receipt_id', 'receiving_id', 'server_revision', 'plan_hash',
        'proof_hash', 'publication_basis_hash', 'request_fingerprint', 'manifest_body', 'published_at'},
    'receiving_action_intents': {'service_origin','tenant_id','intent_id','receipt_id','financial_receipt_id',
        'receiving_id','server_revision','plan_hash','action_kind','request_key','intent_body',
        'request_fingerprint','state','result_fingerprint','applied_receipt_revision','applied_proof_hash',
        'revision','created_at','updated_at'},
    'receiving_action_reference_fences': {'service_origin','tenant_id','intent_id','request_key','receipt_id',
        'financial_receipt_id','receiving_id','server_revision','plan_hash','action_kind','reference_body',
        'reference_fingerprint','evidence_body','evidence_fingerprint','version','resolution_kind','created_at'},
}
_TABLES = set(_TABLE_COLUMNS) | {'events', 'tasks', 'receipt_summaries',
    'cleanup_observations', 'price_jobs', 'price_http_cache'} | _INTAKE_PROJECTION_TABLES | _INTAKE_STOCK_OPERATION_TABLES
_EVIDENCE = frozenset(('empty_or_drafts', 'legacy_settings', 'legacy_vault',
    'legacy_provider_journal', 'legacy_received_receipt', 'legacy_purchase', 'legacy_local_ledger'))


class ProfilePolicyError(StationError):
    def __init__(self, message, code='PROFILE_RECOVERY_REQUIRED'):
        super().__init__(message)
        self.code = code


def _error(code='PROFILE_RECOVERY_REQUIRED'):
    return ProfilePolicyError(
        'This profile needs owner recovery before connection or financial work. '
        'Keep its saved files and original operations; do not change its mode or reuse it for another store.', code)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))


def _regular(path):
    """Do not follow a policy/vault/database name redirected to another file."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, 'st_reparse_tag', 0) & 0x20000000:
        raise _error()
    return info


def _present(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _safe_errors(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ProfilePolicyError:
            raise
        except (OSError, ValueError, TypeError, sqlite3.Error):
            raise _error() from None
    return checked


def _read_json(path, limit=_MAX_JSON):
    if _regular(path).st_size > limit:
        raise _error()
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise _error()
    return _json(raw)


@contextmanager
def _file_lock(folder, name):
    """OS lock survives competing processes and is released on process death."""
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink() or (hasattr(folder, 'is_junction') and folder.is_junction()):
        raise _error()
    path = folder / name
    if _present(path):
        _regular(path)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(descriptor, 'r+b')
    acquired = False
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b'\0'); stream.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _error('PROFILE_BUSY') from None
                time.sleep(.02)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


@contextmanager
def _locked(folder):
    with _file_lock(folder, 'profile-policy.lock'):
        yield folder / POLICY_FILE


def _seal(record):
    return dict(record, sha256=hashlib.sha256(_canonical(record)).hexdigest())


def _write(path, record):
    """Only commit after fsync. A failed replace leaves the prior authority intact."""
    temporary = path.parent / ('.profile-policy-' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(_canonical(_seal(record)) + b'\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def _uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('canonical UUID required')
    return value


def _load(path):
    try:
        record = _read_json(path, 16_384)
        if not isinstance(record, dict) or type(record.get('format_version')) is not int:
            raise ValueError()
        if record['format_version'] != FORMAT_VERSION:
            raise _error('PROFILE_POLICY_UNSUPPORTED')
        expected = {'format_version', 'profile_id', 'mode', 'created_at', 'evidence', 'binding', 'sha256'}
        if 'hosted_features' in record:
            expected.add('hosted_features')
        if set(record) != expected:
            raise ValueError()
        digest = record.pop('sha256')
        if not isinstance(digest, str) or not hmac.compare_digest(digest, hashlib.sha256(_canonical(record)).hexdigest()):
            raise ValueError()
        _uuid(record['profile_id'])
        if record['mode'] not in ('hosted', 'legacy_compatibility'):
            raise ValueError()
        stamp = datetime.fromisoformat(record['created_at'])
        if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError()
        evidence = record['evidence']
        if (not isinstance(evidence, list) or not evidence or evidence != sorted(set(evidence))
                or any(item not in _EVIDENCE for item in evidence)
                or (record['mode'] == 'hosted' and evidence != ['empty_or_drafts'])
                or (record['mode'] == 'legacy_compatibility' and 'empty_or_drafts' in evidence)):
            raise ValueError()
        binding = record['binding']
        if binding is not None:
            if (record['mode'] != 'hosted' or not isinstance(binding, dict)
                    or set(binding) != {'tenant_id', 'service_origin'}
                    or binding['service_origin'] != SERVICE_ORIGIN):
                raise ValueError()
            _uuid(binding['tenant_id'])
        if 'hosted_features' in record:
            features = record['hosted_features']
            if (record['mode'] != 'hosted' or binding is None or type(features) is not dict
                    or not any(set(features) == set(allowed)
                               and all(type(features[name]) is int and features[name] == epoch
                                       for name, epoch in allowed.items())
                               for allowed in _HOSTED_FEATURE_EPOCHS)):
                raise ValueError()
        return record
    except ProfilePolicyError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise _error('PROFILE_POLICY_CORRUPT') from None


def _database_facts(path, *, reference_features=False, intake_projection_features=False,
                    intake_stock_operation_features=False):
    legacy, cloud = set(), False
    if not _present(path):
        return legacy, cloud
    _regular(path)
    # mode=ro keeps business records and all original receiving journal bytes.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        if db.execute('PRAGMA quick_check(1)').fetchone()[0] != 'ok':
            raise _error()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,7) COLLATE NOCASE<>'sqlite_'")}
        if not tables <= _TABLES:
            raise _error()
        if 'receiving_action_reference_fences' in tables and reference_features is not True:
            raise _error('PROFILE_FEATURE_UPGRADE_REQUIRED')
        if tables & _INTAKE_PROJECTION_TABLES and intake_projection_features is not True:
            raise _error('PROFILE_FEATURE_UPGRADE_REQUIRED')
        try:
            if _intake_projection_initialization_state(db) not in ('absent', 'present'):
                raise _error()
            operation_state = _intake_stock_operation_initialization_state(db)
            if operation_state not in ('absent', 'present'):
                raise _error()
        except (StationError, ValueError, TypeError, AttributeError):
            raise _error() from None
        if operation_state == 'present' and intake_stock_operation_features is not True:
            raise _error('PROFILE_FEATURE_UPGRADE_REQUIRED')
        for table in tables & _TABLE_COLUMNS.keys():
            columns = {row[1] for row in db.execute('PRAGMA table_info(' + table + ')')}
            if not _TABLE_COLUMNS[table] <= columns:
                raise _error()
        if tables & {'receiving_recovery_links', 'receiving_recovery_snapshots'}:
            from station_receiving_registry import inspect_database
            try:
                cloud = inspect_database(db)
            except (StationError, ValueError, TypeError, AttributeError):
                raise _error() from None
        if tables & {'receiving_label_jobs', 'receiving_label_publications'}:
            from station_receiving_label_publication import inspect_database as inspect_labels
            try:
                cloud = inspect_labels(db) or cloud
            except (StationError, ValueError, TypeError, AttributeError):
                raise _error() from None
        if 'receiving_action_intents' in tables:
            from station_receiving_action_registry import inspect_database as inspect_actions
            try:
                cloud = inspect_actions(db) or cloud
            except (StationError, ValueError, TypeError, AttributeError):
                raise _error() from None
        if 'receiving_action_reference_fences' in tables:
            try:
                cloud = _inspect_reference_fences(db) or cloud
            except (StationError, ValueError, TypeError, AttributeError):
                raise _error() from None
        if tables & _INTAKE_PROJECTION_TABLES:
            try:
                cloud = _inspect_intake_projections(db) or cloud
            except (StationError, ValueError, TypeError, AttributeError):
                raise _error() from None
        if tables & _INTAKE_STOCK_OPERATION_TABLES:
            try:
                cloud = _inspect_intake_stock_operations(db) or cloud
            except (StationError, ValueError, TypeError, AttributeError):
                raise _error() from None
        if 'settings' in tables:
            for key, raw in db.execute('SELECT key,value FROM settings'):
                if not isinstance(raw, str) or len(raw.encode('utf-8')) > _MAX_JSON:
                    raise _error()
                value = _json(raw)
                if key == 'preferences':
                    if not isinstance(value, dict):
                        raise _error()
                    if _legacy_settings(value):
                        legacy.add('legacy_settings')
        for table in tables & set(_LEGACY_TABLES):
            if db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                legacy.add('legacy_local_ledger')
        if 'operations' in tables:
            for key, raw, response in db.execute('SELECT key,request,response FROM operations'):
                if not isinstance(key, str) or not isinstance(raw, str) or len(raw.encode('utf-8')) > 8_388_608:
                    raise _error()
                request = _json(raw)
                kind = next((kind for kind in ('catalog', 'stock', 'search') if key.startswith(kind + ':')), None)
                expected = {'catalog': '/v2/catalog/batch-upsert', 'stock': '/v2/inventory/changes/batch-create',
                            'search': '/v2/catalog/search'}.get(kind)
                if (not isinstance(request, dict) or set(request) != {'endpoint', 'payload'}
                        or not isinstance(request['payload'], dict) or not expected
                        or request['endpoint'] not in ('https://connect.squareup.com' + expected,
                                                       'https://connect.squareupsandbox.com' + expected)):
                    raise _error()
                if response is not None and (not isinstance(response, str) or not isinstance(_json(response), dict)):
                    raise _error()
                legacy.add('legacy_provider_journal')
        if 'receipts' in tables:
            # SQLite validates/inspects one original at a time; Python does not
            # fetch full card arrays or silently stop at the history UI's limit.
            if db.execute("SELECT 1 FROM receipts WHERE NOT json_valid(body) LIMIT 1").fetchone():
                raise _error()
            rows = db.execute('''SELECT json_type(body),json_type(body,'$.cards'),
                json_type(body,'$.purchase'),json_extract(body,'$.purchase.scope'),
                json_extract(body,'$.purchase.type'),json_type(body,'$.intake_claim'),
                json_type(body,'$.shared_trade'),json_type(body,'$.hosted_receive'),
                json_type(body,'$.square_connection'),json_type(body,'$.catalog_mode'),
                json_extract(body,'$.inventory_status'),json_type(body,'$.purchase.physical_intake_id')
                FROM receipts''')
            for shape, cards, purchase, scope, buy_type, intake, trade, receiving, connection, catalog, status, claim in rows:
                if shape != 'object' or cards != 'array' or purchase not in (None, 'null', 'object'):
                    raise _error()
                central = any(value not in (None, 'null') for value in (intake, trade, receiving, claim)) or scope == 'hosted'
                if any(value not in (None, 'null', 'object') for value in (intake, trade, receiving, connection)):
                    raise _error()
                local = connection not in (None, 'null') or catalog not in (None, 'null') or status in ('received', 'committing', 'needs_attention')
                if central:
                    cloud = True
                    if connection not in (None, 'null') or catalog not in (None, 'null'):
                        raise _error()
                else:
                    if local:
                        legacy.add('legacy_received_receipt')
                    if purchase == 'object':
                        if scope not in (None, 'local-pilot') or buy_type not in ('cash', 'credit'):
                            raise _error()
                        legacy.add('legacy_purchase')
    return legacy, cloud


def _legacy_settings(value):
    configured = False
    for name in ('squareToken', 'squareLocationId'):
        field = value.get(name, '')
        if not isinstance(field, str) or len(field) > 16_384:
            raise _error()
        if field.strip():
            configured = True
    return configured


def _facts(folder, settings, *, reference_features=False, intake_projection_features=False,
           intake_stock_operation_features=False):
    legacy, cloud = _database_facts(folder / 'station.sqlite3', reference_features=reference_features,
                                  intake_projection_features=intake_projection_features,
                                  intake_stock_operation_features=intake_stock_operation_features)
    if settings is not None and _present(settings):
        value = _read_json(settings)
        if not isinstance(value, dict):
            raise _error()
        if _legacy_settings(value):
            legacy.add('legacy_settings')
    vault = folder / 'square.secret'
    if _present(vault):
        size = _regular(vault).st_size
        with vault.open('rb') as stream:
            prefix = stream.read(3)
        if prefix not in (b'DP1', b'FN1') or not 3 < size <= 1_048_576:
            raise _error()
        legacy.add('legacy_vault')
    for name in _CLOUD_FILES:
        path = folder / name
        if _present(path):
            _regular(path)
            cloud = True  # Presence is NOT permission to decrypt/reclassify it.
    if folder.exists() and any(path.name.startswith('cloud.') and path.name not in _CLOUD_FILES for path in folder.iterdir()):
        raise _error()
    return legacy, cloud


def _has_reference_feature(record):
    return record.get('hosted_features', {}).get('original_stock_reference_fences') == 1


def _has_intake_projection_feature(record):
    return record.get('hosted_features', {}).get('original_intake_stock_projections') == 1


def _has_intake_stock_operation_feature(record):
    return record.get('hosted_features', {}).get('original_intake_stock_operations') == 1


def _feature_name(feature):
    if type(feature) is not str or feature not in (
            REFERENCE_FENCE_FEATURE, INTAKE_PROJECTION_FEATURE, INTAKE_STOCK_OPERATIONS_FEATURE):
        raise _error('PROFILE_FEATURE_UNAVAILABLE')


def _feature_enabled(record, feature):
    return {REFERENCE_FENCE_FEATURE: _has_reference_feature,
            INTAKE_PROJECTION_FEATURE: _has_intake_projection_feature,
            INTAKE_STOCK_OPERATIONS_FEATURE: _has_intake_stock_operation_feature}[feature](record)


def _feature_session(facts, tenant):
    if type(facts) is not tuple or len(facts) != 5:
        raise _error('PROFILE_FEATURE_AUTHORITY_CHANGED')
    nonce, actual_tenant, store, role, expiry = facts
    if (type(nonce) is not str or not re.fullmatch('[a-f0-9]{32}', nonce)
            or type(actual_tenant) is not str or actual_tenant != tenant
            or type(store) is not str or not re.fullmatch('[a-z0-9][a-z0-9-]{2,49}', store)
            or type(role) is not str or role not in ('owner', 'manager')
            or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= time.time()):
        raise _error('PROFILE_FEATURE_AUTHORITY_CHANGED')


def _fsync_policy_target(path):
    """Flush the regular final name after replacement, before any schema write."""
    expected = _regular(path)
    with path.open('r+b') as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise _error()
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class ProfilePolicy:
    mode: str
    _root: Path = field(repr=False)
    _settings: object = field(repr=False)
    _identity: bytes = field(repr=False)

    @contextmanager
    def cloud_session_guard(self):
        """Serialize local session I/O, without a policy/DB/vault read.

        This guard is deliberately separate from the policy lock: callers may
        validate/bind policy while holding it. It is not reentrant and must not
        span network requests; compose one guarded critical section using local
        internal read/conditional-clear helpers instead of nesting this guard.
        """
        body_error = None
        try:
            with _file_lock(self._root, CLOUD_SESSION_LOCK):
                try:
                    yield
                except OSError as error:
                    body_error = error
                    raise
        except ProfilePolicyError:
            raise
        except OSError as error:
            if error is body_error:
                raise
            raise _error() from None

    def _record(self, path):
        record = _load(path)
        identity = _canonical({key: value for key, value in record.items() if key != 'binding'})
        if not hmac.compare_digest(identity, self._identity):
            raise _error('PROFILE_POLICY_CHANGED')
        return record

    @_safe_errors
    def safe_snapshot(self):
        with _locked(self._root) as path:
            bound = self._record(path)['binding'] is not None
        legacy = self.mode == 'legacy_compatibility'
        return {'profileVersion': FORMAT_VERSION, 'profileMode': self.mode,
                'localCreditEnabled': legacy, 'legacySquareEntryEnabled': legacy,
                'requiredReceivingRoute': 'legacy_compatible' if legacy else 'hosted', 'storeBound': bound}

    @_safe_errors
    def require_legacy(self, action='legacy operation'):
        with _locked(self._root) as path:
            self._record(path)
        if self.mode != 'legacy_compatibility':
            raise ProfilePolicyError('This profile uses the hosted store workflow. Local Square credentials and local-wallet actions are unavailable.',
                                     'HOSTED_WORKFLOW_REQUIRED')

    @_safe_errors
    def require_feature(self, feature):
        """Read-only capability check. This never activates or creates a table."""
        _feature_name(feature)
        with _locked(self._root) as path:
            record = self._record(path)
            if record['mode'] != 'hosted':
                raise _error('PROFILE_FEATURE_UNAVAILABLE')
            if feature == INTAKE_STOCK_OPERATIONS_FEATURE and not _has_intake_projection_feature(record):
                raise _error('PROFILE_FEATURE_DEPENDENCY_REQUIRED')
            if record['binding'] is None or not _feature_enabled(record, feature):
                raise _error('PROFILE_FEATURE_UPGRADE_REQUIRED')

    @_safe_errors
    def activate_feature(self, feature, store, *, current_session_facts):
        """Explicit private prototype upgrade; never called from startup.

        Caller holds THIS profile's cloud_session_guard then service.lock and
        supplies freshly verified remote owner/manager and local nonce facts.
        This metadata module cannot authenticate a browser's role/nonce. Install
        the returned immutable object into both service.profile/cloud.profile
        while retaining those guards. Never roll an upgraded policy backward.
        """
        _feature_name(feature)
        if Path(store.root).resolve() != self._root:
            raise _error('PROFILE_FEATURE_AUTHORITY_CHANGED')
        with _locked(self._root) as path:
            record = self._record(path)
            if record['mode'] != 'hosted' or record['binding'] is None:
                raise _error('PROFILE_FEATURE_UNAVAILABLE')
            _feature_session(current_session_facts, record['binding']['tenant_id'])
            if feature == INTAKE_STOCK_OPERATIONS_FEATURE and not _has_intake_projection_feature(record):
                raise _error('PROFILE_FEATURE_DEPENDENCY_REQUIRED')
            database = self._root / 'station.sqlite3'
            if _present(database):
                _regular(database)
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
                    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA trusted_schema=OFF')
                    # A cached policy attests its immutable profile identity,
                    # not the current database namespace. Recheck only thin
                    # schema names before any epoch write or resumed DDL.
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND substr(name,1,7) COLLATE NOCASE<>'sqlite_'")}
                    if not tables <= _TABLES:
                        raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA')
                    try:
                        operation_state = _intake_stock_operation_initialization_state(db)
                    except (StationError, ValueError, TypeError, AttributeError):
                        raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA') from None
                    if operation_state not in ('absent', 'present') or (
                            operation_state == 'present' and not _has_intake_stock_operation_feature(record)):
                        raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA')
                    if feature == INTAKE_STOCK_OPERATIONS_FEATURE:
                        try:
                            projection_state = _intake_projection_initialization_state(db)
                        except (StationError, ValueError, TypeError, AttributeError):
                            raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA') from None
                        if projection_state != 'present':
                            raise _error('PROFILE_FEATURE_DEPENDENCY_REQUIRED')
                    if not _feature_enabled(record, feature):
                        selected_tables = {
                            REFERENCE_FENCE_FEATURE: ('receiving_action_reference_fences',),
                            INTAKE_PROJECTION_FEATURE: _INTAKE_PROJECTION_TABLES,
                            INTAKE_STOCK_OPERATIONS_FEATURE: _INTAKE_STOCK_OPERATION_TABLES}[feature]
                        for table in selected_tables:
                            if db.execute('SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1', (table,)).fetchone():
                                raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA')
                        if feature == INTAKE_PROJECTION_FEATURE:
                            try:
                                if _intake_projection_initialization_state(db) != 'absent':
                                    raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA')
                            except (StationError, ValueError, TypeError, AttributeError):
                                raise _error('PROFILE_FEATURE_PRE_EPOCH_METADATA') from None
            elif feature == INTAKE_STOCK_OPERATIONS_FEATURE:
                raise _error('PROFILE_FEATURE_DEPENDENCY_REQUIRED')
            if not _feature_enabled(record, feature):
                selected_epochs = {
                    REFERENCE_FENCE_FEATURE: _REFERENCE_FEATURE_EPOCHS,
                    INTAKE_PROJECTION_FEATURE: _INTAKE_PROJECTION_FEATURE_EPOCHS,
                    INTAKE_STOCK_OPERATIONS_FEATURE: _INTAKE_STOCK_OPERATION_FEATURE_EPOCHS}[feature]
                record = dict(record, hosted_features={**record.get('hosted_features', {}), **selected_epochs})
                _write(path, record)
            # If this fails, the epoch may already be visible: stop before SQL,
            # preserve it, then explicitly reload/resume. Do not restore v1.
            _fsync_policy_target(path)
        # Module imports are complete before caller guards. No generic callback
        # or unrelated schema operation is admitted here.
        if feature == REFERENCE_FENCE_FEATURE:
            _initialize_reference_fences(store)
        elif feature == INTAKE_PROJECTION_FEATURE:
            _initialize_intake_projections(store)
        else:
            _initialize_intake_stock_operations(store)
        with _locked(self._root) as path:
            latest = _load(path)
            if _canonical(latest) != _canonical(record):
                raise _error('PROFILE_POLICY_CHANGED')
            _feature_session(current_session_facts, latest['binding']['tenant_id'])
            identity = _canonical({key: value for key, value in latest.items() if key != 'binding'})
        return ProfilePolicy(latest['mode'], self._root, self._settings, identity)

    @_safe_errors
    def binding(self):
        with _locked(self._root) as path:
            value = self._record(path)['binding']
            return dict(value) if value else None

    @_safe_errors
    def bind_verified_store(self, tenant_id, service_origin):
        """Caller must supply authenticated server facts, never browser inputs."""
        try:
            _uuid(tenant_id)
            if service_origin != SERVICE_ORIGIN:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise _error('PROFILE_STORE_IDENTITY_INVALID') from None
        with _locked(self._root) as path:
            record = self._record(path)
            if record['mode'] != 'hosted':
                raise _error('PROFILE_LEGACY_BINDING_DENIED')
            binding = {'tenant_id': tenant_id, 'service_origin': service_origin}
            if record['binding'] is not None:
                if record['binding'] != binding:
                    raise ProfilePolicyError('This profile belongs to another store. Open a separate profile; saved work cannot be transferred by signing in.',
                                             'PROFILE_STORE_MISMATCH')
            else:
                legacy, _ = _facts(self._root, self._settings)
                if legacy:
                    raise _error('PROFILE_POLICY_CONFLICT')
                record['binding'] = binding
                _write(path, record)
            return dict(binding)


def initialize_profile(data_home, settings_file=None, *, store_root=None):
    """Classify once. Never infer legacy authority from an empty database alone."""
    try:
        home = Path(data_home)
        settings = Path(settings_file) if settings_file is not None else None
        root = Path(store_root) if store_root is not None else home / 'station_data'
        if not home.is_absolute() or not root.is_absolute() or (settings is not None and not settings.is_absolute()):
            raise _error()
        home, root = home.resolve(), root.resolve()
        if root not in (home, home / 'station_data'):
            raise _error()
        if settings is not None:
            settings = settings.resolve()
            if settings.parent != home:
                raise _error()
        with _locked(root) as path:
            if _present(path):
                record = _load(path)
                if record['mode'] == 'hosted':
                    legacy, _ = _facts(root, settings, reference_features=_has_reference_feature(record),
                                       intake_projection_features=_has_intake_projection_feature(record),
                                       intake_stock_operation_features=_has_intake_stock_operation_feature(record))
                    if legacy:
                        raise _error('PROFILE_POLICY_CONFLICT')
            else:
                legacy, cloud = _facts(root, settings)
                if cloud and not legacy:
                    # Existing pre-policy bytes carry only mutable slugs. The
                    # first new sign-in cannot safely attest their tenant UUID.
                    raise _error()
                record = {'format_version': FORMAT_VERSION, 'profile_id': str(uuid.uuid4()),
                    'mode': 'legacy_compatibility' if legacy else 'hosted',
                    'created_at': datetime.now(timezone.utc).isoformat(),
                    'evidence': sorted(legacy) if legacy else ['empty_or_drafts'], 'binding': None}
                _write(path, record)
            identity = _canonical({key: value for key, value in record.items() if key != 'binding'})
            return ProfilePolicy(record['mode'], root, settings, identity)
    except ProfilePolicyError:
        raise
    except (OSError, ValueError, TypeError, sqlite3.Error):
        raise _error() from None
