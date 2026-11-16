"""Local token-age policy and a disabled-by-default background entry point.

No scheduler, thread, route or merchant operation is registered here. A future
approved scheduler may call bounded batches outside request handling. Due at six
days leaves margin within Square's seven-day recommendation; it is not proof of
scheduled execution. Age is time since a successful local token publication,
not inferred provider issue time, expiry minus thirty days, or permission proof.
https://developer.squareup.com/docs/oauth-api/best-practices
"""
from datetime import datetime, timedelta, timezone

from .security import ServiceError


def renewal_health(row, application_id, *, now=None):
    now = now or datetime.now(timezone.utc)
    obtained = row.get('token_obtained_at') if row else None
    known = (isinstance(obtained, datetime) and obtained.utcoffset() is not None and obtained <= now)
    stale = bool(known and now - obtained > timedelta(days=8))
    age_due = bool(known and now - obtained >= timedelta(days=6))
    expires = row.get('expires_at') if row else None
    expired = bool(isinstance(expires, datetime) and expires.utcoffset() is not None and expires <= now)
    near_expiry = bool(isinstance(expires, datetime) and expires.utcoffset() is not None and expires <= now + timedelta(days=7))
    saved_app = row.get('oauth_application_id') if row else None
    binding = 'unknown' if saved_app is None else 'matched' if saved_app == application_id else 'mismatch'
    return {'age_status': 'unknown' if not known else 'stale' if stale else 'due' if age_due else 'fresh',
            'due': bool(row and (not known or age_due or near_expiry or binding == 'unknown')),
            'stale': stale, 'expired': expired,
            'last_obtained_at': obtained.isoformat() if known else None,
            'application_binding': binding, 'automatic_renewal_active': False}


class SquareRenewal:
    """Private background batch interface; default construction does no work.

    There is no automatic retry or scheduling/health claim. The shared durable
    lease bounds overlapping foreground/background attempts across replicas.
    Failed and corrupt rows receive cooldown before I/O/decryption and move
    behind never/least-recently-attempted rows rather than starving LIMIT.
    Results contain safe operational metadata, never credentials or grant proof.
    """
    def __init__(self, square, *, enabled=False):
        self.square = square
        self.enabled = enabled is True

    def run_once(self, *, limit=25):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ServiceError('Select a renewal batch size between 1 and 100.')
        if not self.enabled:
            return {'status': 'disabled', 'attempted': 0, 'renewed': 0, 'failed': 0, 'results': []}
        context = self.square._token_context()
        with self.square.database.transaction() as db:
            rows = db.execute('''SELECT tenant_id FROM ss_square_connections
                WHERE environment=%s AND revoked_at IS NULL AND pending_notice_cursor IS NULL
                  AND (oauth_application_id IS NULL OR oauth_application_id=%s)
                  AND (renewal_lease_until IS NULL OR renewal_lease_until<=clock_timestamp())
                  AND (renewal_retry_after IS NULL OR renewal_retry_after<=clock_timestamp())
                  AND (oauth_application_id IS NULL OR token_obtained_at IS NULL
                       OR token_obtained_at<=clock_timestamp()-interval '6 days'
                       OR token_obtained_at>clock_timestamp()
                       OR expires_at<=clock_timestamp()+interval '7 days')
                ORDER BY renewal_attempted_at ASC NULLS FIRST,tenant_id LIMIT %s''',
                (context.environment, context.application_id, limit)).fetchall()
        results = []
        for row in rows:
            try:
                self.square._require_token_context(context)
                results.append({'tenant_id': str(row['tenant_id']), **self.square.renew(row['tenant_id'])})
            except ServiceError as error:
                results.append({'tenant_id': str(row['tenant_id']), 'status': 'failed', 'error_status': error.status})
        failed = sum(item['status'] == 'failed' for item in results)
        return {'status': 'completed' if not failed else 'completed_with_failures', 'attempted': len(results),
                'renewed': sum(item['status'] == 'renewed' for item in results), 'failed': failed, 'results': results}
