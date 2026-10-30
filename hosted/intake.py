"""Central physical-delivery claims; no provider calls or inferred old receipts.

A source fingerprint detects duplicate evidence, not physical identity. Stores
must use one shared batch reference for a physical delivery. Repeated identical
deliveries require a linked, fresh-MFA manager attestation.
"""
import re
import uuid
from datetime import datetime
from psycopg.types.json import Jsonb
from intake_manifest import validate_manifest, manifest_digest, semantic_fingerprint, source_lines, ManifestError
from .database import audit
from .security import ServiceError
from .settlement import evidence_hash


class IntakeError(ServiceError):
    def __init__(self, message, status=409, *, code='PHYSICAL_INTAKE_CONFLICT', physical_intake_id=None):
        super().__init__(message, status)
        self.code = code
        self.physical_intake_id = str(physical_intake_id) if physical_intake_id else None


def _uuid(value, label='Physical intake ID'):
    try:
        if not isinstance(value, (str, uuid.UUID)):raise ValueError()
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):raise ServiceError(label + ' is invalid.') from None


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', value):
        raise ServiceError('A stable intake request key is required.')
    return value


def current_authority(db, actor, *, fresh_mfa=False, allowed_roles=('owner', 'manager')):
    """Lock staff before security/session rows, serializing role and MFA changes.

    Actor MFA booleans are never authority. The live session must belong to this
    staff/store and current MFA version; deletion/revocation waits for this txn.
    """
    tenant, staff_id = actor['tenant_id'], actor['staff_id']
    staff = db.execute('SELECT role,enabled FROM ss_staff WHERE tenant_id=%s AND id=%s FOR SHARE',
                       (tenant, staff_id)).fetchone()
    if not staff or not staff['enabled'] or staff['role'] not in allowed_roles or staff['role'] != actor.get('role'):
        raise ServiceError('Your current store role cannot perform this intake action.', 403)
    mfa = db.execute('SELECT enabled,version FROM ss_staff_mfa WHERE tenant_id=%s AND staff_id=%s FOR SHARE',
                     (tenant, staff_id)).fetchone()
    session = db.execute('''SELECT *,clock_timestamp() AS checked_at FROM ss_sessions
                            WHERE tenant_id=%s AND staff_id=%s AND token_hash=%s AND expires_at>clock_timestamp() FOR SHARE''',
                         (tenant, staff_id, actor.get('session_hash', ''))).fetchone()
    if not session:
        raise ServiceError('Your session expired. Sign in again.', 401)
    if mfa and mfa['enabled'] and (session['mfa_version'] != mfa['version'] or not session['mfa_authenticated_at']):
        raise ServiceError('Your session changed. Sign in again.', 401)
    if fresh_mfa:
        at = session['mfa_authenticated_at']
        if (not mfa or not mfa['enabled'] or session['mfa_version'] != mfa['version'] or not at or
                not 0 <= (session['checked_at'] - at).total_seconds() <= 900):
            raise IntakeError('Enroll MFA and sign in with a fresh second-factor proof before accepting or attesting a delivery.',
                              403, code='MFA_STEP_UP_REQUIRED')
    return session


def _claim_result(row):
    fields = ('id', 'version', 'state', 'physical_reference', 'manifest_hash', 'semantic_hash',
              'source_manifest', 'bound_trade_id', 'additional_delivery_of', 'created_at', 'consumed_at')
    result = {key: str(row[key]) if isinstance(row[key], uuid.UUID) else row[key].isoformat()
              if isinstance(row[key], datetime) else row[key] for key in fields}
    result['physical_intake_id'] = result.pop('id')
    result['policy'] = {'one_operation_per_claim': True, 'new_acceptance_requires_fresh_mfa': True,
                        'excluded_rows_reusable': False, 'physical_identity_proven_by_hash': False}
    return result


def validate_claim_lines(claim, lines):
    try:
        available = {line['source_line_key']: line['quantity'] for line in source_lines(claim['source_manifest'])}
    except ManifestError:
        raise IntakeError('Stored original intake evidence is inconsistent and requires review.', physical_intake_id=claim['id']) from None
    used = {}
    for line in lines:
        key = line.get('source_line_key')
        if key not in available:
            raise IntakeError('Every reviewed line must retain its original immutable source-line binding.',
                              428, code='SOURCE_MANIFEST_REQUIRED', physical_intake_id=claim['id'])
        used[key] = used.get(key, 0) + line['quantity']
        if used[key] > available[key]:
            raise IntakeError('Reviewed quantities exceed the original physical intake manifest.', physical_intake_id=claim['id'])
    return used


def claim_for_trade(db, tenant, claim_id, manifest_hash, lines, *, lock=False):
    if not claim_id:
        raise IntakeError('Create or recover the central physical intake claim before accepting this trade. Legacy receipts without an original manifest need reimport/review.',
                          428, code='PHYSICAL_INTAKE_CLAIM_REQUIRED')
    row = db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s' + (' FOR UPDATE' if lock else ' FOR SHARE'),
                     (tenant, _uuid(claim_id))).fetchone()
    if not row:raise ServiceError('Physical intake claim not found.', 404)
    if row['manifest_hash'] != manifest_hash:
        raise IntakeError('The original manifest binding changed. Recover the central claim before proceeding.', physical_intake_id=row['id'])
    try:
        if manifest_digest(row['source_manifest']) != row['manifest_hash'] or semantic_fingerprint(row['source_manifest']) != row['semantic_hash']:
            raise ManifestError('Stored binding mismatch.')
    except ManifestError:
        raise IntakeError('Stored original intake evidence is inconsistent and requires review.', physical_intake_id=row['id']) from None
    validate_claim_lines(row, lines)
    return row


class IntakeClaims:
    def __init__(self, database):self.database = database

    def create(self, actor, data):
        allowed = {'request_key', 'physical_reference', 'source_manifest', 'physical_delivery_verified',
                   'additional_delivery_of', 'additional_delivery_confirmed', 'additional_delivery_reason'}
        if not isinstance(data, dict) or set(data) - allowed:raise ServiceError('Unsupported physical intake fields.')
        key = _key(data.get('request_key'))
        reference = data.get('physical_reference')
        if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._:/-]{2,99}', reference.strip()):
            raise ServiceError('Use a shared physical batch reference of 3 to 100 letters, digits, spaces or ._:/-.')
        reference = reference.strip().casefold()
        if data.get('physical_delivery_verified') is not True:
            raise ServiceError('Confirm this reference identifies the physical delivery shared by all store computers.')
        try:manifest = validate_manifest(data.get('source_manifest'))
        except ManifestError:
            raise IntakeError('An intact original intake manifest is required. Reimport or review a legacy receipt; do not reconstruct one from edited cards.',
                              428, code='SOURCE_MANIFEST_REQUIRED') from None
        manifest_hash, semantic = manifest_digest(manifest), semantic_fingerprint(manifest)
        additional = _uuid(data['additional_delivery_of']) if data.get('additional_delivery_of') else None
        reason = data.get('additional_delivery_reason', '')
        if additional:
            if (data.get('additional_delivery_confirmed') is not True or not isinstance(reason, str) or
                    not 10 <= len(reason.strip()) <= 200 or any(ord(c)<32 or ord(c)==127 for c in reason)):
                raise ServiceError('A separate physical delivery needs explicit confirmation and a 10 to 200 character manager reason.')
            reason = reason.strip()
        elif 'additional_delivery_confirmed' in data or reason:
            raise ServiceError('A separate delivery attestation must link the prior physical intake claim.')
        intent = {'physical_reference': reference, 'source_manifest': manifest, 'physical_delivery_verified': True,
                  'additional_delivery_of': str(additional) if additional else None, 'additional_delivery_reason': reason}
        hashed = evidence_hash(intent);tenant = actor['tenant_id']
        with self.database.transaction() as db:
            current_authority(db, actor)
            # One tenant's short claim allocation lane also serializes raw/semantic
            # cross-index collisions; no HTTP or provider operation occurs here.
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (str(tenant) + ':physical-intake',))
            old = db.execute('SELECT * FROM ss_intake_requests WHERE tenant_id=%s AND request_key=%s', (tenant, key)).fetchone()
            if old:
                if old['intent_hash'] != hashed:raise IntakeError('This intake request key already has different immutable evidence.')
                return old['result']
            same_ref = db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND physical_reference=%s', (tenant, reference)).fetchone()
            matches = db.execute('''SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND additional_delivery_of IS NULL
                                    AND (semantic_hash=%s OR (%s::text IS NOT NULL AND raw_csv_sha256=%s)) ORDER BY created_at,id''',
                                 (tenant, semantic, manifest.get('raw_csv_sha256'), manifest.get('raw_csv_sha256'))).fetchall()
            if same_ref:
                if same_ref['manifest_hash'] != manifest_hash or same_ref['additional_delivery_of'] != additional:
                    raise IntakeError('That shared physical batch reference is already bound to different evidence.', physical_intake_id=same_ref['id'])
                result = _claim_result(same_ref)
            elif additional:
                current_authority(db, actor, fresh_mfa=True)
                prior = db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s FOR UPDATE', (tenant, additional)).fetchone()
                if not prior:raise ServiceError('Prior physical intake claim not found.', 404)
                root = prior['additional_delivery_of'] or prior['id']
                if not matches or any(row['id'] != root for row in matches) or prior['semantic_hash'] != semantic:
                    raise IntakeError('A separate identical delivery must link the matching original intake family.', physical_intake_id=prior['id'])
                result = self._insert(db, actor, reference, manifest, manifest_hash, semantic, root, reason)
            elif matches:
                first = matches[0]
                if len(matches) != 1 or first['manifest_hash'] != manifest_hash:
                    raise IntakeError('This original or equivalent source already has a central claim. Recover it, or attest a separate physical delivery with fresh MFA.', physical_intake_id=first['id'])
                result = _claim_result(first)
            else:
                result = self._insert(db, actor, reference, manifest, manifest_hash, semantic, None, '')
            db.execute('INSERT INTO ss_intake_requests(tenant_id,request_key,intent_hash,claim_id,result) VALUES (%s,%s,%s,%s,%s)',
                       (tenant, key, hashed, result['physical_intake_id'], Jsonb(result)))
            return result

    @staticmethod
    def _insert(db, actor, reference, manifest, hashed, semantic, additional, reason):
        ident = uuid.uuid4()
        row = db.execute('''INSERT INTO ss_intake_claims(id,tenant_id,physical_reference,source_manifest,manifest_hash,semantic_hash,
                            raw_csv_sha256,additional_delivery_of,additional_delivery_reason,created_by)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                         (ident, actor['tenant_id'], reference, Jsonb(manifest), hashed, semantic,
                          manifest.get('raw_csv_sha256'), additional, reason, actor['staff_id'])).fetchone()
        audit(db, actor['tenant_id'], actor['staff_id'], 'physical-intake-created', ident,
              {'manifest_hash': hashed, 'additional_delivery_of': str(additional) if additional else None})
        return _claim_result(row)

    def get(self, actor, ident):
        with self.database.transaction() as db:
            current_authority(db, actor)
            row = db.execute('SELECT * FROM ss_intake_claims WHERE tenant_id=%s AND id=%s', (actor['tenant_id'], _uuid(ident))).fetchone()
            if not row:raise ServiceError('Physical intake claim not found.', 404)
            return _claim_result(row)

    def request_status(self, actor, key):
        key = _key(key)
        with self.database.transaction() as db:
            current_authority(db, actor)
            row = db.execute('SELECT result FROM ss_intake_requests WHERE tenant_id=%s AND request_key=%s', (actor['tenant_id'], key)).fetchone()
            return {'found': bool(row), 'result': row['result'] if row else None}
