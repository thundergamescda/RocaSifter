CREATE TABLE IF NOT EXISTS ss_schema_versions (
    version INTEGER PRIMARY KEY, installed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ss_tenants (
    id UUID PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'pilot', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    credit_checkout_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    tax_policy_reference TEXT, discount_id TEXT, square_location_id TEXT
);
CREATE TABLE IF NOT EXISTS ss_staff (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    email TEXT NOT NULL, password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','manager','buyer','cashier')),
    enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(tenant_id,email), UNIQUE(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_sessions (
    token_hash TEXT PRIMARY KEY, tenant_id UUID NOT NULL, staff_id UUID NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY(tenant_id,staff_id) REFERENCES ss_staff(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_login_attempts (
    key_hash TEXT PRIMARY KEY, failures INTEGER NOT NULL DEFAULT 0,
    window_start TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ss_login_attempt_age ON ss_login_attempts(window_start);
CREATE TABLE IF NOT EXISTS ss_invites (
    token_hash TEXT PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    email TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('manager','buyer','cashier')),
    expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ,
    created_by UUID NOT NULL REFERENCES ss_staff(id)
);
CREATE TABLE IF NOT EXISTS ss_bootstrap (
    key TEXT PRIMARY KEY, used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ss_audit (
    id BIGSERIAL PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    staff_id UUID, action TEXT NOT NULL, entity_id TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}', at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ss_customers (
    id UUID NOT NULL, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    display_name TEXT NOT NULL, contact TEXT NOT NULL DEFAULT '',
    square_customer_id TEXT, privacy_basis TEXT NOT NULL,
    balance_cents BIGINT NOT NULL DEFAULT 0 CHECK(balance_cents>=0),
    version BIGINT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,square_customer_id)
);
CREATE TABLE IF NOT EXISTS ss_credit_entries (
    id UUID NOT NULL, tenant_id UUID NOT NULL, customer_id UUID NOT NULL,
    amount_cents BIGINT NOT NULL, balance_after_cents BIGINT NOT NULL CHECK(balance_after_cents>=0),
    kind TEXT NOT NULL, source_key TEXT NOT NULL, reference TEXT NOT NULL,
    reason TEXT NOT NULL, staff_id UUID, at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,source_key),
    FOREIGN KEY(tenant_id,customer_id) REFERENCES ss_customers(tenant_id,id)
);
CREATE INDEX IF NOT EXISTS ss_entries_customer ON ss_credit_entries(tenant_id,customer_id,at);
CREATE TABLE IF NOT EXISTS ss_reservations (
    id UUID NOT NULL, tenant_id UUID NOT NULL, customer_id UUID NOT NULL,
    amount_cents BIGINT NOT NULL CHECK(amount_cents>0),
    state TEXT NOT NULL CHECK(state IN ('reserved','consumed','released','exception')),
    order_id TEXT, request_key TEXT NOT NULL, staff_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,order_id), UNIQUE(tenant_id,request_key),
    FOREIGN KEY(tenant_id,customer_id) REFERENCES ss_customers(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_requests (
    tenant_id UUID NOT NULL REFERENCES ss_tenants(id), key TEXT NOT NULL,
    body_hash TEXT NOT NULL, result JSONB NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,key)
);
CREATE TABLE IF NOT EXISTS ss_square_connections (
    tenant_id UUID PRIMARY KEY REFERENCES ss_tenants(id), merchant_id TEXT UNIQUE NOT NULL,
    environment TEXT NOT NULL CHECK(environment IN ('sandbox','production')),
    encrypted_tokens TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ss_oauth_states (
    state_hash TEXT PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    session_hash TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS ss_square_jobs (
    event_id TEXT PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    event_type TEXT NOT NULL, resource_id TEXT NOT NULL,
    body_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_until TIMESTAMPTZ, last_error_code TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ss_jobs_ready ON ss_square_jobs(state,available_at);
INSERT INTO ss_schema_versions(version) VALUES (1) ON CONFLICT DO NOTHING;

-- Version 2: server-owned immutable checkout evidence and durable reconciliation.
-- Existing reservations deliberately receive no inferred/backfilled terms.
CREATE TABLE IF NOT EXISTS ss_reservation_terms (
    tenant_id UUID NOT NULL, reservation_id UUID NOT NULL,
    terms_version INTEGER NOT NULL CHECK(terms_version=1),
    terms JSONB NOT NULL CHECK(jsonb_typeof(terms)='object'),
    terms_hash TEXT NOT NULL CHECK(length(terms_hash)=64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,reservation_id),
    FOREIGN KEY(tenant_id,reservation_id) REFERENCES ss_reservations(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_square_settlements (
    tenant_id UUID NOT NULL, reservation_id UUID NOT NULL,
    source_order_id TEXT NOT NULL, entry_id UUID NOT NULL,
    applied_cents BIGINT NOT NULL CHECK(applied_cents>0),
    currency TEXT NOT NULL CHECK(currency='USD'),
    discount_uid TEXT NOT NULL, settled_order_version BIGINT NOT NULL CHECK(settled_order_version>0),
    terms_hash TEXT NOT NULL CHECK(length(terms_hash)=64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,reservation_id), UNIQUE(tenant_id,source_order_id), UNIQUE(tenant_id,entry_id),
    FOREIGN KEY(tenant_id,reservation_id) REFERENCES ss_reservation_terms(tenant_id,reservation_id),
    FOREIGN KEY(tenant_id,entry_id) REFERENCES ss_credit_entries(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_credit_returns (
    tenant_id UUID NOT NULL, return_order_id TEXT NOT NULL, source_order_id TEXT NOT NULL,
    entry_id UUID NOT NULL, restored_cents BIGINT NOT NULL CHECK(restored_cents>0),
    return_order_version BIGINT NOT NULL CHECK(return_order_version>0),
    proof_hash TEXT NOT NULL CHECK(length(proof_hash)=64),
    proof JSONB NOT NULL CHECK(jsonb_typeof(proof)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,return_order_id,source_order_id), UNIQUE(tenant_id,entry_id),
    FOREIGN KEY(tenant_id,source_order_id) REFERENCES ss_square_settlements(tenant_id,source_order_id),
    FOREIGN KEY(tenant_id,entry_id) REFERENCES ss_credit_entries(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_reconciliation_exceptions (
    id UUID PRIMARY KEY, tenant_id UUID NOT NULL REFERENCES ss_tenants(id),
    source_key TEXT NOT NULL, code TEXT NOT NULL,
    reservation_id UUID, event_id TEXT, resource_id TEXT,
    details JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(details)='object'),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','resolved')),
    occurrences BIGINT NOT NULL DEFAULT 1 CHECK(occurrences>0),
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ, resolved_by UUID,
    UNIQUE(tenant_id,source_key),
    FOREIGN KEY(tenant_id,reservation_id) REFERENCES ss_reservations(tenant_id,id),
    FOREIGN KEY(tenant_id,resolved_by) REFERENCES ss_staff(tenant_id,id)
);
CREATE INDEX IF NOT EXISTS ss_exceptions_open ON ss_reconciliation_exceptions(tenant_id,status,last_seen_at);
ALTER TABLE ss_reservations ADD COLUMN IF NOT EXISTS last_order_version BIGINT;
ALTER TABLE ss_reservations ADD COLUMN IF NOT EXISTS last_order_state TEXT;
ALTER TABLE ss_square_jobs ADD COLUMN IF NOT EXISTS lease_token UUID;
ALTER TABLE ss_square_jobs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ss_jobs_leased ON ss_square_jobs(state,lease_until);

CREATE OR REPLACE FUNCTION ss_reject_immutable_evidence_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Immutable reconciliation evidence cannot be updated';
END $$;
DROP TRIGGER IF EXISTS ss_terms_immutable ON ss_reservation_terms;
CREATE TRIGGER ss_terms_immutable BEFORE UPDATE OR DELETE ON ss_reservation_terms
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
DROP TRIGGER IF EXISTS ss_settlements_immutable ON ss_square_settlements;
CREATE TRIGGER ss_settlements_immutable BEFORE UPDATE OR DELETE ON ss_square_settlements
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
DROP TRIGGER IF EXISTS ss_returns_immutable ON ss_credit_returns;
CREATE TRIGGER ss_returns_immutable BEFORE UPDATE OR DELETE ON ss_credit_returns
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
ALTER TABLE ss_tenants ADD COLUMN IF NOT EXISTS tax_policy_version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE ss_tenants ADD COLUMN IF NOT EXISTS discount_catalog_version BIGINT;
ALTER TABLE ss_tenants ADD COLUMN IF NOT EXISTS discount_tax_basis TEXT;
ALTER TABLE ss_square_settlements ADD COLUMN IF NOT EXISTS consumed_allocations JSONB NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS ss_credit_origin_policies (
    tenant_id UUID NOT NULL REFERENCES ss_tenants(id), policy_id TEXT NOT NULL,
    version BIGINT NOT NULL CHECK(version>0), origin_code TEXT NOT NULL,
    policy_reference TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD' CHECK(currency='USD'),
    discount_id TEXT NOT NULL, discount_catalog_version BIGINT NOT NULL CHECK(discount_catalog_version>0),
    discount_tax_basis TEXT NOT NULL CHECK(discount_tax_basis IN ('MODIFY_TAX_BASIS','DO_NOT_MODIFY_TAX_BASIS')),
    approved BOOLEAN NOT NULL DEFAULT FALSE, approved_at TIMESTAMPTZ, approved_by UUID,
    approval_reference TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,policy_id,version),
    FOREIGN KEY(tenant_id,approved_by) REFERENCES ss_staff(tenant_id,id),
    CHECK(NOT approved OR (approved_at IS NOT NULL AND approved_by IS NOT NULL AND
        approval_reference IS NOT NULL AND length(trim(approval_reference))>0 AND length(trim(policy_reference))>0 AND
        lower(trim(origin_code)) NOT IN ('unknown','legacy_unknown','opening_unknown','adjustment_unknown')))
);
CREATE TABLE IF NOT EXISTS ss_credit_policy_revocations (
    tenant_id UUID NOT NULL, policy_id TEXT NOT NULL, policy_version BIGINT NOT NULL,
    revoked_by UUID NOT NULL, reason TEXT NOT NULL, revoked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,policy_id,policy_version),
    FOREIGN KEY(tenant_id,policy_id,policy_version) REFERENCES ss_credit_origin_policies(tenant_id,policy_id,version),
    FOREIGN KEY(tenant_id,revoked_by) REFERENCES ss_staff(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_credit_lots (
    id UUID NOT NULL, tenant_id UUID NOT NULL, customer_id UUID NOT NULL,
    source_entry_id UUID NOT NULL, lot_index INTEGER NOT NULL DEFAULT 0 CHECK(lot_index>=0),
    origin_code TEXT NOT NULL DEFAULT 'unknown', policy_id TEXT, policy_version BIGINT,
    issued_cents BIGINT NOT NULL CHECK(issued_cents>0),
    remaining_cents BIGINT NOT NULL CHECK(remaining_cents>=0 AND remaining_cents<=issued_cents),
    parent_lot_id UUID, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,id), UNIQUE(tenant_id,source_entry_id,lot_index),
    FOREIGN KEY(tenant_id,customer_id) REFERENCES ss_customers(tenant_id,id),
    FOREIGN KEY(tenant_id,source_entry_id) REFERENCES ss_credit_entries(tenant_id,id),
    FOREIGN KEY(tenant_id,parent_lot_id) REFERENCES ss_credit_lots(tenant_id,id),
    FOREIGN KEY(tenant_id,policy_id,policy_version) REFERENCES ss_credit_origin_policies(tenant_id,policy_id,version),
    CHECK((policy_id IS NULL)=(policy_version IS NULL))
);
CREATE INDEX IF NOT EXISTS ss_lots_customer ON ss_credit_lots(tenant_id,customer_id,created_at,id);
CREATE TABLE IF NOT EXISTS ss_credit_lot_mutations (
    tenant_id UUID NOT NULL, entry_id UUID NOT NULL, lot_id UUID NOT NULL,
    amount_cents BIGINT NOT NULL CHECK(amount_cents<>0), kind TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(tenant_id,entry_id,lot_id),
    FOREIGN KEY(tenant_id,entry_id) REFERENCES ss_credit_entries(tenant_id,id),
    FOREIGN KEY(tenant_id,lot_id) REFERENCES ss_credit_lots(tenant_id,id)
);
CREATE TABLE IF NOT EXISTS ss_credit_lot_reservations (
    tenant_id UUID NOT NULL, reservation_id UUID NOT NULL, lot_id UUID NOT NULL,
    reserved_cents BIGINT NOT NULL CHECK(reserved_cents>0),
    consumed_cents BIGINT NOT NULL DEFAULT 0 CHECK(consumed_cents>=0 AND consumed_cents<=reserved_cents),
    restored_cents BIGINT NOT NULL DEFAULT 0 CHECK(restored_cents>=0 AND restored_cents<=consumed_cents),
    PRIMARY KEY(tenant_id,reservation_id,lot_id),
    FOREIGN KEY(tenant_id,reservation_id) REFERENCES ss_reservations(tenant_id,id),
    FOREIGN KEY(tenant_id,lot_id) REFERENCES ss_credit_lots(tenant_id,id)
);
CREATE INDEX IF NOT EXISTS ss_lot_reservation_customer ON ss_credit_lot_reservations(tenant_id,lot_id);
DROP TRIGGER IF EXISTS ss_origin_policy_immutable ON ss_credit_origin_policies;
CREATE TRIGGER ss_origin_policy_immutable BEFORE UPDATE OR DELETE ON ss_credit_origin_policies
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
DROP TRIGGER IF EXISTS ss_policy_revocation_immutable ON ss_credit_policy_revocations;
CREATE TRIGGER ss_policy_revocation_immutable BEFORE UPDATE OR DELETE ON ss_credit_policy_revocations
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
DROP TRIGGER IF EXISTS ss_lot_mutations_immutable ON ss_credit_lot_mutations;
CREATE TRIGGER ss_lot_mutations_immutable BEFORE UPDATE OR DELETE ON ss_credit_lot_mutations
    FOR EACH ROW EXECUTE FUNCTION ss_reject_immutable_evidence_update();
CREATE OR REPLACE FUNCTION ss_guard_credit_lot_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR
       NEW.customer_id IS DISTINCT FROM OLD.customer_id OR NEW.source_entry_id IS DISTINCT FROM OLD.source_entry_id OR
       NEW.lot_index IS DISTINCT FROM OLD.lot_index OR NEW.issued_cents IS DISTINCT FROM OLD.issued_cents OR
       NEW.parent_lot_id IS DISTINCT FROM OLD.parent_lot_id OR NEW.created_at IS DISTINCT FROM OLD.created_at OR
       NEW.remaining_cents>OLD.remaining_cents THEN
        RAISE EXCEPTION 'Credit lot issuance evidence cannot be rewritten';
    END IF;
    IF (NEW.origin_code IS DISTINCT FROM OLD.origin_code OR NEW.policy_id IS DISTINCT FROM OLD.policy_id OR
        NEW.policy_version IS DISTINCT FROM OLD.policy_version) AND EXISTS
       (SELECT 1 FROM ss_credit_lot_reservations a WHERE a.tenant_id=OLD.tenant_id AND a.lot_id=OLD.id) THEN
        RAISE EXCEPTION 'Reserved credit lot origin cannot be rewritten';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_lot_issuance_guard ON ss_credit_lots;
CREATE TRIGGER ss_lot_issuance_guard BEFORE UPDATE ON ss_credit_lots
    FOR EACH ROW EXECUTE FUNCTION ss_guard_credit_lot_update();
CREATE OR REPLACE FUNCTION ss_guard_credit_lot_reservation_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.reservation_id IS DISTINCT FROM OLD.reservation_id OR
       NEW.lot_id IS DISTINCT FROM OLD.lot_id OR NEW.reserved_cents IS DISTINCT FROM OLD.reserved_cents OR
       NEW.consumed_cents<OLD.consumed_cents OR NEW.restored_cents<OLD.restored_cents THEN
        RAISE EXCEPTION 'Credit lot reservation evidence cannot be reversed';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_lot_reservation_guard ON ss_credit_lot_reservations;
CREATE TRIGGER ss_lot_reservation_guard BEFORE UPDATE ON ss_credit_lot_reservations
    FOR EACH ROW EXECUTE FUNCTION ss_guard_credit_lot_reservation_update();
-- No balance/lot backfill: incomplete historical origin evidence stays in review.
INSERT INTO ss_schema_versions(version) VALUES (2) ON CONFLICT DO NOTHING;

-- Version 3: accepted trades create credit only from locked server receipts.
-- No historical receipts, credit balances, origin policies or lots are backfilled.
ALTER TABLE ss_requests ADD COLUMN IF NOT EXISTS operation TEXT NOT NULL DEFAULT 'ledger';
ALTER TABLE ss_tenants ADD COLUMN IF NOT EXISTS trade_cash_bps INTEGER NOT NULL DEFAULT 3500;
ALTER TABLE ss_tenants ADD COLUMN IF NOT EXISTS trade_credit_bps INTEGER NOT NULL DEFAULT 5000;
CREATE TABLE IF NOT EXISTS ss_trade_receipts (
    id UUID NOT NULL, tenant_id UUID NOT NULL, customer_id UUID NOT NULL,
    title TEXT NOT NULL, source_reference TEXT NOT NULL DEFAULT '',
    creation_hash TEXT NOT NULL CHECK(length(creation_hash)=64),
    state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','accepted_credit','accepted_cash','voided')),
    version BIGINT NOT NULL DEFAULT 0 CHECK(version>=0), currency TEXT NOT NULL DEFAULT 'USD' CHECK(currency='USD'),
    cash_bps INTEGER NOT NULL CHECK(cash_bps BETWEEN 0 AND 10000),
    credit_bps INTEGER NOT NULL CHECK(credit_bps BETWEEN 0 AND 10000),
    cash_override_cents BIGINT CHECK(cash_override_cents BETWEEN 0 AND 100000000),
    credit_override_cents BIGINT CHECK(credit_override_cents BETWEEN 0 AND 100000000),
    override_reason TEXT NOT NULL DEFAULT '', intake_total_cents BIGINT NOT NULL CHECK(intake_total_cents BETWEEN 0 AND 100000000),
    cash_offer_cents BIGINT NOT NULL CHECK(cash_offer_cents BETWEEN 0 AND 100000000),
    credit_offer_cents BIGINT NOT NULL CHECK(credit_offer_cents BETWEEN 0 AND 100000000),
    lines_hash TEXT NOT NULL CHECK(length(lines_hash)=64),
    created_by UUID NOT NULL, updated_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    selected_offer TEXT CHECK(selected_offer IN ('credit','cash')), accepted_amount_cents BIGINT,
    accepted_at TIMESTAMPTZ, accepted_by UUID, ledger_entry_id UUID,
    receipt_hash TEXT, accepted_receipt JSONB CHECK(accepted_receipt IS NULL OR jsonb_typeof(accepted_receipt)='object'),
    acceptance_result JSONB CHECK(acceptance_result IS NULL OR jsonb_typeof(acceptance_result)='object'),
    voided_at TIMESTAMPTZ, voided_by UUID, void_reason TEXT,
    PRIMARY KEY(tenant_id,id),
    FOREIGN KEY(tenant_id,customer_id) REFERENCES ss_customers(tenant_id,id),
    FOREIGN KEY(tenant_id,created_by) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(tenant_id,updated_by) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(tenant_id,accepted_by) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(tenant_id,voided_by) REFERENCES ss_staff(tenant_id,id),
    FOREIGN KEY(tenant_id,ledger_entry_id) REFERENCES ss_credit_entries(tenant_id,id),
    CHECK((state='draft' AND selected_offer IS NULL AND accepted_amount_cents IS NULL AND
        accepted_at IS NULL AND accepted_by IS NULL AND ledger_entry_id IS NULL AND receipt_hash IS NULL AND
        accepted_receipt IS NULL AND acceptance_result IS NULL AND voided_at IS NULL AND voided_by IS NULL) OR
      (state IN ('accepted_credit','accepted_cash') AND accepted_amount_cents IS NOT NULL AND selected_offer IS NOT NULL AND accepted_amount_cents BETWEEN 1 AND 100000000 AND
        accepted_at IS NOT NULL AND accepted_by IS NOT NULL AND receipt_hash IS NOT NULL AND length(receipt_hash)=64 AND
        accepted_receipt IS NOT NULL AND acceptance_result IS NOT NULL AND voided_at IS NULL AND voided_by IS NULL AND
        ((state='accepted_credit' AND selected_offer='credit' AND ledger_entry_id IS NOT NULL) OR
         (state='accepted_cash' AND selected_offer='cash' AND ledger_entry_id IS NULL))) OR
      (state='voided' AND voided_at IS NOT NULL AND voided_by IS NOT NULL AND void_reason IS NOT NULL AND length(trim(void_reason))>0 AND
        accepted_at IS NULL AND accepted_by IS NULL AND ledger_entry_id IS NULL AND selected_offer IS NULL))
);
CREATE INDEX IF NOT EXISTS ss_trade_receipts_recent ON ss_trade_receipts(tenant_id,updated_at DESC,id DESC);
DROP INDEX IF EXISTS ss_trade_source_reference;
CREATE UNIQUE INDEX ss_trade_source_reference ON ss_trade_receipts(tenant_id,source_reference)
    WHERE source_reference<>'' AND state<>'voided';
CREATE TABLE IF NOT EXISTS ss_trade_receipt_lines (
    id UUID NOT NULL, tenant_id UUID NOT NULL, trade_id UUID NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal>=0), identity JSONB NOT NULL CHECK(jsonb_typeof(identity)='object'),
    quantity BIGINT NOT NULL CHECK(quantity BETWEEN 1 AND 100000),
    unit_intake_cents BIGINT NOT NULL CHECK(unit_intake_cents BETWEEN 0 AND 100000000),
    included BOOLEAN NOT NULL DEFAULT TRUE, reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    line_total_cents BIGINT NOT NULL CHECK(line_total_cents=quantity*unit_intake_cents AND line_total_cents BETWEEN 0 AND 100000000),
    PRIMARY KEY(tenant_id,trade_id,id), UNIQUE(tenant_id,trade_id,ordinal),
    FOREIGN KEY(tenant_id,trade_id) REFERENCES ss_trade_receipts(tenant_id,id)
);
CREATE OR REPLACE FUNCTION ss_guard_final_trade_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state<>'draft' THEN RAISE EXCEPTION 'Final trade receipt evidence is immutable'; END IF;
    IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Trade receipts must be retained; void a draft instead'; END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.id IS DISTINCT FROM OLD.id OR
       NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at OR
       NEW.source_reference IS DISTINCT FROM OLD.source_reference OR NEW.creation_hash IS DISTINCT FROM OLD.creation_hash THEN
        RAISE EXCEPTION 'Trade receipt identity is immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_final_trade_receipt_guard ON ss_trade_receipts;
CREATE TRIGGER ss_final_trade_receipt_guard BEFORE UPDATE OR DELETE ON ss_trade_receipts
    FOR EACH ROW EXECUTE FUNCTION ss_guard_final_trade_receipt();
CREATE OR REPLACE FUNCTION ss_guard_trade_receipt_lines() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_state TEXT;
BEGIN
    IF TG_OP='INSERT' THEN
        SELECT state INTO owner_state FROM ss_trade_receipts WHERE tenant_id=NEW.tenant_id AND id=NEW.trade_id FOR UPDATE;
    ELSE
        SELECT state INTO owner_state FROM ss_trade_receipts WHERE tenant_id=OLD.tenant_id AND id=OLD.trade_id FOR UPDATE;
    END IF;
    IF owner_state IS DISTINCT FROM 'draft' THEN RAISE EXCEPTION 'Final trade line evidence is immutable'; END IF;
    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
    IF TG_OP='UPDATE' AND (NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.trade_id IS DISTINCT FROM OLD.trade_id OR NEW.id IS DISTINCT FROM OLD.id) THEN
        RAISE EXCEPTION 'Trade line identity is immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ss_trade_line_guard ON ss_trade_receipt_lines;
CREATE TRIGGER ss_trade_line_guard BEFORE INSERT OR UPDATE OR DELETE ON ss_trade_receipt_lines
    FOR EACH ROW EXECUTE FUNCTION ss_guard_trade_receipt_lines();
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_constraint WHERE conrelid='ss_credit_origin_policies'::regclass AND conname='ss_policies_no_unreviewed_trade') THEN
        ALTER TABLE ss_credit_origin_policies ADD CONSTRAINT ss_policies_no_unreviewed_trade
        CHECK(NOT approved OR lower(trim(origin_code)) NOT IN ('trade_unreviewed','accepted_trade_unreviewed','trade_unknown'));
    END IF;
END $$;
INSERT INTO ss_schema_versions(version) VALUES (3) ON CONFLICT DO NOTHING;
