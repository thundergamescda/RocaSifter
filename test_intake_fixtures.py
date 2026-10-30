"""Explicit fictional fixture approvals; never called by application code."""
from copy import deepcopy
import uuid
import re
from intake_manifest import capture_manifest, source_line_id
from hosted.intake import IntakeClaims


def _isolated(db):
    schema = db.execute('SELECT current_schema() AS schema').fetchone()['schema']
    if not re.fullmatch(r'ss_test_[a-f0-9]{32}', schema or ''):
        raise RuntimeError('Fictional intake approvals require an isolated ss_test_<uuid> schema.')


def fresh_mfa_actor(db, actor, *, existing_session=False):
    """Seed a proof only inside an already isolated, fictional test tenant.

    The seed marker is deliberately unusable for real authentication. These
    domain tests verify authorization transactions, not the separate MFA crypto.
    """
    _isolated(db)
    db.execute('''INSERT INTO ss_staff_mfa(tenant_id,staff_id,enabled,version,encrypted_seed,enabled_at)
                  VALUES (%s,%s,TRUE,1,'fictional-unusable-encrypted-seed',now())
                  ON CONFLICT(tenant_id,staff_id) DO NOTHING''', (actor['tenant_id'], actor['staff_id']))
    if existing_session:
        db.execute('UPDATE ss_sessions SET mfa_version=1,mfa_authenticated_at=now() WHERE tenant_id=%s AND staff_id=%s AND token_hash=%s',
                   (actor['tenant_id'], actor['staff_id'], actor['session_hash']))
        return actor
    result = dict(actor) | {'session_hash': uuid.uuid4().hex + uuid.uuid4().hex}
    db.execute('''INSERT INTO ss_sessions(token_hash,tenant_id,staff_id,expires_at,mfa_version,mfa_authenticated_at)
                  VALUES (%s,%s,%s,now()+interval '1 hour',1,now())''',
               (result['session_hash'], result['tenant_id'], result['staff_id']))
    return result


def claim_draft(database, actor, data):
    """Persist fictional original rows and explicitly attach their stable keys."""
    with database.transaction() as db:_isolated(db)
    value = deepcopy(data)
    manifest = capture_manifest('manual', [{'identity': row['identity'], 'quantity': row['quantity']} for row in value['lines']])
    for line in value['lines']:line['source_line_key'] = source_line_id(line['identity'])
    claim = IntakeClaims(database).create(actor, {'request_key': 'claim-' + value['request_key'],
                'physical_reference': 'fixture-' + (value.get('source_reference') or uuid.uuid4().hex),
                'source_manifest': manifest, 'physical_delivery_verified': True})
    value.update(physical_intake_id=claim['physical_intake_id'], physical_manifest_hash=claim['manifest_hash'])
    return value, claim
