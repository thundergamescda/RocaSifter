"""Bounded Square cleanup reads, never an eligibility or deletion adapter.

Completed pagination is read coverage over an interval, not an atomic snapshot
or proof that stock stayed zero. Nothing here calls Cleanup.observe. A future
caller must check live staff/session and connection authority before collection
and again before publishing or persisting the result.

Catalog query contract: https://developer.squareup.com/docs/catalog-api/search-catalog-objects
"""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import time

from .receiving_square import ReceivingSquare
from .security import ServiceError, provider_id


_MESSAGE = ('Cleanup read coverage is incomplete. Retry the complete read; '
            'no cleanup eligibility was established.')
_READS = frozenset((('GET', '/v2/locations'),
                   ('POST', '/v2/catalog/search'),
                   ('POST', '/v2/inventory/counts/batch-retrieve')))
_PAGE_SIZE = 1000
_BATCH_SIZE = 100
_MAX_JSON_DEPTH = 64
_QUANTITY = re.compile(r'[+-]?[0-9]+(?:\.[0-9]{1,5})?\Z')
_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                        r'(?:\.[0-9]{1,9})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])\Z')


def _incomplete():
    return ServiceError(_MESSAGE, 502)


def _id(value):
    try:
        result = provider_id(value)
    except ServiceError:
        raise _incomplete() from None
    if result.startswith('#'):
        raise _incomplete()
    return result


def _cursor(result, seen):
    value = result.get('cursor')
    if value is None or (type(value) is str and value == ''):
        return None
    if (type(value) is not str or not 1 <= len(value) <= 4096 or
            value in seen or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise _incomplete()
    seen.add(value)
    return value


def _timestamp(value):
    if type(value) is not str or not 1 <= len(value) <= 64 or not _TIMESTAMP.fullmatch(value):
        raise _incomplete()
    try:
        at = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if at.tzinfo is None or at.utcoffset() is None:
            raise _incomplete()
        at.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise _incomplete() from None


class _ReadBudget:
    def __init__(self, adapter):
        self.deadline = time.monotonic() + adapter.deadline_seconds
        self.max_pages = adapter.max_pages
        self.max_records = adapter.max_records
        self.max_bytes = adapter.max_bytes
        self.pages = self.records = self.bytes = 0

    def check(self):
        if time.monotonic() >= self.deadline:
            raise _incomplete()

    def request(self):
        self.check()
        if self.pages >= self.max_pages:
            raise _incomplete()
        self.pages += 1

    def response(self, result):
        self.check()
        # Keep later canonicalization/deepcopy within a bounded nesting depth.
        # Iterators bound traversal memory even for a wide decoded JSON array.
        stack = [iter((result,))]
        while stack:
            try:
                value = next(stack[-1])
            except StopIteration:
                stack.pop()
                continue
            if type(value) in (dict, list):
                if len(stack) > _MAX_JSON_DEPTH:
                    raise _incomplete()
                stack.append(iter(value.values() if type(value) is dict else value))
        try:
            size = len(json.dumps(result, ensure_ascii=False, sort_keys=True,
                                  separators=(',', ':'), allow_nan=False).encode('utf-8'))
        except (ValueError, TypeError, OverflowError, RecursionError):
            raise _incomplete() from None
        self.bytes += size
        if self.bytes > self.max_bytes:
            raise _incomplete()

    def rows(self, result, field):
        values = result.get(field)
        if type(values) is not list or len(values) > _PAGE_SIZE:
            raise _incomplete()
        self.add_records(len(values))
        return values

    def add_records(self, count):
        self.check()
        self.records += count
        if self.records > self.max_records:
            raise _incomplete()


class CleanupSquare:
    """Server-only read facade composed with the existing receiving transport.

max_bytes bounds aggregate canonical decoded JSON. ReceivingSquare separately
bounds every streamed response to 4 MiB; max_pages bounds the number of those
responses. Its decoder has already parsed JSON, so raw duplicate object keys
cannot be detected here. Conflicting/repeated semantic rows are rejected below.
The inherited request timeout/deadline checks are cooperative per operation and
chunk, not a hard wall-clock guarantee while blocking I/O is pending.
"""
    def __init__(self, square, *, max_pages=100, max_records=100000,
                 max_bytes=16 * 1024 * 1024, deadline_seconds=60):
        if (type(max_pages) is not int or not 1 <= max_pages <= 1000 or
                type(max_records) is not int or not 1 <= max_records <= 1000000 or
                type(max_bytes) is not int or not 1 <= max_bytes <= 128 * 1024 * 1024 or
                type(deadline_seconds) not in (int, float) or
                not 0 < deadline_seconds <= 300 or not math.isfinite(deadline_seconds)):
            raise ValueError('Invalid cleanup read budgets.')
        self._reader = ReceivingSquare(square)
        self.max_pages = max_pages
        self.max_records = max_records
        self.max_bytes = max_bytes
        self.deadline_seconds = deadline_seconds

    def access(self, tenant):
        # The default receiving read access is exactly READ_SCOPES; no write scope.
        return self._reader.access(tenant)

    def assert_current(self, db, tenant, access):
        return self._reader.assert_current(db, tenant, access)

    @staticmethod
    def _merchant(access):
        try:
            return _id(access.proof['merchant_id'])
        except (AttributeError, KeyError, TypeError):
            raise _incomplete() from None

    @classmethod
    def _merchant_matches(cls, access, value):
        merchant = cls._merchant(access)
        if 'merchant_id' in value and value['merchant_id'] != merchant:
            raise _incomplete()

    def _read(self, access, endpoint, payload, budget, *, method='POST'):
        if (method, endpoint) not in _READS:
            raise _incomplete()
        self._merchant(access)
        budget.request()
        try:
            result = self._reader._call(access, endpoint, payload, method=method,
                                        deadline=budget.deadline)
        except Exception:
            raise _incomplete() from None
        if type(result) is not dict:
            raise _incomplete()
        if 'errors' in result and result['errors'] is not None:
            if type(result['errors']) is not list or result['errors']:
                raise _incomplete()
        budget.response(result)
        self._merchant_matches(access, result)
        return result

    def locations(self, access):
        return self._locations(access, _ReadBudget(self))

    def _locations(self, access, budget):
        result = self._read(access, '/v2/locations', None, budget, method='GET')
        # ListLocations is not a paginated request in this facade.
        if _cursor(result, set()) is not None:
            raise _incomplete()
        found = {}
        for row in budget.rows(result, 'locations'):
            if type(row) is not dict or row.get('status') not in ('ACTIVE', 'INACTIVE'):
                raise _incomplete()
            ident = _id(row.get('id'))
            self._merchant_matches(access, row)
            if ident in found:
                raise _incomplete()
            found[ident] = deepcopy(row)
        if not found:
            raise _incomplete()
        return [found[ident] for ident in sorted(found)]

    def catalog(self, access):
        return self._catalog(access, _ReadBudget(self))

    def _catalog_object(self, access, row):
        if (type(row) is not dict or row.get('type') not in ('ITEM', 'ITEM_VARIATION') or
                type(row.get('version')) is not int or not 1 <= row['version'] < 2**63 or
                ('is_deleted' in row and row['is_deleted'] is not None and row['is_deleted'] is not False)):
            raise _incomplete()
        ident = _id(row.get('id'))
        self._merchant_matches(access, row)
        data = row.get('item_data' if row['type'] == 'ITEM' else 'item_variation_data')
        if type(data) is not dict:
            raise _incomplete()
        if row['type'] == 'ITEM_VARIATION':
            _id(data.get('item_id'))
        return ident

    def _catalog(self, access, budget):
        query = {'object_types': ['ITEM', 'ITEM_VARIATION'],
                 'include_deleted_objects': False, 'limit': _PAGE_SIZE}
        found = {}
        cursors = set()
        while True:
            result = self._read(access, '/v2/catalog/search', query, budget)
            for row in budget.rows(result, 'objects'):
                ident = self._catalog_object(access, row)
                if ident in found:
                    raise _incomplete()
                found[ident] = deepcopy(row)
            cursor = _cursor(result, cursors)
            if cursor is None:
                break
            query = dict(query, cursor=cursor)

        parents = {ident: row for ident, row in found.items() if row['type'] == 'ITEM'}
        variations = {ident: row for ident, row in found.items() if row['type'] == 'ITEM_VARIATION'}
        inline = {}
        for parent_id, parent in parents.items():
            children = parent['item_data'].get('variations')
            if type(children) is not list:
                raise _incomplete()
            budget.add_records(len(children))
            for child in children:
                child_id = self._catalog_object(access, child)
                if (child['type'] != 'ITEM_VARIATION' or
                        child['item_variation_data']['item_id'] != parent_id or child_id in inline):
                    raise _incomplete()
                inline[child_id] = child
        if set(inline) != set(variations):
            raise _incomplete()
        for ident, variation in variations.items():
            if variation['item_variation_data']['item_id'] not in parents or variation != inline[ident]:
                raise _incomplete()
        budget.check()
        return {ident: found[ident] for ident in sorted(found)}

    @staticmethod
    def _ids(values, budget):
        if type(values) not in (list, tuple) or len(values) > budget.max_records:
            raise _incomplete()
        result = [_id(value) for value in values]
        if len(set(result)) != len(result):
            raise _incomplete()
        return sorted(result)

    def counts(self, access, variation_ids, location_ids):
        return self._counts(access, variation_ids, location_ids, _ReadBudget(self))

    def _counts(self, access, variation_ids, location_ids, budget, *, on_count=None):
        self._merchant(access)
        variations = self._ids(variation_ids, budget)
        locations = self._ids(location_ids, budget)
        if (variations and not locations) or len(variations) * len(locations) > budget.max_records:
            raise _incomplete()
        found = {}
        for vstart in range(0, len(variations), _BATCH_SIZE):
            variation_batch = variations[vstart:vstart + _BATCH_SIZE]
            for lstart in range(0, len(locations), _BATCH_SIZE):
                location_batch = locations[lstart:lstart + _BATCH_SIZE]
                query = {'catalog_object_ids': variation_batch, 'location_ids': location_batch,
                         'states': ['IN_STOCK'], 'limit': _PAGE_SIZE}
                cursors = set()
                while True:
                    result = self._read(access, '/v2/inventory/counts/batch-retrieve', query, budget)
                    for row in budget.rows(result, 'counts'):
                        if (type(row) is not dict or row.get('state') != 'IN_STOCK' or
                                ('catalog_object_type' in row and row['catalog_object_type'] != 'ITEM_VARIATION')):
                            raise _incomplete()
                        ident = _id(row.get('catalog_object_id'))
                        location = _id(row.get('location_id'))
                        if ident not in variation_batch or location not in location_batch:
                            raise _incomplete()
                        self._merchant_matches(access, row)
                        if 'is_estimated' in row and type(row['is_estimated']) is not bool:
                            raise _incomplete()
                        if 'calculated_at' in row:
                            _timestamp(row['calculated_at'])
                        quantity = row.get('quantity')
                        if type(quantity) is not str or not 1 <= len(quantity) <= 26 or not _QUANTITY.fullmatch(quantity):
                            raise _incomplete()
                        try:
                            if not Decimal(quantity).is_finite():
                                raise _incomplete()
                        except InvalidOperation:
                            raise _incomplete() from None
                        key = (ident, location)
                        validated = deepcopy(row)
                        if on_count is not None:
                            # A repeated tuple invalidates coverage, but both
                            # individually typed witnesses must survive. In
                            # particular, a later positive cannot be discarded.
                            on_count(deepcopy(validated))
                        if key in found:
                            raise _incomplete()
                        found[key] = validated
                    cursor = _cursor(result, cursors)
                    if cursor is None:
                        break
                    query = dict(query, cursor=cursor)
        missing = [(ident, location) for ident in variations for location in locations
                   if (ident, location) not in found]
        budget.check()
        return {'counts': [found[key] for key in sorted(found)], 'missing': missing}

    def collect(self, access):
        budget = _ReadBudget(self)
        locations = self._locations(access, budget)
        objects = self._catalog(access, budget)
        variations = [ident for ident, row in objects.items() if row['type'] == 'ITEM_VARIATION']
        counts = self._counts(access, variations, [row['id'] for row in locations], budget)
        budget.check()
        return {'locations': locations, 'objects': objects, 'counts': counts['counts'],
                'missing': counts['missing'], 'pagination_complete': True,
                'atomic_snapshot': False, 'continuous_zero_verified': False,
                'deletion_enabled': False}

    @staticmethod
    def _context_hash(locations, objects):
        # Hash complete validated bookends; do not substitute catalog version
        # alone for a provider response that changes while keeping that version.
        payload = json.dumps({'locations': locations, 'objects': objects},
                             ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def collect_attempt(self, access):
        """Bracket one bounded read and retain validated partial count evidence.

        All five phases share the same aggregate resource/deadline budget.
        Equal bookends still do not make the intervening count pages atomic or
        prove gap-free inventory history. No result authorizes deletion.
        A caller must separately fence current grants, connection generation,
        consent, lease and notification highwater before committing any history.
        """
        budget = _ReadBudget(self)
        result = {'coverage_complete': False, 'pagination_complete': False,
                  'context_unchanged': False, 'bracketed': False,
                  'reason_codes': [], 'before_context_hash': None,
                  'after_context_hash': None, 'locations': [], 'objects': {},
                  'counts': [], 'missing': None, 'atomic_snapshot': False,
                  'continuous_zero_verified': False, 'deletion_enabled': False}
        phase = 'locations_incomplete'
        variations = None
        try:
            result['locations'] = self._locations(access, budget)
            phase = 'catalog_incomplete'
            result['objects'] = self._catalog(access, budget)
            result['before_context_hash'] = self._context_hash(result['locations'], result['objects'])
            variations = [ident for ident, row in result['objects'].items()
                          if row['type'] == 'ITEM_VARIATION']
            phase = 'counts_incomplete'
            counts = self._counts(access, variations, [row['id'] for row in result['locations']],
                                  budget, on_count=result['counts'].append)
            result['counts'] = counts['counts']
            result['missing'] = counts['missing']
            phase = 'context_check_incomplete'
            after_locations = self._locations(access, budget)
            after_objects = self._catalog(access, budget)
            result['after_context_hash'] = self._context_hash(after_locations, after_objects)
            budget.check()
            result['pagination_complete'] = result['bracketed'] = True
            result['context_unchanged'] = result['before_context_hash'] == result['after_context_hash']
            if not result['context_unchanged']:
                result['reason_codes'].append('context_changed')
            if result['missing']:
                result['reason_codes'].append('missing_counts')
            result['coverage_complete'] = result['context_unchanged'] and not result['missing']
        except Exception:
            # Never expose provider text, credentials or arbitrary exception data.
            # Rows are appended only after all per-count validation succeeds.
            result['reason_codes'].append(phase)
            if variations is not None and len(variations) * len(result['locations']) <= budget.max_records:
                present = {(row['catalog_object_id'], row['location_id']) for row in result['counts']}
                result['missing'] = [(ident, location['id']) for ident in variations
                                     for location in result['locations'] if (ident, location['id']) not in present]
        if any('calculated_at' not in row for row in result['counts']):
            result['reason_codes'].append('count_timestamps_unknown')
        if any(row.get('is_estimated') is True for row in result['counts']):
            result['reason_codes'].append('estimated_counts')
        if result['reason_codes']:
            result['coverage_complete'] = False
        return result
