"""Authenticated inventory receipts and inert, generation-bound refresh intents.

Square.ingest verifies the exact bytes before calling this module. Receipts keep
every typed count, including nonzero, ITEM, estimated and non-IN_STOCK facts.
Provider timestamp strings retain wire precision; received_at is a separate local
clock. A connection generation is associated AT RECEIPT, not at provider event
time. A delayed first delivery therefore cannot prove historical authorization.

There is deliberately no claim, worker, collection, Cleanup.observe, eligibility
or deletion API. An intent is never rebound or executed here. Any future worker
requires live app/environment/grant/revocation/generation and receipt-highwater
fences at claim AND finish; these rows alone authorize no provider operation.
Missing objects/counts, out-of-order delivery, expired retries and outages cannot
prove continuous observation or continuous zero stock.

Provider contracts (not a guarantee of complete delivery):
https://developer.squareup.com/reference/square/inventory-api/webhooks/inventory.count.updated
https://developer.squareup.com/reference/square/objects/InventoryCount
https://developer.squareup.com/docs/webhooks/overview
"""
from datetime import datetime
import hashlib
import json
import math
import re

from .security import ServiceError, provider_id


MAX_EVENT_BYTES = 512 * 1024
MAX_COUNT_FACTS = 1000
_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                        r'(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})')
_QUANTITY = re.compile(r'-?[0-9]+(?:\.[0-9]{1,5})?')
_STATES = frozenset(('CUSTOM', 'IN_STOCK', 'SOLD', 'RETURNED_BY_CUSTOMER',
    'RESERVED_FOR_SALE', 'SOLD_ONLINE', 'ORDERED_FROM_VENDOR', 'RECEIVED_FROM_VENDOR',
    'IN_TRANSIT_TO', 'NONE', 'WASTE', 'UNLINKED_RETURN', 'COMPOSED', 'DECOMPOSED',
    'SUPPORTED_BY_NEWER_VERSION', 'IN_TRANSIT', 'UNTRACKED'))


def signed_envelope(raw):
    """Decode ONLY after ingress authentication; reject ambiguous JSON routing.

    The common pre-dispatch decoder also protects existing event types from a
    duplicate `type` key routing an inventory payload around strict validation.
    Nothing in the decoder itself authenticates bytes or makes database calls.
    """
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def constant(_):
        raise ValueError()

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return number

    try:
        if type(raw) is not bytes or not 1 <= len(raw) <= MAX_EVENT_BYTES:
            raise ValueError()
        payload = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                             parse_constant=constant, parse_float=finite)
        if type(payload) is not dict:
            raise ValueError()
        pending = [(payload, 0)]
        nodes = 0
        while pending:
            value, depth = pending.pop()
            nodes += 1
            if depth > 24 or nodes > 20000:
                raise ValueError()
            if type(value) is dict:
                pending.extend((child, depth + 1) for child in value.values())
            elif type(value) is list:
                pending.extend((child, depth + 1) for child in value)
        return payload
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ServiceError('Event envelope is invalid.') from None


def _timestamp(value):
    if type(value) is not str or len(value) > 35 or not _TIMESTAMP.fullmatch(value):
        raise ValueError()
    # fromisoformat validates the calendar and offset, while the original string
    # (including all fractional digits) is the evidence stored below.
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None or (not value.endswith('Z') and
            (int(value[-5:-3]) > 23 or int(value[-2:]) > 59)):
        raise ValueError()
    return value


def _object(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or not value.keys() <= set(required) | set(optional):
        raise ValueError()
    return value


def inventory_event(payload):
    """Return detached, strictly typed facts; malformed signed input is a 400.

    A documented absent object and an explicit empty array are retained as gaps,
    not transformed into zero counts. Unsupported/new fields or states reject
    the entire event for explicit failure/retry rather than silently losing facts.
    Duplicate tuples remain ordered facts: a later zero never erases a nonzero.
    """
    try:
        _object(payload, ('event_id', 'merchant_id', 'type', 'created_at', 'data'))
        if payload['type'] != 'inventory.count.updated':
            raise ValueError()
        event = provider_id(payload['event_id'], 'Event ID')
        merchant = provider_id(payload['merchant_id'], 'Merchant ID')
        created = _timestamp(payload['created_at'])
        data = _object(payload['data'], ('type', 'id'), ('object',))
        if data['type'] != 'inventory_counts':
            raise ValueError()
        resource = provider_id(data['id'], 'Resource ID')
        facts = []
        status = 'object_absent'
        if 'object' in data:
            obj = _object(data['object'], ('inventory_counts',))
            rows = obj['inventory_counts']
            if type(rows) is not list or len(rows) > MAX_COUNT_FACTS:
                raise ValueError()
            status = 'counts_present' if rows else 'empty_counts'
            for row in rows:
                _object(row, ('catalog_object_id', 'catalog_object_type', 'location_id',
                              'state', 'quantity', 'calculated_at'), ('is_estimated',))
                object_id = provider_id(row['catalog_object_id'], 'Catalog object ID')
                location = provider_id(row['location_id'], 'Location ID')
                if len(object_id) > 100 or len(location) > 100:
                    raise ValueError()
                if row['catalog_object_type'] not in ('ITEM', 'ITEM_VARIATION') or type(row['catalog_object_type']) is not str:
                    raise ValueError()
                if type(row['state']) is not str or row['state'] not in _STATES:
                    raise ValueError()
                quantity = row['quantity']
                if type(quantity) is not str or not 1 <= len(quantity) <= 26 or not _QUANTITY.fullmatch(quantity):
                    raise ValueError()
                calculated = _timestamp(row['calculated_at'])
                if 'is_estimated' in row and type(row['is_estimated']) is not bool:
                    raise ValueError()
                fact = {'catalog_object_id': object_id, 'catalog_object_type': row['catalog_object_type'],
                        'location_id': location, 'state': row['state'], 'quantity': quantity,
                        'calculated_at': calculated}
                if 'is_estimated' in row:
                    fact['is_estimated'] = row['is_estimated']
                facts.append(fact)
        return {'event_id': event, 'merchant_id': merchant, 'event_type': payload['type'],
                'provider_created_at': created, 'resource_id': resource,
                'payload_status': status, 'facts': facts}
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ServiceError('Inventory event envelope is invalid.') from None


def record_inventory(square, raw, event):
    """Atomically append one authenticated receipt, facts and optional intent.

    Internal ingress entry point: only Square.ingest may supply the authenticated
    raw bytes and freshly validated inventory_event result. No network call occurs
    under these locks. Exact replay uses the ORIGINAL binding/outcome forever.
    """
    application = square.config['SQUARE_APPLICATION_ID']
    environment = square.environment
    fingerprint = hashlib.sha256(raw).hexdigest()
    scope = (application, environment, event['event_id'])
    merchant = event['merchant_id']
    with square.database.transaction() as db:
        square._notice_barrier(db)  # FIRST SQL; READ COMMITTED before snapshot.
        old = db.execute('''SELECT body_hash,refresh_queued FROM ss_cleanup_inventory_receipts
                            WHERE application_id=%s AND environment=%s AND event_id=%s''', scope).fetchone()
        if old:
            if old['body_hash'] != fingerprint:
                raise ServiceError('Event ID conflicts with a previous inventory notification.', 409)
            return {'accepted': True, 'duplicate': True, 'refresh_queued': old['refresh_queued'],
                    'refresh_executed': False}
        peek = db.execute('SELECT tenant_id FROM ss_square_connections WHERE merchant_id=%s', (merchant,)).fetchone()
        if peek:
            db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                       ('square-tenant:' + str(peek['tenant_id']),))
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('square-merchant:' + merchant,))
        connection = db.execute('''SELECT tenant_id,environment,authorization_generation,revoked_at,
                                   pending_notice_cursor,expires_at>clock_timestamp() AS token_unexpired
                                   FROM ss_square_connections WHERE merchant_id=%s FOR UPDATE''', (merchant,)).fetchone()
        if connection and (not peek or connection['tenant_id'] != peek['tenant_id']):
            # A different app/environment can have a different notice barrier.
            # Never acquire a new tenant lock after the merchant lock or bind
            # without the expected tenant lock; the provider may retry this 409.
            raise ServiceError('Square connection changed while receiving inventory notification.', 409)
        tenant = connection['tenant_id'] if connection else None
        generation = connection['authorization_generation'] if connection else None
        if not connection:
            binding = 'unbound'
        elif connection['environment'] != environment:
            binding = 'environment_mismatch'
        elif connection['pending_notice_cursor'] is not None:
            binding = 'revocation_pending'
        elif connection['revoked_at'] is not None:
            binding = 'revoked'
        elif not connection['token_unexpired']:
            binding = 'token_expired'
        else:
            binding = 'active'
        queued = binding == 'active'
        receipt = db.execute('''INSERT INTO ss_cleanup_inventory_receipts
            (application_id,environment,event_id,merchant_id,event_type,body_hash,provider_created_at,
             resource_id,payload_status,fact_count,tenant_id,authorization_generation,binding_status,refresh_queued)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING receipt_cursor''',
            (*scope, merchant, event['event_type'], fingerprint, event['provider_created_at'],
             event['resource_id'], event['payload_status'], len(event['facts']), tenant, generation, binding, queued)).fetchone()
        cursor = receipt['receipt_cursor']
        for ordinal, fact in enumerate(event['facts']):
            db.execute('''INSERT INTO ss_cleanup_inventory_facts
                (receipt_cursor,ordinal,catalog_object_id,catalog_object_type,location_id,state,quantity,
                 provider_calculated_at,is_estimated) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (cursor, ordinal, fact['catalog_object_id'], fact['catalog_object_type'], fact['location_id'],
                 fact['state'], fact['quantity'], fact['calculated_at'], fact.get('is_estimated')))
        if queued:
            db.execute('''INSERT INTO ss_cleanup_refresh_queue
                (receipt_cursor,application_id,environment,merchant_id,tenant_id,authorization_generation,binding_status)
                VALUES (%s,%s,%s,%s,%s,%s,'active')''',
                (cursor, application, environment, merchant, tenant, generation))
        return {'accepted': True, 'duplicate': False, 'refresh_queued': queued, 'refresh_executed': False}
