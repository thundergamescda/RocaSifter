"""Typed inventory-only request recovery through the existing encrypted CAS.

Call all methods outside SQL/service/session publication guards. Every journal
change is a detached full-row merge; the existing storage CAS retains the final
encrypted predecessor/current-session check. An absent lookup never permits POST.
"""
import copy
import math
import re
from urllib.parse import quote

from hosted_labels import fingerprint
from station_cloud import CloudError
from station_store import StationError
from station_intake_stock_action_codec import (canonical, validate_intent, validate_operation)


ROW_FIELDS = frozenset(('kind', 'store', 'receipt_id', 'receiving_id', 'request_key', 'body',
                        'label_profile', 'fingerprint', 'created_at'))


def _error(message='Keep this exact original stock action and request key for review.', *,
           code='INTAKE_STOCK_ACTION_INVALID', status=409):
    return CloudError(message, status=status, pending=True, code=code)


def _same(left, right):
    try:
        return canonical(left) == canonical(right)
    except StationError:
        raise _error() from None


def _valid_intent(intent):
    try:
        validate_intent(intent)
    except StationError:
        raise _error() from None


def validate_result(result, intent):
    """Known storage schema/original binding only; never fresh proof authority."""
    _valid_intent(intent)
    try:
        validate_operation(result)
        fields = {'receiving_id': 'receivingId', 'revision': 'serverRevision', 'plan_hash': 'planHash',
                  'physical_intake_id': 'physicalIntakeId', 'physical_manifest_hash': 'physicalManifestHash',
                  'source_snapshot_hash': 'sourceSnapshotHash', 'provider': 'provider',
                  'client_label_profile': 'labelProfile'}
        if any(not _same(result[left], intent[right]) for left, right in fields.items()):
            raise ValueError()
    except (StationError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise _error('The saved stock action result does not match its exact original operation.',
                     code='INTAKE_STOCK_ACTION_RESULT_INVALID', status=502) from None


class IntakeStockActionTransport:
    def __init__(self, cloud):
        self.cloud = cloud

    def _session(self, saved, intent, expected):
        if (type(saved) is not dict or saved.get('session_id') != expected
                or saved.get('tenant_id') != intent['tenantId'] or saved.get('store') != intent['store']
                or saved.get('role') not in ('owner', 'manager')
                or type(saved.get('expires_at')) not in (int, float)
                or not math.isfinite(saved['expires_at']) or saved['expires_at'] <= self.cloud.clock()):
            raise _error('The signed-in store changed; saved stock actions were kept.',
                         code='INTAKE_STOCK_ACTION_SESSION_CHANGED', status=409)

    def _authority(self, intent, expected):
        _valid_intent(intent)
        if type(expected) is not str or not re.fullmatch('[a-f0-9]{32}', expected):
            raise _error(code='INTAKE_STOCK_ACTION_SESSION_CHANGED')
        if self.cloud._profile_snapshot().get('profileMode') != 'hosted':
            raise _error(code='INTAKE_STOCK_ACTION_SESSION_CHANGED')
        authority = self.cloud.record_authority(expected_session_id=expected)
        saved = self.cloud._saved()
        self.cloud._expected(saved, expected)
        self._session(saved, intent, expected)
        if (type(authority) is not dict or authority.get('sessionId') != expected
                or authority.get('tenantId') != intent['tenantId'] or authority.get('store') != intent['store']
                or authority.get('role') != saved['role'] or authority.get('role') not in ('owner', 'manager')):
            raise _error('A current owner or manager of this bound store must review this action.',
                         code='INTAKE_STOCK_ACTION_AUTHORITY_CHANGED', status=403)
        if type(saved.get('token')) is not str or not saved['token']:
            raise _error(code='INTAKE_STOCK_ACTION_SESSION_CHANGED')
        return saved

    def _row(self, row, intent):
        fields = ROW_FIELDS | ({'result', 'result_fingerprint'} if type(row) is dict and 'result' in row else set())
        if (type(row) is not dict or set(row) != fields or row['kind'] != intent['kind']
                or row['store'] != intent['store'] or row['request_key'] != intent['requestKey']
                or row['receipt_id'] != intent['stockReceiptId'] or row['receiving_id'] != intent['receivingId']
                or not _same(row['body'], intent['body']) or not _same(row['label_profile'], intent['labelProfile'])
                or row['fingerprint'] != fingerprint({'body': intent['body'], 'label_profile': intent['labelProfile']})
                or type(row['created_at']) not in (int, float) or not math.isfinite(row['created_at'])
                or row['created_at'] < 0):
            raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
        if 'result' in row:
            validate_result(row['result'], intent)
            if row['result_fingerprint'] != fingerprint(row['result']):
                raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
        return row

    def _find(self, rows, intent):
        matches = [row for row in rows if row.get('request_key') == intent['requestKey']]
        if len(matches) > 1:
            raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
        return self._row(matches[0], intent) if matches else None

    def _snapshot(self, intent):
        # snapshot decrypts/validates the envelope without a file/session guard.
        # Unreadable/opaque storage propagates; it never becomes an empty list.
        _, rows = self.cloud.receiving.storage.snapshot()
        row = self._find(rows, intent)
        return copy.deepcopy(row)

    def _unchanged(self, intent, expected_row):
        current = self._snapshot(intent)
        if not _same(current, expected_row):
            raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
        return current

    def _stage(self, intent, expected, expected_row):
        created = self.cloud.clock()
        if type(created) not in (int, float) or not math.isfinite(created) or created < 0:
            raise _error()
        proposed = {'kind': intent['kind'], 'store': intent['store'], 'receipt_id': intent['stockReceiptId'],
            'receiving_id': intent['receivingId'], 'request_key': intent['requestKey'],
            'body': copy.deepcopy(intent['body']), 'label_profile': copy.deepcopy(intent['labelProfile']),
            'fingerprint': fingerprint({'body': intent['body'], 'label_profile': intent['labelProfile']}),
            'created_at': created}
        self._row(proposed, intent)
        captured = copy.deepcopy(expected_row)

        def merge(rows, saved):
            self._session(saved, intent, expected)
            current = self._find(rows, intent)
            if not _same(current, captured):
                raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
            if current is not None:
                return rows, copy.deepcopy(current)
            rows.append(copy.deepcopy(proposed))
            return rows, copy.deepcopy(proposed)

        return self.cloud.receiving.storage.update(merge, expected)

    def _remember(self, intent, expected, result, expected_row):
        validate_result(result, intent)
        result = copy.deepcopy(result); hashed = fingerprint(result)
        captured = copy.deepcopy(expected_row)
        if captured is None:
            raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
        self._row(captured, intent)

        def merge(rows, saved):
            self._session(saved, intent, expected)
            current = self._find(rows, intent)
            if current is None or not _same(current, captured):
                raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
            if 'result' in current:
                if current['result_fingerprint'] != hashed or not _same(current['result'], result):
                    raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
                return rows, copy.deepcopy(current)
            current.update(result=copy.deepcopy(result), result_fingerprint=hashed)
            return rows, copy.deepcopy(current)

        return self.cloud.receiving.storage.update(merge, expected)

    def lookup(self, intent, expected, *, persist=False):
        _valid_intent(intent)
        intent = copy.deepcopy(intent)
        if type(persist) is not bool:
            raise _error()
        saved = self._authority(intent, expected)
        row = self._snapshot(intent)
        if row is not None and 'result' in row:
            result = copy.deepcopy(row['result']); resolution = 'journal'
        else:
            response = self.cloud._call('GET', '/api/receiving/requests/' + quote(intent['requestKey'], safe=''), token=saved['token'])
            if type(response) is not dict or set(response) != {'found', 'result'} or type(response['found']) is not bool:
                raise _error(code='INTAKE_STOCK_ACTION_RESULT_INVALID', status=502)
            if not response['found']:
                if response['result'] is not None:
                    raise _error(code='INTAKE_STOCK_ACTION_RESULT_INVALID', status=502)
                self._authority(intent, expected)
                row = self._unchanged(intent, row)
                return {'found': False, 'result': None, 'result_fingerprint': None,
                        'resolution': 'not_found', 'journal_row': row}
            result = response['result']; validate_result(result, intent); resolution = 'request_lookup'
            if persist:
                row = self._stage(intent, expected, row)
                row = self._remember(intent, expected, result, row)
        self._authority(intent, expected)
        row = self._unchanged(intent, row)
        return {'found': True, 'result': copy.deepcopy(result), 'result_fingerprint': fingerprint(result),
                'resolution': resolution, 'journal_row': row}

    def perform(self, intent, expected, *, dispatch_confirmed, before_dispatch):
        _valid_intent(intent)
        intent = copy.deepcopy(intent)
        if dispatch_confirmed is not True or not callable(before_dispatch):
            raise _error('Explicitly confirm this exact saved stock action before dispatch.')
        found = self.lookup(intent, expected, persist=True)
        if found['found']:
            return {key: found[key] for key in ('result', 'result_fingerprint', 'resolution', 'journal_row')}
        row = self._stage(intent, expected, found['journal_row'])
        saved = self._authority(intent, expected)
        path = '/api/receiving/' + intent['receivingId'] + '/' + intent['kind']
        body = copy.deepcopy(intent['body']); token = saved['token']
        # No journal work, remote read or other operation after this callback
        # returns and before the exact original POST begins.
        before_dispatch()
        try:
            result = self.cloud._call('POST', path, token=token, data=body)
            validate_result(result, intent)
            row = self._remember(intent, expected, result, row)
        except CloudError as error:
            error.pending = True
            raise
        self._authority(intent, expected)
        row = self._unchanged(intent, row)
        return {'result': copy.deepcopy(result), 'result_fingerprint': fingerprint(result),
                'resolution': 'dispatched', 'journal_row': row}

    def acknowledge(self, intent, expected, *, applied_result_fingerprint, expected_journal_row):
        _valid_intent(intent)
        intent = copy.deepcopy(intent)
        self._authority(intent, expected)
        if type(applied_result_fingerprint) is not str or not re.fullmatch('[a-f0-9]{64}', applied_result_fingerprint):
            raise _error('Only an already durable exact applied-result basis can acknowledge this request.')
        if expected_journal_row is not None:
            self._row(expected_journal_row, intent)
        captured = copy.deepcopy(expected_journal_row)
        if captured is not None:
            if 'result' not in captured or captured['result_fingerprint'] != applied_result_fingerprint:
                raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')

        def merge(rows, saved):
            self._session(saved, intent, expected)
            current = self._find(rows, intent)
            if not _same(current, captured):
                raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
            if current is None:
                # Internal caller must have proved durable applied SQL basis;
                # no GET, recreated journal or pending-result inference occurs.
                return rows, None
            if 'result' not in current or current['result_fingerprint'] != applied_result_fingerprint:
                raise _error(code='INTAKE_STOCK_ACTION_JOURNAL_CHANGED')
            return [item for item in rows if item is not current], None

        self.cloud.receiving.storage.update(merge, expected)
        self._authority(intent, expected)
        return {'acknowledged': True, 'requestKey': intent['requestKey']}
