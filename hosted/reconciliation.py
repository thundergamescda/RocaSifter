"""Guarded, leased Square reconciliation worker. Disabled unless explicitly enabled.

Jobs contain resource IDs only. The worker retrieves current provider resources
through the tenant's server OAuth connection and invokes the transactional ledger
domain. It never performs a Square write or releases credit on a timeout.
"""
import argparse
import os
import time
import uuid
from .database import Database
from .security import ServiceError
from .settlement import Settlement, record_exception, _id, _decode_snapshot, plan_order
from .square import Square


class ResourceError(Exception):
    def __init__(self, code, *, transient=False):
        self.code, self.transient = code, transient
        super().__init__(code)


class LeaseLost(Exception):
    pass


class Reconciler:
    def __init__(self, database, square, *, enabled=False, lease_seconds=90, max_attempts=8):
        if type(enabled) is not bool or type(lease_seconds) is not int or not 30 <= lease_seconds <= 600 or type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise ValueError('Invalid reconciliation worker settings.')
        self.database, self.square, self.enabled = database, square, enabled
        self.lease_seconds, self.max_attempts = lease_seconds, max_attempts
        self.settlement = Settlement(database)

    def run_once(self, *, tenant_id=None):
        if not self.enabled:
            return {'status': 'disabled', 'processed': False}
        if not self.square.configured():
            return {'status': 'not_configured', 'processed': False}
        if tenant_id is not None:
            try:tenant_id = uuid.UUID(str(tenant_id))
            except (ValueError, TypeError):raise ValueError('Tenant filter must be a UUID.') from None
        job = self._claim(tenant_id)
        if not job:
            return {'status': 'idle', 'processed': False}
        try:
            if job['attempts'] > self.max_attempts:
                return self._finish(job, 'exception', 'retry_budget_exhausted')
            result = self._process(job)
            if result.get('needs_review'):
                return self._finish(job, 'exception', result.get('code', 'ledger_review_required'))
            if result.get('action') == 'pending' or result.get('state') == 'pending':
                return self._retry(job, result.get('code', 'resource_pending'))
            return self._finish(job, 'done', result.get('code', 'reconciled'))
        except LeaseLost:
            return {'status': 'lease_lost', 'processed': True, 'event_id': job['event_id']}
        except ResourceError as error:
            return self._retry(job, error.code) if error.transient else self._finish(job, 'exception', error.code)
        except ServiceError as error:
            if error.status in (429, 500, 502, 503, 504):
                return self._retry(job, 'provider_or_service_unavailable')
            return self._finish(job, 'exception', 'authorization_or_ledger_review_required')
        except Exception:
            # No raw exception message/SQL parameters/resource bodies in logs.
            return self._retry(job, 'unexpected_reconciliation_failure')

    def _claim(self, tenant_id):
        token = uuid.uuid4()
        tenant_clause = ' AND tenant_id=%s' if tenant_id is not None else ''
        parameters = ([tenant_id] if tenant_id is not None else []) + [token, self.lease_seconds]
        with self.database.transaction() as db:
            return db.execute('''WITH chosen AS (
                    SELECT event_id FROM ss_square_jobs WHERE available_at<=now()
                    AND (state='pending' OR (state='processing' AND (lease_until IS NULL OR lease_until<now())))''' + tenant_clause + '''
                    ORDER BY available_at,created_at,event_id FOR UPDATE SKIP LOCKED LIMIT 1)
                    UPDATE ss_square_jobs j SET state='processing',attempts=j.attempts+1,
                    lease_token=%s,lease_until=now()+(%s * interval '1 second'),finished_at=NULL
                    FROM chosen WHERE j.event_id=chosen.event_id RETURNING j.*''', parameters).fetchone()

    def _heartbeat(self, job):
        with self.database.transaction() as db:
            row = db.execute('''UPDATE ss_square_jobs SET lease_until=now()+(%s * interval '1 second')
                                WHERE event_id=%s AND tenant_id=%s AND lease_token=%s AND state='processing'
                                RETURNING event_id''',
                             (self.lease_seconds, job['event_id'], job['tenant_id'], job['lease_token'])).fetchone()
        if not row:
            raise LeaseLost()

    def _get(self, job, kind, resource_id):
        if not _id(resource_id) or resource_id in ('.', '..'):
            raise ResourceError('resource_reference_invalid')
        self._heartbeat(job)
        result = self.square.retrieve(job['tenant_id'], kind, resource_id)
        self._heartbeat(job)
        key = {'orders': 'order', 'payments': 'payment', 'refunds': 'refund'}[kind]
        resource = result.get(key) if isinstance(result, dict) else None
        if not isinstance(resource, dict) or resource.get('id') != resource_id:
            raise ResourceError('retrieved_resource_mismatch')
        return resource

    def _process(self, job):
        event = job['event_type']
        if event in ('order.created', 'order.updated'):
            return self._order(job, self._get(job, 'orders', job['resource_id']))
        if event in ('payment.created', 'payment.updated'):
            payment = self._get(job, 'payments', job['resource_id'])
            if not payment.get('order_id'):
                return {'state': 'ignored', 'code': 'payment_without_order'}
            return self._order(job, self._get(job, 'orders', payment['order_id']))
        if event in ('refund.created', 'refund.updated'):
            return self._refund(job)
        raise ResourceError('unsupported_reconciliation_event')

    def _order(self, job, order):
        catalog=None;catalog_error=None;terms=None
        with self.database.transaction() as db:
            reservation = db.execute('SELECT id FROM ss_reservations WHERE tenant_id=%s AND order_id=%s',
                                     (job['tenant_id'], order['id'])).fetchone()
            if not reservation:
                store = db.execute('SELECT discount_id FROM ss_tenants WHERE id=%s', (job['tenant_id'],)).fetchone()
                discounts = order.get('discounts', [])
                if (order.get('state') == 'COMPLETED' and store and store['discount_id'] and isinstance(discounts, list) and
                        any(isinstance(item, dict) and item.get('catalog_object_id') == store['discount_id'] for item in discounts)):
                    raise ResourceError('unreserved_credit_discount')
                return {'state': 'ignored', 'code': 'order_has_no_credit_reservation'}
            snapshot=db.execute('SELECT * FROM ss_reservation_terms WHERE tenant_id=%s AND reservation_id=%s',
                                (job['tenant_id'],reservation['id'])).fetchone()
            try:terms,_=_decode_snapshot(snapshot)
            except ServiceError:pass # The atomic adapter persists the specific review condition.
        if terms is not None and plan_order(terms,order).code=='catalog_discount_unverified':
            self._heartbeat(job)
            try:
                response=self.square.retrieve(job['tenant_id'],'catalog',terms.discount_id,
                                              catalog_version=terms.discount_catalog_version)
                catalog=response.get('object',{}) if isinstance(response,dict) else {}
                if not isinstance(catalog,dict):catalog={}
            except ServiceError:
                # A timeout/permission error is not evidence to spend or release.
                # Keep the reservation in durable review; never fall back to the
                # latest catalog version or an assumed default tax basis.
                catalog_error='catalog_discount_unavailable'
        self._heartbeat(job)
        return self.settlement.reconcile_order(job['tenant_id'], reservation['id'], order,
                                               catalog_discount=catalog,catalog_error=catalog_error)

    def _refund(self, job):
        refund = self._get(job, 'refunds', job['resource_id'])
        if refund.get('status') == 'PENDING':
            raise ResourceError('payment_refund_pending', transient=True)
        if refund.get('status') != 'COMPLETED' or refund.get('unlinked') is True:
            raise ResourceError('payment_refund_not_completed_or_linked')
        if not _id(refund.get('payment_id')) or not _id(refund.get('order_id')):
            raise ResourceError('refund_sale_or_return_reference_missing')
        payment = self._get(job, 'payments', refund['payment_id'])
        if not _id(payment.get('order_id')):
            raise ResourceError('refund_original_order_missing')
        sale = self._get(job, 'orders', payment['order_id'])
        original = self._order(job, sale)
        if original.get('state') == 'ignored' or original.get('needs_review') or original.get('action') == 'pending':
            return original
        if original.get('state') != 'consumed':
            raise ResourceError('refund_original_credit_not_consumed')
        returned = self._get(job, 'orders', refund['order_id'])
        if returned['id'] == sale['id'] or not isinstance(returned.get('returns'), list) or not any(
                isinstance(item, dict) and item.get('source_order_id') == sale['id'] for item in returned['returns']):
            raise ResourceError('refund_return_allocation_missing')
        references = returned.get('refunds')
        if not isinstance(references, list) or not references:
            raise ResourceError('refund_completion_unproven')
        ids = []
        for reference in references:
            if not isinstance(reference, dict) or not _id(reference.get('tender_id')) or not _id(reference.get('id')):
                raise ResourceError('refund_reference_unproven')
            ident = reference['tender_id'] + '_' + reference['id']
            if not _id(ident) or ident in ids:
                raise ResourceError('refund_reference_unproven')
            ids.append(ident)
        if refund['id'] not in ids:
            raise ResourceError('refund_does_not_match_return_order')
        proof = [refund if ident == refund['id'] else self._get(job, 'refunds', ident) for ident in ids]
        self._heartbeat(job)
        return self.settlement.reconcile_return(job['tenant_id'], sale['id'], returned, proof)

    def _retry(self, job, code):
        if job['attempts'] >= self.max_attempts:
            return self._finish(job, 'exception', code)
        delay = min(5 * (2 ** min(job['attempts'] - 1, 8)), 900)
        return self._finish(job, 'pending', code, delay=delay)

    def _finish(self, job, state, code, *, delay=0):
        with self.database.transaction() as db:
            row = db.execute('''SELECT event_id FROM ss_square_jobs WHERE event_id=%s AND tenant_id=%s
                                AND lease_token=%s AND state='processing' FOR UPDATE''',
                             (job['event_id'], job['tenant_id'], job['lease_token'])).fetchone()
            if not row:
                return {'status': 'lease_lost', 'processed': True, 'event_id': job['event_id']}
            if state == 'exception':
                record_exception(db, job['tenant_id'], 'job:' + job['event_id'], code,
                                 event_id=job['event_id'], resource_id=job['resource_id'],
                                 details={'event_type': job['event_type'], 'attempts': job['attempts']})
            db.execute('''UPDATE ss_square_jobs SET state=%s,last_error_code=CASE WHEN %s='done' THEN NULL ELSE %s END,lease_token=NULL,lease_until=NULL,
                          available_at=now()+(%s * interval '1 second'),
                          finished_at=CASE WHEN %s IN ('done','exception') THEN now() ELSE NULL END
                          WHERE event_id=%s AND tenant_id=%s''',
                       (state, state, code, delay, state, job['event_id'], job['tenant_id']))
        return {'status': state, 'processed': True, 'event_id': job['event_id'], 'code': code}


def main():
    parser = argparse.ArgumentParser(description='Disabled-by-default Square reconciliation worker')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--tenant', type=uuid.UUID)
    args = parser.parse_args()
    if os.environ.get('SS_RECONCILIATION_ENABLED') != '1':
        print('Reconciliation is disabled. No jobs or balances changed.');return
    url = os.environ.get('DATABASE_URL')
    if not url:raise SystemExit('Private database configuration is required.')
    config = {key: os.environ.get(key, '') for key in ('APP_ENCRYPTION_KEYS', 'SQUARE_APPLICATION_ID',
              'SQUARE_APPLICATION_SECRET', 'SQUARE_WEBHOOK_SIGNATURE_KEY')}
    config.update(SQUARE_ENVIRONMENT=os.environ.get('SQUARE_ENVIRONMENT', 'sandbox'),
                  PUBLIC_BASE_URL=os.environ.get('PUBLIC_BASE_URL') or os.environ.get('RENDER_EXTERNAL_URL', ''))
    database = Database(url)
    worker = Reconciler(database, Square(database, config), enabled=True)
    try:
        while True:
            result = worker.run_once(tenant_id=args.tenant)
            print('Reconciliation status=' + result['status'], flush=True)
            if args.once or result['status'] == 'not_configured':break
            if not result['processed']:time.sleep(5)
    except Exception as error:
        print('Reconciliation stopped with error type=' + type(error).__name__, flush=True)
        raise SystemExit(1) from None
    finally:database.close()


if __name__ == '__main__':main()
