"""Explicit fictional approval fixtures, restricted to disposable test schemas.

Never import from application routes or deployment migrations. This helper
cannot run against the application's normal schema and grants no real policy
or legal approval.
"""
import re
import uuid
from .credit_lots import record_credit_lot


def approved_checkout_fixture(db, tenant_id, customer_id, *, order_id='sale-1'):
    schema = db.execute('SELECT current_schema() AS name').fetchone()['name']
    if not re.fullmatch(r'ss_test_[a-f0-9]{32}', schema or ''):
        raise ValueError('Fictional approvals require an isolated ss_test_<uuid> schema.')
    owner = db.execute("SELECT id FROM ss_staff WHERE tenant_id=%s AND role='owner' LIMIT 1", (tenant_id,)).fetchone()
    if not owner:
        owner = {'id': uuid.uuid4()}
        db.execute("INSERT INTO ss_staff(id,tenant_id,email,password_hash,role) VALUES (%s,%s,'fixture-owner@example.invalid','fixture-invalid-password-hash','owner')", (owner['id'], tenant_id))
    db.execute('''UPDATE ss_tenants SET credit_checkout_enabled=TRUE,tax_policy_reference='approved-fixture-policy',
                  tax_policy_version=1,discount_id='discount-credit',square_location_id='location-1',
                  discount_catalog_version=123,discount_tax_basis='DO_NOT_MODIFY_TAX_BASIS' WHERE id=%s''', (tenant_id,))
    merchant = 'fixture-merchant-' + str(tenant_id).replace('-', '')
    db.execute('''INSERT INTO ss_square_connections(tenant_id,merchant_id,environment,encrypted_tokens,expires_at)
                  VALUES (%s,%s,'sandbox','fixture-not-a-provider-token',now()+interval '1 day') ON CONFLICT DO NOTHING''',
               (tenant_id, merchant))
    db.execute('''INSERT INTO ss_credit_origin_policies(tenant_id,policy_id,version,origin_code,policy_reference,
                  discount_id,discount_catalog_version,discount_tax_basis,approved,approved_at,approved_by,approval_reference)
                  VALUES (%s,'fixture-origin-policy',1,'fixture-verified-origin','approved-fixture-policy',
                  'discount-credit',123,'DO_NOT_MODIFY_TAX_BASIS',TRUE,now(),%s,'FICTIONAL TEST APPROVAL ONLY') ON CONFLICT DO NOTHING''',
               (tenant_id, owner['id']))
    customer = db.execute('SELECT balance_cents,square_customer_id FROM ss_customers WHERE tenant_id=%s AND id=%s FOR UPDATE',
                          (tenant_id, customer_id)).fetchone()
    total = int(db.execute('SELECT COALESCE(SUM(remaining_cents),0) AS amount FROM ss_credit_lots WHERE tenant_id=%s AND customer_id=%s',
                           (tenant_id, customer_id)).fetchone()['amount'])
    missing = customer['balance_cents'] - total
    if missing > 0:
        entry = uuid.uuid4()
        db.execute('''INSERT INTO ss_credit_entries(id,tenant_id,customer_id,amount_cents,balance_after_cents,kind,source_key,reference,reason)
                      VALUES (%s,%s,%s,%s,%s,'opening',%s,'FIXTURE','Fictional lot provenance fixture')''',
                   (entry, tenant_id, customer_id, missing, customer['balance_cents'], 'fixture-lot:' + str(customer_id)))
        record_credit_lot(db, tenant_id, customer_id, entry)
    # Explicit fixture-only review; no production origin classification is inferred.
    db.execute("""UPDATE ss_credit_lots SET origin_code='fixture-verified-origin',policy_id='fixture-origin-policy',policy_version=1
                  WHERE tenant_id=%s AND customer_id=%s""", (tenant_id, customer_id))
    return {'order_id': order_id, 'order_version': 1, 'merchant_id': merchant, 'environment': 'sandbox',
            'location_id': 'location-1', 'square_customer_id': customer['square_customer_id'],
            'discount_id': 'discount-credit', 'discount_catalog_version': 123, 'discount_uid': 'credit-uid',
            'discount_tax_basis': 'DO_NOT_MODIFY_TAX_BASIS', 'currency': 'USD'}
